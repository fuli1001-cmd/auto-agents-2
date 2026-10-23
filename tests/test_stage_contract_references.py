"""Real supplied workflow sources, with factual grounding and immutable review bindings."""
import asyncio
import copy

import pytest
from test_execution_stage_contract import TimingAgent, fixture_patch, review, setup_timing
from test_recovery_policy import ready

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.context_plan import ContextPlanner, remap_references
from auto_agents_2.decisions import DecisionRequest, DecisionResult, DecisionService
from auto_agents_2.errors import AA2Error, RecoveryPathExhausted
from auto_agents_2.evidence_refs import controller_context_catalog
from auto_agents_2.execution_stages import stage_contract
from auto_agents_2.faults import StateConflict
from auto_agents_2.interaction import choice_spec
from auto_agents_2.plan_revision import (
    PlanPatch,
    apply_patch,
    timing_review_binding,
    validate_timing_references,
    validate_timing_review,
)
from auto_agents_2.prompts import PromptCompiler
from auto_agents_2.recovery import Diagnosis, adjudicate
from auto_agents_2.util import digest
from auto_agents_2.workspace import tree_hash


def contexts():
    previous, patch = fixture_patch()
    candidate, _, _ = apply_patch(previous, patch, 1, set(patch.failure_ids),
                                 {t.id: t.origin_id for t in previous.tasks})
    author = {'controller_stage_contract': stage_contract(), 'plan_patch': True,
              'plan': previous.model_dump(), 'specification': {'requirements': ['retain all assertions']}}
    reviewer = {**author, 'previous_plan': previous.model_dump(), 'plan': candidate.model_dump(),
                'patch': patch.model_dump(), 'review_kind': 'plan_patch'}
    return patch, author, reviewer


@pytest.mark.parametrize('reference', ['controller_stage_contract', 'controller_stage_contract.content', 'hash'])
def test_supplied_exact_protocol_supports_author_and_review_citations(reference):
    patch, author, reviewer = contexts()
    ref = stage_contract()['hash'] if reference == 'hash' else reference
    patch.timing_corrections[0].evidence_refs.append(ref)
    verdict = review(evidence_refs=['previous_plan.tasks[2].acceptance[0]', ref])
    validate_timing_references(patch, author)
    validate_timing_review(patch, verdict, reviewer)
    catalog = controller_context_catalog(author)
    assert catalog.resolve(ref) == ref and ref in catalog.protocol and ref not in catalog.factual
    assert catalog.entries[ref].origin == 'controller_protocol'


@pytest.mark.parametrize('defect', ['absent', 'version', 'content', 'hash', 'reference_version', 'authority', 'unknown', 'extra'])
@pytest.mark.parametrize('role', ['author', 'review'])
def test_unknown_tampered_or_stale_contract_is_not_promoted_by_a_root_name(role, defect):
    patch, author, reviewer = contexts()
    context = author if role == 'author' else reviewer
    ref = 'controller_stage_contract'
    if defect == 'absent':
        context.pop(ref)
    elif defect == 'unknown':
        ref += '.invented_receipt'
    elif defect == 'extra':
        context[ref]['passed'] = True
    else:
        context[ref][defect] = 'fabricated-or-obsolete'
    patch.timing_corrections[0].evidence_refs.append(ref)
    with pytest.raises(AA2Error, match='resolvable controller evidence'):
        if role == 'author':
            validate_timing_references(patch, context)
        else:
            validate_timing_review(patch, review(evidence_refs=['previous_plan', ref]), context)


def test_proposal_copy_cannot_mint_protocol_or_hash_authority():
    record = stage_contract()
    catalog = controller_context_catalog({'failure': {'proposal': {'controller_stage_contract': record}}})
    assert catalog.resolve(record['hash']) is None
    assert catalog.resolve('controller_stage_contract') is None
    assert 'failure.proposal.controller_stage_contract' in catalog.proposals
    catalog = controller_context_catalog({'controller_stage_contract': record},
                                         provenance={'controller_stage_contract': 'proposal'})
    assert not catalog.protocol and not catalog.factual
    assert record['hash'] not in catalog.entries


def test_reference_syntax_metadata_is_not_factual_grounding():
    patch, author, _ = contexts()
    author['evidence_reference_contract'] = controller_context_catalog(author).contract(author)
    patch.timing_corrections[0].evidence_refs = ['controller_stage_contract', 'evidence_reference_contract']
    with pytest.raises(AA2Error):
        validate_timing_references(patch, author)


@pytest.mark.parametrize('role', ['author', 'review', 'diagnosis', 'decision'])
def test_protocol_alone_cannot_establish_project_facts(role, tmp_path):
    patch, author, reviewer = contexts()
    refs = ['controller_stage_contract', stage_contract()['hash']]
    with pytest.raises((AA2Error, ValueError)):
        if role == 'author':
            patch.timing_corrections[0].evidence_refs = refs
            validate_timing_references(patch, author)
        elif role == 'review':
            validate_timing_review(patch, review(evidence_refs=['patch.reason', *refs]), reviewer)
        elif role == 'diagnosis':
            _, _, _, plan, failure = ready(tmp_path)
            adjudicate(failure, diagnosis=Diagnosis(action='plan_patch', task_ids=['T3'], evidence_refs=refs,
                reason='Unsupported project conclusion'), plan=plan, evidence_context=author)
        else:
            request = DecisionRequest(kind='recovery_action', owner='T3', allowed_choices=['approve'],
                scope={'candidates': {'plan_patch': {}}}, evidence=author, state_identity='fixture')
            DecisionService.validate(request, DecisionResult(action='approve', evidence_refs=refs,
                reason='Unsupported project conclusion', selected_action='plan_patch', expected_result='Changed plan'))


@pytest.mark.parametrize('role', ['author', 'review', 'diagnosis', 'decision'])
def test_model_view_and_reference_catalog_share_the_exact_protocol(role, tmp_path):
    workflow, rid, work, _, _ = ready(tmp_path)
    patch, author, reviewer = contexts()
    stage, context = {'author': ('plan', author), 'review': ('review', reviewer),
        'diagnosis': ('recovery_diagnosis', author),
        'decision': ('decision', {'decision_request': {'evidence': author}})}[role]
    # Diagnosis uses its own registered purpose, not the author's plan_patch marker.
    if role == 'diagnosis':
        context = {key: value for key, value in context.items() if key != 'plan_patch'}
    evidence = context['decision_request']['evidence'] if role == 'decision' else context
    context['evidence_reference_contract'] = controller_context_catalog(evidence).contract(evidence)
    prepared = ContextPlanner(workflow.artifacts).prepare(stage, context, work, run_id=rid)
    payload = prepared.payload['decision_request']['evidence'] if role == 'decision' else prepared.payload
    assert payload['controller_stage_contract'] == stage_contract()
    assert payload['controller_stage_contract']['hash'] not in prepared.reference_map
    for ref in ['controller_stage_contract', stage_contract()['hash']]:
        assert remap_references({'evidence_refs': [ref]}, prepared.reference_map)['evidence_refs'] == [ref]
    prompt, _ = PromptCompiler().compile(stage, prepared.payload)
    assert stage_contract()['hash'] in prompt and 'workflow_timing_only' in prompt
    # Inline fingerprints do not invent artifact mounts or require nonexistent files.
    with ArtifactStore(workflow.store.root).reading(context, rid):
        pass


class ReferenceTimingAgent(TimingAgent):
    def __init__(self):
        super().__init__()
        self.reference_roles = []

    async def invoke(self, stage, context, schema, workspace, rid, operation, fresh=False):
        from auto_agents_2.plan_revision import EvidencePatchReview
        if schema in {Diagnosis, PlanPatch, EvidencePatchReview} or stage == 'decision':
            evidence = context['decision_request']['evidence'] if stage == 'decision' else context
            assert evidence['controller_stage_contract'] == stage_contract()
            assert 'controller_stage_contract' in context['evidence_reference_contract']['roots']
            self.reference_roles.append(stage if schema is not EvidencePatchReview else 'timing_review')
        response = await super().invoke(stage, context, schema, workspace, rid, operation, fresh)
        if schema is Diagnosis or stage == 'decision':
            response.evidence_refs.append('controller_stage_contract')
        elif schema is PlanPatch:
            response.timing_corrections[0].evidence_refs.append('controller_stage_contract')
        elif schema is EvidencePatchReview:
            response.timing_reviews[0].evidence_refs.append(stage_contract()['hash'])
        return response


async def test_actual_recovery_author_review_and_activation_bind_supplied_protocol(tmp_path):
    agent = ReferenceTimingAgent()
    workflow, rid, _, previous, failure = setup_timing(tmp_path, agent)
    await workflow.recovery.resume(rid)
    assert {'recovery_diagnosis', 'plan', 'timing_review'} <= set(agent.reference_roles)
    journal = workflow.store.get(rid, f'plan_revision/journal/{failure.id}/1')
    assert journal['phase'] == 'activated'
    assert journal['controller_stage_contract'] == stage_contract()
    assert journal['timing_review_binding'] == timing_review_binding(
        journal['previous_plan'], journal['patch'], journal['plan'], journal['review'], stage_contract())
    assert workflow.store.get(rid, 'plan')['tasks'][2]['checks'] == previous.model_dump()['tasks'][2]['checks']
    assert not workflow.store.get(rid, 'develop/done') and not workflow.store.pending(rid)


async def test_actual_recovery_decision_receives_protocol_inside_evidence(tmp_path):
    agent = ReferenceTimingAgent()
    workflow, rid, work, plan, failure = ready(tmp_path, agent)
    await workflow.recovery_policy.authorize(rid, failure, workflow.store.get(rid, 'recovery/active'),
        work, plan, adjudicate(failure), operation='protocol-decision', force_decision=True)
    assert 'decision' in agent.reference_roles


async def test_stale_frozen_author_context_is_preserved_without_dispatch(tmp_path):
    agent = ReferenceTimingAgent()
    workflow, rid, _, previous, failure = setup_timing(tmp_path, agent)
    key = f'recovery/patch-context/{failure.id}/1'
    frozen = {'controller_stage_contract': {**stage_contract(), 'version': 'old'}}
    workflow.store.checkpoint(rid, key, frozen)
    with pytest.raises(StateConflict, match='stage contract changed'):
        await workflow.recovery.resume(rid)
    assert 'plan' not in agent.reference_roles
    assert workflow.store.get(rid, key) == frozen
    assert workflow.store.get(rid, 'plan') == previous.model_dump()


@pytest.mark.parametrize('defect', ['content', 'version', 'hash', 'missing', 'changed_current'])
async def test_prepared_journal_rejects_protocol_drift_before_activation(tmp_path, monkeypatch, defect):
    import auto_agents_2.execution_stages as stages

    agent = ReferenceTimingAgent()
    workflow, rid, _, previous, failure = setup_timing(tmp_path, agent)
    activate = workflow.store.activate_plan_patch
    def pause(*args):
        raise asyncio.CancelledError()
    monkeypatch.setattr(workflow.store, 'activate_plan_patch', pause)
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    key = f'plan_revision/journal/{failure.id}/1'
    journal = workflow.store.get(rid, key)
    if defect == 'missing':
        journal.pop('controller_stage_contract')
    elif defect == 'changed_current':
        monkeypatch.setattr(stages, 'STAGE_CONTRACT', stages.STAGE_CONTRACT + '\nChanged producer contract')
    else:
        journal['controller_stage_contract'][defect] = 'obsolete-or-forged'
    workflow.store.checkpoint(rid, key, journal)
    with pytest.raises(AA2Error):
        activate(rid, f'{failure.id}/1')
    assert workflow.store.get(rid, 'plan') == previous.model_dump()
    assert workflow.store.get(rid, key)['phase'] == 'prepared'


@pytest.mark.parametrize('human_question', [False, True])
def test_new_supplied_contract_can_resume_old_reference_dead_end_without_resetting_history(tmp_path, monkeypatch, human_question):
    workflow, rid, work, plan, failure = ready(tmp_path)
    guard = workflow.recovery_policy.progress
    inputs = guard.inputs
    def legacy_inputs(*args):
        value = inputs(*args)
        value.pop('controller_stage_contract')
        return value
    monkeypatch.setattr(guard, 'inputs', legacy_inputs)
    old = guard.basis(legacy_inputs(rid, failure, work, plan), tree_hash(work), 'plan_patch')
    for operation in ['rejected-author', 'rejected-review']:
        guard.record(rid, 'T3', 'plan_patch', old, operation, {'status': 'rejected'})
    excluded = guard.exclusions(rid, failure, work, plan)
    workflow.store.checkpoint(rid, 'recovery/budgets/diagnosis/T3', {'round': 16})
    cycle, operation = 'saved-cycle', 'saved-selection'
    with pytest.raises(RecoveryPathExhausted):
        guard.stop(rid, failure, work, plan, cycle, operation, excluded, 'No useful alternative')
    with pytest.raises(RecoveryPathExhausted):
        guard.resume_operation(rid, failure, work, plan, cycle, operation)
    if human_question:
        workflow.store.request(rid, 'recovery-cycle/' + digest([cycle, operation]), 'Real pending permission',
            'fingerprint', choice_spec(['continue', 'pause'], ['continue', 'pause'], yes_no=True))
    monkeypatch.setattr(guard, 'inputs', inputs)
    assert guard.exclusions(rid, failure, work, plan) == {}
    if human_question:
        with pytest.raises(StateConflict):
            guard.resume_operation(rid, failure, work, plan, cycle, operation)
        assert len(workflow.store.pending(rid)) == 1
    else:
        fresh = guard.resume_operation(rid, failure, work, plan, cycle, operation)
        assert fresh.startswith(operation + '/evidence/')
        assert guard.resume_operation(rid, failure, work, plan, cycle, operation) == fresh
        current = guard.basis(inputs(rid, failure, work, plan), tree_hash(work), 'plan_patch')
        for attempt in ['new-one', 'new-two']:
            guard.record(rid, 'T3', 'plan_patch', current, attempt, {'status': 'rejected'})
        assert 'plan_patch' in guard.exclusions(rid, failure, work, plan)
        with pytest.raises(RecoveryPathExhausted):
            guard.stop(rid, failure, work, plan, cycle, fresh, guard.exclusions(rid, failure, work, plan), 'Still no progress')
        with pytest.raises(RecoveryPathExhausted):
            guard.resume_operation(rid, failure, work, plan, cycle, operation)
    assert workflow.store.get(rid, 'recovery/budgets/diagnosis/T3') == {'round': 16}
    assert len(workflow.store.get(rid, 'recovery/no-progress/plan_patch/' + old)['operations']) == 2


def test_stale_protocol_prompt_rewording_and_diagnosis_counts_do_not_refresh_basis(tmp_path):
    workflow, rid, work, plan, failure = ready(tmp_path)
    guard = workflow.recovery_policy.progress
    context = guard.inputs(rid, failure, work, plan)
    source = tree_hash(work)
    old = copy.deepcopy(context)
    old.pop('controller_stage_contract')
    assert guard.basis(old, source, 'diagnose') == guard.basis(context, source, 'diagnose')
    for defect in ['hash', 'content', 'version']:
        invalid = copy.deepcopy(context)
        invalid['controller_stage_contract'][defect] = 'arbitrary'
        assert guard.basis(invalid, source, 'plan_patch') == guard.basis(old, source, 'plan_patch')
    changed = copy.deepcopy(context)
    changed['prompt_version'] = 'another prompt'
    changed['failure']['proposal'] = {'summary': 'Different wording'}
    assert guard.basis(changed, source, 'plan_patch') == guard.basis(context, source, 'plan_patch')


async def test_formal_policy_reselects_after_new_protocol_without_reusing_old_exhausted_decision(tmp_path, monkeypatch):
    from test_decisions import ApprovingAgent

    class SelectPatch(ApprovingAgent):
        def __init__(self):
            super().__init__()
            self.wait = True
            self.requests = []

        async def invoke(self, stage, context, schema, *args, **kwargs):
            if stage == 'decision':
                request = context['decision_request']
                self.requests.append(copy.deepcopy(request))
                if self.wait:
                    return DecisionResult(action='ask_user', reason='No valid remaining action')
                assert 'plan_patch' in request['scope']['candidates']
                return DecisionResult(action='approve', selected_action='plan_patch', evidence_refs=['failure',
                    'controller_stage_contract'], reason='The actual protocol is now referenceable', expected_result='Reviewed amendment')
            return await super().invoke(stage, context, schema, *args, **kwargs)

    agent = SelectPatch()
    workflow, rid, work, plan, failure = ready(tmp_path, agent)
    policy, guard = workflow.recovery_policy, workflow.recovery_policy.progress
    inputs = guard.inputs
    def legacy(*args):
        context = inputs(*args)
        context.pop('controller_stage_contract')
        return context
    monkeypatch.setattr(guard, 'inputs', legacy)
    basis = guard.basis(legacy(rid, failure, work, plan), tree_hash(work), 'plan_patch')
    for attempt in ['author-reference-rejection', 'review-reference-rejection']:
        guard.record(rid, failure.origin_id, 'plan_patch', basis, attempt, {'status': 'rejected'})
    active = workflow.store.get(rid, 'recovery/active')
    suggestion = adjudicate(failure).model_copy(update={'action': 'plan_patch'})
    with pytest.raises(RecoveryPathExhausted):
        await policy.authorize(rid, failure, active, work, plan, suggestion, operation='select-action', force_decision=True)
    saved = copy.deepcopy(workflow.store.decision_requests(rid))
    assert len(agent.requests) == 1 and 'plan_patch' not in agent.requests[0]['scope']['candidates']
    with pytest.raises(RecoveryPathExhausted):
        await policy.authorize(rid, failure, active, work, plan, suggestion, operation='select-action', force_decision=True)
    assert len(agent.requests) == 1
    monkeypatch.setattr(guard, 'inputs', inputs)
    agent.wait = False
    selected, permit = await policy.authorize(rid, failure, active, work, plan, suggestion,
                                             operation='select-action', force_decision=True)
    assert selected.action == 'plan_patch' and len(agent.requests) == 2
    for key, value in saved:
        assert workflow.store.get(rid, key) == value
    assert workflow.store.get(rid, permit)['phase'] == 'reserved'
    assert not workflow.store.pending(rid) and not workflow.store.get(rid, 'develop/done')
