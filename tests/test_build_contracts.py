import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest
from test_runtime import configured
from test_workflow import CodingAgent, engine

from auto_agents_2.builds import BUILD_CONTRACT_VERSION, normalized, verification_commands
from auto_agents_2.environment import Environment
from auto_agents_2.errors import PolicyViolation, VerificationPreparationError
from auto_agents_2.evidence import reusable
from auto_agents_2.models import Change, Command, ImplementationResult, PlanResult, ReviewResult, TaskSpec
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.util import digest, write_json
from auto_agents_2.verification import VerificationService, passed
from auto_agents_2.workspace import git, tree_hash


def fixture(work):
    for name in ("frontend", "backend"):
        (work / name).mkdir(parents=True, exist_ok=True)
    (work / "frontend/source.txt").write_text("current build")
    (work / "backend/test_app.py").write_text(
        "from pathlib import Path\ndef test_real_build():\n"
        "    assert (Path(__file__).parents[1]/'frontend/dist/index.html').read_text() == 'current build'\n")
    build = Command(argv=[sys.executable, "-c", "from pathlib import Path; "
        "assert not Path('dist/stale').exists(); "
        "Path('dist/index.html').write_text(Path('source.txt').read_text())"],
        cwd="frontend", inputs=["frontend/source.txt"], outputs=["frontend/dist"])
    check = Command(argv=[sys.executable, "-m", "pytest", "-q", "test_app.py"], cwd="backend", expected_tests=1,
                    inputs=["backend/test_app.py", "frontend/dist"])
    return build, check


def service(project):
    _, store = configured(project)
    environment = Environment(Path(sys.prefix), "build-tests")
    return VerificationService(store.root, SandboxRunner(store.root)), environment


def test_runtime_keeps_external_proxy_and_explicitly_bypasses_loopback(tmp_path, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")
    monkeypatch.setenv("no_proxy", "private.example,127.0.0.0/8")
    monkeypatch.setenv("NO_PROXY", "other.example")
    monkeypatch.setenv("npm_config_no_proxy", "npm.example")
    variables = Environment(Path(sys.prefix), "fixture").variables(tmp_path)
    assert variables["http_proxy"] == "http://proxy.invalid:3128"
    for name in ("no_proxy", "NO_PROXY", "npm_config_no_proxy", "npm_config_noproxy"):
        assert {"private.example", "other.example", "npm.example", "127.0.0.1", "localhost", "[::1]"} <= set(variables[name].split(","))


async def test_build_precedes_consumer_in_same_workspace_and_outputs_invalidate_reuse(tmp_path):
    build, check = fixture(tmp_path)
    (tmp_path / "frontend/dist").mkdir()
    (tmp_path / "frontend/dist/stale").write_text("old build")
    verifier, environment = service(tmp_path)
    before = tree_hash(tmp_path)
    results = await verifier.run([check, build], tmp_path, environment)
    assert passed(results) and [r.argv for r in results] == [build.argv, check.argv]
    assert results[1].tests == 1 and results[0].generated_outputs["frontend/dist"]
    assert not (tmp_path / "frontend/dist/stale").exists()
    assert tree_hash(tmp_path) == before
    assert reusable([check, build], results, tmp_path, environment, verifier.artifacts).allowed
    (tmp_path / "frontend/dist/index.html").write_text("tampered")
    assert not reusable([check, build], results, tmp_path, environment, verifier.artifacts).allowed
    (tmp_path / "frontend/dist/index.html").unlink()
    assert not reusable([check, build], results, tmp_path, environment, verifier.artifacts).allowed


@pytest.mark.parametrize("code,reason", [
    ("raise SystemExit(7)", "process_failed"),
    ("pass", "build_outputs_missing_or_invalid"),
    ("from pathlib import Path; Path('source.txt').write_text('changed')", "process_failed"),
    ("from pathlib import Path; Path('dist/link').symlink_to('../source.txt')", "build_outputs_missing_or_invalid"),
])
async def test_failed_or_empty_build_cannot_consume_old_outputs(tmp_path, code, reason):
    build, check = fixture(tmp_path)
    build.argv[-1] = code
    (tmp_path / "frontend/dist").mkdir()
    (tmp_path / "frontend/dist/stale").write_text("preserve old build")
    verifier, environment = service(tmp_path)
    results = await verifier.run(verification_commands([check], tmp_path, [build]), tmp_path, environment)
    assert len(results) == 2 and reason in results[0].failure_reasons
    assert results[1].process_exit_code is None and results[1].failure_reasons == ["prerequisite_failed"]
    assert results[1].report_status == "not_executed"
    assert not passed(results) and not results[0].generated_outputs
    assert (tmp_path / "frontend/dist/stale").read_text() == "preserve old build"
    assert (tmp_path / "frontend/source.txt").read_text() == "current build"


async def test_independent_failures_are_collected_in_one_observation(tmp_path):
    build, consumer = fixture(tmp_path)
    build.argv[-1] = "raise SystemExit(2)"
    independent = Command(argv=[sys.executable, "-c", "raise SystemExit(3)"], cwd="backend")
    verifier, environment = service(tmp_path)
    rows = await verifier.run([independent, build, consumer], tmp_path, environment)
    assert [r.process_exit_code for r in rows] == [3, 2, None]
    assert rows[-1].failure_reasons == ["prerequisite_failed"]
    assert not any(r.exit_code == 0 for r in rows)


async def test_build_failure_does_not_hide_independent_unit_test_results(tmp_path):
    build, consumer = fixture(tmp_path)
    build.argv[-1] = "raise SystemExit(2)"
    (tmp_path / "frontend/test_independent.py").write_text("def test_unit(): assert True\n")
    independent = Command(argv=[sys.executable, "-m", "pytest", "-q", "test_independent.py"], cwd="frontend", expected_tests=1)
    verifier, environment = service(tmp_path)
    rows = await verifier.run([build, consumer, independent], tmp_path, environment)
    assert rows[1].report_status == "not_executed"
    assert rows[2].tests == 1 and rows[2].report_status == "valid" and rows[2].exit_code == 0


def test_only_consuming_tasks_receive_builds_and_final_acceptance_prepares_first(tmp_path):
    build, check = fixture(tmp_path)
    isolated = Command(argv=[sys.executable, "-c", "pass"], cwd="backend")
    assert verification_commands([isolated], tmp_path, [build]) == [isolated]
    assert verification_commands([check], tmp_path, [build]) == [build, check]
    assert verification_commands([isolated, check, build], tmp_path, [build], prepare_all=True) == [build, isolated, check]


@pytest.mark.parametrize("framework,directory", [("vite", "dist"), ("next", ".next")])
def test_legacy_builds_are_recognized_and_duplicate_timeouts_keep_stronger_contract(tmp_path, framework, directory):
    (tmp_path / "frontend").mkdir()
    write_json(tmp_path / "frontend/package.json", {"devDependencies": {framework: "fixture"},
               "scripts": {"build": "tsc --noEmit && " + framework + " build"}})
    build = Command(argv=["npm", "run", "build"], cwd="frontend", timeout=120, inputs=["frontend/src"])
    preparation = build.model_copy(update={"timeout": 180, "inputs": ["frontend/package.json"]})
    consumer = Command(argv=[sys.executable, "-c", "pass"], inputs=[f"frontend/{directory}/index.html"])
    sequence = verification_commands([consumer, build], tmp_path, [preparation])
    assert len(sequence) == 2 and sequence[0].outputs == [f"frontend/{directory}"]
    assert sequence[0].timeout == 180 and set(sequence[0].inputs) == {"frontend/src", "frontend/package.json"}
    assert sequence[1] == consumer
    assert verification_commands(sequence, tmp_path, [preparation]) == sequence


@pytest.mark.parametrize("output", ["frontend/src", "../dist", "/tmp/dist", "frontend/dist/child"])
def test_output_declaration_cannot_expand_write_permissions(tmp_path, output):
    build, _ = fixture(tmp_path)
    with pytest.raises(PolicyViolation):
        normalized(build.model_copy(update={"outputs": [output]}), tmp_path)


@pytest.mark.parametrize("condition", ["symlink", "tracked"])
def test_build_cannot_replace_source_or_a_link(tmp_path, condition):
    build, _ = fixture(tmp_path)
    if condition == "symlink":
        (tmp_path / "frontend/dist").symlink_to(tmp_path / "backend", target_is_directory=True)
    else:
        git(tmp_path, "init", "-q")
        (tmp_path / "frontend/dist").mkdir()
        (tmp_path / "frontend/dist/user.txt").write_text("tracked user source")
        git(tmp_path, "add", "frontend/dist/user.txt")
    with pytest.raises(PolicyViolation):
        normalized(build, tmp_path)


@pytest.mark.parametrize("case", ["cycle", "ambiguous", "self_dependency"])
def test_invalid_build_graph_fails_before_execution(tmp_path, case):
    first, consumer = fixture(tmp_path)
    if case == "cycle":
        first.inputs = ["backend/dist"]
        second = Command(argv=[sys.executable, "-c", "pass"], cwd="backend",
                         inputs=["frontend/dist"], outputs=["backend/dist"])
    elif case == "ambiguous":
        second = first.model_copy(update={"argv": [sys.executable, "-c", "print('different producer')"]})
    else:
        first.inputs = ["frontend"]
        second = first
    with pytest.raises(VerificationPreparationError):
        verification_commands([consumer], tmp_path, [first, second])


async def test_publication_failure_rolls_back_all_generated_directories(tmp_path, monkeypatch):
    build, _ = fixture(tmp_path)
    build.outputs.append("frontend/.next")
    build.argv[-1] += "; Path('.next/BUILD_ID').write_text('new')"
    for name in ("dist", ".next"):
        (tmp_path / "frontend" / name).mkdir()
        (tmp_path / "frontend" / name / "original").write_text("old")
    verifier, environment = service(tmp_path)
    rename = Path.rename

    def fail_second_publication(path, destination):
        if "generated" in path.parts and path.name == ".next" and destination == tmp_path / "frontend/.next":
            raise OSError("injected publication failure")
        return rename(path, destination)

    monkeypatch.setattr(Path, "rename", fail_second_publication)
    with pytest.raises(OSError, match="injected"):
        await verifier.run([build], tmp_path, environment)
    assert all((tmp_path / "frontend" / name / "original").read_text() == "old" for name in ("dist", ".next"))


async def blocked_workflow(tmp_path, *, failing=False, proposed_changes=False):
    class Agent(CodingAgent):
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            self.calls.append((stage, fresh))
            assert context["verification_contract"]["commands"][0]["outputs"] == ["frontend/dist"]
            if stage == "implement":
                assert context["feedback"]["controller_recheck"]["previous_request"]["replan_reason"] == "Build prerequisite missing"
                return ImplementationResult(summary="Rechecked build and original tests", changes=[])
            assert stage == "review" and fresh and context["checks"][1]["tests"] == 1
            return ReviewResult(approved=True, summary="Reviewed actual results")

    workflow = engine(tmp_path, Agent())
    workflow.sandbox = SandboxRunner(workflow.store.root)
    workflow.verifier = VerificationService(workflow.store.root, workflow.sandbox, workflow.resources)
    rid = workflow.store.admit("fixture://build", {})["id"]
    build, check = fixture(tmp_path)
    if failing:
        (tmp_path / "frontend/source.txt").write_text("actual product failure")
    git(tmp_path, "add", "frontend", "backend")
    git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "candidate")
    base = workflow.workspaces.committed_baseline(rid, "main")
    work = workflow.workspaces.allocate(rid, "T004-p0", base)
    task = TaskSpec(id="T004", origin_id="T004", title="integration", description="real artifacts",
        requirement_ids=["R1"], allowed_paths=["backend"], acceptance=["real build"], checks=[check])
    plan = PlanResult(summary="plan", document="original plan", tasks=[task], final_checks=[check, build],
                      runtime_prepare=[build], runtime_probes=[check])
    failed = (await workflow.verifier.run([check], work, await workflow._environment()))[0]
    failed = failed.model_copy(update={"checker_version": "verification-v4",
        "command_hash": digest({k: v for k, v in check.model_dump().items() if k != "outputs"})})
    response = ImplementationResult(summary="missing build",
        changes=[Change(path="README.md", content="Unapplied old report")] if proposed_changes else [],
        replan_stage="plan", replan_reason="Build prerequisite missing")
    workflow.store.checkpoints(rid, {"plan": plan.model_dump(), "develop/done": ["T001", "T002", "T003"],
        "develop/task/T004": {"phase": "implementing", "feedback": {"checks": [failed.model_dump()]}},
        "invocation/develop/task/T004": {"result": response.model_dump()},
        "rounds/T004": {"round": 3, "stalled": 0, "failure": "old"},
        "replanning/progress": {"tracked": {}, "evaluations": 7, "stalled": 6}})
    pending = workflow.store.prepare_replan(rid, "plan", response.replan_reason)
    workflow.store.request(rid, f"replan-stalled/{pending['evaluation']}", "stop", digest(pending))
    return workflow, rid, plan, base, work, pending


@pytest.mark.parametrize("proposed_changes", [False, True])
async def test_build_recovery_preserves_accepted_work_budgets_and_reviews_full_request(tmp_path, proposed_changes):
    workflow, rid, plan, base, work, pending = await blocked_workflow(tmp_path, proposed_changes=proposed_changes)
    counters = workflow.store.get(rid, "rounds/T004"), workflow.store.get(rid, "replanning/progress")
    before = tree_hash(work)
    assert await workflow._refresh_legacy_build_checks(rid)
    assert workflow.store.get(rid, "replanning/pending") is None and not workflow.store.pending(rid)
    assert workflow.store.get(rid, "develop/done") == ["T001", "T002", "T003"]
    assert workflow.store.get(rid, "plan") == plan.model_dump() and tree_hash(work) == before
    assert (workflow.store.get(rid, "rounds/T004"), workflow.store.get(rid, "replanning/progress")) == counters
    record = workflow.store.get(rid, "candidate/build-recovery/T004/p0")
    assert record["recovered"] and record["pending"] == pending and record["checks"][0]["generated_outputs"]
    assert bool(record["invocation"]["result"]["changes"]) == proposed_changes
    assert not (work / "README.md").exists()
    await workflow._task(rid, plan.tasks[0], base, plan, {"specification": {}})
    assert workflow.agent.calls == [("implement", False), ("review", True)]
    assert workflow.store.get(rid, "develop/task/T004")["commit"]
    assert workflow.store.get(rid, "rounds/T004") == counters[0]


@pytest.mark.parametrize("condition", ["product_failure", "user_change", "unrelated", "already_seen"])
async def test_build_recovery_does_not_clear_unproven_or_new_replans(tmp_path, condition):
    workflow, rid, _, _, work, pending = await blocked_workflow(tmp_path, failing=condition == "product_failure")
    if condition == "user_change":
        (work / "user.txt").write_text("preserve")
    elif condition == "unrelated":
        pending["reason"] = "other issue"
        workflow.store.checkpoint(rid, "replanning/pending", pending)
    elif condition == "already_seen":
        saved = workflow.store.get(rid, "develop/task/T004")
        saved["verification_contract_version"] = BUILD_CONTRACT_VERSION
        workflow.store.checkpoint(rid, "develop/task/T004", saved)
    assert not await workflow._refresh_legacy_build_checks(rid)
    current = workflow.store.get(rid, "replanning/pending")
    assert {k: v for k, v in current.items() if k != "verification_recheck"} == pending
    if condition == "product_failure":
        assert current["verification_recheck"]["checks"][0]["exit_code"] == 0
        assert current["verification_recheck"]["checks"][1]["exit_code"] != 0
    assert not workflow.agent.calls


@pytest.mark.parametrize("fault", ["interruption", "transaction"])
async def test_build_recovery_failure_keeps_pending_request_and_can_resume(tmp_path, fault):
    workflow, rid, _, _, _, pending = await blocked_workflow(tmp_path)
    original = workflow.store.get(rid, "develop/task/T004")
    run = workflow.verifier.run

    async def interrupted(*args):
        raise asyncio.CancelledError()

    if fault == "interruption":
        workflow.verifier.run = interrupted
        error = asyncio.CancelledError
    else:
        with workflow.store.connect() as db:
            db.execute("CREATE TRIGGER fail_recovery BEFORE DELETE ON checkpoints "
                       "WHEN OLD.key='replanning/pending' BEGIN SELECT RAISE(ABORT,'injected'); END")
        error = sqlite3.IntegrityError
    with pytest.raises(error):
        await workflow._refresh_legacy_build_checks(rid)
    assert workflow.store.get(rid, "replanning/pending") == pending
    assert workflow.store.get(rid, "develop/task/T004") == original
    workflow.verifier.run = run
    if fault == "transaction":
        with workflow.store.connect() as db:
            db.execute("DROP TRIGGER fail_recovery")
    assert await workflow._refresh_legacy_build_checks(rid)


async def test_replan_prompt_leads_with_current_failures_and_retains_original_request(tmp_path):
    workflow, rid, _, _, _, pending = await blocked_workflow(tmp_path, failing=True)
    assert not await workflow._refresh_legacy_build_checks(rid)
    from auto_agents_2.util import digest
    workflow.store.checkpoint(rid, "recovery/rules/" + digest(
        ["gates", "upstream", ["accepted_requirements"], "plan_patch"]), {"previous": True})
    from auto_agents_2.decisions import DecisionResult
    async def decision(*args, **kwargs):
        return DecisionResult(action="ask_user", reason="Review the current failing checks before continuing")
    workflow.agent.invoke = decision

    async def inspect(question):
        prompt = question["prompt"]
        assert prompt.index("test_app::test_real_build") < prompt.index(pending["reason"])
        assert "当前故障" in prompt and "原恢复请求" in prompt
        raise asyncio.CancelledError()

    workflow.question_handler = inspect
    with pytest.raises(asyncio.CancelledError):
        await workflow._resume_replan(rid)
    assert not workflow.store.get(rid, "replanning/pending")["granted"]
