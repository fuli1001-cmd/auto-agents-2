"""Uncommitted source observed before a repair remains auditable afterwards."""
import copy
import json
import sys
from types import SimpleNamespace

import pytest
from test_provider_prompt_delivery import fixture_router
from test_review_evidence import commit
from test_workflow import CodingAgent, engine

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.context_plan import ContextPlanner, remap_references
from auto_agents_2.evidence_refs import controller_context_catalog
from auto_agents_2.faults import StateConflict
from auto_agents_2.models import ReviewResult
from auto_agents_2.source_history import SourceHistory
from auto_agents_2.util import digest, write_json
from auto_agents_2.workspace import git, tree_hash

TEST = "frontend/tests/components/App.test.tsx"
OLD = b"test('retry', () => {\r\n  expect(query({exact: true})).toBeVisible();\r\n});\r\ntest('unrelated', () => { expect(kept()).toBe(23); });\r\n"
NEW = OLD.replace(b"query({exact: true})", b"query({name: /^Retry$/})")


def observed_fixture(tmp_path, workflow=None, rid=None):
    workflow = workflow or engine(tmp_path, CodingAgent())
    rid = rid or workflow.store.admit("uncommitted test repair", {})["id"]
    (tmp_path / "base.py").write_text("value = 1\n")
    commit(tmp_path)
    workflow.store.checkpoint(rid, "baseline", git(tmp_path, "rev-parse", "HEAD"))
    path = tmp_path / TEST
    path.parent.mkdir(parents=True)
    path.write_bytes(OLD)
    original = SourceHistory(workflow).collect(rid, tmp_path, ["frontend"])
    original_manifest = workflow.artifacts.json(original["artifact"])
    assert TEST not in original_manifest["snapshots"][0]["files"]
    path.write_bytes(NEW)
    return workflow, rid, original


def observation(workflow, result, source):
    return next(row for row in workflow.artifacts.json(result["artifact"])["observations"]
                if row["source_hash"] == source)


def test_applied_uncommitted_test_preimage_survives_repair_and_recheck(tmp_path):
    workflow, rid, old = observed_fixture(tmp_path)
    original = workflow.artifacts.bytes(old["artifact"])
    result = SourceHistory(workflow).collect(rid, tmp_path, ["frontend"])
    item = observation(workflow, result, old["source_hash"])
    assert item["status"] == "observed_only" and not item.get("approved") and not item.get("commit")
    assert workflow.artifacts.bytes(item["files"][TEST]["content"]) == OLD
    delta = next(row for row in item["changes_to_current"] if row["path"] == TEST)
    assert workflow.artifacts.bytes(delta["before"]["content"]) == OLD
    assert workflow.artifacts.bytes(delta["after"]["content"]) == NEW
    patch = workflow.artifacts.bytes(delta["diff"])
    assert b"-  expect(query({exact: true}))" in patch
    assert b"+  expect(query({name: /^Retry$/}))" in patch
    assert b" test('unrelated', () => { expect(kept()).toBe(23); });\r\n" in patch
    assert item["origins"][0]["manifest_hash"] == old["artifact"]["hash"]
    assert workflow.artifacts.bytes(old["artifact"]) == original
    assert SourceHistory(workflow).collect(rid, tmp_path, ["frontend"]) == result
    assert not workflow.store.get(rid, "develop/done")


@pytest.mark.parametrize("version", [1, 2, 3])
def test_existing_manifest_versions_are_read_without_rewriting_history(tmp_path, version):
    workflow, rid, old = observed_fixture(tmp_path)
    with workflow.store.connect() as db:
        row = db.execute("SELECT key,data FROM checkpoints WHERE run_id=? AND key LIKE 'review-evidence/source-history/%'", (rid,)).fetchone()
    manifest = workflow.artifacts.json(old["artifact"])
    manifest.pop("observations", None)
    manifest["version"] = version
    old["artifact"] = workflow.artifacts.put(manifest, "source_history")
    workflow.store.checkpoint(rid, row[0], old)
    result = SourceHistory(workflow).collect(rid, tmp_path, ["frontend"])
    item = observation(workflow, result, old["source_hash"])
    assert workflow.artifacts.bytes(item["files"][TEST]["content"]) == OLD
    assert item["origins"][0]["manifest_hash"] == old["artifact"]["hash"]
    assert workflow.store.get(rid, row[0]) == old


def test_multiple_repairs_keep_every_distinct_observation_without_recursive_growth(tmp_path):
    workflow, rid, old = observed_fixture(tmp_path)
    second = SourceHistory(workflow).collect(rid, tmp_path, ["frontend"])
    (tmp_path / TEST).write_bytes(NEW + b"// third actual candidate\n")
    result = SourceHistory(workflow).collect(rid, tmp_path, ["frontend"])
    rows = workflow.artifacts.json(result["artifact"])["observations"]
    assert {row["source_hash"] for row in rows} == {old["source_hash"], second["source_hash"]}
    assert all("observations" not in row and "proposals" not in row for row in rows)
    for _ in range(4):
        assert SourceHistory(workflow).collect(rid, tmp_path, ["frontend"]) == result
    (tmp_path / TEST).write_bytes(OLD)
    revisited = SourceHistory(workflow).collect(rid, tmp_path, ["frontend"])
    assert SourceHistory(workflow).collect(rid, tmp_path, ["frontend"]) == revisited
    assert old["source_hash"] not in {r["source_hash"] for r in workflow.artifacts.json(revisited["artifact"])["observations"]}


def test_scope_union_is_stable_and_cannot_claim_uncaptured_paths_absent(tmp_path):
    workflow, rid, old = observed_fixture(tmp_path)
    (tmp_path / "helper.py").write_text("kept()\n")
    first = SourceHistory(workflow).collect(rid, tmp_path, ["frontend"])
    SourceHistory(workflow).collect(rid, tmp_path, ["helper.py"])
    (tmp_path / TEST).write_bytes(NEW + b"// newer\n")
    result = SourceHistory(workflow).collect(rid, tmp_path, ["frontend", "helper.py"])
    item = observation(workflow, result, first["source_hash"])
    assert {TEST, "helper.py"} <= item["files"].keys()
    assert len(item["origins"]) == 2
    manifest = workflow.artifacts.json(result["artifact"])
    assert "not proof of absence" in manifest["coverage"]["observations"]
    assert SourceHistory(workflow).collect(rid, tmp_path, ["frontend", "helper.py"]) == result


def test_non_executable_permission_bits_do_not_invent_source_identity_conflicts(tmp_path):
    workflow, rid, old = observed_fixture(tmp_path)
    manifest = workflow.artifacts.json(old["artifact"])
    manifest["current"][TEST]["mode"] = 0o600
    workflow.store.checkpoint(rid, "review-evidence/source-history/permission-fixture", {
        **old, "artifact": workflow.artifacts.put(manifest, "source_history")})
    result = SourceHistory(workflow).collect(rid, tmp_path, ["frontend"])
    item = observation(workflow, result, old["source_hash"])
    assert item["files"][TEST]["mode"] == 0o644  # The first actual observation is retained.
    assert workflow.artifacts.bytes(item["files"][TEST]["content"]) == OLD


def test_current_policy_filters_old_bodies_before_read_and_does_not_follow_json_refs(tmp_path, monkeypatch):
    workflow, rid, old = observed_fixture(tmp_path)
    unrelated = workflow.artifacts.put("UNRELATED_RECORD_FIXTURE", "private_fixture")
    paths = {"private/test_old.py": "PRIVATE_FIXTURE", "helper.py": "OUTSIDE_SCOPE_FIXTURE",
             "frontend/tests/reference.json": json.dumps({"report": unrelated})}
    for name, body in paths.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    prior = SourceHistory(workflow).collect(rid, tmp_path, ["frontend", "helper.py", "private"])
    prior_manifest = workflow.artifacts.json(prior["artifact"])
    forbidden = {prior_manifest["current"][name]["content"]["hash"] for name in ["private/test_old.py", "helper.py"]}
    config = workflow.config.load().model_dump(mode="json")
    config["sensitive_paths"] = ["private/**"]
    write_json(workflow.config.path, config)
    for name in paths:
        (tmp_path / name).unlink()
    (tmp_path / TEST).write_bytes(NEW + b"// final\n")
    read = workflow.artifacts.bytes

    def bounded(ref):
        assert ref["hash"] not in forbidden
        return read(ref)

    monkeypatch.setattr(workflow.artifacts, "bytes", bounded)
    result = SourceHistory(workflow).collect(rid, tmp_path, ["frontend"])
    item = observation(workflow, result, prior["source_hash"])
    assert not forbidden & {row["content"]["hash"] for row in item["files"].values()}
    context = ContextPlanner(workflow.artifacts).prepare("review", {"source_history": result}, tmp_path)
    mounted = {row["hash"] for row in context.payload["evidence_materials"]}
    assert digest(paths["frontend/tests/reference.json"]) in mounted
    assert not forbidden & mounted and unrelated["hash"] not in mounted
    assert prior["artifact"]["hash"] not in mounted


@pytest.mark.parametrize("damage", ["missing", "corrupt", "source_identity", "kind", "size", "conflicting_body", "executable"])
def test_invalid_observed_source_cannot_silently_disappear(tmp_path, damage):
    workflow, rid, old = observed_fixture(tmp_path)
    manifest = workflow.artifacts.json(old["artifact"])
    content = manifest["current"][TEST]["content"]
    body = workflow.artifacts.root / content["hash"][:2] / content["hash"]
    if damage == "missing":
        body.unlink()
    elif damage == "corrupt":
        body.write_text("CORRUPTED_TEST_FIXTURE")
    else:
        changed = copy.deepcopy(manifest)
        if damage == "source_identity":
            changed["source_hash"] = "0" * 64
        elif damage == "kind":
            changed["current"][TEST]["content"]["kind"] = "document"
        elif damage == "size":
            changed["current"][TEST]["content"]["bytes"] += 1
        elif damage == "executable":
            changed["current"][TEST]["mode"] = 0o755
        else:
            changed["current"][TEST]["content"] = workflow.artifacts.put("DIFFERENT_SOURCE_FIXTURE", "source_snapshot")
        forged = {**old, "artifact": workflow.artifacts.put(changed, "source_history")}
        workflow.store.checkpoint(rid, "review-evidence/source-history/controller-damaged-fixture", forged)
    before = tree_hash(tmp_path)
    with pytest.raises(StateConflict, match="source observation"):
        SourceHistory(workflow).collect(rid, tmp_path, ["frontend"])
    assert tree_hash(tmp_path) == before and not workflow.store.get(rid, "develop/done")


def test_proposal_and_arbitrary_hash_do_not_become_observed_file_versions(tmp_path):
    workflow, rid, old = observed_fixture(tmp_path)
    fake = workflow.artifacts.put({"version": 3, "source_hash": "0" * 64,
        "current": {TEST: {"content": workflow.artifacts.put("UNAPPLIED_FIXTURE", "source_snapshot"), "mode": 0o644}}}, "source_history")
    workflow.store.checkpoint(rid, "provider-result/implement", {"artifact": fake})
    workflow.store.checkpoint(rid, "review-evidence/model-citation", {"artifact": fake})
    result = SourceHistory(workflow).collect(rid, tmp_path, ["frontend"])
    rows = workflow.artifacts.json(result["artifact"])["observations"]
    assert [row["source_hash"] for row in rows] == [old["source_hash"]]
    assert "UNAPPLIED_FIXTURE" not in str(rows)


@pytest.mark.parametrize("stage,extra", [
    ("implement", {}), ("review", {"review_kind": "test_changes"}),
    ("review", {"review_kind": "implementation"}), ("verify", {}),
    ("recovery_diagnosis", {}), ("plan", {"plan_patch": True}),
])
def test_shared_consumers_receive_readable_observation_and_canonical_citation(tmp_path, stage, extra):
    workflow, rid, old = observed_fixture(tmp_path)
    result = SourceHistory(workflow).collect(rid, tmp_path, ["frontend"])
    item = observation(workflow, result, old["source_hash"])
    before = tree_hash(tmp_path)
    view = ContextPlanner(workflow.artifacts).prepare(stage, {**extra, "source_history": result}, tmp_path)
    mounted = {ref["hash"] for ref in view.payload["evidence_materials"]}
    ref = item["files"][TEST]["content"]
    assert ref["hash"] in mounted
    assert workflow.artifacts.bytes(ref) == OLD
    resolved = remap_references({"evidence_refs": [ref["hash"]]}, view.reference_map)["evidence_refs"]
    assert set(resolved) <= controller_context_catalog({"source_history": result}).controller
    assert tree_hash(tmp_path) == before and not workflow.store.get(rid, "develop/done")


async def test_real_read_only_provider_receives_prior_observation_but_can_still_reject(tmp_path, monkeypatch):
    monkeypatch.setattr("auto_agents_2.providers.diagnostic_summary", lambda text: text)
    router, project, store, rid = fixture_router(tmp_path)
    workflow = SimpleNamespace(config=router.config, store=store, artifacts=ArtifactStore(store.root))
    (project / ".gitignore").write_text(".aa2/\n")
    git(project, "init", "-q")
    _, _, old = observed_fixture(project, workflow, rid)
    result = SourceHistory(workflow).collect(rid, project, ["frontend"])
    wanted = observation(workflow, result, old["source_hash"])["files"][TEST]["content"]["hash"]
    binary = tmp_path / "provider"
    binary.write_text(f"#!{sys.executable}\nWANTED={wanted!r}\nEXPECTED={OLD!r}\n" + '''
import hashlib, json, pathlib, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("fixture-cli 1")
    sys.exit(0)
schema = json.loads(pathlib.Path(args[args.index("--output-schema") + 1]).read_text())
wire = sys.stdin.read()
context = json.loads(wire.split("<context>\\n", 1)[1].split("\\n</context>", 1)[0])
if "text_token" in schema["properties"]:
    result = {"text_token": pathlib.Path(context["context_files"][0]).read_text(), "image_token": ""}
else:
    refs = context["evidence_materials"]
    if isinstance(refs, dict):
        refs = json.loads(pathlib.Path(refs["artifact"]["location"]).read_text())
    ref = next(r for r in refs if r["hash"] == WANTED)
    path = pathlib.Path(ref["location"])
    assert path.read_bytes() == EXPECTED
    assert hashlib.sha256(path.read_bytes()).hexdigest() == WANTED
    try:
        path.write_bytes(b"must not edit evidence")
    except OSError:
        pass
    else:
        raise AssertionError("Historical source became writable")
    result = {"approved": False, "summary": "Old assertions readable; independent semantic review still rejects fixture", "findings": []}
pathlib.Path(args[args.index("-o") + 1]).write_text(json.dumps(result))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
''')
    review = await router.invoke("review", {"source_history": result}, ReviewResult, project, rid, "repair-review", fresh=True)
    assert not review.approved and "readable" in review.summary
    assert not store.get(rid, "develop/done")
    assert not list((store.root / "processes").glob("*.json"))
