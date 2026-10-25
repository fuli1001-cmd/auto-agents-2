"""Role-specific citations survive real PlanPatch preparation and transaction reentry."""
import asyncio
import copy

import pytest
from test_stage_contract_corrections import setup_correction

from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.contract_corrections import contract_review_binding
from auto_agents_2.errors import AA2Error
from auto_agents_2.evidence_refs import controller_context_catalog
from auto_agents_2.plan_revision import EvidencePatchReview, PlanPatch
from auto_agents_2.util import digest
from auto_agents_2.workspace import tree_hash


def cite_roles(agent, *, author=(), reviewer=()):
    invoke = agent.invoke
    async def cited(stage, context, schema, *args, **kwargs):
        result = await invoke(stage, context, schema, *args, **kwargs)
        if schema is PlanPatch:
            for row in result.contract_corrections:
                row.evidence_refs += list(author)
        if issubclass(schema, EvidencePatchReview):
            for row in result.contract_reviews:
                row.evidence_refs += list(reviewer)
        return result
    agent.invoke = cited


async def prepare(workflow, rid, record, monkeypatch):
    activate = workflow.store.activate_plan_patch
    def interrupt(*args):
        raise asyncio.CancelledError()
    monkeypatch.setattr(workflow.store, 'activate_plan_patch', interrupt)
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    monkeypatch.setattr(workflow.store, 'activate_plan_patch', activate)
    identity = f'{record.id}/1'
    key = 'plan_revision/journal/' + identity
    return identity, key, workflow.store.get(rid, key)


async def test_real_citations_survive_immediate_activation(tmp_path):
    workflow, rid, _, _, record, agent = setup_correction(tmp_path)
    cite_roles(agent, author=['plan.tasks[2].checks[0]', 'recovery_decision'],
               reviewer=['patch.contract_corrections[0]', 'controller_stage_contract', 'recovery_decision'])
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, f'plan_revision/journal/{record.id}/1')['phase'] == 'activated'
    assert workflow.store.get(rid, 'plan_revision/current')['revision'] == 2
    assert [stage for stage, _ in agent.views] == ['plan', 'review']


async def test_candidate_source_dispute_reaches_review_and_activation_without_formal_source_exception(tmp_path):
    from authority_review_fixtures import authority_review

    from auto_agents_2.engine import Replan
    from auto_agents_2.models import ReviewResult
    from auto_agents_2.review_authority import source_text
    workflow, rid, work, old, record, agent = setup_correction(tmp_path)
    proposed = old.model_copy(deep=True)
    proposed.tasks[2].checks[0].argv.append('--unaccepted-method')
    context, _ = workflow.authority.prepare(rid, 'review', {'review_kind': 'plan_patch',
        'plan': proposed.model_dump(), 'previous_plan': old.model_dump(), 'patch': {}},
        ReviewResult, work, 'candidate-source-review')
    basis = context['controller_review_basis']
    key, source = next((key, source) for key, source in basis['sources'].items()
        if source.get('root') == 'candidate' and source['contract_target']['target'] == 'checks'
        and source['contract_target']['task_id'] == 'T3')
    rejected = authority_review(context, approved=False)
    finding = rejected.authority['findings'][0]
    finding.update(kind='source_dispute', owner='plan', method_source=key, method_quote=source['text'])
    for row in rejected.authority['coverage']:
        if row['criterion_id'] == finding['criterion_id']:
            row['status'] = 'unverified'
    workflow.authority.record(rid, rejected, context, 'candidate-source-review')
    with pytest.raises(Replan):
        workflow.authority.route_dispute(rid, rejected)
    request = workflow.store.get(rid, 'authority/correction')
    assert request['previous_kind'] == 'proposal' and request['previous'] == proposed.model_dump()
    assert workflow.store.get(rid, 'plan') == old.model_dump()
    invoke = agent.invoke
    async def review_amendment(stage, ctx, schema, *args, **kwargs):
        result = await invoke(stage, ctx, schema, *args, **kwargs)
        if issubclass(schema, EvidencePatchReview):
            correction = ctx['controller_review_basis']['correction']
            result.authority['amendments'] = [{'source': 'previous_contract', 'old_quote': quote,
                'new_quote': source_text(ctx['plan']['tasks'][2]['checks'][0]),
                'preserve_ids': correction['preserve_ids'],
                'reason': 'Correct the unaccepted method while preserving every formal assertion'}
                for quote in correction['clauses']]
        return result
    agent.invoke = review_amendment
    workflow.recovery.submit(rid, record)
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, 'plan_revision/current')['revision'] == 2
    assert workflow.store.get(rid, f'plan_revision/journal/{record.id}/2')['phase'] == 'activated'
    assert workflow.store.get(rid, 'authority/correction')['completed']['artifact_hash'] == digest(workflow.store.get(rid, 'plan'))
    assert workflow.store.get(rid, 'rounds/T3') == {'round': 17}
    assert workflow.store.get(rid, 'plan_revision/history/1')['plan'] == old.model_dump()
    assert [stage for stage, _ in agent.views] == ['plan', 'review']


@pytest.mark.parametrize('role,reference', [
    ('author', 'plan.tasks[2].checks[0]'), ('author', 'recovery_decision.reason'),
    ('author', 'task_origins'), ('author', 'project.workspace'),
    ('author', 'source_history.artifact'), ('author', 'controller_stage_contract'),
    ('reviewer', 'patch.contract_corrections[0]'), ('reviewer', 'plan.tasks[2].checks[0]'),
    ('reviewer', 'recovery_decision'), ('reviewer', 'controller_stage_contract'),
    ('reviewer', 'retained_candidates.T3.project.workspace'), ('reviewer', 'affected[0]'),
    ('reviewer', 'reopen[0]'), ('reviewer', 'project.workspace'),
])
async def test_supplied_role_citations_activate_and_resume_without_new_invocations(tmp_path, monkeypatch, role, reference):
    workflow, rid, work, old, record, agent = setup_correction(tmp_path)
    workflow.store.checkpoint(rid, 'develop/task/T3', {
        'workspace': str(work), 'phase': 'rejected', 'source_hash': tree_hash(work)})
    cite_roles(agent, **{role: [reference]})
    identity, key, journal = await prepare(workflow, rid, record, monkeypatch)
    assert journal['version'] == 3
    assert journal['contract_author_context'] == agent.views[0][1]
    assert journal['contract_review_context'] == agent.views[1][1]
    assert journal['contract_author_context']['plan'] == old.model_dump()
    catalog = controller_context_catalog(journal['contract_review_context'],
                                         provenance={'patch': 'proposal', 'plan': 'proposal'})
    assert 'patch.contract_corrections[0]' in catalog.proposals
    assert 'plan.tasks[2].checks[0]' in catalog.proposals
    assert 'previous_plan.tasks[2].checks[0]' in catalog.factual
    await workflow.recovery.resume(rid)
    workflow.store.activate_plan_patch(rid, identity)
    assert workflow.store.get(rid, key)['phase'] == 'activated'
    assert workflow.store.get(rid, 'plan_revision/current')['revision'] == 2
    assert [stage for stage, _ in agent.views] == ['plan', 'review']
    assert workflow.store.get(rid, 'rounds/T3') == {'round': 17}
    assert workflow.store.get(rid, 'develop/done') == ['T1', 'T2']


@pytest.mark.parametrize('bad', [None, 'scope', 'other_attempt', 'action', 'answer', 'missing_request'])
async def test_old_authority_prepared_grant_is_supplemented_only_with_original_binding(tmp_path, monkeypatch, bad):
    workflow, rid, _, old, record, agent = setup_correction(tmp_path)
    identity, key, journal = await prepare(workflow, rid, record, monkeypatch)
    journal['review'].pop('authority')
    # Reproduce the WHOLE pre-authority record format, including its original
    # capability admission hash; do not leave a mismatched modern receipt.
    admission = journal['evidence_admission']
    admission.pop('authority')
    admission['review_hash'] = digest(journal['review'])
    admission['binding'] = digest({k: v for k, v in admission.items() if k != 'binding'})
    journal['contract_review_binding'] = contract_review_binding(journal)
    workflow.store.checkpoint(rid, key, journal)
    active = workflow.store.get(rid, 'recovery/active')
    permit_key = active['permit']
    permit = workflow.store.get(rid, permit_key)
    for name in ('authority_sources', 'authority_failure', 'authority_transition'):
        permit.pop(name, None)
    if bad == 'scope':
        permit['task_scopes']['T3'] = ['unrelated.py']
    elif bad == 'other_attempt':
        cycle = workflow.store.get(rid, 'recovery/cycles/' + permit['cycle_id'])
        cycle['observation'] += 1
        workflow.store.checkpoint(rid, 'recovery/cycles/' + permit['cycle_id'], cycle)
    elif bad == 'action':
        permit['action']['task_ids'] = ['T1']
    elif bad == 'answer':
        question = workflow.store.request(rid, 'design/0', 'New explicit constraint', 'new-version')
        workflow.store.answer(question['id'], 'Keep another feature')
    elif bad == 'missing_request':
        workflow.store.checkpoint(rid, permit_key.replace('permits/', 'request-snapshots/'), None)
    workflow.store.checkpoint(rid, permit_key, permit)
    workflow.store.checkpoint(rid, 'authority/protocol', 1)
    if bad:
        with pytest.raises(AA2Error):
            await workflow.recovery.resume(rid)
        assert workflow.store.get(rid, key)['phase'] == 'prepared'
        assert workflow.store.get(rid, 'plan') == old.model_dump()
    else:
        await workflow.recovery.resume(rid)
        workflow.store.activate_plan_patch(rid, identity)
        assert workflow.store.get(rid, key)['phase'] == 'activated'
        assert workflow.store.get(rid, 'plan_revision/current')['revision'] == 2
        assert workflow.store.get(rid, 'authority/permit-history/' + digest(permit)) == permit
        assert workflow.store.get(rid, key)['authority_reassessment']['permit'] == permit_key
    assert workflow.store.get(rid, 'rounds/T3') == {'round': 17}
    assert workflow.store.get(rid, 'develop/done') == (['T1', 'T2', 'T3', 'T4'] if bad else ['T1', 'T2'])


@pytest.mark.parametrize('bad', [None, 'history', 'candidate'])
async def test_legacy_activated_patch_reassesses_without_activating_twice(tmp_path, monkeypatch, bad):
    workflow, rid, _, _, record, agent = setup_correction(tmp_path)
    activate = workflow.store.activate_plan_patch
    def stop_after_activation(*args):
        activate(*args)
        raise asyncio.CancelledError()
    with monkeypatch.context() as stopped:
        stopped.setattr(workflow.store, 'activate_plan_patch', stop_after_activation)
        with pytest.raises(asyncio.CancelledError):
            await workflow.recovery.resume(rid)
    key = f'plan_revision/journal/{record.id}/1'
    journal = workflow.store.get(rid, key)
    assert journal['phase'] == 'activated'
    journal['review'].pop('authority')
    admission = journal['evidence_admission']
    admission.pop('authority')
    admission['review_hash'] = digest(journal['review'])
    admission['binding'] = digest({k: v for k, v in admission.items() if k != 'binding'})
    journal['contract_review_binding'] = contract_review_binding(journal)
    workflow.store.checkpoint(rid, key, journal)
    active = workflow.store.get(rid, 'recovery/active')
    permit = workflow.store.get(rid, active['permit'])
    for name in ('authority_sources', 'authority_failure', 'authority_transition'):
        permit.pop(name, None)
    workflow.store.checkpoint(rid, active['permit'], permit)
    workflow.store.checkpoint(rid, 'authority/protocol', 1)
    if bad == 'history':
        history = workflow.store.get(rid, 'plan_revision/history/1')
        history['plan']['document'] += ' unbound history'
        workflow.store.checkpoint(rid, 'plan_revision/history/1', history)
    elif bad == 'candidate':
        changed = workflow.store.get(rid, 'plan')
        changed['document'] += ' unbound candidate'
        workflow.store.checkpoint(rid, 'plan', changed)
    before = workflow.store.get(rid, 'plan')
    if bad:
        with pytest.raises(AA2Error, match='Retained activation'):
            await workflow.recovery.resume(rid)
        assert not workflow.store.get(rid, key).get('authority_reassessment')
    else:
        await workflow.recovery.resume(rid)
        assert workflow.store.get(rid, key)['authority_reassessment']['phase'] == 'activated'
        assert workflow.store.get(rid, 'recovery/active') is None
        assert sum(stage == 'plan' for stage, _ in agent.views) == 1
    assert workflow.store.get(rid, 'plan_revision/current')['revision'] == 2
    assert workflow.store.get(rid, 'plan') == before
    assert workflow.store.get(rid, 'rounds/T3') == {'round': 17}


@pytest.mark.parametrize('verdict', ['reject', 'source_dispute'])
async def test_completed_activation_outcome_survives_new_negative_reassessment(tmp_path, monkeypatch, verdict):
    from authority_review_fixtures import authority_review

    from auto_agents_2.engine import Replan
    workflow, rid, _, _, record, agent = setup_correction(tmp_path)
    complete = workflow.recovery_policy.complete
    def stop_after_completion(run, key, outcome):
        complete(run, key, outcome)
        if outcome.get('status') == 'applied':
            raise asyncio.CancelledError()
    with monkeypatch.context() as stopped:
        stopped.setattr(workflow.recovery_policy, 'complete', stop_after_completion)
        with pytest.raises(asyncio.CancelledError):
            await workflow.recovery.resume(rid)
    key = f'plan_revision/journal/{record.id}/1'
    journal = workflow.store.get(rid, key)
    journal['review'].pop('authority')
    admission = journal['evidence_admission']
    admission.pop('authority')
    admission['review_hash'] = digest(journal['review'])
    admission['binding'] = digest({k: v for k, v in admission.items() if k != 'binding'})
    journal['contract_review_binding'] = contract_review_binding(journal)
    workflow.store.checkpoint(rid, key, journal)
    active = workflow.store.get(rid, 'recovery/active')
    permit_key = active['permit']
    permit = workflow.store.get(rid, permit_key)
    assert permit['phase'] == 'completed' and permit['outcome']['status'] == 'applied'
    workflow.store.checkpoint(rid, 'authority/protocol', 1)
    async def negative(stage, context, schema, *args, **kwargs):
        assert stage == 'review' and context['feedback']['journal_phase'] == 'activated'
        result = authority_review(context, approved=False, summary='New independently grounded concern')
        if verdict == 'source_dispute':
            proof = result.authority['findings'][0]
            proof.update(kind='source_dispute', owner='plan', method_source='accepted_plan',
                         method_quote=context['plan']['document'])
            next(c for c in result.authority['coverage'] if c['criterion_id'] == proof['criterion_id'])['status'] = 'unverified'
        return result
    agent.invoke = negative
    if verdict == 'source_dispute':
        with pytest.raises(Replan) as raised:
            await workflow.recovery.resume(rid)
        assert raised.value.stage == 'plan'
        assert workflow.store.get(rid, 'replanning/pending')['stage'] == 'plan'
    else:
        await workflow.recovery.resume(rid)
        successor = workflow.store.get(rid, 'recovery/active')
        assert successor['attempt'] == active['attempt'] + 1
        assert successor['failure']['evidence']['patch_review']['approved'] is False
    assert workflow.store.get(rid, permit_key) == permit
    assert workflow.store.get(rid, key)['phase'] == 'activated'
    assert workflow.store.get(rid, 'plan_revision/current')['revision'] == 2
    assert workflow.store.get(rid, 'rounds/T3') == {'round': 17}


@pytest.mark.parametrize('bad', ['author_missing', 'author_changed', 'review_missing', 'role_swap',
                                 'unknown_author', 'unknown_review', 'proposal_fact', 'stale_protocol'])
async def test_activation_rejects_missing_changed_or_wrong_authority_inputs(tmp_path, monkeypatch, bad):
    workflow, rid, _, old, record, agent = setup_correction(tmp_path)
    cite_roles(agent, author=['plan.tasks[2].checks[0]', 'recovery_decision'],
               reviewer=['patch.contract_corrections[0]', 'controller_stage_contract'])
    identity, key, journal = await prepare(workflow, rid, record, monkeypatch)
    if bad == 'author_missing':
        journal.pop('contract_author_context')
    elif bad == 'author_changed':
        journal['contract_author_context']['recovery_decision']['reason'] = 'changed'
    elif bad == 'review_missing':
        journal['contract_review_context'].pop('patch')
    elif bad == 'role_swap':
        journal['contract_author_context']['plan'] = copy.deepcopy(journal['plan'])
        journal['contract_review_binding'] = contract_review_binding(journal)
    elif bad == 'unknown_author':
        journal['patch']['contract_corrections'][0]['evidence_refs'] += ['no_such_source']
    elif bad == 'unknown_review':
        journal['review']['contract_reviews'][0]['evidence_refs'] += ['no_such_source']
    elif bad == 'proposal_fact':
        # Counts and a hash embedded in the candidate cannot replace the old plan.
        journal['review']['contract_reviews'][0]['evidence_refs'] = [
            'plan.tasks[2].checks[0]', 'original_requirements', 'failure']
    elif bad == 'stale_protocol':
        for name in ('contract_author_context', 'contract_review_context'):
            journal[name]['controller_stage_contract']['hash'] = '0' * 64
    if bad in {'unknown_author', 'unknown_review', 'proposal_fact', 'stale_protocol'}:
        journal['contract_review_context']['patch'] = copy.deepcopy(journal['patch'])
        journal['contract_review_binding'] = contract_review_binding(journal)
    workflow.store.checkpoint(rid, key, journal)
    with pytest.raises(AA2Error):
        workflow.store.activate_plan_patch(rid, identity)
    assert workflow.store.get(rid, 'plan') == old.model_dump()
    assert workflow.store.get(rid, key)['phase'] == 'prepared'
    assert workflow.store.get(rid, 'rounds/T3') == {'round': 17}


def archive_legacy_inputs(workflow, rid, work, identity, journal):
    """Render the actual inputs through the real projector, then reproduce v2's loss."""
    for role, operation in [('author', 'recovery/patch/'), ('review', 'recovery/patch-review/')]:
        context = journal['contract_' + role + '_context']
        prepared = ContextPlanner(workflow.artifacts).prepare(
            'plan' if role == 'author' else 'review', context, work, operation + identity, run_id=rid)
        ref = workflow.artifacts.put(prepared.record(), 'prepared_context')
        workflow.store.checkpoint(rid, 'presentation/' + operation + identity + '/' + digest(context),
                                  {'identity': digest(context), 'artifact': ref})
    journal = copy.deepcopy(journal)
    journal['version'] = 2
    journal.pop('contract_author_context')
    journal['contract_review_context'] = {name: journal['contract_review_context'][name] for name in (
        'controller_correction_contract', 'correction_request', 'original_capture',
        'original_requirements', 'specification', 'previous_plan', 'failure', 'design', 'source_history')}
    journal['contract_review_binding'] = contract_review_binding(journal)
    workflow.store.checkpoint(rid, 'plan_revision/journal/' + identity, journal)
    return journal


@pytest.mark.parametrize('bad', [None, 'base_missing', 'base_changed', 'receipt_missing',
                                'presentation_missing', 'artifact_changed', 'unretained_source', 'stale_capture'])
async def test_legacy_prepared_replay_requires_original_frozen_evidence(tmp_path, monkeypatch, bad):
    workflow, rid, work, old, record, agent = setup_correction(tmp_path)
    cite_roles(agent, author=['plan.tasks[2].checks[0]', 'recovery_decision'],
               reviewer=['patch.contract_corrections[0]', 'recovery_decision', 'controller_stage_contract'])
    identity, key, journal = await prepare(workflow, rid, record, monkeypatch)
    legacy = archive_legacy_inputs(workflow, rid, work, identity, journal)
    base_key = 'recovery/patch-context/' + identity
    if bad == 'base_missing':
        workflow.store.checkpoint(rid, base_key, None)
    elif bad == 'base_changed':
        base = workflow.store.get(rid, base_key)
        base['recovery_decision']['reason'] = 'not the originally supplied objective'
        workflow.store.checkpoint(rid, base_key, base)
    elif bad == 'receipt_missing':
        workflow.store.checkpoint(rid, 'invocation/recovery/patch-review/' + identity, {})
    elif bad in {'presentation_missing', 'artifact_changed'}:
        with workflow.store.connect() as db:
            entries = list(db.execute('SELECT key,data FROM checkpoints WHERE run_id=? AND key LIKE ?',
                (rid, 'presentation/recovery/patch-review/' + identity + '/%')))
        import json
        for name, data in entries:
            ref = json.loads(data)['artifact']
            path = workflow.artifacts.root / ref['hash'][:2] / ref['hash']
            if bad == 'presentation_missing':
                workflow.store.checkpoint(rid, name, {})
            else:
                path.write_text('{}')  # Isolated fixture: hash corruption must fail closed.
    elif bad == 'unretained_source':
        legacy['review']['contract_reviews'][0]['evidence_refs'] += ['project.workspace']
        legacy['contract_review_binding'] = contract_review_binding(legacy)
        workflow.store.checkpoint(rid, key, legacy)
        receipt = workflow.store.get(rid, 'invocation/recovery/patch-review/' + identity)
        receipt['result'] = copy.deepcopy(legacy['review'])
        workflow.store.checkpoint(rid, 'invocation/recovery/patch-review/' + identity, receipt)
    elif bad == 'stale_capture':
        workflow.store.checkpoint(rid, 'capture', {'artifact': {'hash': '0' * 64}})
    if bad:
        with pytest.raises(AA2Error):
            await workflow.recovery.resume(rid)
        assert workflow.store.get(rid, key)['phase'] == 'prepared'
        assert workflow.store.get(rid, 'plan') == old.model_dump()
    else:
        await workflow.recovery.resume(rid)
        workflow.store.activate_plan_patch(rid, identity)
        assert workflow.store.get(rid, 'plan_revision/current')['revision'] == 2
        activated = workflow.store.get(rid, key)
        assert {**activated, 'phase': 'prepared'} == legacy
    assert [stage for stage, _ in agent.views] == ['plan', 'review']
    assert workflow.store.get(rid, 'rounds/T3') == {'round': 17}
