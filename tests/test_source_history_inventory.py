"""Historical path existence must not be inferred from selected text bodies."""
import json

import pytest
from test_review_evidence import commit
from test_workflow import CodingAgent, engine

import auto_agents_2.source_history as history_module
from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.errors import AA2Error
from auto_agents_2.source_history import SourceHistory
from auto_agents_2.util import digest, write_json
from auto_agents_2.workspace import git, tree_hash


def recorded_tree(tmp_path):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("historical configuration coverage", {})["id"]
    files = {
        "app.py": "value = 1\n",
        "pytest.ini": "[pytest]\ntestpaths = tests/unit\n",
        "tests/unit/test_app.py": "def test_value():\n    assert 1 == 1\n",
        "frontend/tests/setup.ts": "// retained setup\r\n",
        "frontend/package-lock.json": '{"lockfileVersion": 3}\n',
        "docs/design.md": "# Historical design\n",
        "support/config_helper.py": "def configure():\n    return 'fixture'\n",
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
    commit(tmp_path)
    sha = git(tmp_path, "rev-parse", "HEAD")
    workflow.store.checkpoint(rid, "baseline", sha)
    return workflow, rid, sha, files


def test_complete_tree_distinguishes_absence_from_outside_body_scope(tmp_path):
    workflow, rid, sha, original = recorded_tree(tmp_path)
    history = SourceHistory(workflow).collect(rid, tmp_path, ["app.py"])
    report = workflow.artifacts.json(history["artifact"])
    snap = report["snapshots"][0]
    assert snap["commit"] == sha and snap["root_tree"] == git(tmp_path, "rev-parse", sha + "^{tree}")
    assert not snap["private_paths_omitted"]
    assert set(snap["public_tree"]) == set(original)
    assert all(snap["public_tree"][p]["object"] == git(tmp_path, "rev-parse", sha + ":" + p) for p in original)
    assert snap["public_tree"]["support/config_helper.py"]["body_status"] == "outside_scope"
    assert "support/config_helper.py" not in snap["files"]
    # These existing protected files are readable even though they are outside app.py.
    for name in ["pytest.ini", "tests/unit/test_app.py", "frontend/tests/setup.ts", "frontend/package-lock.json"]:
        assert snap["public_tree"][name]["body_status"] == "included"
        assert workflow.artifacts.bytes(snap["files"][name]["content"]) == original[name].encode()
    for name in ["conftest.py", "tests/conftest.py", "tests/unit/conftest.py", "pyproject.toml", "setup.cfg", "tox.ini"]:
        assert name not in snap["public_tree"]
    assert report["missing"] == [] and report["coverage"]["missing"] == "unresolved_anchors_only"
    assert report["coverage"]["inventory"] == "complete_public_git_tree"
    assert report["coverage"]["body_selection"] == "protected_files_plus_scope"
    assert report["scope"] == ["app.py"] and report["version"] == 4


def test_present_unreadable_and_private_entries_are_not_reported_as_absent(tmp_path, monkeypatch):
    workflow, rid, _, _ = recorded_tree(tmp_path)
    (tmp_path / "tests/unit/binary.dat").write_bytes(b"\xff\xfe")
    (tmp_path / "private").mkdir()
    (tmp_path / "private/conftest.py").write_text("PRIVATE_PATH_FIXTURE_PLACEHOLDER")
    (tmp_path / ".env").write_text("SECRET_FIXTURE_PLACEHOLDER")
    (tmp_path / "conftest.py").symlink_to("private/conftest.py")
    config = workflow.config.load().model_dump(mode="json")
    config["sensitive_paths"] = ["private/**"]
    write_json(workflow.config.path, config)
    git(tmp_path, "add", "--force", ".env", "private/conftest.py")
    commit(tmp_path)
    sha = git(tmp_path, "rev-parse", "HEAD")
    workflow.store.checkpoint(rid, "baseline", sha)
    forbidden = {git(tmp_path, "rev-parse", sha + ":" + name) for name in
                 [".env", "private/conftest.py", "conftest.py", "support/config_helper.py", "docs/design.md"]}
    original_run = history_module.subprocess.run
    read_objects = []

    def observe(argv, *args, **kwargs):
        if "cat-file" in argv:
            read_objects.append(argv[-1])
            assert argv[-1] not in forbidden, "Inventory must not read excluded bodies or link targets"
        return original_run(argv, *args, **kwargs)

    monkeypatch.setattr(history_module.subprocess, "run", observe)
    report = workflow.artifacts.json(SourceHistory(workflow).collect(rid, tmp_path, ["app.py"])["artifact"])
    snap = report["snapshots"][0]
    assert snap["private_paths_omitted"]
    assert "private/conftest.py" not in snap["public_tree"] and ".env" not in snap["public_tree"]
    assert snap["public_tree"]["conftest.py"]["body_status"] == "unsupported_type"
    assert snap["public_tree"]["conftest.py"]["mode"] == "120000"
    assert snap["public_tree"]["tests/unit/binary.dat"]["body_status"] == "non_utf8"
    assert "conftest.py" not in snap["files"] and "tests/unit/binary.dat" not in snap["files"]
    assert read_objects and "SECRET_FIXTURE_PLACEHOLDER" not in json.dumps(report)
    assert "PRIVATE_PATH_FIXTURE_PLACEHOLDER" not in json.dumps(report)


def test_gitlink_is_an_explicit_boundary_without_reading_external_tree(tmp_path):
    workflow, rid, base, _ = recorded_tree(tmp_path)
    git(tmp_path, "update-index", "--add", "--cacheinfo", "160000," + base + ",vendor")
    git(tmp_path, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "gitlink")
    workflow.store.checkpoint(rid, "baseline", git(tmp_path, "rev-parse", "HEAD"))
    report = workflow.artifacts.json(SourceHistory(workflow).collect(rid, tmp_path)["artifact"])
    entry = report["snapshots"][0]["public_tree"]["vendor"]
    assert entry == {"mode": "160000", "type": "commit", "object": base, "body_status": "unsupported_type"}
    assert not any(name.startswith("vendor/") for name in report["snapshots"][0]["public_tree"])
    assert "untracked" in report["coverage"]["limits"] and "external" in report["coverage"]["limits"]


def test_missing_anchor_stays_incomplete_even_with_a_complete_public_tree(tmp_path):
    workflow, rid, _, _ = recorded_tree(tmp_path)
    missing = f"refs/aa2/{rid}/replans/0/missing"
    workflow.store.checkpoint(rid, "replan_feedback", {"recovery_refs": [missing]})
    result = SourceHistory(workflow).collect(rid, tmp_path)
    report = workflow.artifacts.json(result["artifact"])
    assert result["status"] == "incomplete" and report["snapshots"][0]["public_tree"]
    assert result["missing"] == [{"anchor": missing, "status": "missing"}]


def test_tree_read_failure_cannot_publish_a_complete_inventory(tmp_path, monkeypatch):
    workflow, rid, _, _ = recorded_tree(tmp_path)
    original_git = history_module.git

    def fail(work, *args, **kwargs):
        if "ls-tree" in args:
            raise AA2Error("historical tree unavailable")
        return original_git(work, *args, **kwargs)

    monkeypatch.setattr(history_module, "git", fail)
    with pytest.raises(AA2Error, match="historical tree unavailable"):
        SourceHistory(workflow).collect(rid, tmp_path)
    with workflow.store.connect() as db:
        assert not db.execute("SELECT 1 FROM checkpoints WHERE run_id=? AND key LIKE 'review-evidence/source-history/%'", (rid,)).fetchall()


def test_new_collection_does_not_reuse_or_overwrite_a_frozen_v2_manifest(tmp_path):
    workflow, rid, base, _ = recorded_tree(tmp_path)
    source = tree_hash(tmp_path)
    old = {"status": "available", "source_hash": source, "anchors": {"baseline": base}, "missing": [],
           "artifact": workflow.artifacts.put({"version": 2, "scope": ["app.py"]}, "source_history")}
    old_key = "review-evidence/source-history/" + digest([2, source, old["anchors"], [], ["app.py"], [], []])
    workflow.store.checkpoint(rid, old_key, old)
    workflow.store.checkpoint(rid, "recovery/patch-context/frozen", {"source_history": old})
    new = SourceHistory(workflow).collect(rid, tmp_path, ["app.py"])
    assert new["artifact"] != old["artifact"]
    assert workflow.artifacts.json(new["artifact"])["snapshots"][0]["public_tree"]
    assert SourceHistory(workflow).collect(rid, tmp_path, ["app.py"]) == new
    assert workflow.store.get(rid, old_key) == old
    assert workflow.store.get(rid, "recovery/patch-context/frozen") == {"source_history": old}


@pytest.mark.parametrize("stage,extra", [
    ("implement", {}), ("plan", {"plan_patch": True}),
    ("review", {"review_kind": "implementation"}), ("review", {"review_kind": "plan_patch"}),
    ("recovery_diagnosis", {}), ("verify", {}),
])
def test_shared_consumers_can_read_inventory_without_mounting_unselected_bodies(tmp_path, stage, extra):
    workflow, rid, _, original = recorded_tree(tmp_path)
    history = SourceHistory(workflow).collect(rid, tmp_path, ["app.py"])
    before = tree_hash(tmp_path)
    view = ContextPlanner(workflow.artifacts).prepare(stage, {**extra, "source_history": history}, tmp_path)
    visible = workflow.artifacts.json(view.payload["source_history"]["artifact"])
    assert set(visible["snapshots"][0]["public_tree"]) == set(original)
    mounted = {ref["hash"] for ref in view.payload["evidence_materials"]}
    assert digest(original["support/config_helper.py"].encode()) not in mounted
    assert digest(original["pytest.ini"].encode()) in mounted
    assert tree_hash(tmp_path) == before
