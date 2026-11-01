"""Bounded diagnostic inputs from controller records, never new action authority."""
import copy
import json

from .faults import StateConflict
from .util import digest

HANDOFF_FIELDS = ('diagnosis_request', 'history', 'previous_failure_observations', 'current_task_results')


def diagnosis_handoff(engine, rid, failure, permit_key, operation, source_hash=None):
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
            'current_task_results': task_result_observations(store, rid, failure,
                source_hash or (permit or {}).get('source_hash')),
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


def task_result_observations(store, rid, failure, source_hash):
    """Current owner/dependency checkpoints, not a second acceptance or cache policy."""
    plan = store.get(rid, 'plan') or {}
    tasks = {task['id']: task for task in plan.get('tasks', [])}
    # The controller's accepted graph fixes scope; model repair targets and old
    # diagnoses cannot select attachments or add tasks to this observation.
    selected, pending = set(), ([failure.task_id] if failure.task_id else list(tasks))
    while pending:
        task_id = pending.pop()
        if task_id in selected or task_id not in tasks:
            continue
        selected.add(task_id)
        pending.extend(tasks[task_id].get('depends_on', []))
    done = set(store.get(rid, 'develop/done', []))
    rows = []
    for task_id, task in tasks.items():
        if task_id not in selected:
            continue
        key = 'develop/task/' + task_id
        saved = store.get(rid, key) or {}
        state = store.get(rid, 'task_state/' + task_id) or {}
        row = {'task_id': task_id, 'depends_on': task.get('depends_on', []),
               'task_hash': digest(task), 'planned_checks': task.get('checks', []),
               'checkpoint': key, 'checkpoint_hash': digest(saved), 'done': task_id in done,
               'state': {k: state[k] for k in ('version', 'revision', 'implementation', 'evidence') if k in state},
               'result': result_observation(store, rid, saved, key, source_hash, 'current_checkpoint')}
        # Reopening preserves earlier observations. Supply the nearest result
        # without pretending its old approval makes the reopened task done.
        previous, path = saved.get('previous_evidence'), key
        while isinstance(previous, dict):
            path += '.previous_evidence'
            if previous.get('checks') or previous.get('review'):
                row['previous_result'] = result_observation(
                    store, rid, previous, path, source_hash, 'historical_observation')
                break
            previous = previous.get('previous_evidence')
        rows.append(row)
    return {'version': 1, 'source_hash': source_hash, 'plan_hash': digest(plan),
            'scope': 'failure task and transitive dependencies; all plan tasks for delivery',
            'tasks': rows,
            'meaning': 'Compare these current controller checkpoints with older failure history before proposing recovery. '
                       'Read complete checks and independent review records for relevant tasks. A retained previous_result '
                       'is historical, and a review proposal or source match alone is not current acceptance. '
                       'Different whole-tree source hashes do not by themselves require rerunning every check: compare '
                       'the declared check inputs, command/environment bindings and relevant source changes. '
                       'Do not recheck merely because older recovery history omits a supplied newer result. '
                       'Existing evidence-reuse, authorization, independent review and task acceptance gates still apply.'}


def result_observation(store, rid, saved, key, current_source, status):
    source = saved.get('source_hash')
    out = {k: copy.deepcopy(saved[k]) for k in ('phase', 'revision', 'commit', 'source_hash', 'checks') if k in saved}
    out.update(checkpoint=key, checkpoint_hash=digest(saved), status=status,
               source_relation=('same' if source == current_source else 'different')
                   if source and current_source else 'unknown')
    review = saved.get('review')
    if isinstance(review, dict):
        review_hash = digest(review)
        receipt_key = 'authority/reviews/' + review_hash
        record = store.get(rid, receipt_key)
        matches = bool(record and record.get('review') == review and record.get('review_hash') == review_hash)
        receipt = {'checkpoint': receipt_key,
                   'status': 'matching_record' if matches else 'mismatched' if record else 'missing'}
        if matches:
            basis = record.get('basis', {})
            receipt.update({k: record[k] for k in ('version', 'operation', 'review_hash') if k in record})
            receipt.update({k: basis[k] for k in ('scope', 'source_hash', 'candidate_hash', 'source_identity') if k in basis})
            receipt['basis_hash'] = basis.get('hash')
        # The receipt is a controller fact; the model's complete verdict remains
        # a proposal. Its prose and cited hashes must not mint factual evidence.
        out['review'] = {'review_hash': review_hash, 'receipt': receipt, 'proposal': copy.deepcopy(review)}
    return out
