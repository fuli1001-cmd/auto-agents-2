import json

import pytest
from typer.testing import CliRunner

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.cli import app
from auto_agents_2.config import ConfigStore, validate_extra_args
from auto_agents_2.errors import AA2Error, ConfigurationError, IncompleteIteration, PolicyViolation
from auto_agents_2.models import Change, Coverage, IntakeResult, Requirement, ReviewResult
from auto_agents_2.prompts import PromptCompiler
from auto_agents_2.providers import EventDecoder
from auto_agents_2.requirements import validate_coverage
from auto_agents_2.resources import ResourceManager
from auto_agents_2.store import Store
from auto_agents_2.util import write_json
from auto_agents_2.workspace import WorkspaceManager, apply_changes, git, tree_hash


@pytest.fixture
def project(tmp_path):
    cfg = ConfigStore(tmp_path)
    cfg.initialize()
    WorkspaceManager(tmp_path).initialize()
    return tmp_path


def test_incomplete_is_atomic_and_does_not_read_new_source_or_switch(project):
    store = Store(project)
    original = store.admit("one.md", {})
    with pytest.raises(IncompleteIteration):
        store.admit("two.md", {})
    runner = CliRunner()
    response = runner.invoke(app, ["run", "--project", str(project), "--requirements", "/missing/file.md", "--provider", "not-configured"])
    assert response.exit_code == 2
    assert "ITERATION_INCOMPLETE" in response.output
    assert store.current()["id"] == original["id"]
    assert ConfigStore(project).load().active_provider == "codex"


def test_checkpoint_and_question_survive_restart(project):
    store = Store(project)
    run = store.admit("idea.md", {})
    store.checkpoint(run["id"], "implement/one", {"round": 2, "files": ["app.py"]})
    question = store.request(run["id"], "approval", "approve?", "sha-v1")
    restarted = Store(project)
    assert restarted.get(run["id"], "implement/one")["round"] == 2
    with pytest.raises(AA2Error):
        restarted.answer(question["id"], "approve", "sha-v2")
    restarted.answer(question["id"], "approve", "sha-v1")
    with pytest.raises(AA2Error):
        restarted.answer(question["id"], "approve", "sha-v1")


def test_usage_deduplicates_and_counts_failed_calls(project):
    store = Store(project)
    rid = store.admit("idea", {})["id"]
    for _ in range(2):
        store.add_usage(rid, "codex", "a", "one", {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12, "complete": True})
    store.add_usage(rid, "agy", "b", "one", {"total_tokens": 3, "complete": False})
    summary = Store(project).usage_summary(rid)
    assert summary["total_tokens"] == 15
    assert not summary["complete"]


def test_artifacts_detect_tampering(tmp_path):
    artifacts = ArtifactStore(tmp_path)
    ref = artifacts.put({"version": 1})
    assert artifacts.json(ref) == {"version": 1}
    (artifacts.root / ref["hash"][:2] / ref["hash"]).write_text("bad")
    with pytest.raises(ValueError):
        artifacts.read(ref)


def test_scope_validation_is_all_or_nothing(project):
    (project / "app.py").write_text("old")
    with pytest.raises(PolicyViolation):
        apply_changes(project, [Change(path="app.py", content="new"), Change(path="../bad", content="bad")], ["*"])
    assert (project / "app.py").read_text() == "old"
    (project / "tests").mkdir()
    (project / "tests/test_app.py").write_text("assert False")
    with pytest.raises(PolicyViolation):
        apply_changes(project, [Change(path="tests/test_app.py", content="assert True")], ["tests"])
    with pytest.raises(PolicyViolation, match="replan"):
        apply_changes(project, [Change(path="design/prototypes/index.html", content="changed after approval")], ["design/prototypes"], protected=False)
    outside = project.parent / "outside"
    outside.mkdir(exist_ok=True)
    (project / "escape").symlink_to(outside)
    with pytest.raises(PolicyViolation):
        apply_changes(project, [Change(path="escape/file", content="bad")], ["escape"])


def test_source_and_dependency_changes_affect_fingerprint(project):
    (project / "app.py").write_text("x=1")
    before = tree_hash(project)
    (project / ".aa2/temporary").write_text("noise")
    assert tree_hash(project) == before
    (project / "requirements.txt").write_text("fastapi==1.0")
    assert tree_hash(project) != before


def test_worktrees_preserve_user_index_and_uncommitted_changes(project):
    (project / "app.py").write_text("user change")
    manager = WorkspaceManager(project)
    before = git(project, "status", "--porcelain")
    base = manager.baseline("run_test")
    assert git(project, "status", "--porcelain") == before
    a = manager.allocate("run_test", "task-a", base)
    b = manager.allocate("run_test", "task-b", base)
    (a / "app.py").write_text("A")
    assert (b / "app.py").read_text() == "user change"
    assert (project / "app.py").read_text() == "user change"


def test_coverage_requires_current_evidence(tmp_path):
    result = IntakeResult(summary="same", document="same", requirements=[Requirement(id="R1", description="x", acceptance=["y"])],
                          coverage=[Coverage(requirement_id="R1", status="satisfied", evidence_paths=["missing.py"], rationale="claimed")])
    assert not validate_coverage(result, tmp_path)[0]
    (tmp_path / "missing.py").write_text("implementation")
    assert validate_coverage(result, tmp_path)[0]


def test_config_profiles_environment_and_overrides(project, monkeypatch):
    home = project / "provider-home"
    home.mkdir()
    (home / "deep.config.toml").write_text('model="configured-model"\nmodel_reasoning_effort="high"\n')
    config = ConfigStore(project)
    body = json.loads(config.path.read_text())
    body["providers"]["codex"].update(binary="python3", environment={"CODEX_HOME": str(home), "REMOVE_ME": None, "EMPTY": ""},
                                        profile_map={"deep": "deep", "balanced": "deep", "max": "deep"},
                                        extra_args=["--model", "explicit-model"])
    write_json(config.path, body)
    monkeypatch.setenv("REMOVE_ME", "secret")
    effective = config.resolve("implement")
    assert "REMOVE_ME" not in effective["environment"]
    assert effective["environment"]["EMPTY"] == ""
    assert effective["model"] == "explicit-model"
    config.activate("antigravity")
    assert config.load().active_provider == "antigravity"
    with pytest.raises(ConfigurationError):
        config.activate("unknown")
    for args in (["--dangerously-bypass-approvals-and-sandbox"], ["-c", "sandbox_mode=unsafe"], ["--output-format", "text"]):
        with pytest.raises(ConfigurationError):
            validate_extra_args(args)


def test_automatic_and_manual_gc_share_protection(project):
    store = Store(project)
    rid = store.admit("x", {})["id"]
    resource = store.root / "tmp/task-one"
    resource.mkdir(parents=True)
    (resource / "data").write_text("important checkpoint")
    manager = ResourceManager(store, ConfigStore(project).load().cleanup)
    resource_id = manager.register(resource, "recovery", rid)
    manager.release(resource_id)
    assert manager.collect()["removed"] == 0
    store.update(rid, status="SUCCEEDED")
    assert manager.collect(dry_run=True)["candidates"] == [str(resource)]
    assert resource.exists()
    assert manager.collect()["removed"] == 1
    assert not resource.exists()


def test_streams_normalize_native_usage_without_double_counting():
    parser = EventDecoder("antigravity")
    for total in (10, 25):
        parser.feed({"event": "result", "result": {"status": "SUCCESS", "response": "{}", "usage": {"total_tokens": total}}})
    assert parser.usage.total_tokens == 25
    codex = EventDecoder("codex")
    codex.feed({"type": "thread.started", "thread_id": "session-one"})
    codex.feed({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 8, "output_tokens": 5, "reasoning_output_tokens": 3}})
    assert codex.usage.total_tokens == 15
    assert codex.usage.cached_tokens == 8


def test_prompt_adaptation_preserves_contract():
    compiler = PromptCompiler()
    for model in ("gpt-6.1-sol", "gemini-3.8-flash", "claude-opus-5.5", "custom-model"):
        prompt, family = compiler.compile("review", {"acceptance": "return 403"}, model)
        assert "return 403" in prompt
        assert "read-only" in prompt
        assert "Do not install" in prompt
        assert family
    with pytest.raises(ValueError):
        ReviewResult(approved=True, summary="wrong", findings=[{"severity": "major", "location": "x", "description": "y", "evidence": "z", "resolution": "fix"}])
