"""Finite admission counterexamples and actual engine/recovery lifecycle boundaries."""
import asyncio
import copy

import pytest
from evidence_test_fixtures import empty_contract, evidence_review
from test_contract_activation_contexts import cite_roles, prepare
from test_recovery_contracts import apply, plan

from auto_agents_2.errors import AA2Error
from auto_agents_2.evidence_admission import (
    admission_context,
    capability_catalog,
    validate_admission_review,
    validate_obligations,
)
from auto_agents_2.evidence_refs import controller_context_catalog
from auto_agents_2.faults import HumanIntervention
from auto_agents_2.models import EvidenceContract, EvidenceObligation, PlanResult
from auto_agents_2.plan_revision import PlanPatch
from auto_agents_2.util import digest


def context():
    rows = {"capture": {"artifact": {"hash": "a" * 64}},
            "clarify": {"requirements": [{"id": "R1", "description": "Preserve behavior"}]}}
    return admission_context(rows.get)


def candidate(ctx=None):
    ctx = ctx or context()
    value = plan()
    value.evidence_contract = empty_contract(ctx)
    value.evidence_contract.obligations.append(EvidenceObligation(id="env", target="acceptance", task_id="T1",
        index=0, value=value.tasks[0].acceptance[0], origin="model_derived", requirement_ids=["R1"],
        capability="installed_dependency_snapshot", capability_version=1, producer="EnvironmentManager",
        production_stage="environment", required_stage="verification"))
    return value


def reviewed(value, ctx=None, **changes):
    ctx = {**(ctx or context()), "plan": value.model_dump(), "review_kind": "plan"}
    return evidence_review(ctx, **changes), ctx


def test_supported_declaration_is_not_production_and_catalog_is_not_factual():
    value = candidate()
    review, ctx = reviewed(value)
    validate_admission_review(value, review, ctx)
    catalog = controller_context_catalog(ctx)
    identity = ctx["controller_evidence_capabilities"]["hash"]
    assert identity in catalog.protocol and identity not in catalog.factual
    assert "controller_evidence_capabilities.capabilities[0]" not in catalog.factual
    bad = copy.deepcopy(ctx)
    bad["controller_evidence_capabilities"]["capabilities"][0]["available"] = False
    assert "controller_evidence_capabilities" not in controller_context_catalog(bad).controller


@pytest.mark.parametrize("field,value", [
    ("capability", "effective_solver_configuration"), ("capability_version", 2),
    ("producer", "ProductTask"), ("production_stage", "plan"),
    ("required_stage", "implementation"), ("capability", "unknown_future_collector"),
    ("capability", "shared_product_browser_identity"),
])
def test_unsupported_capabilities_cannot_pass_even_with_approved_review(field, value):
    proposal = candidate()
    setattr(proposal.evidence_contract.obligations[0], field, value)
    review, ctx = reviewed(proposal)
    assert validate_obligations(proposal, ctx) == ["env"]
    with pytest.raises(AA2Error, match="Evidence admission rejected"):
        validate_admission_review(proposal, review, ctx)


@pytest.mark.parametrize("bad", ["missing", "catalog", "source", "duplicate_id", "duplicate_target", "value", "task", "index", "requirement"])
def test_malformed_or_stale_obligation_fails_before_review(bad):
    proposal = candidate()
    contract = proposal.evidence_contract
    row = contract.obligations[0]
    if bad == "missing":
        proposal.evidence_contract = None
    elif bad in {"catalog", "source"}:
        setattr(contract, "catalog_hash" if bad == "catalog" else "source_binding", "stale")
    elif bad.startswith("duplicate"):
        contract.obligations.append(row.model_copy(update={"id": "second"} if bad == "duplicate_target" else {}))
    elif bad == "value":
        row.value = "a different clause"
    elif bad == "task":
        row.task_id = "absent"
    elif bad == "index":
        row.index = 100
    else:
        row.requirement_ids = ["invented"]
    with pytest.raises(AA2Error):
        validate_obligations(proposal, context())


@pytest.mark.parametrize("flag", ["mapping_complete", "origins_accurate", "capabilities_sufficient", "requirements_preserved"])
def test_approved_boolean_cannot_bypass_missing_semantic_assessments(flag):
    proposal = candidate()
    review, ctx = reviewed(proposal, **{flag: False})
    with pytest.raises(AA2Error, match="independent semantic"):
        validate_admission_review(proposal, review, ctx)


@pytest.mark.parametrize("field", ["candidate_hash", "catalog_hash", "source_binding"])
def test_review_for_other_candidate_or_source_is_rejected(field):
    proposal = candidate()
    review, ctx = reviewed(proposal, **{field: "stale"})
    with pytest.raises(AA2Error, match="exact candidate"):
        validate_admission_review(proposal, review, ctx)


def test_source_reviewed_user_requirement_is_a_blocker_not_a_replan_waiver():
    proposal = candidate()
    row = proposal.evidence_contract.obligations[0]
    row.origin = "user"
    row.capability = "effective_solver_configuration"
    review, ctx = reviewed(proposal, unsupported_user_obligations=["env"], capabilities_sufficient=False)
    with pytest.raises(HumanIntervention) as error:
        validate_admission_review(proposal, review, ctx)
    assert error.value.requirement.kind == "external_input"
    with pytest.raises(HumanIntervention):  # Reconfirmation does not create the collector.
        validate_admission_review(proposal, review, ctx)
    row.origin = "model_derived"
    review, ctx = reviewed(proposal, unsupported_user_obligations=["env"])
    with pytest.raises(AA2Error, match="exact source mapping"):
        validate_admission_review(proposal, review, ctx)


def test_obligation_cannot_borrow_another_tasks_requirement_identity():
    ctx = context()
    source = ctx['evidence_requirement_source']
    source['requirement_ids'].append('R2')
    source['hash'] = digest({k:v for k,v in source.items() if k != 'hash'})
    value = candidate(ctx)
    value.evidence_contract.obligations[0].requirement_ids = ['R2']
    with pytest.raises(AA2Error, match="task's declared"):
        validate_obligations(value, ctx)


def test_legacy_shape_and_new_schema_do_not_silently_downgrade():
    old = plan()
    assert "evidence_contract" not in old.model_dump()
    data = old.model_dump()
    assert PlanResult.model_validate(data).model_dump() == data
    patch = PlanPatch(base_revision=1, failure_ids=["x"], reason="x", document="x")
    assert "evidence_contract" not in patch.model_dump()
    invalid = candidate().model_dump()
    invalid["evidence_contract"]["version"] = 999
    with pytest.raises(ValueError):
        PlanResult.model_validate(invalid)
    with pytest.raises(ValueError):
        EvidenceContract.model_validate({"version": 1})


def test_ordinary_patch_cannot_remove_or_reattribute_existing_obligation():
    from test_recovery_contracts import patch_for
    old = candidate()
    patch = patch_for(old, evidence_contract=empty_contract(context()))
    with pytest.raises(AA2Error, match="formal mapped"):
        apply(old, patch)
    patch.evidence_contract = old.evidence_contract.model_copy(deep=True)
    patch.evidence_contract.obligations[0].origin = "user"
    with pytest.raises(AA2Error, match="formal mapped"):
        apply(old, patch)


@pytest.mark.asyncio
async def test_actual_engine_author_review_pause_resume_and_reuse(tmp_path):
    from test_workflow import CodingAgent, engine

    from auto_agents_2.context_plan import ContextPlanner
    from auto_agents_2.prompts import PromptCompiler
    class Agent(CodingAgent):
        views = []
        async def invoke(self, stage, ctx, schema, workspace, rid, operation, fresh=False):
            if stage in {"design", "plan"} or ctx.get("review_kind") == "plan":
                self.views.append(copy.deepcopy(ctx))
                assert ctx['controller_evidence_capabilities'] == capability_catalog()
                projected = ContextPlanner(workflow.artifacts).prepare(stage, ctx, workspace, operation, run_id=rid)
                assert projected.payload['controller_evidence_capabilities'] == ctx['controller_evidence_capabilities']
                prompt, _ = PromptCompiler().compile(stage, projected.payload)
                assert 'testInfo.outputPath' in prompt
            return await super().invoke(stage, ctx, schema, workspace, rid, operation, fresh)
    request = tmp_path / 'request.md'
    request.write_text('Add two numbers')
    project = tmp_path / 'project'
    project.mkdir()
    agent = Agent()
    agent.interrupt_review = True
    workflow = engine(project, agent)
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    first = workflow.store.current()
    assert first['status'] == 'PAUSED'
    admission = workflow.store.get(first['id'], 'evidence_admission/initial')
    assert admission['candidate_hash'] == digest(workflow.store.get(first['id'], 'plan'))
    assert len(agent.views) == 3
    resumed = await engine(project, agent).start()
    assert resumed['status'] == 'SUCCEEDED'
    assert len(agent.views) == 3
    unchanged = await engine(project, agent).start(str(request))
    assert unchanged['data']['outcome'] == 'NO_CHANGES'


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['none', 'record_missing', 'source_changed', 'catalog_changed', 'candidate_changed', 'review_changed', 'affected_changed'])
async def test_real_patch_prepared_resume_revalidates_admission_and_activates_once(tmp_path, monkeypatch, bad):
    from test_stage_contract_corrections import setup_correction
    workflow, rid, _, old, record, agent = setup_correction(tmp_path)
    cite_roles(agent, author=['plan.tasks[2].checks[0]', 'recovery_decision'],
               reviewer=['patch.contract_corrections[0]', 'controller_stage_contract'])
    identity, key, journal = await prepare(workflow, rid, record, monkeypatch)
    assert journal['phase'] == 'prepared'
    assert workflow.store.get(rid, 'plan') == old.model_dump()
    if bad == 'record_missing':
        journal.pop('evidence_admission')
    elif bad == 'source_changed':
        specification = workflow.store.get(rid, 'clarify')
        specification['document'] += ' changed'
        workflow.store.checkpoint(rid, 'clarify', specification)
    elif bad == 'catalog_changed':
        journal['evidence_admission']['context']['controller_evidence_capabilities']['hash'] = 'stale'
    elif bad == 'candidate_changed':
        journal['plan']['evidence_contract']['obligations'] = [candidate().evidence_contract.obligations[0].model_dump()]
    elif bad == 'review_changed':
        journal['review']['evidence_assessment']['mapping_complete'] = False
    elif bad == 'affected_changed':
        journal['affected'] = []
    workflow.store.checkpoint(rid, key, journal)
    if bad != 'none':
        with pytest.raises(AA2Error):
            workflow.store.activate_plan_patch(rid, identity)
        assert workflow.store.get(rid, 'plan') == old.model_dump()
    else:
        await workflow.recovery.resume(rid)
        workflow.store.activate_plan_patch(rid, identity)
        assert workflow.store.get(rid, 'plan_revision/current')['revision'] == 2
        assert workflow.store.get(rid, 'develop/done') == ['T1', 'T2']
        assert workflow.store.get(rid, 'task_state/T4')['evidence'] == 'stale'
        assert [stage for stage, _ in agent.views] == ['plan', 'review']
    assert workflow.store.get(rid, 'rounds/T3') == {'round': 17}


@pytest.mark.asyncio
async def test_actual_engine_preserves_unsupported_user_request_and_blocks(tmp_path):
    from test_workflow import CodingAgent, engine
    class Agent(CodingAgent):
        async def invoke(self, stage, ctx, schema, *args, **kwargs):
            result = await super().invoke(stage, ctx, schema, *args, **kwargs)
            if stage == 'clarify':
                result.requirements[0].acceptance.append('Record effective solver configuration')
            if stage == 'plan':
                result.tasks[0].acceptance.append('Record effective solver configuration')
                result.evidence_contract.obligations.append(EvidenceObligation(id='user-solver',
                    target='acceptance', task_id='T1', index=1, value=result.tasks[0].acceptance[1],
                    origin='user', requirement_ids=['R1'], capability='effective_solver_configuration',
                    capability_version=1, producer='EnvironmentManager', production_stage='environment',
                    required_stage='verification'))
            if ctx.get('review_kind') == 'plan':
                result = evidence_review(ctx, capabilities_sufficient=False,
                                         unsupported_user_obligations=['user-solver'])
                result.approved = False
            return result
    project = tmp_path / 'project'
    project.mkdir()
    request = tmp_path / 'request.md'
    request.write_text('Add numbers and record effective solver configuration')
    workflow = engine(project, Agent())
    questions = []
    async def pause(question):
        questions.append(question)
        return 'n'  # The public choice protocol accepts keys, not internal values.
    workflow.question_handler = pause
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    run = workflow.store.current()
    assert run['status'] == 'PAUSED' and questions
    assert workflow.store.get(run['id'], 'plan') is None
    blocked = workflow.store.get(run['id'], 'evidence_admission/blocked')
    assert blocked['plan']['evidence_contract']['obligations'][0]['origin'] == 'user'
    assert workflow.artifacts.read(workflow.store.get(run['id'], 'capture')['artifact']) == request.read_text()
    assert 'no supported producer' in questions[0]['prompt']


@pytest.mark.asyncio
async def test_actual_engine_returns_model_derived_bad_clause_to_plan_then_verifies(tmp_path):
    from test_decisions import ApprovingAgent
    from test_workflow import engine
    class Agent(ApprovingAgent):
        plans = 0
        async def invoke(self, stage, ctx, schema, *args, **kwargs):
            result = await super().invoke(stage, ctx, schema, *args, **kwargs)
            if stage == 'plan':
                self.plans += 1
                if self.plans == 1:
                    result.tasks[0].acceptance.append('Derived requirement for effective solver configuration')
                    result.evidence_contract.obligations.append(EvidenceObligation(id='derived-solver',
                        target='acceptance', task_id='T1', index=1, value=result.tasks[0].acceptance[1],
                        origin='model_derived', requirement_ids=['R1'], capability='effective_solver_configuration',
                        capability_version=1, producer='EnvironmentManager', production_stage='environment',
                        required_stage='verification'))
            return result
    request = tmp_path / 'request.md'
    request.write_text('Add two numbers')
    project = tmp_path / 'project'
    project.mkdir()
    agent = Agent()
    workflow = engine(project, agent)
    outcome = await workflow.start(str(request))
    assert outcome['status'] == 'SUCCEEDED' and agent.plans == 2
    accepted = workflow.store.get(outcome['id'], 'plan')
    assert accepted['tasks'][0]['acceptance'] == ['sum']
    assert workflow.store.get(outcome['id'], 'rounds/T1')['round'] == 1
    assert workflow.store.get(outcome['id'], 'evidence_admission/initial')['candidate_hash'] == digest(accepted)


@pytest.mark.asyncio
async def test_actual_patch_new_obligation_invalidates_old_evidence_without_resetting_counts(tmp_path):
    from test_stage_contract_corrections import setup_correction
    workflow, rid, _, _, _, agent = setup_correction(tmp_path)
    invoke = agent.invoke
    async def with_obligation(stage, ctx, schema, *args, **kwargs):
        result = await invoke(stage, ctx, schema, *args, **kwargs)
        if schema is PlanPatch:
            result.evidence_contract.obligations.append(EvidenceObligation(id='snapshot', target='checks',
                task_id='T3', index=0, value=result.upsert_tasks[0].checks[0], origin='model_derived',
                requirement_ids=['R3'], capability='installed_dependency_snapshot', capability_version=1,
                producer='EnvironmentManager', production_stage='environment', required_stage='verification'))
        return result
    agent.invoke = with_obligation
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, 'plan_revision/current')['revision'] == 2
    assert workflow.store.get(rid, 'develop/done') == []
    assert workflow.store.get(rid, 'rounds/T3') == {'round': 17}
    assert workflow.store.get(rid, 'task_state/T1')['evidence'] == 'stale'


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['missing_contract', 'unsupported'])
async def test_actual_patch_cannot_activate_missing_or_unsupported_contract(tmp_path, bad):
    from test_stage_contract_corrections import setup_correction
    workflow, rid, _, old, record, agent = setup_correction(tmp_path)
    invoke = agent.invoke
    async def malformed(stage, ctx, schema, *args, **kwargs):
        result = await invoke(stage, ctx, schema, *args, **kwargs)
        if schema is PlanPatch:
            if bad == 'missing_contract':
                result.evidence_contract = None
            else:
                result.evidence_contract.obligations.append(EvidenceObligation(id='unknown', target='checks',
                    task_id='T3', index=0, value=result.upsert_tasks[0].checks[0], origin='model_derived',
                    requirement_ids=['R3'], capability='effective_solver_configuration', capability_version=1,
                    producer='EnvironmentManager', production_stage='environment', required_stage='verification'))
        return result
    agent.invoke = malformed
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, 'plan') == old.model_dump()
    assert workflow.store.get(rid, 'rounds/T3') == {'round': 17}
    rejected = workflow.store.get(rid, f'recovery/patch-rejection/{record.id}/1')
    assert rejected['validation_error']



@pytest.mark.parametrize('change', ['hash', 'version'])
def test_source_envelope_must_be_versioned_and_self_consistent(change):
    ctx = context()
    proposal = candidate(ctx)
    ctx['evidence_requirement_source'][change] = 'invalid'
    with pytest.raises(AA2Error, match='source binding'):
        validate_obligations(proposal, ctx)


@pytest.mark.asyncio
async def test_plain_patch_cannot_strip_new_admission_to_claim_legacy(tmp_path, monkeypatch):
    from test_recovery_contracts import patch_for
    from test_stage_contract_corrections import setup_correction
    workflow, rid, _, old, record, agent = setup_correction(tmp_path)
    invoke = agent.invoke
    async def ordinary(stage, ctx, schema, *args, **kwargs):
        if schema is PlanPatch:
            return patch_for(PlanResult.model_validate(ctx['plan']), evidence_contract=empty_contract(ctx))
        if ctx.get('review_kind') == 'plan_patch':
            return evidence_review(ctx)
        return await invoke(stage, ctx, schema, *args, **kwargs)
    agent.invoke = ordinary
    identity, key, journal = await prepare(workflow, rid, record, monkeypatch)
    assert journal['version'] == 1 and not journal['patch']['contract_corrections']
    journal.pop('evidence_admission')
    journal['plan'].pop('evidence_contract')
    journal['patch'].pop('evidence_contract')
    workflow.store.checkpoint(rid, key, journal)
    with pytest.raises(AA2Error, match='missing its admission'):
        workflow.store.activate_plan_patch(rid, identity)
    assert workflow.store.get(rid, 'plan') == old.model_dump()
