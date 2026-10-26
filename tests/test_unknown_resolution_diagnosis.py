"""Unknown solver observations admit diagnosis, never implicit repair or retry."""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from authority_review_fixtures import seed_original
from test_manifest_references import plan
from test_npm_failure_routing import installer
from test_workflow import CodingAgent, engine

from auto_agents_2.engine import WorkflowEngine
from auto_agents_2.environment import Environment, EnvironmentManager
from auto_agents_2.faults import HumanIntervention
from auto_agents_2.models import Change, ImplementationResult

UNKNOWN = "npm error Unable to resolve reference $vitest"


class ResolutionAgent(CodingAgent):
    def __init__(self):
        super().__init__()
        self.implementations = 0
        self.by_task = {}
        self.diagnoses = []

    async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
        if context.get("review_kind") == "recovery_diagnosis":
            self.diagnoses.append(context)
            assert context["failure"]["evidence"]["environment_failure"]["classification"] == "unknown"
        if stage == "implement":
            self.implementations += 1
            task_id = context["task"]["id"]
            self.by_task[task_id] = self.by_task.get(task_id, 0) + 1
            if self.by_task[task_id] > 1:
                assert UNKNOWN in context["feedback"]["environment_error"]
                return ImplementationResult(
                    summary="fixture repair through normal implementation",
                    changes=[Change(path="package.json", content='{"name":"fixture","version":"1.0.1"}')],
                )
            result = await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)
            result.changes.append(Change(path="package.json", content='{"name":"fixture","version":"1.0.0"}'))
            return result
        return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)


def configured_case(tmp_path, message=UNKNOWN):
    agent = ResolutionAgent()
    workflow = engine(tmp_path, agent)
    runtime = installer(workflow.store.root / "environments/fixture-node", message)
    (runtime.prefix / "bin/python").symlink_to(sys.executable)
    (runtime.prefix / "bin/npm").write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, sys
from pathlib import Path
if json.loads(Path('package.json').read_text())['version'] == '1.0.0':
    message = MESSAGE
    Path(os.environ['npm_config_logs_dir'], 'fixture-debug-0.log').write_text(message + '\\n')
    print(message, file=sys.stderr)
    sys.exit(1)
if '--package-lock-only' in sys.argv:
    Path('package-lock.json').write_text('{"lockfileVersion":3,"packages":{}}')
else:
    Path('node_modules').mkdir(exist_ok=True)
""".replace("MESSAGE", repr(message))
    )
    ready = {"fingerprint": "fixture-node", "integrity": EnvironmentManager.integrity(runtime.prefix)}
    (runtime.prefix / "aa2-ready.json").write_text(json.dumps(ready))
    runtime = Environment(runtime.prefix, EnvironmentManager.installed_identity(runtime.prefix))
    workflow._environment = WorkflowEngine._environment.__get__(workflow)

    async def ensure(*args):
        return runtime

    async def resolution(work, environment):
        return environment

    workflow.environments.ensure = ensure
    workflow.environments.with_resolution = resolution
    rid = workflow.store.admit("addition", {})["id"]
    seed_original(workflow, rid)
    proposal = plan()
    proposal.tasks[0].origin_id = "T1"
    proposal.tasks[0].allowed_paths.extend(["package.json", "package-lock.json"])
    base = workflow.workspaces.committed_baseline(rid, "main")
    workflow.store.checkpoints(rid, {"plan": proposal.model_dump(), "task_origins": {"T1": "T1"}})
    return workflow, rid, proposal, base, {"specification": {"description": "addition"}}


async def failed_command(tmp_path, message=UNKNOWN):
    workflow, rid, proposal, base, context = configured_case(tmp_path, message)
    with pytest.raises(HumanIntervention) as caught:
        await workflow._observed_task(rid, proposal.tasks[0], base, proposal, context)
    return workflow, rid, proposal, base, context, caught.value


async def test_real_unknown_npm_task_enters_diagnosis_then_protected_repair(tmp_path):
    workflow, rid, proposal, base, context, boundary = await failed_command(tmp_path)
    source = workflow.store.get(rid, "develop/task/T1")["source_hash"]
    await workflow._wait_prerequisite(rid, boundary)
    active = workflow.store.get(rid, "recovery/active")
    assert active["failure"]["kind"] == "environment"
    assert active["failure"]["evidence"]["environment_failure"]["classification"] == "unknown"
    assert not workflow.store.pending(rid)
    assert workflow.store.get(rid, "rounds/T1")["round"] == 1
    assert not workflow.agent.diagnoses
    await workflow.recovery.resume(rid)
    assert len(workflow.agent.diagnoses) == 1
    saved = workflow.store.get(rid, "develop/task/T1")
    assert saved["source_hash"] == source and saved["recovery_permit"]
    await workflow._task(rid, proposal.tasks[0], base, proposal, context)
    saved = workflow.store.get(rid, "develop/task/T1")
    assert saved["commit"] and saved["checks"][0]["exit_code"] == 0
    assert workflow.agent.implementations == 2
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/T1")["round"] == 1


@pytest.mark.parametrize(
    "message",
    [
        "npm error code EACCES",
        "npm error code E401",
        "npm error code EINTEGRITY",
        "npm error code ENOSPC",
        "npm error code SELF_SIGNED_CERT_IN_CHAIN",
    ],
)
async def test_real_prerequisites_still_wait_without_diagnosis_or_install_retry(tmp_path, message):
    workflow, rid, _, _, _, boundary = await failed_command(tmp_path, message)

    async def leave_pending(question):
        raise asyncio.CancelledError()

    workflow.question_handler = leave_pending
    with pytest.raises(asyncio.CancelledError):
        await workflow._wait_prerequisite(rid, boundary)
    assert len(workflow.store.pending(rid)) == 1
    assert workflow.store.pending(rid)[0]["answer"] is None
    assert workflow.store.get(rid, "recovery/active") is None
    assert not workflow.agent.diagnoses
    assert workflow.agent.implementations == 1


def pending_observation(workflow, rid):
    return next(iter(workflow._environment_waits(rid).values()))


def replace_observation(workflow, rid, old, update):
    import copy

    from auto_agents_2.util import digest

    value = copy.deepcopy(old)
    update(value)
    value["identity"] = digest([value["reason_code"], value["phase"], value["requirement"]])
    workflow.store.checkpoints(
        rid,
        {
            "environment/prerequisite-queue": {value["identity"]: value},
            "environment/prerequisite-observations/" + value["identity"]: value,
        },
    )
    return value


async def old_wait(workflow, rid, boundary, monkeypatch):
    from auto_agents_2 import resolution_diagnosis

    async def pause(question):
        raise asyncio.CancelledError()

    original = workflow.question_handler
    workflow.question_handler = pause
    with monkeypatch.context() as patch:
        patch.setattr(resolution_diagnosis, "admit", lambda *args: False)
        with pytest.raises(asyncio.CancelledError):
            await workflow._wait_prerequisite(rid, boundary)
    workflow.question_handler = original
    return workflow.store.pending(rid)[0]


async def test_unanswered_wait_converts_atomically_and_reentry_does_not_repeat(tmp_path, monkeypatch):
    from auto_agents_2.resolution_diagnosis import PREFIX

    workflow, rid, _, _, _, boundary = await failed_command(tmp_path)
    q = await old_wait(workflow, rid, boundary, monkeypatch)
    old = workflow.store.get(rid, "environment/prerequisite-wait")
    workflow.store.checkpoint(rid, "prototype_approval", {"untouched": "original-bound-approval"})
    before_round = workflow.store.get(rid, "rounds/T1")
    await workflow._resume_environment_waits(rid)
    active = workflow.store.get(rid, "recovery/active")
    record = workflow.store.get(rid, PREFIX + old["identity"])
    assert record["questions"][0]["id"] == q["id"]
    assert workflow.store.question(q["id"])["superseded"] == 1
    assert workflow.store.question(q["id"])["answer"] is None
    assert workflow.store.question(q["id"])["prompt"] == q["prompt"]
    assert workflow.store.get(rid, "prototype_approval") == {"untouched": "original-bound-approval"}
    assert workflow.store.get(rid, "rounds/T1") == before_round
    assert workflow.store.get(rid, "prerequisite/" + old["identity"], 0) == 0
    await workflow._resume_environment_waits(rid)
    assert workflow.store.get(rid, "recovery/active") == active
    assert workflow.store.get(rid, "recovery/count/" + active["failure"]["id"]) == 1
    assert workflow.agent.implementations == 1 and not workflow.agent.diagnoses


@pytest.mark.parametrize("cut", ["before", "inside", "after"])
async def test_conversion_interrupt_keeps_whole_old_or_new_state(tmp_path, monkeypatch, cut):
    from auto_agents_2.resolution_diagnosis import PREFIX

    workflow, rid, _, _, _, boundary = await failed_command(tmp_path)
    q = await old_wait(workflow, rid, boundary, monkeypatch)
    original = workflow.store.get(rid, "environment/prerequisite-wait")
    method = workflow.store.admit_resolution_diagnosis
    put = workflow.store._put
    with monkeypatch.context() as patch:
        if cut == "inside":

            def fail_put(db, run, key, value):
                if key == "recovery/active":
                    raise asyncio.CancelledError()
                return put(db, run, key, value)

            patch.setattr(workflow.store, "_put", fail_put)
        else:

            def fail(*args):
                if cut == "after":
                    method(*args)
                raise asyncio.CancelledError()

            patch.setattr(workflow.store, "admit_resolution_diagnosis", fail)
        with pytest.raises(asyncio.CancelledError):
            await workflow._resume_environment_waits(rid)
    converted = cut == "after"
    assert bool(workflow.store.get(rid, PREFIX + original["identity"])) == converted
    assert bool(workflow.store.get(rid, "recovery/active")) == converted
    assert bool(workflow.store.question(q["id"])["superseded"]) == converted
    assert workflow.store.question(q["id"])["answer"] is None
    assert workflow.store.get(rid, "develop/task/T1")["phase"] == ("rejected" if converted else "applied")
    await workflow._resume_environment_waits(rid)
    active = workflow.store.get(rid, "recovery/active")
    assert workflow.store.get(rid, "recovery/count/" + active["failure"]["id"]) == 1
    assert workflow.agent.implementations == 1


@pytest.mark.parametrize("change", ["answer", "plan", "candidate", "queue", "count", "active"])
async def test_conversion_cas_preserves_concurrent_change(tmp_path, monkeypatch, change):
    from auto_agents_2.faults import StateConflict

    workflow, rid, _, _, _, boundary = await failed_command(tmp_path)
    q = await old_wait(workflow, rid, boundary, monkeypatch)
    original = workflow.store.admit_resolution_diagnosis

    def compete(run, receipt, record, guards, updates, keys, questions):
        if change == "answer":
            workflow.store.answer(q["id"], "n")
        else:
            key = {
                "plan": "plan",
                "candidate": "develop/task/T1",
                "queue": "environment/prerequisite-queue",
                "active": "recovery/active",
                "count": "recovery/count/" + record["failure"]["id"],
            }[change]
            workflow.store.checkpoint(rid, key, 9 if change == "count" else {"concurrent": change})
        return original(run, receipt, record, guards, updates, keys, questions)

    monkeypatch.setattr(workflow.store, "admit_resolution_diagnosis", compete)
    with pytest.raises(StateConflict):
        await workflow._resume_environment_waits(rid)
    assert workflow.store.question(q["id"])["superseded"] == 0
    assert workflow.store.get(rid, "environment/prerequisite-wait")


@pytest.mark.parametrize("answer", ["y", "n"])
async def test_old_human_answer_is_not_reinterpreted_as_diagnosis(tmp_path, monkeypatch, answer):
    from auto_agents_2.resolution_diagnosis import admit

    workflow, rid, _, _, _, boundary = await failed_command(tmp_path)
    q = await old_wait(workflow, rid, boundary, monkeypatch)
    workflow.store.answer(q["id"], answer)
    assert not admit(workflow, rid, pending_observation(workflow, rid))
    assert workflow.store.question(q["id"])["answer"] == answer
    assert not workflow.store.question(q["id"])["superseded"]


@pytest.mark.parametrize(
    "fault",
    [
        "plan",
        "task_revision",
        "source",
        "base",
        "wrong_cwd",
        "arbitrary_command",
        "missing_log",
        "corrupt_log",
        "oversized_log",
        "incomplete",
        "runtime",
        "missing_observation",
        "outside_runtime",
        "outside_workspace",
        "wrong_kind",
        "manual_requirement",
        "out_of_scope",
        "extra_node_root",
    ],
)
async def test_invalid_or_ambiguous_evidence_stays_outside_admission(tmp_path, fault):
    from pathlib import Path

    from auto_agents_2.resolution_diagnosis import eligible

    workflow, rid, proposal, _, _, _ = await failed_command(tmp_path)
    pending = pending_observation(workflow, rid)
    assert eligible(workflow, rid, pending)
    ev = pending["requirement"]["evidence"]
    if fault == "plan":
        workflow.store.checkpoint(
            rid, "plan", proposal.model_copy(update={"summary": "changed"}).model_dump()
        )
    elif fault == "task_revision":
        workflow.store.checkpoint(rid, "task_state/T1", {"revision": 2})
    elif fault in {"source", "extra_node_root"}:
        work = Path(ev["controller_task"]["workspace"])
        if fault == "source":
            (work / "package.json").write_text('{"name":"changed"}')
        else:
            (work / "frontend").mkdir()
            (work / "frontend/package.json").write_text('{"name":"second-root"}')
            from auto_agents_2.workspace import tree_hash

            saved = workflow.store.get(rid, "develop/task/T1")
            saved["source_hash"] = tree_hash(work)
            workflow.store.checkpoint(rid, "develop/task/T1", saved)
            pending = replace_observation(
                workflow,
                rid,
                pending,
                lambda p: p["requirement"]["evidence"]["controller_task"].update(
                    source_hash=saved["source_hash"]
                ),
            )
    elif fault in {"missing_log", "corrupt_log"}:
        sha = ev["logs"][1]["hash"]
        path = workflow.store.root / "artifacts" / sha[:2] / sha
        if fault == "missing_log":
            path.unlink()
        else:
            path.write_text("corrupted")
    elif fault == "missing_observation":
        workflow.store.checkpoint(rid, "environment/prerequisite-observations/" + pending["identity"], None)
    else:

        def mutate(p):
            e = p["requirement"]["evidence"]
            if fault == "base":
                e["controller_task"]["base"] = "0" * 40
            elif fault == "wrong_cwd":
                e["cwd"] = str(tmp_path)
            elif fault == "arbitrary_command":
                e["argv"][1:] = ["run", "arbitrary"]
            elif fault == "oversized_log":
                e["logs"][1]["bytes"] = 20 * 1024 * 1024
            elif fault == "incomplete":
                e["capture_status"] = "incomplete"
            elif fault == "runtime":
                e["runtime_identity"] = "0" * 64
            elif fault == "outside_runtime":
                e["argv"][0] = "/usr/bin/npm"
            elif fault == "outside_workspace":
                e["controller_task"]["workspace"] = "/tmp"
            elif fault == "wrong_kind":
                e["logs"][1]["kind"] = "document"
            elif fault == "manual_requirement":
                p["requirement"]["kind"] = "authorization"
            elif fault == "out_of_scope":
                from auto_agents_2.util import digest

                changed = proposal.model_copy(deep=True)
                changed.tasks[0].allowed_paths = ["calc.py", "tests"]
                workflow.store.checkpoint(rid, "plan", changed.model_dump())
                e["controller_task"].update(
                    plan_hash=digest(changed.model_dump()), task_hash=digest(changed.tasks[0].model_dump())
                )

        pending = replace_observation(workflow, rid, pending, mutate)
    assert eligible(workflow, rid, pending) is None
    assert workflow.store.get(rid, "recovery/active") is None


async def test_forced_write_or_retry_suggestion_only_reserves_diagnosis(tmp_path):
    from pathlib import Path

    from auto_agents_2.recovery import FailureRecord, RecoveryDecision

    workflow, rid, proposal, _, _, boundary = await failed_command(tmp_path)
    await workflow._wait_prerequisite(rid, boundary)
    active = workflow.store.get(rid, "recovery/active")
    failure = FailureRecord.model_validate(active["failure"])
    work = Path(workflow.store.get(rid, "develop/task/T1")["workspace"])
    suggested = RecoveryDecision(
        failure_id=failure.id,
        action="repair",
        task_ids=["T1"],
        evidence_refs=["failure.evidence"],
        reason="not an authorization",
        budget_owner="T1",
    )
    choice, key = await workflow.recovery_policy.authorize(
        rid,
        failure,
        active,
        work,
        proposal,
        suggested,
        operation="choose/1",
        established=True,
        candidates={"repair": suggested},
    )
    assert choice.action == "diagnose"
    assert workflow.store.get(rid, key)["action"]["action"] == "diagnose"
    assert workflow.agent.implementations == 1


@pytest.mark.parametrize("fault", ["missing", "stale_plan", "invalid", "tampered_result", "wrong_cycle"])
async def test_dispatched_action_rejects_missing_or_damaged_diagnosis_proof(tmp_path, fault):
    from auto_agents_2.faults import StateConflict

    workflow, rid, _, _, _, boundary = await failed_command(tmp_path)
    await workflow._wait_prerequisite(rid, boundary)
    await workflow.recovery.resume(rid)
    permit_key = workflow.store.get(rid, "develop/task/T1")["recovery_permit"]
    permit = workflow.store.get(rid, permit_key)
    proof = permit["resolution_diagnosis"]
    diagnosis_key = proof["diagnosis_permit"]
    # Fault injection is deliberately below the production immutable-record guard.
    with workflow.store.connect() as db:
        if fault == "missing":
            db.execute("DELETE FROM checkpoints WHERE run_id=? AND key=?", (rid, diagnosis_key))
        else:
            raw = workflow.store.get(rid, diagnosis_key)
            if fault == "stale_plan":
                raw["plan_hash"] = "0" * 64
            elif fault == "invalid":
                raw["outcome"]["status"] = "invalid"
            elif fault == "tampered_result":
                raw["outcome"]["diagnosis"]["action"] = "recheck"
            elif fault == "wrong_cycle":
                raw["cycle_id"] = "another-cycle"
            db.execute(
                "UPDATE checkpoints SET data=? WHERE run_id=? AND key=?",
                (json.dumps(raw), rid, diagnosis_key),
            )
    with pytest.raises(StateConflict):
        workflow.recovery_policy.validate_consumption(rid, permit_key)


async def test_invalid_diagnosis_retries_once_without_gaining_write_permission(tmp_path):
    from auto_agents_2.recovery import Diagnosis

    workflow, rid, _, _, _, boundary = await failed_command(tmp_path)
    original = workflow.agent.invoke
    bad = True

    async def invoke(stage, context, schema, *args, **kwargs):
        nonlocal bad
        if schema is Diagnosis and bad:
            bad = False
            return Diagnosis(
                action="repair",
                task_ids=["T1"],
                evidence_refs=["not-an-observation"],
                reason="invalid reference",
            )
        return await original(stage, context, schema, *args, **kwargs)

    workflow.agent.invoke = invoke
    await workflow._wait_prerequisite(rid, boundary)
    await workflow.recovery.resume(rid)
    first = workflow.store.get(rid, "recovery/active")
    old_key = first["diagnosis_permit"]
    assert workflow.store.get(rid, old_key)["outcome"]["status"] == "invalid"
    assert not workflow.store.get(rid, "develop/task/T1").get("recovery_permit")
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "recovery/active") is None
    assert workflow.store.get(rid, old_key)["outcome"]["status"] == "invalid"
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/T1")["round"] == 2


async def test_consumed_old_pause_answer_remains_human_boundary(tmp_path, monkeypatch):
    from auto_agents_2 import resolution_diagnosis

    workflow, rid, _, _, _, boundary = await failed_command(tmp_path)

    async def answer_pause(question):
        return "n"

    workflow.question_handler = answer_pause
    with monkeypatch.context() as patch:
        patch.setattr(resolution_diagnosis, "admit", lambda *args: False)
        with pytest.raises(asyncio.CancelledError):
            await workflow._wait_prerequisite(rid, boundary)
    pending = pending_observation(workflow, rid)
    assert not workflow.store.pending(rid)
    assert not resolution_diagnosis.admit(workflow, rid, pending)
    calls = []

    async def wait_again(question):
        calls.append(question)
        raise asyncio.CancelledError()

    workflow.question_handler = wait_again
    with pytest.raises(asyncio.CancelledError):
        await workflow._resume_environment_waits(rid)
    assert len(calls) == 1 and not workflow.store.get(rid, "recovery/active")
    assert workflow.agent.implementations == 1


@pytest.mark.parametrize("answer", [None, "n"])
async def test_active_recovery_cannot_skip_its_external_prerequisite(tmp_path, monkeypatch, answer):
    workflow, rid, _, _, _, boundary = await failed_command(tmp_path, "npm error code EACCES")
    q = await old_wait(workflow, rid, boundary, monkeypatch)
    active = {"existing_recovery": "preview or plan recheck awaiting access prerequisite"}
    workflow.store.checkpoint(rid, "recovery/active", active)
    if answer:
        workflow.store.answer(q["id"], answer)

    async def stop(question):
        raise asyncio.CancelledError()

    workflow.question_handler = stop
    with pytest.raises(asyncio.CancelledError):
        await workflow._resume_environment_waits(rid)
    assert workflow.store.get(rid, "recovery/active") == active
    assert workflow.store.get(rid, "environment/prerequisite-wait")
    assert workflow.agent.implementations == 1 and not workflow.agent.diagnoses


@pytest.mark.parametrize("generated", [False, True])
async def test_ci_admission_requires_preexisting_lock_source_identity(tmp_path, generated):
    from pathlib import Path

    from auto_agents_2.resolution_diagnosis import eligible
    from auto_agents_2.workspace import tree_hash

    workflow, rid, proposal, base, context = configured_case(tmp_path)
    runtime = await workflow.environments.ensure(None)
    binary = runtime.prefix / "bin/npm"
    if generated:
        body = binary.read_text().replace(
            "if json.loads(Path('package.json').read_text())['version'] == '1.0.0':",
            "if '--package-lock-only' not in sys.argv:",
        )
        binary.write_text(body)
    else:
        original = workflow.agent.invoke

        async def invoke(stage, *args, **kwargs):
            result = await original(stage, *args, **kwargs)
            if stage == "implement":
                result.changes.append(
                    Change(path="package-lock.json", content='{"lockfileVersion":3,"packages":{}}')
                )
            return result

        workflow.agent.invoke = invoke
    ready = {"fingerprint": "fixture-node", "integrity": EnvironmentManager.integrity(runtime.prefix)}
    (runtime.prefix / "aa2-ready.json").write_text(json.dumps(ready))
    runtime = Environment(runtime.prefix, EnvironmentManager.installed_identity(runtime.prefix))

    async def ensure(*args):
        return runtime

    workflow.environments.ensure = ensure
    with pytest.raises(HumanIntervention):
        await workflow._observed_task(rid, proposal.tasks[0], base, proposal, context)
    pending = pending_observation(workflow, rid)
    assert pending["requirement"]["evidence"]["argv"][1:] == ["ci"]
    saved = workflow.store.get(rid, "develop/task/T1")
    if generated:
        assert saved["source_hash"] != tree_hash(Path(saved["workspace"]))
        assert eligible(workflow, rid, pending) is None
    else:
        assert eligible(workflow, rid, pending)


async def test_unknown_failure_closes_previous_repair_and_does_not_replay_it(tmp_path):
    workflow, rid, proposal, base, context, boundary = await failed_command(tmp_path)
    await workflow._wait_prerequisite(rid, boundary)
    await workflow.recovery.resume(rid)
    key = workflow.store.get(rid, "develop/task/T1")["recovery_permit"]
    original = workflow.agent.invoke

    async def invoke(stage, *args, **kwargs):
        result = await original(stage, *args, **kwargs)
        if stage == "implement":
            result.changes[0].content = '{"name":"fixture","version":"1.0.0","description":"new candidate"}'
        return result

    workflow.agent.invoke = invoke
    with pytest.raises(HumanIntervention) as failure:
        await workflow._observed_task(rid, proposal.tasks[0], base, proposal, context)
    assert workflow.store.get(rid, key)["phase"] == "started"
    await workflow._wait_prerequisite(rid, failure.value)
    previous = workflow.store.get(rid, key)
    assert previous["phase"] == "completed"
    assert previous["outcome"]["tasks"]["T1"]["status"] == "failed"
    active = workflow.store.get(rid, "recovery/active")
    assert active["attempt"] == 2
    assert workflow.store.get(rid, "rounds/T1")["round"] == 2
    await workflow._resume_environment_waits(rid)
    assert workflow.store.get(rid, key) == previous
    assert workflow.store.get(rid, "recovery/active") == active
    assert workflow.agent.implementations == 2


async def test_unknown_diagnosis_can_activate_plan_patch_and_authorized_followup(tmp_path):
    from evidence_test_fixtures import empty_contract

    from auto_agents_2.models import PlanResult
    from auto_agents_2.plan_revision import PlanPatch
    from auto_agents_2.recovery import Diagnosis
    from auto_agents_2.review_authority import VERSION

    workflow, rid, proposal, _, _, boundary = await failed_command(tmp_path)
    workflow.store.checkpoint(rid, "authority/protocol", VERSION)
    from test_recovery_policy import seed_requirements

    from auto_agents_2.workspace import git

    seed_requirements(workflow, rid, proposal)
    base = git(Path(workflow.store.get(rid, "develop/task/T1")["workspace"]), "rev-parse", "HEAD")
    workflow.workspaces.allocate(rid, "integration", base)
    original = workflow.agent.invoke

    async def invoke(stage, context, schema, *args, **kwargs):
        if schema is Diagnosis:
            return Diagnosis(
                action="plan_patch",
                task_ids=["T1"],
                evidence_refs=["failure.evidence"],
                reason="Clarify the owned dependency preparation without deleting the original checks",
            )
        if schema is PlanPatch:
            revised = PlanResult.model_validate(context["plan"]).tasks[0].model_copy(deep=True)
            revised.description += "; retain all checks and clarify existing dependency setup"
            return PlanPatch(
                base_revision=context["base_revision"],
                failure_ids=[context["failure"]["id"]],
                reason="Clarify existing dependency setup",
                document="The same task and obligations; dependency preparation clarified.",
                upsert_tasks=[revised],
                evidence_contract=empty_contract(context),
            )
        return await original(stage, context, schema, *args, **kwargs)

    workflow.agent.invoke = invoke
    await workflow._wait_prerequisite(rid, boundary)
    active = workflow.store.get(rid, "recovery/active")
    await workflow.recovery.resume(rid)
    journal = workflow.store.get(rid, f"plan_revision/journal/{active['failure']['id']}/{active['attempt']}")
    assert journal["phase"] == "activated"
    assert workflow.store.get(rid, "recovery/active") is None
    saved = workflow.store.get(rid, "develop/task/T1")
    continuation = workflow.recovery_policy.validate_consumption(
        rid, saved["recovery_permit"], expected={"repair"}
    )
    assert continuation["resolution_diagnosis"]["patch_permit"]
    updated = PlanResult.model_validate(workflow.store.get(rid, "plan"))
    assert updated.tasks[0].checks == proposal.tasks[0].checks
    assert updated.tasks[0].allowed_paths == proposal.tasks[0].allowed_paths


async def test_actual_diagnosis_no_progress_limit_is_not_increased(tmp_path):
    from auto_agents_2.decisions import DecisionResult
    from auto_agents_2.errors import RecoveryPathExhausted
    from auto_agents_2.recovery import Diagnosis

    workflow, rid, proposal, base, context, boundary = await failed_command(tmp_path)
    original = workflow.agent.invoke
    diagnoses, options = [], []

    async def invoke(stage, context, schema, *args, **kwargs):
        if schema is Diagnosis:
            diagnoses.append(context)
            return Diagnosis(
                action="recheck", task_ids=["T1"], evidence_refs=["failure.evidence"], reason="same facts"
            )
        if stage == "decision":
            choices = context["decision_request"]["scope"]["candidates"]
            options.append(set(choices))
            if "diagnose" in choices:
                return DecisionResult(
                    action="approve",
                    selected_action="diagnose",
                    evidence_refs=["failure.evidence"],
                    reason="Investigate with no new observation",
                    expected_result="read-only diagnosis",
                )
            return DecisionResult(action="ask_user", reason="No new diagnosis evidence")
        return await original(stage, context, schema, *args, **kwargs)

    async def pause(question):
        raise asyncio.CancelledError()

    workflow.agent.invoke = invoke
    workflow.question_handler = pause
    workflow.store.checkpoint(rid, "recovery/budgets/diagnosis/T1", {"round": 10})
    await workflow._wait_prerequisite(rid, boundary)
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "recovery/active") is None
    # The first legitimate recheck is executed, then its second unchanged
    # installation observation enters the existing repeated-recheck decision.
    with pytest.raises(HumanIntervention) as second:
        await workflow._observed_task(rid, proposal.tasks[0], base, proposal, context)
    await workflow._wait_prerequisite(rid, second.value)
    with pytest.raises(RecoveryPathExhausted):
        await workflow.recovery.resume(rid)
    assert len(diagnoses) == 2 and "diagnose" not in options[-1]
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/T1")["round"] == 12
    assert workflow.agent.implementations == 1
    prior = workflow.store.get(rid, "develop/task/T1")["recovery_permit"]
    assert workflow.store.get(rid, prior)["phase"] == "completed"


async def test_start_consumes_both_unknown_waits_before_development_resumes(tmp_path):
    workflow, rid, proposal, base, context = configured_case(tmp_path)
    second = proposal.tasks[0].model_copy(deep=True, update={"id": "T2", "origin_id": "T2"})
    proposal.tasks.append(second)
    workflow.store.checkpoints(rid, {"plan": proposal.model_dump(), "task_origins": {"T1": "T1", "T2": "T2"}})
    results = await asyncio.gather(
        *(workflow._observed_task(rid, t, base, proposal, context) for t in proposal.tasks),
        return_exceptions=True,
    )
    assert all(isinstance(r, HumanIntervention) for r in results)
    assert len(workflow._environment_waits(rid)) == 2
    rounds = {task.id: workflow.store.get(rid, "rounds/" + task.id) for task in proposal.tasks}
    entered = []

    async def execute(run):
        assert not workflow._environment_waits(run) and not workflow.store.get(run, "recovery/active")
        assert len(workflow.agent.diagnoses) == 2
        for task in proposal.tasks:
            saved = workflow.store.get(run, "develop/task/" + task.id)
            workflow.recovery_policy.validate_consumption(run, saved["recovery_permit"], expected={"repair"})
            assert workflow.store.get(run, "rounds/" + task.id) == rounds[task.id]
        entered.append(run)
        return workflow.store.run(run)  # Boundary harness stops before any retry, not an acceptance verdict.

    workflow._execute = execute
    await workflow.start(run_id=rid, allow_resume=True)
    assert entered == [rid]
    assert workflow.agent.implementations == 2
    assert not workflow.store.pending(rid)


@pytest.mark.parametrize("damage", ["missing", "completed", "wrong_scope", "wrong_plan"])
async def test_new_wait_cannot_consume_invalid_previous_repair(tmp_path, damage):
    from auto_agents_2.faults import StateConflict

    workflow, rid, proposal, base, context, boundary = await failed_command(tmp_path)
    await workflow._wait_prerequisite(rid, boundary)
    await workflow.recovery.resume(rid)
    key = workflow.store.get(rid, "develop/task/T1")["recovery_permit"]
    original = workflow.agent.invoke

    async def invoke(stage, *args, **kwargs):
        result = await original(stage, *args, **kwargs)
        if stage == "implement":
            result.changes[0].content = '{"name":"fixture","version":"1.0.0","description":"new candidate"}'
        return result

    workflow.agent.invoke = invoke
    with pytest.raises(HumanIntervention) as failure:
        await workflow._observed_task(rid, proposal.tasks[0], base, proposal, context)
    value = workflow.store.get(rid, key)
    if damage == "completed":
        value["phase"] = "completed"
    elif damage == "wrong_scope":
        value["task_scopes"]["T1"] = ["outside.py"]
    elif damage == "wrong_plan":
        value["plan_hash"] = "0" * 64
    # Deliberate durable-state damage, never used to authorize a production run.
    with workflow.store.connect() as db:
        if damage == "missing":
            db.execute("DELETE FROM checkpoints WHERE run_id=? AND key=?", (rid, key))
        else:
            db.execute(
                "UPDATE checkpoints SET data=? WHERE run_id=? AND key=?", (json.dumps(value), rid, key)
            )
    with pytest.raises(StateConflict):
        await workflow._wait_prerequisite(rid, failure.value)
    assert not workflow.store.get(rid, "recovery/active")
    assert len(workflow._environment_waits(rid)) == 1


async def test_invalid_patch_retains_admission_and_can_be_corrected(tmp_path):
    from evidence_test_fixtures import empty_contract
    from test_recovery_policy import seed_requirements

    from auto_agents_2.decisions import DecisionResult
    from auto_agents_2.plan_revision import PlanPatch
    from auto_agents_2.recovery import Diagnosis
    from auto_agents_2.workspace import git

    workflow, rid, proposal, _, _, boundary = await failed_command(tmp_path)
    seed_requirements(workflow, rid, proposal)
    work = Path(workflow.store.get(rid, "develop/task/T1")["workspace"])
    workflow.workspaces.allocate(rid, "integration", git(work, "rev-parse", "HEAD"))
    original = workflow.agent.invoke
    bad_patch = True

    async def invoke(stage, context, schema, *args, **kwargs):
        nonlocal bad_patch
        if stage == "decision":
            choices = context["decision_request"]["scope"]["candidates"]
            return DecisionResult(
                action="approve",
                selected_action="diagnose" if "diagnose" in choices else "repair",
                evidence_refs=["failure.evidence"],
                reason="Use the existing bounded correction",
                expected_result="new valid diagnosis",
            )
        if schema is Diagnosis and bad_patch:
            return Diagnosis(
                action="plan_patch", task_ids=["T1"], evidence_refs=["failure.evidence"], reason="try patch"
            )
        if schema is PlanPatch:
            bad_patch = False
            return PlanPatch(
                base_revision=context["base_revision"],
                failure_ids=[context["failure"]["id"]],
                reason="invalid fixture proposal",
                document="invalid origin",
                upsert_tasks=[proposal.tasks[0].model_copy(update={"origin_id": "invented"})],
                evidence_contract=empty_contract(context),
            )
        return await original(stage, context, schema, *args, **kwargs)

    workflow.agent.invoke = invoke
    await workflow._wait_prerequisite(rid, boundary)
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "recovery/active")["attempt"] == 2
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, "recovery/active") is None
    assert workflow.store.get(rid, "develop/task/T1")["recovery_permit"]


@pytest.mark.parametrize("shape", ["debug", "streams", "reference", "evidence", "requirement"])
async def test_malformed_capture_objects_never_become_internal_errors(tmp_path, shape):
    from auto_agents_2.resolution_diagnosis import eligible

    workflow, rid, _, _, _, _ = await failed_command(tmp_path)

    def mutate(pending):
        if shape == "requirement":
            pending["requirement"] = []
        elif shape == "evidence":
            pending["requirement"]["evidence"] = []
        elif shape == "debug":
            pending["requirement"]["evidence"]["npm_debug"] = "invalid"
        elif shape == "streams":
            pending["requirement"]["evidence"]["logs"] = "xx"
        else:
            pending["requirement"]["evidence"]["logs"] = [None, {}]

    value = replace_observation(workflow, rid, pending_observation(workflow, rid), mutate)
    assert eligible(workflow, rid, value) is None
    assert workflow.store.get(rid, "recovery/active") is None
