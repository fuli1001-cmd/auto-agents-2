"""Real Engine consumers preserve source authority across interruptions and reuse."""
import copy

import pytest
from authority_review_fixtures import authority_review
from test_authority_workflow import BAD_METHOD, SourceAgent
from test_recovery_contracts import failure, plan
from test_workflow import engine

from auto_agents_2.engine import Replan
from auto_agents_2.errors import RecoveryPathExhausted
from auto_agents_2.faults import StateConflict
from auto_agents_2.models import Change, ImplementationResult, PlanResult, ReviewResult
from auto_agents_2.recovery_policy import ActionPermit
from auto_agents_2.review_authority import InvalidAuthorityReview, validate_review
from auto_agents_2.util import digest
from auto_agents_2.workspace import git, prepare_changes, tree_hash


def setup(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    workflow = engine(project, SourceAgent())
    workflow.workspaces.initialize()
    rid = workflow.store.admit("fixture", {})["id"]
    workflow.store.checkpoints(rid, {"capture": {"artifact": workflow.artifacts.put("Preserve saved data", "requirements")},
                                    "authority/protocol": 1})
    return workflow, rid, project


@pytest.mark.parametrize('bad', [None, 'target', 'value', 'missing', 'quote'])
@pytest.mark.parametrize('root', ['accepted_plan', 'candidate'])
def test_controller_dispute_records_and_rechecks_exact_plan_clause(tmp_path, bad, root):
    from auto_agents_2.errors import AA2Error
    from auto_agents_2.review_authority import plan_source_correction
    workflow, rid, work = setup(tmp_path)
    accepted = plan()
    a = accepted.tasks[2].checks[0].model_copy(update={'argv': ['python', '-c', 'print(1)\nprint(2)']})
    b = a.model_copy(update={'argv': ['python', '-c', 'print(1)', 'print(2)']})
    accepted.tasks[2].checks = [a, b]
    workflow.store.checkpoint(rid, 'plan', accepted.model_dump())
    ctx, _ = workflow.authority.prepare(rid, 'review', {'review_kind': 'plan', 'plan': accepted.model_dump()},
                                        ReviewResult, work, 'clause-review')
    sources = ctx['controller_review_basis']['sources']
    selected = [(key, value) for key, value in sources.items()
                if value.get('root') == root and value.get('contract_target', {}).get('task_id') == 'T3'
                and value['contract_target']['target'] == 'checks']
    assert len(selected) == 2 and selected[0][1]['text'] == selected[1][1]['text']
    assert selected[0][1]['contract_target']['value_hash'] != selected[1][1]['contract_target']['value_hash']
    key, source = selected[0]
    review = authority_review(ctx, approved=False)
    finding = review.authority['findings'][0]
    finding.update(kind='source_dispute', owner='plan', method_source=key, method_quote=source['text'])
    for row in review.authority['coverage']:
        if row['criterion_id'] == finding['criterion_id']:
            row['status'] = 'unverified'
    if bad == 'quote':
        finding['method_quote'] = a.argv[0]
        with pytest.raises(InvalidAuthorityReview, match='incomplete_contract_clause'):
            workflow.authority.record(rid, review, ctx, 'clause-review')
        return
    workflow.authority.record(rid, review, ctx, 'clause-review')
    with pytest.raises(Replan) as caught:
        workflow.authority.route_dispute(rid, review)
    assert caught.value.stage == 'plan'
    def get(name):
        return workflow.store.authority_get(rid, name)
    request = workflow.store.get(rid, 'authority/correction')
    assert plan_source_correction(get, accepted) == (request if root == 'accepted_plan' else None)
    assert request['mapped_clauses'] == [{'source': key, **source['contract_target']}]
    if bad:
        edited = copy.deepcopy(request)
        if bad == 'target':
            edited['mapped_clauses'][0]['index'] = 1
        elif bad == 'value':
            edited['mapped_clauses'][0]['value_hash'] = digest(b.model_dump())
        else:
            edited.pop('mapped_clauses')
        workflow.store.checkpoint(rid, 'authority/correction', edited)
        with pytest.raises(AA2Error, match='exact target'):
            plan_source_correction(get, accepted)


def review_context(workflow, rid, work, document=BAD_METHOD):
    return workflow.authority.prepare(rid, "review", {"review_kind": "design", "design": {"document": document}},
                                      ReviewResult, work, "design/review")[0]


def submitted_answer(workflow, rid, text=BAD_METHOD, key="clarify/1/0"):
    row = workflow.store.request(rid, key, "Question context is not an answer", "binding")
    workflow.store.answer(row["id"], text)
    return row


@pytest.mark.parametrize("key", ["clarify/1/0", "prototype-feedback"])
def test_external_answer_has_its_own_source_and_automatic_decisions_do_not(tmp_path, key):
    workflow, rid, work = setup(tmp_path)
    workflow.store.checkpoint(rid, "clarify_context", {"decisions": [{"question": "Model suggestion", "answer": "automatic method"}]})
    before = workflow.authority.identity(rid)
    row = submitted_answer(workflow, rid, key=key)
    assert workflow.authority.identity(rid) != before
    ctx = review_context(workflow, rid, work)
    basis = ctx["controller_review_basis"]
    source = "answer:" + row["id"]
    assert basis["sources"][source]["text"] == BAD_METHOD
    assert not any(s["text"] == "automatic method" for s in basis["sources"].values())
    result = authority_review(ctx)
    result.authority["technical_choices"] = [{"id": "native", "clause": BAD_METHOD, "goal_ids": [source],
        "method_origin": "explicit_input_method", "method_source": source, "input_quote": BAD_METHOD,
        "feasibility": "not_yet_verified", "reason": "Explicit external answer, still needs execution"}]
    validate_review(result, basis)
    result.authority["technical_choices"][0]["input_quote"] = row["prompt"]
    with pytest.raises(InvalidAuthorityReview, match="method_does_not_quote"):
        validate_review(result, basis)
    from auto_agents_2.prototype_contract import source_context
    from auto_agents_2.prototype_review import review_basis
    proto_source = source_context(workflow.store, workflow.artifacts, rid)["prototype_requirement_source"]
    proto_basis = review_basis(proto_source, {}, {"identity": "candidate"}, [])
    assert any(c["id"] == source and c["statement"] == BAD_METHOD for c in proto_basis["criteria"])


@pytest.mark.parametrize("key", ["prototype-approval", "recovery-cycle/1", "replan-stalled/1", "provider/x"])
def test_approval_and_recovery_answers_are_not_new_requirements(tmp_path, key):
    workflow, rid, _ = setup(tmp_path)
    row = workflow.store.request(rid, key, "Approve?", "binding")
    workflow.store.answer(row["id"], "yes", artifact_hash="binding")
    assert workflow.store.submitted_answers(rid) == []


def test_real_answer_cannot_be_waived_as_a_derived_method(tmp_path):
    workflow, rid, work = setup(tmp_path)
    submitted_answer(workflow, rid)
    ctx = review_context(workflow, rid, work)
    result = authority_review(ctx, approved=False)
    proof = result.authority["findings"][0]
    proof.update(kind="source_dispute", owner="design", method_source="candidate", method_quote=BAD_METHOD)
    next(c for c in result.authority["coverage"] if c["criterion_id"] == proof["criterion_id"])["status"] = "unverified"
    with pytest.raises(InvalidAuthorityReview, match="explicit_input_clause"):
        validate_review(result, ctx["controller_review_basis"])


def test_selected_requirement_option_preserves_its_original_key_label_and_value(tmp_path):
    from auto_agents_2.interaction import choice_spec
    workflow, rid, work = setup(tmp_path)
    interaction = choice_spec([BAD_METHOD, "Use an available automatic method"], ["native", "automatic"])
    row = workflow.store.request(rid, "design/0", "Choose proof method", "versioned-question", interaction)
    workflow.store.answer(row["id"], "1")
    ctx = review_context(workflow, rid, work)
    key = "answer:" + row["id"]
    source = ctx["controller_review_basis"]["sources"][key]
    assert source["answer_record"]["raw_answer"] == "1"
    assert source["answer_record"]["selected_option"] == interaction["options"][0]
    assert source["answer_record"]["interaction"] == interaction
    result = authority_review(ctx)
    result.authority["technical_choices"] = [{"id": "native", "clause": BAD_METHOD, "goal_ids": [key],
        "method_origin": "explicit_input_method", "method_source": key, "input_quote": BAD_METHOD,
        "feasibility": "not_yet_verified", "reason": "Respondent selected the version-bound option, not verbatim authorship"}]
    validate_review(result, ctx["controller_review_basis"])
    assert "Use an available automatic method" not in source["text"]


@pytest.mark.parametrize("accepted_between", [False, True])
def test_rephrased_source_disputes_share_lineage_even_after_another_approval(tmp_path, accepted_between):
    workflow, rid, work = setup(tmp_path)
    for index in range(3):
        method = BAD_METHOD + str(index)
        ctx = review_context(workflow, rid, work, method)
        result = authority_review(ctx, approved=False)
        proof = result.authority["findings"][0]
        proof.update(kind="source_dispute", owner="design", method_source="candidate", method_quote=method)
        next(c for c in result.authority["coverage"] if c["criterion_id"] == proof["criterion_id"])["status"] = "unverified"
        workflow.authority.record(rid, result, ctx, "design/review/" + str(index))
        with pytest.raises(Replan if index < 2 else RecoveryPathExhausted):
            workflow.authority.route_dispute(rid, result)
        if accepted_between and index < 2:
            previous = workflow.store.get(rid, "authority/correction")
            workflow.store.checkpoint(rid, "authority/correction", {**previous, "completed": {"review_key": "fixture-adoption"}})
    request = workflow.store.get(rid, "authority/correction")
    assert len(workflow.store.get(rid, "authority/no-progress/" + request["lineage"])) == 2


def test_new_submitted_input_can_open_a_new_source_assessment_without_erasing_history(tmp_path):
    workflow, rid, work = setup(tmp_path)
    lineages = []
    for index in range(3):
        submitted_answer(workflow, rid, "New explicit constraint " + str(index), key=f"clarify/{index}/0")
        ctx = review_context(workflow, rid, work, BAD_METHOD + str(index))
        result = authority_review(ctx, approved=False)
        proof = result.authority["findings"][0]
        proof.update(kind="source_dispute", owner="design", method_source="candidate", method_quote=BAD_METHOD + str(index))
        next(c for c in result.authority["coverage"] if c["criterion_id"] == proof["criterion_id"])["status"] = "unverified"
        workflow.authority.record(rid, result, ctx, "design/review/" + str(index))
        with pytest.raises(Replan):
            workflow.authority.route_dispute(rid, result)
        lineages.append(workflow.store.get(rid, "authority/correction")["lineage"])
    assert len(set(lineages)) == 3
    assert all(len(workflow.store.get(rid, "authority/no-progress/" + key)) == 1 for key in lineages)


def test_correction_lineage_uses_changed_execution_facts_not_report_timestamps(tmp_path):
    workflow, rid, work = setup(tmp_path)
    lineages = []
    for index in range(3):
        observation = {"command_hash": "fixed-command", "input_hash": "fixed-input",
            "environment_hash": "fixed-environment", "exit_code": 1 if index < 2 else 0,
            "process_exit_code": 1 if index < 2 else 0, "tests": 1,
            "failure_reasons": ["failed"] if index < 2 else [], "timestamp": index}
        ctx, _ = workflow.authority.prepare(rid, "review", {"review_kind": "design",
            "design": {"document": BAD_METHOD + str(index)}, "checks": [observation]},
            ReviewResult, work, "design/review")
        result = authority_review(ctx, approved=False)
        proof = result.authority["findings"][0]
        proof.update(kind="source_dispute", owner="design", method_source="candidate", method_quote=BAD_METHOD + str(index))
        next(c for c in result.authority["coverage"] if c["criterion_id"] == proof["criterion_id"])["status"] = "unverified"
        workflow.authority.record(rid, result, ctx, "design/review/" + str(index))
        with pytest.raises(Replan):
            workflow.authority.route_dispute(rid, result)
        lineages.append(workflow.store.get(rid, "authority/correction")["lineage"])
    assert lineages[0] == lineages[1] != lineages[2]
    assert len(workflow.store.get(rid, "authority/no-progress/" + lineages[0])) == 2


def grounded_permit(workflow, rid, work, *, action="repair", phase="reserved"):
    ctx = review_context(workflow, rid, work)
    review = authority_review(ctx, approved=False)
    workflow.authority.record(rid, review, ctx, "design/review")
    fact = failure(evidence={"review": review.model_dump()})
    permit = ActionPermit(cycle_id="cycle", owner="T3", action={"action": action, "failure_id": fact.id},
        actor="human", source_hash=tree_hash(work), plan_hash="plan", phase=phase,
        authority_failure=fact.model_dump(), authority_sources=workflow.authority.identity(rid),
        task_scopes={"T3": plan().tasks[2].allowed_paths}, outcome={"expected_tasks": ["T3"]})
    workflow.store.checkpoint(rid, "permit", permit.model_dump())
    return fact, permit


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["start", "task", "delivery", "reentry"])
async def test_dispatched_mutation_rechecks_authority_at_actual_consumption(tmp_path, consumer):
    workflow, rid, work = setup(tmp_path)
    fact, permit = grounded_permit(workflow, rid, work, action="design" if consumer == "reentry" else "repair",
                                  phase="completed" if consumer == "task" else "started")
    workflow.store.checkpoint(rid, "clarify", {"document": "New requirement version"})
    with pytest.raises(StateConflict, match="authority sources"):
        if consumer == "start":
            workflow.recovery_policy.start(rid, "permit")
        elif consumer == "task":
            workflow.recovery_policy.task_authorized(rid, {"recovery_permit": "permit"}, plan().tasks[2])
        elif consumer == "delivery":
            observation = digest(["observation", {}])
            workflow.store.checkpoint(rid, "recovery/delivery/" + observation,
                {"decision": {"action": "repair"}, "permit": "permit", "failure": fact.model_dump()})
            await workflow.recovery._delivery_decision(rid, work, {}, ["part3.py"], observation="observation")
        else:
            await workflow._grant_replan(rid, {"stage": "design", "granted": True, "evaluation": 1, "permit": "permit"})
    assert workflow.store.get(rid, "permit")["phase"] == permit.phase


def test_unchanged_dispatched_permit_remains_usable(tmp_path):
    workflow, rid, work = setup(tmp_path)
    grounded_permit(workflow, rid, work)
    workflow.recovery_policy.start(rid, "permit")
    assert workflow.store.get(rid, "permit")["phase"] == "started"
    assert workflow.recovery_policy.task_authorized(rid, {"recovery_permit": "permit"}, plan().tasks[2])


def test_new_answer_invalidates_a_prepared_review_without_mutating_it(tmp_path):
    workflow, rid, work = setup(tmp_path)
    ctx = review_context(workflow, rid, work)
    result = authority_review(ctx)
    workflow.authority.record(rid, result, ctx, "design/review")
    submitted_answer(workflow, rid, "Use keyboard-only controls")
    assert not workflow.authority.reusable(rid, result.model_dump())
    validate_review(result, ctx["controller_review_basis"])  # Historical record stays valid as history.


def test_prepared_protected_patch_requires_exact_reviewed_content_and_current_parents(tmp_path):
    workflow, rid, work = setup(tmp_path)
    task = plan().tasks[2]
    change = ImplementationResult(summary="test update", changes=[Change(path="tests/test_3.py", content="assert True")])
    prepared = prepare_changes(work, change.changes, task.allowed_paths, protected=False)
    ctx, _ = workflow.authority.prepare(rid, "review", {"review_kind": "test_changes", "candidate_changes": change.model_dump(),
        "purpose": "Preserve original tests", "project": workflow.contexts.build(work)}, ReviewResult, work, "test-review")
    result = authority_review(ctx)
    workflow.authority.record(rid, result, ctx, "test-review")
    saved = {"implementation": change.model_dump(), "patch": prepared, "source_hash": tree_hash(work),
             "authority_sources": workflow.authority.identity(rid), "protected_review": result.model_dump(),
             "protected_patch_hash": digest(prepared)}
    workflow.authority.prepared_task(rid, saved, task)
    bad = copy.deepcopy(saved)
    bad["patch"][0]["content"] = "assert False"
    with pytest.raises(StateConflict, match="exact reviewed candidate"):
        workflow.authority.prepared_task(rid, bad, task)
    workflow.store.checkpoint(rid, "clarify", {"document": "Changed goals"})
    with pytest.raises(StateConflict, match="current authority"):
        workflow.authority.prepared_task(rid, saved, task)
    assert not (work / "tests/test_3.py").exists()


@pytest.mark.asyncio
async def test_completed_task_is_reverified_without_another_implementation_round(tmp_path, monkeypatch):
    import asyncio
    project = tmp_path / "project"
    project.mkdir()
    request = tmp_path / "request.md"
    request.write_text("Add two numbers")
    workflow = engine(project, SourceAgent(invalid_review=True))
    invoke = workflow.agent.invoke
    async def pause(stage, *args, **kwargs):
        if stage == "readme":
            raise asyncio.CancelledError()
        return await invoke(stage, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(workflow.agent, "invoke", pause)
        with pytest.raises(asyncio.CancelledError):
            await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    saved = workflow.store.get(rid, "develop/task/T1")
    counters = workflow.store.get(rid, "rounds/T1")
    saved["review"].pop("authority")
    workflow.store.checkpoint(rid, "develop/task/T1", saved)
    calls = len(workflow.agent.views)
    result = await workflow.start(run_id=rid)
    assert result["status"] == "SUCCEEDED"
    assert workflow.store.get(rid, "rounds/T1") == counters
    new = workflow.agent.views[calls:]
    assert not any(stage == "implement" for stage, _ in new)
    assert any(ctx.get("review_kind") == "implementation" for _, ctx in new)
    assert workflow.store.get(rid, "authority/task-history/T1/" + digest(saved)) == saved
    assert git(project, "status", "--porcelain") == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, "partial", "missing_task_input", "other_call", "task_changed", "repair_grant"])
async def test_legacy_first_candidate_reassessed_without_reauthoring_or_new_round(tmp_path, monkeypatch, bad):
    import asyncio
    from pathlib import Path

    from auto_agents_2.context_plan import ContextPlanner
    from auto_agents_2.workspace import apply_prepared

    project = tmp_path / "project"
    project.mkdir()
    request = tmp_path / "request.md"
    request.write_text("Add two numbers")
    workflow = engine(project, SourceAgent(invalid_review=True))
    def stop(work, patch):
        if any(item["path"] == "calc.py" for item in patch):
            raise asyncio.CancelledError()
        return apply_prepared(work, patch)
    with monkeypatch.context() as changed:
        changed.setattr("auto_agents_2.engine.apply_prepared", stop)
        with pytest.raises(asyncio.CancelledError):
            await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    key = "develop/task/T1"
    saved = workflow.store.get(rid, key)
    rounds = workflow.store.get(rid, "rounds/T1")
    work = Path(saved["workspace"])
    original_task = next(ctx for stage, ctx in workflow.agent.views if stage == "implement")
    call = workflow.store.get(rid, "invocation/" + key)
    # Scenario agents bypass ProviderRouter; freeze their ACTUAL author input
    # through the same production projector and exact invocation identity.
    prepared = ContextPlanner(workflow.artifacts).prepare("implement", original_task, work, key, run_id=rid)
    ref = workflow.artifacts.put(prepared.record(), "prepared_context")
    if bad != "missing_task_input":
        identity = "another-call" if bad == "other_call" else call["identity"]
        workflow.store.checkpoint(rid, "presentation/" + key + "/" + identity,
                                  {"identity": identity, "artifact": ref})
    saved.pop("authority_sources")
    saved.pop("protected_review", None)
    saved.pop("protected_patch_hash", None)
    if bad == "partial":
        apply_prepared(work, saved["patch"][:1])
    if bad == "repair_grant":
        saved["recovery_permit"] = "old-unproven-repair"
    workflow.store.checkpoint(rid, key, saved)
    plan_value = workflow.store.get(rid, "plan")
    task = PlanResult.model_validate(plan_value).tasks[0]
    if bad == "task_changed":
        task.acceptance.append("Another constraint with the same task id")
        plan_value["tasks"][0] = task.model_dump()
        workflow.store.checkpoint(rid, "plan", plan_value)
    before = tree_hash(work)
    calls = len(workflow.agent.views)
    if bad:
        with pytest.raises(StateConflict):
            await workflow.authority.reaudit_task(rid, saved, task, work)
        assert workflow.store.get(rid, key) == saved and tree_hash(work) == before
    else:
        result = await workflow.start(run_id=rid)
        assert result["status"] == "SUCCEEDED"
        assert workflow.store.get(rid, "authority/task-history/T1/" + digest(saved)) == saved
        assert not any(stage == "implement" for stage, _ in workflow.agent.views[calls:])
        assert any(ctx.get("review_kind") == "test_changes" for _, ctx in workflow.agent.views[calls:])
    assert workflow.store.get(rid, "rounds/T1") == rounds
