"""Diagnosis catalogs offer proposals; only validated, bound decisions can be authorized."""
import copy

import pytest
from pydantic import ValidationError
from test_final_task_repair_route import final_failure, repair
from test_recovery_policy import ready

from auto_agents_2.faults import StateConflict
from auto_agents_2.recovery import Diagnosis, RecoveryDecision, adjudicate


@pytest.mark.parametrize('owner', ['task', 'final'])
def test_catalog_has_proposal_authority_and_cannot_deserialize_as_decision(tmp_path, owner):
    workflow, rid, work, plan, failure = ready(tmp_path, record=final_failure() if owner == 'final' else None)
    proposals = workflow.recovery_policy.diagnosis_actions(rid, failure, work, plan)
    assert 'repair' in proposals
    for name, row in proposals.items():
        assert row['authority'] == 'proposal_only' and row['action'] == name
        assert row['owner'] and row['requires']
        assert not {'failure_id', 'budget_owner', 'source_hash', 'plan_hash', 'permit'} & row.keys()
        with pytest.raises(ValidationError):
            RecoveryDecision.model_validate(row)
    if owner == 'final':
        assert proposals['repair']['task_ids'] == []
        choices, _, _ = workflow.recovery_policy.available_actions(rid, failure, work, plan, adjudicate(failure))
        assert 'repair' not in choices  # It is not yet a scoped execution candidate.


@pytest.mark.parametrize('owner,grounded', [('task', True), ('final', True), ('final', False), ('delivery', True)])
async def test_actual_controller_diagnosis_callers_share_proposal_contract(tmp_path, owner, grounded):
    from test_diagnosis_decisions import proposal_failure, setup
    from test_final_task_repair_route import ready as reviewed_ready

    if owner == 'delivery':
        workflow, rid = setup(tmp_path)
        (tmp_path / 'value.py').write_text('value = 1\n')
    else:
        fixture = reviewed_ready if owner == 'final' and grounded else ready
        workflow, rid, work, plan, failure = fixture(tmp_path,
            record=final_failure() if owner == 'final' else proposal_failure())
    actual = workflow.agent.invoke
    seen = []
    async def invoke(stage, context, *args, **kwargs):
        if stage != 'recovery_diagnosis':
            return await actual(stage, context, *args, **kwargs)
        seen.append(copy.deepcopy(context['allowed_actions']))
        assert all(row['authority'] == 'proposal_only' for row in seen[-1].values())
        assert all('budget_owner' not in row for row in seen[-1].values())
        if owner == 'final':
            return repair()
        return Diagnosis(action='recheck', task_ids=[] if owner == 'delivery' else [failure.task_id],
            evidence_refs=['failure.evidence'], reason='Collect existing checks, without changing their contract')
    workflow.agent.invoke = invoke
    if owner == 'delivery':
        assert await workflow.recovery.delivery_decision(rid, tmp_path,
            {'review': {'approved': False, 'summary': 'Need startup evidence', 'findings': []}}, ['value.py']) == 'recheck'
    elif owner == 'final' and not grounded:
        # Diagnosis may inspect the old verdict, but it cannot turn that verdict
        # into execution authority. Keep the original raw-input refusal case.
        with pytest.raises(StateConflict, match='ungrounded review'):
            await workflow.recovery.resume(rid)
        assert workflow.store.get(rid, 'recovery/active') is not None
        assert workflow.store.get(rid, 'plan') == plan.model_dump()
    else:
        await workflow.recovery.resume(rid)
        assert workflow.store.get(rid, 'recovery/active') is None
    assert len(seen) == 1 and not workflow.store.pending(rid)


@pytest.mark.parametrize('entry', ['suggestion', 'candidates', 'dictionary'])
async def test_proposal_cannot_reserve_a_permit_or_consume_a_budget(tmp_path, entry):
    from auto_agents_2.recovery import RecoveryProposal

    workflow, rid, work, plan, failure = ready(tmp_path)
    policy = workflow.recovery_policy
    proposal = RecoveryProposal.model_validate(policy.diagnosis_actions(rid, failure, work, plan)['diagnose'])
    decision = adjudicate(failure)
    active = workflow.store.get(rid, 'recovery/active')
    with workflow.store.connect() as db:
        before = list(db.execute('SELECT key,data FROM checkpoints WHERE run_id=? ORDER BY key', (rid,)))
        before = [tuple(row) for row in before]
    kwargs = {'candidates': {'diagnose': proposal}} if entry == 'candidates' else {}
    suggested = decision if entry == 'candidates' else proposal.model_dump() if entry == 'dictionary' else proposal
    with pytest.raises(StateConflict, match='validated recovery decisions'):
        await policy.authorize(rid, failure, active, work, plan, suggested, operation='not-a-permit', **kwargs)
    with workflow.store.connect() as db:
        assert [tuple(row) for row in db.execute(
            'SELECT key,data FROM checkpoints WHERE run_id=? ORDER BY key', (rid,))] == before
    assert not workflow.agent.calls and not workflow.store.pending(rid)


def test_legacy_allowed_action_values_remain_membership_only_and_do_not_enlarge_scope(tmp_path):
    workflow, rid, work, plan, failure = ready(tmp_path, record=final_failure())
    legacy = {'repair': {'action': 'repair', 'task_ids': [], 'repair_targets': []}}
    context = {'failure': failure.model_dump(), 'plan': plan.model_dump(), 'allowed_actions': legacy}
    decision = adjudicate(failure, diagnosis=repair(), plan=plan, evidence_context=context, workspace=work)
    assert decision.task_ids == ['T3'] and decision.repair_targets == repair().repair_targets
    assert legacy == {'repair': {'action': 'repair', 'task_ids': [], 'repair_targets': []}}


async def test_completed_legacy_catalog_is_not_rewritten_or_rediagnosed_on_resume(tmp_path, monkeypatch):
    from test_diagnosis_decisions import pending_completed_diagnosis

    from auto_agents_2.recovery_policy import RecoveryPolicy
    original = RecoveryPolicy.proposal_actions
    monkeypatch.setattr(RecoveryPolicy, 'proposal_actions', staticmethod(
        lambda choices: {name: value.model_dump() for name, value in choices.items()}))
    workflow, rid, work, contract, active, invocation_key = await pending_completed_diagnosis(tmp_path, monkeypatch)
    permit_key = active['diagnosis_permit']
    receipt = workflow.store.get(rid, permit_key)
    invocation = workflow.store.get(rid, invocation_key)
    with workflow.store.connect() as db:
        keys = [row[0] for row in db.execute(
            "SELECT key FROM checkpoints WHERE run_id=? AND key LIKE 'recovery/invocation-inputs/%'", (rid,))]
    frozen = {key: workflow.store.get(rid, key) for key in keys}
    assert frozen and any('failure_id' in value['context']['allowed_actions']['diagnose'] for value in frozen.values())
    monkeypatch.setattr(RecoveryPolicy, 'proposal_actions', staticmethod(original))
    await workflow.recovery.resume(rid)
    assert workflow.store.get(rid, permit_key) == receipt
    assert workflow.store.get(rid, invocation_key) == invocation
    assert {key: workflow.store.get(rid, key) for key in keys} == frozen
    assert workflow.agent.calls == [('recovery_diagnosis', True), ('decision', True)]
    assert workflow.store.get(rid, 'recovery/budgets/diagnosis/T3')['round'] == 1
    assert workflow.store.get(rid, 'develop/task/T3')['phase'] == 'reverify'
