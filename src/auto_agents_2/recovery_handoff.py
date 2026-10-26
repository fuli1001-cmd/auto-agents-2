"""Bounded diagnostic inputs from controller records, never new action authority."""
import copy
import json

from .faults import StateConflict
from .util import digest

HANDOFF_FIELDS = ('diagnosis_request', 'history', 'previous_failure_observations')


def diagnosis_handoff(engine, rid, failure, permit_key, operation):
    store = engine.store
    frozen = store.get(rid, 'recovery/invocation-inputs/' + digest(['recovery_diagnosis', operation]))
    if frozen is not None:
        # Both the invocation and its adjudicator must see the same handoff.
        # In particular an old interrupted call retains the ABSENCE of new fields.
        return {name: copy.deepcopy(frozen['context'][name]) for name in HANDOFF_FIELDS
                if name in frozen['context']}
    permit = store.get(rid, permit_key) if permit_key else None
    if permit_key and (not permit or permit.get('owner') != failure.origin_id or
            permit.get('action', {}).get('action') != 'diagnose' or
            permit['action'].get('failure_id') != failure.id):
        raise StateConflict('Diagnostic handoff does not match the reserved action')
    request = {'permit': permit_key, 'owner': failure.origin_id, 'action': 'diagnose',
               'authority': 'existing_action_permit' if permit else 'existing_published_delivery_boundary'}
    if permit:
        request.update({name: permit[name] for name in ('cycle_id', 'source_hash', 'plan_hash')})
        request['proposal'] = {name: permit.get(name, '') for name in ('reason', 'expected_result')}
    history = store.recovery_history(rid, failure.origin_id)
    history['recent'] = [
        {**{name: row.get(name) for name in ('cycle_id', 'action', 'actor', 'phase', 'source_hash',
                                           'plan_hash', 'outcome', 'outcome_hash')},
         'proposal': {name: row.get(name, '') for name in ('reason', 'expected_result')}}
        for row in history['recent']]
    # Scope is determined from controller checkpoints, never from model paths or
    # requested hashes. These are historical observations, not current acceptance.
    with store.connect() as db:
        rows = db.execute("SELECT key,data FROM checkpoints WHERE run_id=? AND key LIKE ? ORDER BY rowid",
                          (rid, f'recovery/failures/{failure.id}/%')).fetchall()
        if failure.origin_id == 'delivery' and failure.component == 'delivery':
            rows += db.execute("SELECT key,data FROM checkpoints WHERE run_id=? AND (key LIKE 'recovery/delivery-policy/%' OR key LIKE 'recovery/delivery/%') ORDER BY rowid",
                               (rid,)).fetchall()
    previous, seen = [], set()
    for row in rows:
        original = json.loads(row['data'])
        if row['key'].startswith(('recovery/delivery-policy/', 'recovery/delivery/')):
            original = original.get('failure', {})
        value = original
        if (value.get('id'), value.get('origin_id'), value.get('task_id')) != (
                failure.id, failure.origin_id, failure.task_id):
            continue
        if failure.observation_id and value.get('observation_id') == failure.observation_id:
            continue
        # Do not bring arbitrary model-owned attachments into the material graph.
        # Model diagnoses already have a typed, proposal-marked history slot.
        value = {name: item for name, item in value.items() if name not in {'proposal', 'evidence'}}
        value['evidence'] = machine_observations(original.get('evidence', {}))
        identity = value.get('observation_id') or digest(value)
        if identity in seen:
            continue
        seen.add(identity)
        previous.append({'checkpoint': row['key'], 'status': 'historical_observation', 'failure': value})
    return {'diagnosis_request': request, 'history': history,
            'previous_failure_observations': {'total': len(previous), 'recent': previous[-16:],
                'older_observations': max(0, len(previous) - 16)}}


def machine_observations(evidence):
    """Keep the existing check_rows envelopes, not arbitrary model attachment roots."""
    if not isinstance(evidence, dict):
        return {}
    selected = {name: value for name, value in evidence.items()
                if name in {'checks', 'environment_error', 'environment_failure', 'candidate_error'}}
    for name in ('runtime_failure', 'feedback'):
        nested = evidence.get(name)
        if isinstance(nested, dict):
            selected[name] = {key: value for key, value in nested.items()
                              if key in {'checks', 'environment_error', 'environment_failure', 'candidate_error'}}
    return selected
