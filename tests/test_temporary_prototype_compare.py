"""Synthetic comparison only: no native profiles, credentials, network or real models."""
import asyncio
import copy
import sys
from pathlib import Path

import pytest
from authority_review_fixtures import authority_review
from browser_fixtures import prototype_rows
from prototype_review_fixtures import prototype_verdict
from test_decisions import ApprovingAgent
from test_prototype_recovery import prototype_fixture
from test_workflow import engine, fixture_host_volume
from visual_design_fixtures import acquired_source, visual_brief

from auto_agents_2.config import ConfigStore
from auto_agents_2.engine import Replan
from auto_agents_2.errors import PolicyViolation
from auto_agents_2.faults import StateConflict
from auto_agents_2.models import (
    BrowserAction,
    Change,
    Coverage,
    DocumentResult,
    IntakeResult,
    Requirement,
    SourceEvidence,
)
from auto_agents_2.providers import ProviderRouter
from auto_agents_2.research import ResearchService
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.storage_space import StorageMonitor
from auto_agents_2.temporary_prototype_compare import (
    ComparisonEngine,
    StageConfig,
    frozen_input,
    initialize_arm,
    validate_frozen,
)
from auto_agents_2.util import digest, write_json
from auto_agents_2.workspace import git


def provider_configs(project):
    config = ConfigStore(project)
    body = config.load().model_dump(mode="json")
    body["providers"] = {"codex": body["providers"]["codex"]}
    body["active_provider"] = "codex"
    body["providers"]["codex"]["binary"] = sys.executable
    body["cleanup"]["min_free_bytes"] = 0
    write_json(config.path, body)
    agy = copy.deepcopy(body["providers"]["codex"])
    agy.update(kind="antigravity", binary=sys.executable, extra_args=[], environment={},
        profile_map={level: {"model": "gemini-3.8-flash-high", "effort": "high"}
                     for level in ("balanced", "deep", "max")})
    return config.load(), agy


async def test_stage_selection_is_concurrent_local_and_restored(tmp_path):
    ConfigStore(tmp_path).initialize()
    base, agy = provider_configs(tmp_path)
    configs = [StageConfig(tmp_path, arm, agy, digest(base.model_dump(mode="json"))) for arm in ("codex", "agy")]
    original = ConfigStore(tmp_path).path.read_bytes()
    async def inspect(config, arm):
        assert config.load() == base
        with config.at("prototype"):
            await asyncio.sleep(0)
            assert config.load().active_provider == arm
            assert set(config.load().providers) == {arm}
            with config.at("review"):
                assert config.load() == base
            assert config.load().active_provider == arm
        assert config.load() == base
    await asyncio.gather(*(inspect(cfg, arm) for cfg, arm in zip(configs, ("codex", "agy"))))
    assert ConfigStore(tmp_path).path.read_bytes() == original


def test_codex_selection_preserves_native_profile_denial(tmp_path, monkeypatch):
    ConfigStore(tmp_path).initialize()
    _, agy = provider_configs(tmp_path)
    body = ConfigStore(tmp_path).load().model_dump(mode="json")
    body["providers"]["codex"]["profile_map"]["max"] = "max"
    write_json(ConfigStore(tmp_path).path, body)
    calls = []
    def denied(path, **kwargs):
        calls.append(path.name)
        if path.name == "max.config.toml":
            raise PolicyViolation("EXAMPLE denied selected input")
        return b""
    monkeypatch.setattr("auto_agents_2.transports.native_input_bytes", denied)
    cfg = StageConfig(tmp_path, "codex", agy, digest(ConfigStore(tmp_path).load().model_dump(mode="json")))
    with cfg.at("prototype"), pytest.raises(PolicyViolation, match="denied selected input"):
        cfg.resolve("prototype")
    assert calls == ["config.toml", "max.config.toml"]
    assert cfg.load().providers["codex"].profile_map["max"] == "max"


def test_agy_selection_does_not_bypass_native_oauth_denial(tmp_path, monkeypatch):
    ConfigStore(tmp_path).initialize()
    _, agy = provider_configs(tmp_path)
    home = tmp_path / "owned-home"
    token = home / ".gemini/antigravity-cli/antigravity-oauth-token"
    token.parent.mkdir(parents=True)
    token.write_text("EXAMPLE_NOT_A_CREDENTIAL")
    monkeypatch.setenv("HOME", str(home))
    original = Path.is_file
    def denied(path):
        if path == token:
            raise PolicyViolation("EXAMPLE denied OAuth input metadata")
        return original(path)
    monkeypatch.setattr(Path, "is_file", denied)
    cfg = StageConfig(tmp_path, "agy", agy, digest(ConfigStore(tmp_path).load().model_dump(mode="json")))
    workflow = engine(tmp_path, ApprovingAgent())
    router = ProviderRouter(cfg, workflow.store, workflow.sandbox)
    with cfg.at("prototype"), pytest.raises(PolicyViolation, match="denied OAuth"):
        router.effective("prototype", "agy")


@pytest.mark.parametrize("damage", [None, "review", "image", "actions"])
async def test_deferred_human_gate_retains_full_automation_check(tmp_path, monkeypatch, damage):
    workflow, rid, _, _, calls = prototype_fixture(tmp_path, monkeypatch)
    async def no_question(*args):
        pytest.fail("Comparison must not ask for or publish human approval")
    workflow.question_handler = no_question
    async def forbidden_catalog(*args):
        pytest.fail("Both arms must use the frozen catalog rather than a fresh query")
    monkeypatch.setattr("auto_agents_2.engine.DesignCatalog.candidates", forbidden_catalog)
    from visual_design_fixtures import URL
    spec = IntakeResult(summary="UI", document="UI", frontend=True)
    document, _ = await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI",
                                                   defer_human=True, frozen_design_candidates=[{"url": URL}])
    assert calls[-2:] == ["browser", "review"]
    assert workflow.store.get(rid, "prototype_approval") is None
    assert not any(q["key"] == "prototype-approval" for q in workflow.store.pending(rid))
    pending = workflow.store.get(rid, "prototype/pending")
    review, visuals = copy.deepcopy(pending["review"]), copy.deepcopy(pending["visuals"])
    if damage == "review":
        review["approved"] = False
    elif damage == "image":
        visuals[0]["image_artifact"]["hash"] = "0" * 64
    elif damage == "actions":
        document.browser_actions = [BrowserAction(route="/different.html", action="expect_text", selector="h1", value="Changed")]
    if damage:
        with pytest.raises((Replan, ValueError)):
            await workflow._record_prototype_approval(rid, tmp_path, document, review, visuals, defer_human=True)
    else:
        assert await workflow._record_prototype_approval(rid, tmp_path, document, review, visuals, defer_human=True) is None
    assert workflow.store.get(rid, "prototype_approval") is None


class ComparisonAgent(ApprovingAgent):
    def __init__(self, label="source"):
        super().__init__()
        self.label, self.observed = label, []

    async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
        self.observed.append((stage, context))
        if stage == "clarify":
            return IntakeResult(summary="Task UI", document="Create a simple task interface.", frontend=True,
                ui_changes=True, prototype_paths=["design/prototypes/**", "DESIGN.md"], visual_brief=visual_brief(update="required"),
                requirements=[Requirement(id="R1", description="Task UI", acceptance=["Show the task"])],
                coverage=[Coverage(requirement_id="R1", status="changed", rationale="New blank project")])
        if stage == "provider_research":
            return DocumentResult(summary="research", document="Retained fixture fact",
                sources=[SourceEvidence(url="https://fixture.invalid/reference", claim="Retained fixture fact")])
        if stage == "prototype":
            assert not (workspace / "DESIGN.md").exists()
            assert not (workspace / "design/prototypes").exists()
            return DocumentResult(summary="Fresh task UI", document="New isolated design", files=[
                Change(path="DESIGN.md", content="Fresh task design. Awaiting user comparison; not approved."),
                Change(path="design/prototypes/index.html", content="<h1>" + self.label + " task</h1>")],
                browser_actions=[BrowserAction(route="/index.html", action="expect_text", selector="h1", value=self.label + " task")])
        if context.get("review_kind") == "prototype":
            return prototype_verdict(context)
        if context.get("review_kind") == "research":
            return authority_review(context)
        return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh=fresh)


async def shared_source(tmp_path, monkeypatch):
    project = tmp_path / "shared"
    project.mkdir()
    workflow = engine(project, ComparisonAgent())
    cfg, agy = provider_configs(project)
    (project / "requirements.md").write_text("Create a simple task-focused Chinese interface.\n")
    git(project, "add", "requirements.md")
    git(project, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "requirements")
    async def retrieve(service, sources):
        return [{**acquired_source(service.artifacts, url=source.url), **source.model_dump()} for source in sources]
    monkeypatch.setattr(ResearchService, "verify", retrieve)
    result = await workflow.start(str(project / "requirements.md"), stop_after="provider_research")
    assert result["status"] == "PAUSED"
    source = frozen_input(project, result["id"])
    source.update(design_candidates=[], configuration_hash=digest(cfg.model_dump(mode="json")), agy_configuration_hash=digest(agy))
    source["input_hash"] = digest(source)
    return workflow, result["id"], source, cfg, agy


async def test_both_arms_import_one_input_run_normally_and_never_approve(tmp_path, monkeypatch):
    parent, original_rid, source, cfg, agy = await shared_source(tmp_path, monkeypatch)
    original_events = parent.store.events()
    engines, runs = {}, {}
    for arm in ("codex", "agy"):
        project = tmp_path / arm
        rid = initialize_arm(project, arm, source, cfg)
        agent = ComparisonAgent(arm)
        current = ComparisonEngine(project, arm, agy, agent=agent,
            sandbox=SandboxRunner(project / ".aa2", _test_unconfined=True))
        current.storage = StorageMonitor(current.store.root, backing_volume_reader=fixture_host_volume)
        async def preflight():
            return {"ok": True, "fixture": True}
        async def installed(*args):
            pass
        current.toolchain.check = preflight
        current._ensure_browser = installed
        engines[arm], runs[arm] = current, rid
    async def browser(work, *args):
        selected = next(value for value in engines.values() if work.is_relative_to(value.project))
        return prototype_rows(work, args[1] if len(args) > 1 else [], selected.artifacts)
    monkeypatch.setattr("auto_agents_2.engine.inspect_prototype", browser)
    # Production launches separate OS processes. Run these in-memory engine fixtures
    # sequentially because the unchanged browser runtime has process-global state.
    from auto_agents_2.design import browser_environment
    results = []
    for arm in engines:
        browser_environment(engines[arm].store.root / "cache/browsers")
        results.append(await engines[arm].start(run_id=runs[arm]))
    assert all(row["status"] == "PAUSED" and row["stage"] == "prototype" for row in results)
    for arm, current in engines.items():
        rid = runs[arm]
        ready = current.store.get(rid, "comparison/result")
        assert ready, current.store.get(rid, "comparison/blocked")
        assert ready["status"] == "ready_for_user_comparison" and ready["input_hash"] == source["input_hash"]
        assert current.store.get(rid, "prototype") is None and current.store.get(rid, "prototype_approval") is None
        assert current.store.get(rid, "design") is None and current.store.get(rid, "plan") is None
        assert current.store.get(rid, "provider_research") == source["checkpoints"]["provider_research"]
        assert not any(stage in {"clarify", "provider_research", "design", "plan", "implement"} for stage, _ in current.agent.observed)
        before = len(current.agent.observed)
        assert initialize_arm(current.project, arm, source, cfg) == rid
        browser_environment(current.store.root / "cache/browsers")
        await current.start(run_id=rid)
        assert len(current.agent.observed) == before
    assert runs["codex"] != runs["agy"]
    assert parent.store.run(original_rid)["stage"] == "provider_research"
    assert parent.store.events() == original_events


async def test_import_rejects_corrupt_source_and_changed_manifest(tmp_path, monkeypatch):
    parent, rid, source, cfg, _ = await shared_source(tmp_path, monkeypatch)
    bad = copy.deepcopy(source)
    bad["design_candidates"] = [{"url": "https://different.invalid"}]
    with pytest.raises(StateConflict, match="Frozen"):
        validate_frozen(bad)
    ref = source["checkpoints"]["capture"]["artifact"]
    (parent.artifacts.root / ref["hash"][:2] / ref["hash"]).write_text("Changed owned fixture")
    with pytest.raises(StateConflict, match="bytes changed"):
        initialize_arm(tmp_path / "damaged", "codex", source, cfg)
    assert parent.store.run(rid)["status"] == "PAUSED"


async def test_configuration_drift_and_downstream_reentry_are_preserved(tmp_path, monkeypatch):
    from auto_agents_2.temporary_prototype_compare import ComparisonPause
    parent, _, source, cfg, agy = await shared_source(tmp_path, monkeypatch)
    project = tmp_path / "arm"
    rid = initialize_arm(project, "codex", source, cfg)
    workflow = ComparisonEngine(project, "codex", agy, agent=ComparisonAgent())
    pending = {"stage": "provider_research", "reason": "Fixture source correction", "granted": True, "permit": "owned-permit"}
    workflow.store.checkpoints(rid, {"replanning/pending": pending, "owned-permit": {"phase": "ready"}})
    shared = workflow.store.get(rid, "provider_research")
    async def forbidden(*args):
        pytest.fail("Shared-source reentry must stop before existing controller work")
    workflow.toolchain.check = forbidden
    with pytest.raises(ComparisonPause):
        await workflow.start(run_id=rid)
    with pytest.raises(ComparisonPause):
        await workflow._resume_replan(rid)
    assert workflow.store.get(rid, "replanning/pending") == pending
    assert workflow.store.get(rid, "owned-permit") == {"phase": "ready"}
    assert workflow.store.get(rid, "provider_research") == shared
    body = cfg.model_dump(mode="json")
    body["efforts"]["review"] = "deep"
    write_json(ConfigStore(project).path, body)
    with pytest.raises(StateConflict, match="configuration changed"):
        workflow.config.load()
    with pytest.raises(StateConflict):
        initialize_arm(project, "codex", source, cfg)


async def test_arm_controllers_are_distinct_processes(tmp_path, monkeypatch):
    import os

    from auto_agents_2.temporary_prototype_compare import run_arm_process
    real_create = asyncio.create_subprocess_exec
    calls, pids = [], []
    async def fixture_process(*args, **kwargs):
        calls.append((args, kwargs["start_new_session"]))
        process = await real_create(sys.executable, "-c", "import os,time; print(os.getpid(), flush=True); time.sleep(.15)", **kwargs)
        pids.append(process.pid)
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fixture_process)
    for arm in ("codex", "agy"):
        (tmp_path / arm / ".aa2").mkdir(parents=True)
    codes = await asyncio.gather(*(run_arm_process(tmp_path / arm, arm, "owned-run", tmp_path / "fixture-config.json")
                                   for arm in ("codex", "agy")))
    assert codes == [0, 0] and len(set(pids)) == 2 and os.getpid() not in pids
    assert all(session and "auto_agents_2.temporary_prototype_compare" in args for args, session in calls)
    assert {args[args.index("--arm") + 1] for args, _ in calls} == {"codex", "agy"}


async def test_failed_child_cannot_publish_retained_ready_result(tmp_path, monkeypatch):
    import json

    import auto_agents_2.temporary_prototype_compare as comparison
    parent, rid, _, cfg, agy = await shared_source(tmp_path, monkeypatch)
    body = cfg.model_dump(mode="json")
    body.update(providers={"agy": agy}, active_provider="agy")
    agy_config = tmp_path / "owned-agy-config.json"
    write_json(agy_config, body)
    async def candidates(*args):
        return []
    monkeypatch.setattr("auto_agents_2.design.DesignCatalog.candidates", candidates)
    codes = {"codex": 0, "agy": 0}
    async def child(project, arm, run, config):
        return codes[arm]
    monkeypatch.setattr(comparison, "run_arm_process", child)
    def retained_ready(project, run, input_hash):
        return {"run_id": run, "status": "ready_for_user_comparison",
                "result": {"fixture": "retained result"}}
    monkeypatch.setattr(comparison, "arm_result", retained_ready)
    output = tmp_path / "comparison"
    await comparison.compare(parent.project, rid, output, agy_config)
    assert json.loads((output / "results.json").read_text())["status"] == "awaiting_user_comparison"
    codes["agy"] = 23
    results = await comparison.compare(parent.project, rid, output, agy_config)
    assert results["agy"]["status"] == "incomplete"
    assert results["agy"]["result"] is None and results["agy"]["controller_exit_code"] == 23
    assert results["codex"]["status"] == "ready_for_user_comparison"
    assert json.loads((output / "results.json").read_text())["status"] == "incomplete"
    assert parent.store.run(rid)["status"] == "PAUSED"
