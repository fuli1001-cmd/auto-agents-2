"""Admit evidenced task npm failures to diagnosis, without granting a retry or edit."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .environment import EnvironmentManager
from .errors import AA2Error
from .faults import StateConflict
from .installation_failure import MAX_LOG_BYTES, MAX_LOG_FILES, MAX_TOTAL_BYTES
from .models import PlanResult
from .profiles import ProjectProfile
from .util import digest
from .workspace import git, path_allowed, tree_hash

PREFIX = 'environment/resolution-diagnosis/'
WAIT = 'environment/prerequisite-wait'
QUEUE = 'environment/prerequisite-queue'
OBSERVATIONS = 'environment/prerequisite-observations/'
# These are external prerequisites, not dependency choices. Unknown text is not
# classified as a dependency defect; this list only conservatively excludes it.
PREREQUISITE = re.compile(
    r'\b(?:EACCES|EPERM|E401|E403|ENEEDAUTH|EAUTH|EINTEGRITY|ENOSPC|EDQUOT|EROFS|'
    r'ECONNREFUSED|ENOTFOUND|EAI_AGAIN|ECONNRESET|ETIMEDOUT|CERT_HAS_EXPIRED|'
    r'SELF_SIGNED_CERT_IN_CHAIN|DEPTH_ZERO_SELF_SIGNED_CERT|'
    r'UNABLE_TO_VERIFY_LEAF_SIGNATURE|UNABLE_TO_GET_ISSUER_CERT_LOCALLY)\b|'
    r'permission denied|authentication required|unauthorized|forbidden|'
    r'certificate verify failed|unable to verify.*certificate|no space left|read-only file system', re.I)


def _logs(engine, rid, evidence):
    debug = evidence.get('npm_debug', {})
    streams = evidence.get('logs', [])
    if (not isinstance(debug, dict) or not isinstance(streams, list) or len(streams) != 2 or debug.get('status') not in {'captured', 'not_recorded'}
            or debug.get('unavailable') or not isinstance(debug.get('logs'), list)
            or bool(debug['logs']) != (debug['status'] == 'captured')
            or len(debug['logs']) > MAX_LOG_FILES):
        return False
    total = 0
    for ref in [*streams, *debug['logs']]:
        if not isinstance(ref, dict):
            return False
        sha, size = ref.get('hash'), ref.get('bytes')
        if (not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha)
                or ref.get('kind') != 'dependency_log' or type(size) is not int
                or not 0 <= size <= MAX_LOG_BYTES):
            return False
        total += size
        if total > MAX_TOTAL_BYTES:
            return False
        path = engine.store.root / 'artifacts' / sha[:2] / sha
        if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size != size:
            return False
        with engine.store.connect() as db:
            if not db.execute('SELECT 1 FROM artifact_owners WHERE hash=? AND run_id=?', (sha, rid)).fetchone():
                return False
        raw = path.read_bytes()
        if digest(raw) != sha or PREREQUISITE.search(raw.decode('utf-8')):
            return False
    return True


def eligible(engine, rid, pending):
    """Read-only, fail-closed admission. No reconstructed or model-supplied facts."""
    try:
        if not isinstance(pending, dict) or not isinstance(pending.get('requirement'), dict):
            return None
        requirement = pending['requirement']
        evidence = requirement['evidence']
        if not isinstance(evidence, dict):
            return None
        if (pending.get('version') != 1 or pending.get('reason_code') != 'ENVIRONMENT_COMMAND_ERROR'
                or pending.get('phase') != 'develop' or requirement.get('kind') != 'external_input'
                or pending.get('identity') != digest([pending['reason_code'], pending['phase'], requirement])
                or engine.store.get(rid, OBSERVATIONS + pending['identity']) != pending
                or evidence.get('version') != 2 or evidence.get('authority') != 'external_command_observation'
                or evidence.get('classification') != 'unknown' or evidence.get('capture_status') != 'complete'
                or evidence.get('outcome') != 'nonzero_exit' or type(evidence.get('exit_code')) is not int
                or evidence['exit_code'] == 0 or PREREQUISITE.search(requirement['reason'])):
            return None
        plan = PlanResult.model_validate(engine.store.get(rid, 'plan'))
        binding = evidence['controller_task']
        task = next(t for t in plan.tasks if t.id == binding['task_id'])
        saved = engine.store.get(rid, 'develop/task/' + task.id, {})
        work = Path(saved['workspace'])
        owned = engine.store.root / 'worktrees' / rid
        if (not work.is_absolute() or work.is_symlink() or not work.resolve().is_relative_to(owned.resolve())
                or saved.get('commit') or saved.get('phase') not in {'applied', 'tested'}
                or binding != {'task_id': task.id, 'origin_id': task.origin_id or task.id,
                    'plan_hash': digest(plan.model_dump()), 'task_hash': digest(task.model_dump()),
                    'base': git(work, 'rev-parse', 'HEAD'), 'source_hash': saved.get('source_hash'),
                    'workspace': str(work),
                    'task_revision': engine.store.get(rid, 'task_state/' + task.id, {}).get('revision')}
                or not saved.get('source_hash') or tree_hash(work) != saved['source_hash']):
            return None
        argv = evidence['argv']
        if not isinstance(argv, list) or argv[1:] not in [['install', '--package-lock-only', '--ignore-scripts'], ['ci']]:
            return None
        npm = Path(argv[0])
        prefix = npm.parent.parent
        if (npm.name != 'npm' or npm.parent.name != 'bin' or not npm.is_file()
                or prefix.parent != engine.store.root / 'environments'
                or prefix.is_symlink() or prefix.resolve().parent != (engine.store.root / 'environments').resolve()
                or EnvironmentManager.installed_identity(prefix) != evidence.get('runtime_identity')
                or not EnvironmentManager.valid(prefix, evidence['runtime_identity'])):
            return None
        roots = ProjectProfile.detect(work, engine.config.load().project_profile).node_roots
        cwd = Path(evidence['cwd'])
        if argv[1:] == ['ci']:
            selected = [p for p in roots if p == cwd and (p / 'package-lock.json').is_file()]
        else:
            if (cwd.name != 'resolve' or cwd.parent.parent != engine.store.root / 'tmp'
                    or not re.fullmatch('node_[0-9a-f]+', cwd.parent.name)):
                return None
            # The temporary copy may already have been removed. Multiple roots
            # cannot be disambiguated from a legacy observation; do not guess.
            selected = list(roots) if len(roots) == 1 and not (roots[0] / 'package-lock.json').exists() else []
        if len(selected) != 1:
            return None
        for name in ['package.json', *(['package-lock.json'] if argv[1:] == ['ci'] else [])]:
            manifest = selected[0] / name
            relative = manifest.relative_to(work).as_posix()
            if (manifest.is_symlink() or manifest.resolve() != work.resolve() / relative
                    or not path_allowed(relative, task.allowed_paths)
                    or not isinstance(json.loads(manifest.read_text()), dict)):
                return None
        if not _logs(engine, rid, evidence):
            return None
        return plan, task, saved, work
    except (OSError, ValueError, TypeError, KeyError, StopIteration, IndexError, AA2Error):
        return None


def admit(engine, rid, pending):
    """Atomically replace one *unanswered* wait with a diagnosis-only failure."""
    store = engine.store
    identity = pending['identity']
    receipt_key = PREFIX + identity
    if store.get(rid, receipt_key):
        raise StateConflict('A converted installation observation cannot be replayed as a new prerequisite')
    if store.get(rid, 'recovery/active'):
        return False
    checked = eligible(engine, rid, pending)
    if not checked:
        return False
    plan, task, saved, work = checked
    task_key = 'develop/task/' + task.id
    queue = store.get(rid, QUEUE, {})
    current = store.get(rid, WAIT)
    if queue.get(identity) != pending or current not in (None, pending):
        return False
    answer_key = 'prerequisite/' + identity
    attempt = store.get(rid, answer_key, 0)
    question_keys = [f'{answer_key}/{attempt}']
    questions = store.resolution_wait_questions(rid, question_keys)
    if any(q['answer'] is not None or q['artifact_hash'] != identity for q in questions):
        return False
    guarded_keys = [task_key, 'task_state/' + task.id, 'plan', 'plan_revision/current',
                    QUEUE, WAIT, OBSERVATIONS + identity, answer_key, 'recovery/active', receipt_key]
    guards = {key: store.get(rid, key) for key in guarded_keys}
    if guards[task_key] != saved or guards['plan'] != plan.model_dump() or guards[QUEUE] != queue or guards[WAIT] != current:
        raise StateConflict('Resolution admission changed while its evidence was being checked')
    failed = {**saved, 'phase': 'rejected', 'environment_error': pending['requirement']['reason'],
              'environment_failure': pending['requirement']['evidence']}
    failure = engine._task_failure(rid, task, failed, kind='environment', step='environment', record_outcome=False)
    marker = {'version': 1, 'receipt': receipt_key, 'observation': identity}
    failure = failure.model_copy(update={'evidence': {**failure.evidence, 'resolution_diagnosis': marker}})
    count_key = 'recovery/count/' + failure.id
    guards[count_key] = store.get(rid, count_key)
    number = (guards[count_key] or 0) + 1
    failure = failure.model_copy(update={'observation_id': digest([rid, failure.id, number])})
    updates = {}
    previous_key = saved.get('recovery_permit')
    if previous_key:
        previous = engine.recovery_policy.validate_consumption(rid, previous_key, expected={'repair', 'recheck'})
        if (not previous or previous.get('phase') != 'started' or task.id not in previous.get('task_scopes', {})
                or previous['task_scopes'][task.id] != task.allowed_paths
                or previous.get('plan_hash') != digest(plan.model_dump())
                or task.id not in previous.get('outcome', {}).get('expected_tasks', [])
                or task.id in previous.get('outcome', {}).get('tasks', {})):
            raise StateConflict('Unknown installation failure has no matching unfinished task permit')
        updates.update(engine.recovery_policy.task_outcome_updates(rid, saved, task.id,
            {'status': 'failed', 'kind': 'environment', 'evidence': task_key, 'observation': digest(failure.evidence)}))
        if previous_key not in updates:
            raise StateConflict('Installation failure did not close its dispatched task result')
        guards.update({key: store.get(rid, key) for key in updates})
        guards[previous_key] = previous
    history_key = f'recovery/failures/{failure.id}/{number}'
    archive_key = 'candidate/environment-failures/' + task.id + '/unknown-' + identity
    guards.update({history_key: store.get(rid, history_key), archive_key: store.get(rid, archive_key)})
    if guards[history_key] is not None or guards[archive_key] is not None:
        raise StateConflict('Installation observation already has a failure history')
    updates.update({task_key: failed, archive_key: failed,
        QUEUE: {k: v for k, v in queue.items() if k != identity}, WAIT: None if current == pending else current,
        'recovery/active': {'version': 2, 'failure': failure.model_dump(), 'attempt': number},
        history_key: failure.model_dump(), count_key: number})
    record = {**marker, 'failure': failure.model_dump(), 'attempt': number,
              'source_hash': saved['source_hash'], 'plan_hash': digest(plan.model_dump()),
              'task_key': task_key, 'questions': questions, 'previous_permit': previous_key,
              'allowed_initial_action': 'diagnose'}
    if tree_hash(work) != saved['source_hash']:
        raise StateConflict('Installation evidence source changed during admission')
    store.admit_resolution_diagnosis(rid, receipt_key, record, guards, updates, question_keys, questions)
    return True


def _receipt(policy, rid, failure):
    marker = failure.evidence.get('resolution_diagnosis')
    if not marker:
        return None
    if not isinstance(marker, dict) or not isinstance(marker.get('receipt'), str) or not marker['receipt'].startswith(PREFIX):
        raise StateConflict('Unknown resolution failure has malformed admission metadata')
    record = policy.store.get(rid, marker.get('receipt', ''), {})
    if not isinstance(record, dict) or not isinstance(record.get('failure'), dict):
        raise StateConflict('Unknown resolution failure lost its original admission')
    original = record['failure']
    if (marker.get('version') != 1 or record.get('allowed_initial_action') != 'diagnose'
            or original.get('id') != failure.id or original.get('observation_id') != failure.observation_id
            or original.get('evidence', {}).get('resolution_diagnosis') != marker):
        raise StateConflict('Unknown resolution failure lost its diagnosis admission receipt')
    return record


def authorization_proof(policy, rid, failure, active, work, plan, operation):
    """None means no special route; {} means diagnosis is the sole first action."""
    record = _receipt(policy, rid, failure)
    if record is None:
        return None
    if operation == 'patch-followup':
        key = active.get('patch_permit')
        previous = policy.store.get(rid, key, {}) if key else {}
        outcome = policy.completed_outcome(rid, key, action='plan_patch')
        if previous:
            validate_permit(policy, rid, previous)
        journal_key = f"plan_revision/journal/{failure.id}/{active['attempt']}"
        journal = policy.store.get(rid, journal_key, {})
        if (not previous or previous.get('phase') != 'completed' or not outcome or outcome.get('status') != 'applied'
                or previous['cycle_id'] != policy.cycle_id(failure, active) or journal.get('phase') != 'activated'):
            raise StateConflict('Plan continuation requires its completed patch and activated journal')
        return {'patch_permit': key, 'patch_hash': digest(previous), 'journal': journal_key, 'journal_hash': digest(journal)}
    key = active.get('diagnosis_permit')
    if not key or policy.completed_diagnosis(rid, key, failure, active, work, plan, allow_invalid=True) is None:
        return {}
    return {'diagnosis_permit': key, 'diagnosis_hash': digest(policy.store.get(rid, key))}


def validate_permit(policy, rid, permit):
    """Consume the immutable proof, also after a dispatched edit changed source."""
    from .recovery import FailureRecord
    raw = permit.get('authority_failure', {})
    if not raw.get('evidence', {}).get('resolution_diagnosis'):
        if permit.get('resolution_diagnosis'):
            raise StateConflict('Resolution action lost its original failure admission')
        return
    failure = FailureRecord.model_validate(raw)
    record = _receipt(policy, rid, failure)
    if permit['action']['action'] == 'diagnose':
        if permit['source_hash'] != record['source_hash'] or permit['plan_hash'] != record['plan_hash']:
            raise StateConflict('Diagnosis admission inputs changed')
        return
    proof = permit.get('resolution_diagnosis', {})
    if not isinstance(proof, dict):
        raise StateConflict('Resolution action has malformed completion proof')
    if key := proof.get('patch_permit'):
        previous = policy.store.get(rid, key)
        journal = policy.store.get(rid, proof.get('journal', ''))
        if (not previous or key == proof.get('diagnosis_permit') or digest(previous) != proof.get('patch_hash')
                or previous.get('phase') != 'completed' or previous['action']['action'] != 'plan_patch'
                or previous['cycle_id'] != permit['cycle_id'] or previous['outcome'].get('status') != 'applied'
                or not journal or journal.get('phase') != 'activated' or digest(journal) != proof.get('journal_hash')):
            raise StateConflict('Resolution continuation lost its activated patch proof')
        validate_permit(policy, rid, previous)
        return
    key = proof.get('diagnosis_permit', '')
    diagnosis = policy.store.get(rid, key, {})
    binding = policy.store.get(rid, key.replace('recovery/permits/', 'recovery/authorization-inputs/', 1), {})
    if (not key or digest(diagnosis) != proof.get('diagnosis_hash') or diagnosis.get('phase') != 'completed'
            or diagnosis.get('outcome', {}).get('status') != 'validated'
            or diagnosis.get('outcome_hash') != digest(diagnosis.get('outcome'))
            or diagnosis.get('action', {}).get('action') != 'diagnose'
            or diagnosis.get('cycle_id') != permit['cycle_id'] or diagnosis.get('owner') != permit['owner']
            or diagnosis.get('source_hash') != permit['source_hash'] or diagnosis.get('plan_hash') != permit['plan_hash']
            or binding.get('source_hash') != permit['source_hash'] or binding.get('plan_hash') != permit['plan_hash']):
        raise StateConflict('Unknown resolution action has no bound completed diagnosis proof')
