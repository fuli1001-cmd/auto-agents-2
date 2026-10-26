"""Evidence-bound recording correction for task-local file-count failures.

This is an additional restriction on existing task authority, never edit or retry
permission on its own. Other capture and external prerequisites keep their route.
"""
from __future__ import annotations

import re
from pathlib import Path

from pydantic import Field

from .collection import capture_contract, policy_identity, run_collection
from .errors import AA2Error, CollectionError, PolicyViolation
from .faults import StateConflict
from .models import PlanResult, Record, ReviewResult
from .plan_revision import EvidencePatchReview
from .safe_files import read_regular
from .test_reports import read_junit
from .util import digest
from .workspace import git, path_allowed, prepared_before_hash, protected_change_path, tree_hash

PREFIX = 'recovery/recording/'
OWNER = PREFIX + 'tasks/'


class RecordingAssessment(Record):
    binding_hash: str
    recording_only: bool
    assertions_preserved: bool
    test_execution_preserved: bool
    viewports_preserved: bool
    required_files_and_contents_preserved: bool
    adopted_methods_preserved: bool
    reason: str = Field(min_length=1)


class RecordingReview(Record):
    review: ReviewResult
    recording: RecordingAssessment


class RecordingPatchReview(Record):
    review: EvidencePatchReview
    recording: RecordingAssessment


def _receipt(store, rid, key):
    value = store.get(rid, key)
    if (not isinstance(value, dict) or value.get('version') != 1
            or key != PREFIX + 'receipts/' + value.get('hash', '')
            or digest({k: v for k, v in value.items() if k != 'hash'}) != value['hash']):
        raise StateConflict('Recording recovery lost its immutable admission receipt')
    return value


def current(engine, rid, task_id):
    key = engine.store.get(rid, OWNER + task_id)
    if not key:
        return None
    value = _receipt(engine.store, rid, key)
    if value['task_id'] != task_id or value['policy']['policy_hash'] != policy_identity(engine.store.root):
        raise StateConflict('Recording task or capture policy changed; preserve the original evidence')
    return value


def _owned_bytes(engine, rid, ref, maximum):
    sha, size = ref.get('hash'), ref.get('bytes')
    if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha) or type(size) is not int or not 0 <= size <= maximum:
        raise ValueError('invalid artifact identity')
    path = engine.store.root / 'artifacts' / sha[:2] / sha
    raw = read_regular(path, maximum)
    with engine.store.connect() as db:
        owned = db.execute('SELECT 1 FROM artifact_owners WHERE hash=? AND run_id=?', (sha, rid)).fetchone()
    if not owned or len(raw) != size or digest(raw) != sha:
        raise ValueError('missing, foreign or changed artifact')
    return raw


def _recording_files(work, task, commands):
    inputs = [p for command in commands for p in command.inputs]
    files = []
    for path in git(work, 'ls-files', '--cached', '--others', '--exclude-standard').splitlines():
        target = work / path
        # Dependencies, environment/permission files and product source are not
        # recording inputs even when broadly protected by the ordinary reviewer.
        in_tests = any(p in {'tests', 'test', '__tests__'} for p in Path(path).parts)
        if (not (in_tests or Path(path).name in {'playwright.config.ts', 'playwright.config.js'})
                or not protected_change_path(path) or not path_allowed(path, task.allowed_paths)
                or not path_allowed(path, inputs) or target.is_symlink() or not target.is_file()
                or target.resolve() != work.resolve() / path):
            continue
        files.append(path)
    return sorted(files)


def observation_key(failure, active):
    return PREFIX + 'observations/' + digest([failure.id, active['attempt'], digest(failure.evidence)])


def _retained(engine, rid, observation):
    """An admission is a fact about retained bytes, never a permanent exemption."""
    limit = observation['policy']['limits']['max_file_bytes']
    for row in observation['checks']:
        if row.get('report'):
            _owned_bytes(engine, rid, row['report'], 32 * 1024**2)
        cap = row.get('attachment_capture', {})
        if cap.get('status') != 'failed':
            continue
        scratch = Path(cap['scratch'])
        if (scratch.parent != engine.store.root / 'tmp' or scratch.is_symlink()
                or scratch.resolve() != scratch.absolute()
                or engine.store.get(rid, 'evidence/capture/' + scratch.name) != cap):
            raise StateConflict('Retained recording capture changed')
        with engine.store.connect() as db:
            held = db.execute('SELECT 1 FROM resource_holds WHERE id=? AND path=? AND run_id=?',
                             (cap.get('hold'), str(scratch), rid)).fetchone()
        if not held:
            raise StateConflict('Retained recording capture lost its hold')
        for item in row['attachments']:
            if _owned_bytes(engine, rid, item['artifact'], limit) != read_regular(
                    scratch / 'playwright-results' / item['path'], limit):
                raise StateConflict('Retained recording attachment bytes changed')


def _observation(engine, rid, key, record):
    value = engine.store.get(rid, key)
    if not value:
        return None
    if (key != PREFIX + 'observations/' + digest([value.get('failure_id'), value.get('attempt'), value.get('failure_evidence_hash')])
            or value.get('receipt_hash') != record['hash'] or value.get('hash') != digest(
            {k: v for k, v in value.items() if k != 'hash'})
            or value.get('policy') != capture_contract(engine.store.root)):
        raise StateConflict('Recording observation or policy binding changed')
    try:
        _retained(engine, rid, value)
    except (OSError, ValueError, KeyError, TypeError, StopIteration, CollectionError) as exc:
        raise StateConflict('Recording observation lost its retained evidence') from exc
    return value


def _matches_current(engine, rid, observation, failure, active, work, plan):
    if observation['failure_evidence_hash'] != digest(failure.evidence):
        raise StateConflict('Recording failure identity changed')
    if (observation['source_hash'] == tree_hash(work) and observation['plan'] == plan.model_dump()
            and observation['plan_revision'] == engine.store.get(rid, 'plan_revision/current')
            and observation['task_revision'] == engine.store.get(rid, 'task_state/' + failure.task_id, {}).get('revision')):
        return
    # Only the already reviewed plan-document transition may explain a changed
    # source before followup. Product changes or another failure cannot do so.
    journal = engine.store.get(rid, f"plan_revision/journal/{failure.id}/{active['attempt']}", {})
    if journal.get('phase') == 'activated':
        validate_journal(journal, lambda key: engine.store.get(rid, key), engine.store.root)
        preimage = journal.get('document_preimage')
        snapshot = journal.get('snapshots', {}).get(failure.task_id, {})
        retained = Path(snapshot.get('workspace', '')) == work and tree_hash(work) == observation['source_hash']
        document = work / 'docs/aa2/plan.md'
        integrated = (work == engine.store.root / 'worktrees' / rid / 'integration' and
            (tree_hash(work) == journal['source_hash'] or preimage and document.is_file()
             and not document.is_symlink() and document.read_text() == plan.document
             and prepared_before_hash(work, [preimage]) == journal['source_hash']))
        if (snapshot.get('source_hash') == observation['source_hash']
                and journal.get('previous_plan') == observation['plan']
                and journal.get('plan') == plan.model_dump()
                and engine.store.get(rid, 'plan_revision/current', {}).get('revision') == journal['base_revision'] + 1
                and (retained or integrated)):
            return
    raise StateConflict('Recording source or plan changed before its authorized transition')


async def prepare(engine, rid, active, failure, plan, work):
    """Prepare a narrow receipt; only a later issued permit retires unanswered waits."""
    if not failure.task_id:
        return None  # Final/delivery retain the existing prerequisite boundary.
    existing = current(engine, rid, failure.task_id)
    key_observation = observation_key(failure, active)
    prior = _observation(engine, rid, key_observation, existing) if existing else None
    if prior:
        _matches_current(engine, rid, prior, failure, active, work, plan)
        # Full metadata traversal remains in the cancellable managed helper.
        # Synchronous permit consumption checks immutable receipts and archived
        # byte identities; it never starts an unbounded filesystem walk.
        for capture in prior['captures']:
            cap = engine.store.get(rid, capture['checkpoint'])
            scratch = Path(cap['scratch'])
            inventory = await run_collection(engine.store.root, {'action': 'attachment_inventory',
                'path': str(scratch / 'playwright-results'), 'policy': prior['policy']['limits']},
                timeout=prior['policy']['limits']['timeout_seconds'], paths=[scratch])
            if inventory != capture['inventory']:
                raise StateConflict('Retained recording inventory changed before recovery reentry')
        return existing
    try:
        task = next(t for t in plan.tasks if t.id == failure.task_id)
        saved_key = 'develop/task/' + task.id
        saved = engine.store.get(rid, saved_key, {})
        owned = engine.store.root / 'worktrees' / rid
        source = tree_hash(work)
        if (saved.get('commit') or task.id in engine.store.get(rid, 'develop/done', [])
                or Path(saved.get('workspace', '')) != work or work.is_symlink()
                or work.resolve() != work.absolute() or not work.is_relative_to(owned)
                or saved.get('source_hash') != source or failure.evidence.get('task') != task.model_dump()):
            return None
        from .builds import verification_commands
        commands = verification_commands(task.checks, work, plan.runtime_prepare)
        rows = failure.evidence.get('checks', [])
        matching = {digest(c.model_dump()): c for c in commands}
        if (not rows or len(rows) != len(matching) or {r.get('command_hash') for r in rows} != set(matching)
                or failure.evidence.get('commands') != [c.model_dump() for c in commands]
                or saved.get('checks') != rows):
            return None
        policy = capture_contract(engine.store.root)
        captures, guards = [], {'recovery/active': active, saved_key: saved,
            'plan': plan.model_dump(), 'plan_revision/current': engine.store.get(rid, 'plan_revision/current'),
            'task_state/' + task.id: engine.store.get(rid, 'task_state/' + task.id)}
        for row in rows:
            command = matching[row['command_hash']]
            if row.get('source_hash') != source or row.get('timed_out') or row.get('process_exit_code') != 0:
                return None
            reasons = row.get('failure_reasons', [])
            if not reasons:
                if row.get('exit_code') != 0:
                    return None
                continue
            cap = row.get('attachment_capture', {})
            if (reasons != ['evidence_collection_failed'] or row.get('test_runner') != 'playwright'
                    or row.get('report_status') != 'valid' or row.get('skipped') != 0
                    or row.get('capture_policy_hash') != policy['policy_hash']
                    or cap.get('status') != 'failed' or cap.get('reason') != 'max_files'
                    or cap.get('limits') != policy['limits'] or cap.get('policy_hash') != policy['policy_hash']
                    or cap.get('source_hash') != source or cap.get('command_hash') != row['command_hash']
                    or cap.get('files') != policy['limits']['max_files'] + 1):
                return None
            cases = read_junit(_owned_bytes(engine, rid, row['report'], 32 * 1024**2))
            if (cases.failures or cases.skipped or cases.ids != row.get('test_ids')
                    or cases.passed != row.get('passed_test_ids') or cases.passed_count != row.get('tests')
                    or row['tests'] < max(1, command.expected_tests)):
                return None
            scratch = Path(cap['scratch'])
            if (scratch.parent != engine.store.root / 'tmp' or scratch.resolve() != scratch.absolute()
                    or not re.fullmatch('check_[0-9a-f]+', scratch.name)):
                return None
            cap_key = 'evidence/capture/' + scratch.name
            if engine.store.get(rid, cap_key) != cap:
                return None
            with engine.store.connect() as db:
                held = db.execute('SELECT 1 FROM resource_holds WHERE id=? AND path=? AND run_id=?',
                                  (cap.get('hold'), str(scratch), rid)).fetchone()
            if not held:
                return None
            inventory = await run_collection(engine.store.root, {'action': 'attachment_inventory',
                'path': str(scratch / 'playwright-results'), 'policy': policy['limits']},
                timeout=policy['limits']['timeout_seconds'], paths=[scratch])
            index = {r['path']: r for r in inventory['entries']}
            if inventory['files'] <= policy['limits']['max_files'] or cap.get('path') not in index:
                return None
            attachments = row.get('attachments', [])
            if len(attachments) != policy['limits']['max_files'] or len({a['path'] for a in attachments}) != len(attachments):
                return None
            for item in attachments:
                if item['path'] not in index:
                    return None
                raw = _owned_bytes(engine, rid, item['artifact'], policy['limits']['max_file_bytes'])
                retained = read_regular(scratch / 'playwright-results' / item['path'], policy['limits']['max_file_bytes'])
                if retained != raw:
                    return None
            guards[cap_key] = cap
            captures.append({'checkpoint': cap_key, 'capture_hash': digest(cap), 'inventory': inventory})
        files = _recording_files(work, task, commands)
        if existing:
            files = sorted(set(files) & set(existing['files']))
        if not captures or not files:
            return None
        failed = [row for row in rows if 'evidence_collection_failed' in row.get('failure_reasons', [])]
        identity = digest(failed)
        qkeys = engine.store.decision_question_keys(rid, 'evidence-capture/' + identity)
        questions = engine.store.decision_questions(rid, qkeys)
        if any(q['answer'] is not None for q in questions):
            return None
        record = {'version': 1, 'task_id': task.id, 'origin_id': task.origin_id or task.id,
            'plan': plan.model_dump(), 'task': task.model_dump(), 'source_hash': source,
            'base': git(work, 'rev-parse', 'HEAD'), 'policy': policy, 'captures': captures,
            'files': files, 'original_active': active, 'questions': questions, 'question_keys': qkeys,
            'initial_action': 'diagnose'}
        record['hash'] = digest(record)
        record = existing or record
        key = PREFIX + 'receipts/' + record['hash']
        if tree_hash(work) != source or policy != capture_contract(engine.store.root):
            raise StateConflict('Recording evidence changed during admission')
        observation = {'version': 1, 'receipt_hash': record['hash'], 'source_hash': source,
            'failure_id': failure.id, 'attempt': active['attempt'],
            'plan_revision': guards['plan_revision/current'],
            'task_revision': (guards['task_state/' + task.id] or {}).get('revision'),
            'plan': plan.model_dump(), 'policy': policy, 'captures': captures, 'checks': rows,
            'failure_evidence_hash': digest(failure.evidence), 'question_keys': qkeys}
        observation['hash'] = digest(observation)
        engine.store.prepare_recording_recovery(rid, key, record, guards, qkeys, questions,
            previous=key if existing else None, observation_key=key_observation, observation=observation)
        return record
    except (OSError, ValueError, KeyError, TypeError, StopIteration, CollectionError):
        return None


def context(record):
    return {'receipt': PREFIX + 'receipts/' + record['hash'], 'receipt_hash': record['hash'],
        'task_id': record['task_id'], 'initial_action': 'diagnose', 'allowed_files': record['files'],
        'policy': record['policy'], 'observed_captures': record['captures'],
        'constraint': 'Read-only diagnosis first. Subsequent authorization allows only recording changes '
            'to these existing files, with explicit independent preservation review. No product/dependency '
            'edits, file additions/deletions, unchanged rechecks, quota increases or lost tests/assertions/'
            'viewports/PNG/state JSON/download bytes. A changed accepted attachment method needs formal '
            'contract correction; PlanPatch cannot override an adopted design method. If that is necessary, '
            'preserve the candidate and report the source-owner boundary; this route grants no design re-entry.'}


def authorization(policy, rid, failure, active, work, plan, operation):
    record = current(policy.engine, rid, failure.task_id) if failure.task_id else None
    if not record:
        return None
    ph = digest(plan.model_dump())
    if ph != digest(record['plan']) and not policy.store.get(rid, PREFIX + 'plans/' + record['hash'] + '/' + ph):
        raise StateConflict('Recording recovery plan changed outside its reviewed activation')
    proof = {'receipt': PREFIX + 'receipts/' + record['hash'], 'receipt_hash': record['hash']}
    observation_id = observation_key(failure, active)
    observed = _observation(policy.engine, rid, observation_id, record)
    if observed:
        _matches_current(policy.engine, rid, observed, failure, active, work, plan)
        proof.update(observation=observation_id, observation_hash=observed['hash'])
    if operation == 'patch-followup':
        key = active.get('patch_permit')
        previous = policy.store.get(rid, key or '', {})
        journal_key = f"plan_revision/journal/{failure.id}/{active['attempt']}"
        journal = policy.store.get(rid, journal_key, {})
        if (not previous or previous.get('recording', {}).get('receipt') != proof['receipt']
                or not policy.completed_outcome(rid, key, action='plan_patch')
                or journal.get('phase') != 'activated' or journal.get('recording', {}).get('receipt') != proof['receipt']):
            raise StateConflict('Recording followup requires its completed bounded PlanPatch')
        validate_journal(journal, lambda k: policy.store.get(rid, k), policy.store.root)
        return {**proof, 'patch_permit': key, 'patch_hash': digest(previous),
                'journal': journal_key, 'journal_hash': digest(journal)}
    key = active.get('diagnosis_permit')
    if key and policy.completed_diagnosis(rid, key, failure, active, work, plan, allow_invalid=True) is not None:
        proof.update(diagnosis_permit=key, diagnosis_hash=digest(policy.store.get(rid, key)))
    return proof


def validate_permit(policy, rid, permit):
    proof = permit.get('recording') or {}
    raw = permit.get('authority_failure', {})
    tid = raw.get('task_id', '')
    current_key = policy.store.get(rid, OWNER + tid) if tid else None
    if not proof:
        if current_key and permit['action']['action'] in {'diagnose', 'repair', 'plan_patch', 'recheck'}:
            raise StateConflict('A recording correction cannot consume an unrestricted old permit')
        return None
    record = _receipt(policy.store, rid, proof.get('receipt', ''))
    if (proof.get('receipt_hash') != record['hash'] or current_key != proof['receipt']
            or record['policy']['policy_hash'] != policy_identity(policy.store.root)
            or tid != record['task_id'] or permit['owner'] != record['origin_id']
            or permit['action']['action'] not in {'diagnose', 'repair', 'plan_patch'}
            or set(permit['action'].get('task_ids', [])) - {tid}):
        raise StateConflict('Recording permission scope or policy changed')
    observed = _observation(policy.engine, rid, proof.get('observation', ''), record) if proof.get('observation') else None
    if observed and (proof.get('observation_hash') != observed['hash']
            or observed['failure_evidence_hash'] != digest(raw.get('evidence'))
            or not proof.get('patch_permit') and
               (observed['source_hash'] != permit['source_hash'] or digest(observed['plan']) != permit['plan_hash'])):
        raise StateConflict('Recording permit no longer matches its admitted observation')
    if proof.get('observation') and not observed:
        raise StateConflict('Recording permit lost its admitted observation')
    if (any('evidence_collection_failed' in row.get('failure_reasons', [])
            for row in raw.get('evidence', {}).get('checks', [])) and not observed):
        raise StateConflict('Capture-failure permission requires its verified count observation')
    if permit['action']['action'] == 'diagnose':
        return record
    if proof.get('patch_permit'):
        previous = policy.store.get(rid, proof['patch_permit'], {})
        journal = policy.store.get(rid, proof.get('journal', ''), {})
        if (digest(previous) != proof.get('patch_hash') or previous.get('phase') != 'completed'
                or previous.get('recording', {}).get('receipt') != proof['receipt']
                or previous.get('action', {}).get('action') != 'plan_patch'
                or previous.get('outcome', {}).get('status') != 'applied'
                or journal.get('phase') != 'activated' or digest(journal) != proof.get('journal_hash')
                or digest(journal.get('plan')) != permit['plan_hash']):
            raise StateConflict('Recording followup lost its reviewed activated plan')
        validate_journal(journal, lambda k: policy.store.get(rid, k), policy.store.root)
        validate_permit(policy, rid, previous)
    else:
        previous = policy.store.get(rid, proof.get('diagnosis_permit', ''), {})
        if (not previous or digest(previous) != proof.get('diagnosis_hash')
                or previous.get('recording', {}).get('receipt') != proof['receipt']
                or previous.get('phase') != 'completed' or previous.get('outcome', {}).get('status') != 'validated'
                or previous.get('outcome_hash') != digest(previous.get('outcome'))
                or previous.get('action', {}).get('action') != 'diagnose'
                or any(previous.get(k) != permit.get(k) for k in ('cycle_id', 'source_hash', 'plan_hash', 'owner'))):
            raise StateConflict('Recording changes require a bound completed diagnosis')
        validate_permit(policy, rid, previous)
    return record


def task_record(engine, rid, saved, task):
    record = current(engine, rid, task.id)
    if not record:
        return None
    permit = engine.recovery_policy.validate_consumption(rid, saved.get('recovery_permit'), expected={'repair'})
    if not permit or permit.get('recording', {}).get('receipt_hash') != record['hash']:
        raise StateConflict('Recording candidate lost its restricted repair permit')
    return record


def check_changes(record, prepared):
    if not prepared or not any(row.get('before') != row.get('after') for row in prepared):
        raise PolicyViolation('Recording correction must change its actual recording inputs')
    for row in prepared:
        if (row['path'] not in record['files'] or row.get('before') is None
                or row.get('operation') == 'delete' or row.get('content') is None):
            raise PolicyViolation('Recording correction permits only existing recording files; no additions/deletions or product edits')


def review_binding(record, source, plan, candidate, *, phase):
    return digest(['recording-preservation-v1', record['hash'], record['policy']['policy_hash'],
                   source, plan.model_dump() if hasattr(plan, 'model_dump') else plan, candidate, phase])


def require_review(value, expected):
    assessment = RecordingAssessment.model_validate(value['recording'])
    if (not value.get('review', {}).get('approved') or assessment.binding_hash != expected
            or not all(getattr(assessment, k) for k in RecordingAssessment.model_fields if k not in {'binding_hash', 'reason'})):
        raise PolicyViolation('Independent recording review did not preserve scope, evidence, assertions or adopted methods')


def validate_prepared(engine, rid, saved, task, plan):
    record = task_record(engine, rid, saved, task)
    if not record:
        return
    check_changes(record, saved['patch'])
    expected = review_binding(record, saved['source_hash'], plan, saved['patch'], phase='implementation')
    review = saved.get('recording_review')
    if not review or review.get('review') != saved.get('protected_review'):
        raise StateConflict('Prepared recording correction lacks its independent preservation review')
    require_review(review, expected)


def validate_patch(record, previous, patch, candidate, affected, reopen):
    """Additional exact structural limits; semantic method preservation is reviewed."""
    tid = record['task_id']
    rows = patch.contract_corrections
    if (not rows or patch.timing_corrections or patch.remove_tasks
            or any(r.target != 'acceptance' or r.task_id != tid for r in rows)
            or {t.id for t in patch.upsert_tasks} != {tid}
            or set(affected) != {tid} or set(reopen) != {tid}):
        raise AA2Error('Recording PlanPatch must map only this task attachment-method acceptance clauses')
    after = candidate.model_dump()
    expected = previous.model_copy(deep=True)
    old_task = next(t for t in previous.tasks if t.id == tid)
    target = next(t for t in expected.tasks if t.id == tid)
    submitted = next(t for t in candidate.tasks if t.id == tid)
    for row in rows:
        # Only formal attachment obligations identify eligible clauses. A phrase
        # in a model's reason cannot turn a business clause into recording scope.
        if not previous.evidence_contract or not any(o.task_id == tid and o.target == 'acceptance'
                and o.index == row.index and o.capability == 'test_file_attachments'
                for o in previous.evidence_contract.obligations):
            raise AA2Error('Recording correction target lacks its accepted file-attachment obligation')
        target.acceptance[row.index] = row.replacement
        for obligation in expected.evidence_contract.obligations:
            if (obligation.task_id, obligation.target, obligation.index) == (tid, 'acceptance', row.index):
                obligation.value = row.replacement
    target.title, target.description = submitted.title, submitted.description
    expected.document = candidate.document
    if expected.model_dump() != after or old_task.allowed_paths != submitted.allowed_paths:
        raise AA2Error('Recording PlanPatch changes checks, scope, evidence identity, other tasks or runtime contracts')


def journal_scope(journal, get):
    proof = journal.get('recording')
    key = (journal.get('correction_request') or {}).get('permit')
    permit = get(key) if key else None
    if not proof:
        if permit and permit.get('recording'):
            raise StateConflict('Recording plan lost its restricted activation record')
        return None
    record = get(proof['receipt'])
    if (not record or record.get('hash') != proof.get('receipt_hash')
            or digest({k: v for k, v in record.items() if k != 'hash'}) != record['hash']
            or get(OWNER + record['task_id']) != proof['receipt']
            or not permit or permit.get('recording') != proof):
        raise StateConflict('Recording plan lost its controller-owned correction scope')
    return record


def validate_journal(journal, get, root):
    proof = journal.get('recording')
    permit_key = (journal.get('correction_request') or {}).get('permit')
    permit = get(permit_key) if permit_key else None
    if not proof:
        if permit and permit.get('recording'):
            raise StateConflict('Recording plan lost its restricted activation record')
        return
    record = get(proof['receipt'])
    if (not record or record.get('hash') != proof.get('receipt_hash')
            or digest({k: v for k, v in record.items() if k != 'hash'}) != record['hash']
            or get(OWNER + record['task_id']) != proof['receipt']
            or record['policy']['policy_hash'] != policy_identity(root)
            or not permit or permit.get('recording') != proof):
        raise StateConflict('Recording PlanPatch admission or policy changed before activation')
    from .plan_revision import PlanPatch
    previous, candidate = (PlanResult.model_validate(journal[k]) for k in ('previous_plan', 'plan'))
    validate_patch(record, previous, PlanPatch.model_validate(journal['patch']), candidate,
                   journal['affected'], journal['reopen'])
    expected = review_binding(record, journal['source_hash'], previous,
                              {'patch': journal['patch'], 'plan': journal['plan']}, phase='plan_patch')
    value = journal.get('recording_review')
    if not value or value['review'] != journal['review']:
        raise StateConflict('Recording PlanPatch lacks its independently reviewed preservation binding')
    require_review(value, expected)
