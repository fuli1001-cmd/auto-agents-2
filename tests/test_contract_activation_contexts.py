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
        if schema is EvidencePatchReview:
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
