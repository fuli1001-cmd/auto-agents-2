"""Location compaction changes presentation, never the admitted read-only files."""
import copy
import sys

import pytest
from test_provider_prompt_delivery import fixture_router

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.errors import ProviderError
from auto_agents_2.materials import present_materials
from auto_agents_2.models import ReviewResult


def inputs(store):
    artifacts = ArtifactStore(store.root)
    attachments = [{"path": f"case-{i}/actual.json", "purpose": "diagnostic",
                    "artifact": artifacts.put({"case": i, "actual": False}, "test_diagnostic")}
                   for i in range(60)]
    report = artifacts.put({"tests": 60, "passed": 0}, "machine_report")
    check = {"argv": ["pytest"], "exit_code": 1, "source_hash": "current-source", "environment_hash": "locked-env",
             "report_status": "valid", "failure_reasons": ["assertions_failed"], "test_ids": [f"case-{i}" for i in range(60)],
             "passed_test_ids": [], "attachments": attachments, "report": report}
    explicit = artifacts.put("EXPLICIT_INPUT_MUST_REMAIN_VISIBLE", "document")
    explicit_path = str(artifacts.root / explicit["hash"][:2] / explicit["hash"])
    return {"evidence": [check, copy.deepcopy(check)], "mandatory": ["FIRST_CONSTRAINT", "LAST_CONSTRAINT"],
            "context_files": [explicit_path]}, attachments


async def test_generated_index_and_every_original_attachment_are_readable_and_read_only(tmp_path, monkeypatch):
    monkeypatch.setattr("auto_agents_2.providers.diagnostic_summary", lambda text: text)
    router, project, store, rid = fixture_router(tmp_path)
    binary = tmp_path / "provider"
    binary.write_text(f"#!{sys.executable}\n" + '''
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
    assert len(wire) < 64000
    assert context["mandatory"] == ["FIRST_CONSTRAINT", "LAST_CONSTRAINT"]
    assert len(context["context_files"]) == 1
    assert pathlib.Path(context["context_files"][0]).read_text() == "EXPLICIT_INPUT_MUST_REMAIN_VISIBLE"
    descriptor = context["evidence_materials"]
    assert isinstance(descriptor, dict) and descriptor["entries"] >= 60
    index_path = pathlib.Path(descriptor["artifact"]["location"])
    raw = index_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == descriptor["artifact"]["hash"]
    files = json.loads(raw)
    cases = set()
    for ref in files:
        path = pathlib.Path(ref["location"])
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == ref["hash"]
        if ref["kind"] == "test_diagnostic":
            cases.add(json.loads(raw)["case"])
        try:
            path.write_bytes(b"must not mutate evidence")
        except OSError:
            pass
        else:
            raise AssertionError("Evidence became writable")
    assert cases == set(range(60))
    report_path = pathlib.Path(context["evidence"][0]["report"]["location"])
    assert json.loads(report_path.read_text()) == {"tests": 60, "passed": 0}
    try:
        index_path.write_bytes(b"must not mutate index")
    except OSError:
        pass
    else:
        raise AssertionError("Index became writable")
    assert context["evidence"][1]["same_observation_as"] == "evidence[0]"
    result = {"approved": False, "summary": "All real attachments remain readable; assertions still failed", "findings": []}
pathlib.Path(args[args.index("-o") + 1]).write_text(json.dumps(result))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
''')
    context, attachments = inputs(store)
    original = copy.deepcopy(context)
    result = await router.invoke("verify", context, ReviewResult, project, rid, "indexed-evidence", fresh=True)
    assert not result.approved and context == original
    material = store.get(rid, "materials/indexed-evidence")
    assert material["omitted_mandatory_content"] == []
    assert material["prompt_characters"] < 64000
    mounted = {row["hash"] for row in material["materials"]}
    assert {row["artifact"]["hash"] for row in attachments} <= mounted
    assert not list((store.root / "processes").glob("*.json"))


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
async def test_bad_attachment_after_freeze_still_prevents_dispatch(tmp_path, damage):
    router, project, store, rid = fixture_router(tmp_path)
    context, attachments = inputs(store)
    prepare = router._prepare_context

    def damage_after_freeze(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        ref = attachments[-1]["artifact"]
        path = store.root / "artifacts" / ref["hash"][:2] / ref["hash"]
        if damage == "missing":
            path.unlink()
        else:
            path.write_bytes(b"corrupt fixture")
        return prepared

    router._prepare_context = damage_after_freeze
    with pytest.raises(ProviderError, match="unavailable|invalid"):
        await router.invoke("verify", context, ReviewResult, project, rid, "bad-index", fresh=True)
    assert store.get(rid, "invocation/bad-index") is None
    assert store.get(rid, "provider-result/bad-index") is None
    assert not [e for e in store.events(rid) if e["kind"] == "agent.started"]


def test_small_or_unclassified_metadata_remains_exact(tmp_path):
    class UnusedArtifacts:
        def put(self, *args):
            pytest.fail("No catalogue is required")
    for value in ([{"hash": "x", "kind": "k", "bytes": 1, "location": "path"}],
                  [{"mandatory": "preserve this unknown record" * 1000}]):
        context = {"evidence_materials": value, "extension_constraints": "keep exact"}
        rendered = present_materials(context, UnusedArtifacts(), explicit_files={"explicit"})
        assert rendered["evidence_materials"] == value
        assert rendered["extension_constraints"] == "keep exact"
        assert rendered["context_files"] == ["explicit"]
