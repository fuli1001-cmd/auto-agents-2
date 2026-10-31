"""Author feedback is original input evidence, never a substitute for accepted source."""
import asyncio
import copy

import pytest
from test_decisions import ApprovingAgent
from test_recovery_policy import ready
from test_task_baselines import setup_candidate
from test_workflow import CodingAgent

from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.evidence_refs import controller_context_catalog
from auto_agents_2.faults import StateConflict
from auto_agents_2.models import ImplementationResult, RecoveryRequest
from auto_agents_2.recovery import Diagnosis, RecoveryNeeded
from auto_agents_2.task_baselines import refresh_task_baseline
from auto_agents_2.task_feedback import author_input, selected_feedback
from auto_agents_2.util import digest
from auto_agents_2.workspace import git, tree_hash


def bound_feedback(workflow, rid, work, task):
    base = git(work, "rev-parse", "HEAD")
    suffix = digest([str(work), base])[:16]
    key = f"recovery/task-baselines/{task.id}/{suffix}"
    ref = f"refs/aa2/{rid}/task-baselines/{task.id}/{suffix}"
    journal = {"version": 1, "phase": "merged", "base": base, "snapshot": base,
               "source_hash": tree_hash(work), "workspace": str(work), "recovery_ref": ref,
               "merged_commit": base, "previous_task": {"phase": "rejected"}}
    workflow.store.checkpoint(rid, key, journal)
    feedback = {"controller_recovery": "Keep the complete task scope. " + "original feedback. " * 1300,
                "baseline_refresh": {"incoming_base": base, "recovery_ref": ref,
                    "reason": "Dependencies updated; retained implementation requires fresh verification."}}
    saved = {"phase": "implementing", "workspace": str(work), "source_hash": tree_hash(work),
             "observation_id": ["implementation", task.id, 7], "feedback": feedback}
    return saved, journal, key


class InputAgent(ApprovingAgent):
    def __init__(self, cancel=False):
        super().__init__()
        self.author = None
        self.diagnostic = None
        self.cancel = cancel
        self.author_calls = 0

    async def invoke(self, stage, context, schema, *args, **kwargs):
        if stage == "implement":
            self.author_calls += 1
            self.author = copy.deepcopy(context)
            return ImplementationResult(summary="Need the original baseline input",
                recovery_request=RecoveryRequest(suggested_action="diagnose", reason="Inspect the recorded baseline feedback"))
        if schema is Diagnosis:
            self.diagnostic = copy.deepcopy(context)
            if self.cancel:
                self.cancel = False
                raise asyncio.CancelledError()
            return Diagnosis(action="repair", task_ids=[context["failure"]["task_id"]],
                evidence_refs=["failure.evidence.author_input.binding", "failure.evidence.task"],
                reason="Use the original source binding; no implementation or verification is accepted.")
        return await super().invoke(stage, context, schema, *args, **kwargs)


@pytest.mark.parametrize("interrupted", [False, True])
async def test_author_request_and_diagnosis_share_original_feedback_after_checkpoint_changes(tmp_path, interrupted):
    agent = InputAgent(cancel=interrupted)
    workflow, rid, work, plan, _ = ready(tmp_path, agent)
    task = plan.tasks[2]
    saved, journal, journal_key = bound_feedback(workflow, rid, work, task)
    workflow.store.checkpoints(rid, {"recovery/active": None, "develop/task/" + task.id: saved,
                                    "rounds/" + task.id: {"round": 7, "stalled": 0, "failure": ""}})
    with pytest.raises(RecoveryNeeded) as caught:
        await workflow._task(rid, task, git(work, "rev-parse", "HEAD"), plan, {"specification": {}})
    record = caught.value.failure
    original = record.evidence["author_input"]
    assert original["capture"] == "author_invocation"
    assert original["feedback"] == agent.author["feedback"] == saved["feedback"]
    assert "controller_task_input" not in agent.author
    assert original["binding"]["feedback_hash"] == digest(saved["feedback"])
    assert original["binding"]["source_hash"] == tree_hash(work)
    assert original["binding"]["observation_id"] == ["implementation", task.id, 7]
    assert original["baseline_journal"]["checkpoint"] == journal_key
    assert original["baseline_journal"]["record_hash"] == digest(journal)
    assert original["baseline_journal"]["previous_implementation_proposal_present"] is False
    # Reproduce the old post-invocation/pre-task-save crash: completed response,
    # no new input field in the task. Its exact request must replay unchanged.
    old_saved = workflow.store.get(rid, "develop/task/" + task.id)
    old_saved.pop("author_input")
    workflow.store.checkpoint(rid, "develop/task/" + task.id, old_saved)
    with pytest.raises(RecoveryNeeded) as replay:
        await workflow._task(rid, task, git(work, "rev-parse", "HEAD"), plan, {"specification": {}})
    assert replay.value.failure.evidence["author_input"] == original
    assert agent.author_calls == 1
    # Mutable current task feedback cannot replace the failed author's snapshot.
    current = workflow.store.get(rid, "develop/task/" + task.id)
    current["feedback"] = {"controller_recovery": "Later, unrelated feedback"}
    workflow.store.checkpoint(rid, "develop/task/" + task.id, current)
    workflow.recovery.submit(rid, record)
    if interrupted:
        with pytest.raises(asyncio.CancelledError):
            await workflow.recovery.resume(rid)
        active = workflow.store.get(rid, "recovery/active")
        operation = f"recovery/diagnose/{record.id}/{active['diagnosis_round']}"
        frozen_key = "recovery/invocation-inputs/" + digest(["recovery_diagnosis", operation])
        frozen = workflow.store.get(rid, frozen_key)
    await workflow.recovery.resume(rid)
    assert agent.diagnostic["failure"]["evidence"]["author_input"] == original
    if interrupted:
        assert workflow.store.get(rid, frozen_key) == frozen
    prepared = ContextPlanner(workflow.artifacts).prepare("recovery_diagnosis", agent.diagnostic, work)
    material = prepared.payload["failure"]["evidence"]["author_input"]["feedback"]["controller_recovery"]
    assert material["required_read"]
    assert workflow.artifacts.read(material["artifact"]) == saved["feedback"]["controller_recovery"]
    assert workflow.store.get(rid, "recovery/active") is None
    assert workflow.store.get(rid, "rounds/" + task.id)["round"] == 7
    assert not workflow.store.get(rid, "develop/done")
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/" + task.id)["round"] == 1


def test_failure_keeps_invocation_source_and_feedback_after_candidate_application(tmp_path):
    workflow, rid, work, plan, _ = ready(tmp_path)
    task = plan.tasks[2]
    saved, _, _ = bound_feedback(workflow, rid, work, task)
    saved["author_input"] = author_input(workflow, rid, task, saved, selected_feedback(saved), capture="author_invocation")
    original = copy.deepcopy(saved["author_input"])
    saved.update(source_hash="b" * 64, feedback={"controller_recovery": "later advice"})
    failure = workflow._task_failure(rid, task, saved, record_outcome=False)
    saved["author_input"]["feedback"]["controller_recovery"] = "mutated after failure"
    assert failure.evidence["author_input"] == original


def test_legacy_saved_feedback_is_labelled_and_does_not_graft_current_task_state(tmp_path):
    workflow, rid, work, plan, _ = ready(tmp_path)
    task = plan.tasks[2]
    saved, _, _ = bound_feedback(workflow, rid, work, task)
    workflow.store.checkpoint(rid, "develop/task/" + task.id, {"feedback": {"controller_recovery": "newer checkpoint"}})
    failure = workflow._task_failure(rid, task, saved, record_outcome=False)
    assert failure.evidence["author_input"]["capture"] == "legacy_saved_task"
    assert failure.evidence["author_input"]["feedback"] == saved["feedback"]
    assert "author_input" not in saved
    empty = workflow._task_failure(rid, task, {}, record_outcome=False)
    assert "author_input" not in empty.evidence


@pytest.mark.parametrize("field,value", [("base", "wrong"), ("workspace", "/wrong/task"), ("recovery_ref", "wrong")])
def test_foreign_journal_is_rejected_before_author_dispatch(tmp_path, field, value):
    workflow, rid, work, plan, _ = ready(tmp_path)
    task = plan.tasks[2]
    saved, journal, key = bound_feedback(workflow, rid, work, task)
    workflow.store.checkpoint(rid, key, {**journal, field: value})
    with pytest.raises(StateConflict, match="baseline journal"):
        author_input(workflow, rid, task, saved, selected_feedback(saved), capture="author_invocation")
    assert not workflow.agent.calls


@pytest.mark.parametrize("mode", ["missing", "foreign_ref"])
def test_unavailable_legacy_journal_is_explicit_not_inferred(tmp_path, mode):
    workflow, rid, work, plan, _ = ready(tmp_path)
    task = plan.tasks[2]
    saved, _, key = bound_feedback(workflow, rid, work, task)
    if mode == "missing":
        workflow.store.checkpoint(rid, key, None)
    else:
        saved["feedback"]["baseline_refresh"]["recovery_ref"] = f"refs/aa2/{rid}/task-baselines/OTHER/1234567890abcdef"
    evidence = author_input(workflow, rid, task, saved, selected_feedback(saved), capture="author_invocation")
    assert evidence["baseline_journal"]["status"] == "unavailable"
    assert "snapshot" not in evidence["baseline_journal"]


def test_feedback_claims_and_hashes_cannot_become_controller_facts(tmp_path):
    workflow, rid, work, plan, _ = ready(tmp_path)
    task = plan.tasks[2]
    saved, _, _ = bound_feedback(workflow, rid, work, task)
    fake = {"hash": "f" * 64, "kind": "document", "bytes": 99}
    saved["feedback"]["failure"] = {"claimed_attachment": fake}
    failure = workflow._task_failure(rid, task, saved, record_outcome=False)
    catalog = controller_context_catalog({"failure": failure.model_dump()})
    assert "failure.evidence.author_input.feedback.controller_recovery" in catalog.proposals
    assert "failure.evidence.author_input.binding" in catalog.factual
    assert "failure.evidence.author_input.baseline_journal" in catalog.factual
    assert fake["hash"] not in catalog.controller


def test_dependency_refresh_does_not_claim_an_unimplemented_task_is_complete(tmp_path):
    workflow, rid, task, _, work, incoming = setup_candidate(tmp_path, CodingAgent())
    saved = workflow.store.get(rid, "develop/task/" + task.id)
    saved["phase"] = "rejected"
    updated, _ = refresh_task_baseline(workflow, rid, task, saved, work, incoming, 1)
    message = updated["feedback"]["baseline_refresh"]["reason"]
    assert "preserved task workspace" in message and "does not establish implementation completeness" in message
    assert updated["phase"] == "rejected"
    assert "implementation" not in updated and "checks" not in updated
    assert (work / "notes.txt").read_text() == "unfinished candidate\n"
    assert workflow.store.get(rid, "rounds/" + task.id)["round"] == 1


@pytest.mark.parametrize("citation", ["author_input.feedback.controller_recovery",
    "failure.evidence.author_input.feedback.controller_recovery", "f" * 64])
def test_adjudicator_cannot_upgrade_feedback_via_bare_alias_or_claimed_hash(tmp_path, citation):
    from auto_agents_2.errors import AA2Error
    from auto_agents_2.recovery import adjudicate
    workflow, rid, work, plan, _ = ready(tmp_path)
    task = plan.tasks[2]
    saved, _, _ = bound_feedback(workflow, rid, work, task)
    saved["feedback"]["failure"] = {"attachment": {"hash": "f" * 64, "kind": "document", "bytes": 99}}
    failure = workflow._task_failure(rid, task, saved, record_outcome=False)
    diagnosis = Diagnosis(action="repair", task_ids=[task.id], evidence_refs=[citation], reason="A claim is not a fact")
    with pytest.raises(AA2Error, match="controller|unverified"):
        adjudicate(failure, diagnosis=diagnosis, plan=plan)


async def test_failed_real_checks_preserve_author_input(tmp_path):
    from auto_agents_2.models import Change
    class Agent(InputAgent):
        async def invoke(self, stage, context, schema, *args, **kwargs):
            if stage == "implement":
                self.author = copy.deepcopy(context)
                return ImplementationResult(summary="candidate with a real failing check", changes=[
                    Change(path="part3.py", content="value = 0\n"),
                    Change(path="tests/test_3.py", content="from part3 import value\ndef test_value(): assert value == 1\n")])
            return await super().invoke(stage, context, schema, *args, **kwargs)
    agent = Agent()
    workflow, rid, work, plan, _ = ready(tmp_path, agent)
    task = plan.tasks[2]
    saved, _, _ = bound_feedback(workflow, rid, work, task)
    workflow.store.checkpoints(rid, {"recovery/active": None, "develop/task/" + task.id: saved,
                                    "rounds/" + task.id: {"round": 7, "stalled": 0, "failure": ""}})
    with pytest.raises(RecoveryNeeded) as caught:
        await workflow._task(rid, task, git(work, "rev-parse", "HEAD"), plan, {"specification": {}})
    record = caught.value.failure
    assert record.component == "verification" and record.step == "checks"
    assert record.evidence["checks"][0]["process_exit_code"] == 1
    assert record.evidence["author_input"]["feedback"] == agent.author["feedback"]
    assert record.evidence["author_input"]["binding"]["source_hash"] == saved["source_hash"]
    assert record.evidence["checks"][0]["source_hash"] != saved["source_hash"]
    assert workflow.store.get(rid, "task_state/" + task.id)["evidence"] == "failed"


def test_verification_input_failure_keeps_original_feedback_and_current_error(tmp_path, monkeypatch):
    from auto_agents_2.recovery import FailureRecord
    workflow, rid, work, plan, _ = ready(tmp_path)
    task = plan.tasks[2]
    saved, _, _ = bound_feedback(workflow, rid, work, task)
    saved["author_input"] = author_input(workflow, rid, task, saved, selected_feedback(saved), capture="author_invocation")
    actual = FailureRecord.create(kind="evidence", component="verification", origin_id=task.id, task_id=task.id,
        step="verification_inputs", obligations=task.requirement_ids, reason_code="test_report_missing",
        evidence={"verification_error": {"reason": "missing report"}, "checks": []})
    monkeypatch.setattr(workflow, "_verification_problem", lambda *args, **kwargs: actual)
    with pytest.raises(RecoveryNeeded) as caught:
        workflow._require_task_evidence(rid, task, saved, task.checks, [], work, None)
    record = caught.value.failure
    assert record.evidence["author_input"] == saved["author_input"]
    assert record.reason_code == "test_report_missing"
    assert record.evidence["verification_error"] == actual.evidence["verification_error"]
    assert workflow.store.get(rid, "task_state/" + task.id)["evidence"] == "stale"


def test_dependency_reverification_retains_original_author_input(tmp_path):
    workflow, rid, task, _, work, incoming = setup_candidate(tmp_path, CodingAgent())
    saved = workflow.store.get(rid, "develop/task/" + task.id)
    saved["feedback"] = {"controller_recovery": "original before dependency merge"}
    saved["author_input"] = author_input(workflow, rid, task, saved, saved["feedback"], capture="author_invocation")
    original = copy.deepcopy(saved["author_input"])
    updated, _ = refresh_task_baseline(workflow, rid, task, saved, work, incoming, 1)
    assert updated["phase"] == "reverify" and updated["author_input"] == original
    assert updated["source_hash"] != original["binding"]["source_hash"]


async def test_interrupted_author_with_user_source_edit_is_not_misbound_or_overwritten(tmp_path):
    workflow, rid, work, plan, _ = ready(tmp_path, InputAgent())
    task = plan.tasks[2]
    saved, _, _ = bound_feedback(workflow, rid, work, task)
    saved["authority_sources"] = workflow.authority.identity(rid)
    workflow.store.checkpoints(rid, {"recovery/active": None, "develop/task/" + task.id: saved})
    (work / "part3.py").write_text("# user's uncommitted edit\n")
    with pytest.raises(StateConflict, match="Author input source changed"):
        await workflow._task(rid, task, git(work, "rev-parse", "HEAD"), plan, {"specification": {}})
    assert workflow.agent.author is None
    assert (work / "part3.py").read_text() == "# user's uncommitted edit\n"
    assert "author_input" not in workflow.store.get(rid, "develop/task/" + task.id)


@pytest.mark.parametrize("branch", ["preparation", "refresh"])
async def test_check_preparation_and_interruption_keep_author_feedback(tmp_path, monkeypatch, branch):
    from auto_agents_2.builds import VerificationPreparationError
    from auto_agents_2.verification import VerificationRefreshNeeded
    workflow, rid, work, plan, _ = ready(tmp_path)
    task = plan.tasks[2]
    saved, _, _ = bound_feedback(workflow, rid, work, task)
    saved["author_input"] = author_input(workflow, rid, task, saved, selected_feedback(saved), capture="author_invocation")
    workflow.store.checkpoint(rid, "develop/task/" + task.id, saved)
    if branch == "preparation":
        failure = workflow._preparation_failure(rid, task.checks, work, VerificationPreparationError("missing build"), task).failure
        assert failure.component == "verification_preparation"
    else:
        async def interrupted(*args, **kwargs):
            raise VerificationRefreshNeeded([], "execution_interrupted")
        monkeypatch.setattr(workflow.verifier, "run", interrupted)
        with pytest.raises(RecoveryNeeded) as caught:
            await workflow._checks(rid, task.checks, work, None, task)
        failure = caught.value.failure
        assert failure.evidence["execution_interrupted"]
    assert failure.evidence["author_input"] == saved["author_input"]


async def test_real_readonly_provider_receives_original_feedback_material_and_binding(tmp_path):
    import sys

    from auto_agents_2.providers import ProviderRouter
    from auto_agents_2.sandbox import SandboxRunner
    from auto_agents_2.util import write_json

    project = tmp_path / "project"
    project.mkdir()
    workflow, rid, work, plan, _ = ready(project)
    task = plan.tasks[2]
    saved, journal, _ = bound_feedback(workflow, rid, work, task)
    saved["author_input"] = author_input(workflow, rid, task, saved, selected_feedback(saved), capture="author_invocation")
    failure = workflow._task_failure(rid, task, saved, kind="proposal", step="task", record_outcome=False)
    workflow.store.checkpoints(rid, {"recovery/active": None, "develop/task/" + task.id: saved})
    workflow.recovery.submit(rid, failure)
    native = tmp_path / "fixture-native-home"
    native.mkdir()
    binary = tmp_path / "fixture-provider"
    binary.write_text(f"#!{sys.executable}\nEXPECTED = {saved['feedback']['controller_recovery']!r}\n"
                      f"SOURCE = {saved['source_hash']!r}\nJOURNAL = {digest(journal)!r}\n" + r'''
import hashlib, json, pathlib, sys
args = sys.argv[1:]
if args == ['--version']:
    print('fixture-cli 1')
    sys.exit(0)
assert args[args.index('-s') + 1] == 'read-only'
schema = json.loads(pathlib.Path(args[args.index('--output-schema') + 1]).read_text())
wire = sys.stdin.read()
if '<request_file>' in wire:
    request = json.loads(wire.split('<request_file>\n', 1)[1].split('\n</request_file>', 1)[0])
    wire = pathlib.Path(request['path']).read_text()
context = json.loads(wire.split('<context>\n', 1)[1].split('\n</context>', 1)[0])
if 'text_token' in schema['properties']:
    result = {'text_token': pathlib.Path(context['context_files'][0]).read_text(), 'image_token': ''}
else:
    record = context['failure']['evidence']['author_input']
    assert record['binding']['source_hash'] == SOURCE
    assert record['baseline_journal']['record_hash'] == JOURNAL
    assert record['baseline_journal']['previous_implementation_proposal_present'] is False
    material = record['feedback']['controller_recovery']
    assert material['required_read']
    artifact = material['artifact']
    path = pathlib.Path(artifact['location'])
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == artifact['hash']
    assert raw.decode() == EXPECTED
    try:
        path.write_text('forbidden evidence modification')
    except OSError:
        pass
    else:
        raise AssertionError('Feedback material must remain read-only')
    result = {'action': 'repair', 'task_ids': ['T3'], 'repair_targets': [],
              'reason': 'Read exact author feedback and bound baseline journal; no acceptance.',
              'evidence_refs': ['failure.evidence.author_input.binding', 'failure.evidence.task']}
pathlib.Path(args[args.index('-o') + 1]).write_text(json.dumps(result))
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}))
''')
    binary.chmod(0o755)
    config = workflow.config.load().model_dump(mode="json")
    config["providers"] = {"codex": config["providers"]["codex"]}
    config["providers"]["codex"].update(binary=str(binary), environment={"CODEX_HOME": str(native)})
    config["providers"]["codex"]["profile_map"] = {key: {"model": "fixture"} for key in ("balanced", "deep", "max")}
    config["execution"]["infra_retries"] = 0
    write_json(workflow.config.path, config)
    workflow.agent = ProviderRouter(workflow.config, workflow.store, SandboxRunner(workflow.store.root))
    await workflow.recovery.resume(rid)
    operation = f"recovery/diagnose/{failure.id}/1"
    result = workflow.store.get(rid, "invocation/" + operation)
    assert result["result"]["action"] == "repair"
    material = workflow.store.get(rid, "materials/" + operation)
    assert not material["omitted_mandatory_content"]
    assert workflow.store.get(rid, "recovery/active") is None
    assert workflow.store.get(rid, "develop/task/T3")["recovery_permit"]
    assert not workflow.store.get(rid, "develop/done")
    assert not list((workflow.store.root / "processes").glob("*.json"))



@pytest.mark.parametrize("wrapper", ["recheck", "plan_revision"])
def test_reverification_preserves_original_author_input_through_controller_retention(tmp_path, wrapper):
    from auto_agents_2.task_feedback import failure_input
    workflow, rid, work, plan, _ = ready(tmp_path)
    task = plan.tasks[2]
    saved, _, _ = bound_feedback(workflow, rid, work, task)
    saved["author_input"] = author_input(workflow, rid, task, saved, selected_feedback(saved), capture="author_invocation")
    original = copy.deepcopy(saved["author_input"])
    for _ in range(3):
        saved = {"phase": "reverify", "workspace": str(work), "source_hash": "a" * 64,
                 "feedback": {"controller_recheck": wrapper}, "previous_evidence": saved}
    assert failure_input(workflow, rid, task, saved) == {"author_input": original}
    # A model's nested failure/proposal is never a retention chain.
    assert failure_input(workflow, rid, task, {"proposal": saved}) == {}


def test_dependency_merge_lifts_wrapped_author_input_before_dropping_old_state(tmp_path):
    workflow, rid, task, _, work, incoming = setup_candidate(tmp_path, CodingAgent())
    saved = workflow.store.get(rid, "develop/task/" + task.id)
    saved["feedback"] = {"controller_recovery": "before dependency change"}
    saved["author_input"] = author_input(workflow, rid, task, saved, saved["feedback"], capture="author_invocation")
    original = copy.deepcopy(saved["author_input"])
    wrapped = {"phase": "reverify", "workspace": str(work), "source_hash": saved["source_hash"], "previous_evidence": saved}
    updated, _ = refresh_task_baseline(workflow, rid, task, wrapped, work, incoming, 1)
    assert updated["author_input"] == original
    assert updated["source_hash"] != original["binding"]["source_hash"]


@pytest.mark.parametrize("field,value", [("run_id", "foreign"), ("origin_id", "OTHER"), ("feedback_hash", "f" * 64)])
def test_foreign_or_damaged_retained_input_is_not_silently_accepted(tmp_path, field, value):
    from auto_agents_2.task_feedback import failure_input
    workflow, rid, work, plan, _ = ready(tmp_path)
    task = plan.tasks[2]
    saved, _, _ = bound_feedback(workflow, rid, work, task)
    saved["author_input"] = author_input(workflow, rid, task, saved, selected_feedback(saved), capture="author_invocation")
    saved["author_input"]["binding"][field] = value
    with pytest.raises(StateConflict, match="Retained author input binding"):
        failure_input(workflow, rid, task, {"previous_evidence": saved})
