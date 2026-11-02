"""Exclude repeated evidence-neutral recovery paths, not cumulative work budgets."""
from .errors import RecoveryPathExhausted
from .execution_stages import STAGE_CONTRACT_VERSION, current_stage_contract, stage_contract
from .faults import StateConflict
from .recovery import check_rows
from .source_history import SourceHistory
from .stage_responsibilities import correction_contract
from .plan_proposals import PROTOCOL_KEY as PLAN_PROPOSAL_KEY, current_protocol as current_plan_proposal, protocol as plan_proposal_protocol
from .util import digest
from .workspace import tree_hash

NO_PROGRESS_LIMIT = 2


class RecoveryProgress:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def inputs(self, rid, failure, work, plan):
        history = SourceHistory(self.engine)
        source_history = (history.collect_plan(rid, work, plan,
            task_ids=[failure.task_id] if failure.task_id else None) if plan is not None
            else history.collect_protected(rid, work))
        return {PLAN_PROPOSAL_KEY: plan_proposal_protocol(), 'controller_stage_contract': stage_contract(),
                'failure': failure.model_dump(), 'plan': plan.model_dump() if plan else None,
                'specification': self.store.get(rid, 'clarify'), 'design': self.store.get(rid, 'design'),
                'prototype_approval': self.store.get(rid, 'prototype_approval'),
                'source_history': source_history}

    @staticmethod
    def basis(context, source_hash, action):
        failure = context['failure']
        evidence = failure.get('evidence', {})
        # Recommendations, citations, attempt counters and rewording do not change facts.
        facts = {name: failure.get(name) for name in ('id', 'kind', 'component', 'origin_id', 'task_id', 'step', 'obligations')}
        if action == 'diagnose':
            facts['observations'] = {name: evidence.get(name) for name in
                                     ('review', 'environment_error', 'candidate_error')}
            facts['checks'] = check_rows(evidence)
        if evidence.get('environment_failure'):
            from .diagnostics import references
            detail = evidence['environment_failure']
            facts['installation_diagnostics'] = {
                'logs': sorted({ref['hash'] for ref in references(detail)}),
                'runtime_identity': detail.get('runtime_identity'),
                'capture_status': detail.get('npm_debug', {}).get('status')}
        materials = {name: context.get(name) for name in
                     ('plan', 'specification', 'design', 'prototype_approval', 'source_history')}
        # The newly supplied citation contract changes what a timing amendment can
        # actually cite. Preserve old ledgers; prose/prompt edits and diagnosis
        # counters alone cannot create fresh attempts or replace human permission.
        protocol = context.get('controller_stage_contract')
        if action == 'plan_patch' and current_stage_contract(protocol):
            materials['controller_stage_contract'] = protocol
        if action == 'plan_patch' and current_plan_proposal(context.get(PLAN_PROPOSAL_KEY)):
            materials[PLAN_PROPOSAL_KEY] = context[PLAN_PROPOSAL_KEY]
        if action == 'plan_patch':
            materials['controller_correction_contract'] = correction_contract()
            facts['observations'] = {name: evidence.get(name) for name in ('environment_error', 'candidate_error')}
            facts['checks'] = check_rows(evidence)
        return digest([STAGE_CONTRACT_VERSION, action, source_hash, facts, materials])

    def diagnosis_basis(self, rid, failure, work, plan, operation):
        key = 'recovery/diagnosis-progress-basis/' + operation
        saved = self.store.get(rid, key)
        if saved is None:
            # Use exactly the same factual projection as exclusions, including
            # for delivery's explicit action catalog. Presentation is not progress.
            saved = self.basis(self.inputs(rid, failure, work, plan), tree_hash(work), 'diagnose')
            self.store.checkpoint(rid, key, saved)
        return saved

    def enroll_diagnosis(self, rid, owner, basis, operation, outcome, *, completed):
        key = 'recovery/diagnosis-progress-enrollment/' + operation
        expected = {'owner': owner, 'basis': basis, 'outcome_hash': digest(outcome)}
        saved = self.store.get(rid, key)
        if saved is not None and saved != expected:
            raise StateConflict('Diagnostic progress enrollment changed; preserve its completed observation')
        if saved is None and not completed:
            # Persist before completion. A crash after completion can finish the
            # ledger; old completed results without enrollment are not backfilled.
            self.store.checkpoint(rid, key, expected)

    def complete_diagnosis(self, rid, owner, basis, operation, outcome):
        saved = self.store.get(rid, 'recovery/diagnosis-progress-enrollment/' + operation)
        if saved is None:
            return
        if saved != {'owner': owner, 'basis': basis, 'outcome_hash': digest(outcome)}:
            raise StateConflict('Diagnostic progress does not match its completed observation')
        self.record(rid, owner, 'diagnose', basis, operation,
            {'status': 'completed_without_execution', 'evidence_ref': 'invocation/' + operation,
             'diagnosis': outcome['diagnosis']})

    def record(self, rid, owner, action, basis, operation, outcome):
        key = f'recovery/no-progress/{action}/{basis}'
        saved = self.store.get(rid, key, {'action': action, 'basis': basis, 'operations': {}})
        if operation in saved['operations']:
            return
        saved['operations'][operation] = outcome
        self.store.checkpoints(rid, {key: saved, 'recovery/no-progress/owner/' + owner: True})
        self.store.event(rid, 'recovery.no_progress', {'owner': owner, 'action': action, 'evidence': key,
            'attempts_with_same_evidence': len(saved['operations']), 'excluded': len(saved['operations']) >= NO_PROGRESS_LIMIT})

    @staticmethod
    def _stop_key(cycle_id, operation):
        return 'recovery/stopped-selection/' + digest([cycle_id, operation])

    def _author_task(self, rid, failure, plan):
        return (plan is not None and failure.task_id in {task.id for task in plan.tasks}
                and (self.store.get(rid, 'clarify') or {}).get('frontend'))

    def author_source(self, rid, failure, work, plan):
        if not self._author_task(rid, failure, plan):
            return {}
        prototype = self.store.get(rid, 'prototype', {})
        approval = self.engine.approvals.current(rid, work, prototype.get('browser_actions', []),
            self.engine.browser_check_version()) if prototype else None
        return {name: approval[name] for name in ('identity', 'actions_hash')} if approval else {}

    def author_materials(self, rid, failure, work, plan):
        return self.engine.recovery._approval(rid, work) if self._author_task(rid, failure, plan) else {}

    def verify_author_materials(self, rid, failure, work, plan, materials):
        if not materials:
            return
        try:
            ref = materials['prototype_approval']['browser_actions']['artifact']
            if len(self.engine.artifacts.bytes(ref)) != ref['bytes']:
                raise ValueError('Action artifact size changed')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StateConflict('Approved action material changed during recovery decision') from exc
        # Read the original ref before _approval can republish a missing body.
        if self.author_materials(rid, failure, work, plan) != materials:
            raise StateConflict('Approved action binding changed during recovery decision')

    def _facts(self, rid, failure, work, plan, *, author_materials=None):
        context = self.inputs(rid, failure, work, plan)
        source = tree_hash(work)
        facts = {action: self.basis(context, source, action) for action in ('diagnose', 'plan_patch')}
        source = (self.author_source(rid, failure, work, plan) if author_materials is None else
                  {name: author_materials['prototype_approval'][name] for name in ('identity', 'actions_hash')}
                  if author_materials else {})
        if source:
            # Exact logical content, not prose, locations or a transport version.
            # This read-only lookup must not republish a deleted frozen artifact.
            facts['author_materials'] = digest(source)
        return facts

    def resume_operation(self, rid, failure, work, plan, cycle_id, operation, *, author_materials=None):
        stopped = self.store.get(rid, self._stop_key(cycle_id, operation))
        if not stopped:
            return operation
        if self.store.decision_questions(rid, [stopped['question_key']]):
            raise StateConflict('A recorded user question cannot be replaced by recovery progress handling')
        facts = self._facts(rid, failure, work, plan, author_materials=author_materials)
        if 'author_materials' in stopped['facts'] and 'author_materials' not in facts:
            raise StateConflict('Approved action material is no longer current; preserve the stopped selection')
        if stopped['facts'] == facts:
            raise RecoveryPathExhausted(stopped['excluded_actions'], stopped['reason'])
        # This prior selection stopped WITHOUT asking/receiving human permission.
        # New actual materials can create a distinct request; retain every old result.
        operation += '/evidence/' + digest(facts)
        stopped = self.store.get(rid, self._stop_key(cycle_id, operation))
        if stopped:
            if self.store.decision_questions(rid, [stopped['question_key']]):
                raise StateConflict('A recorded user question cannot be replaced by recovery progress handling')
            raise RecoveryPathExhausted(stopped['excluded_actions'], stopped['reason'])
        return operation

    def stop(self, rid, failure, work, plan, cycle_id, operation, exclusions, reason, *, author_materials=None):
        question_key = 'recovery-cycle/' + digest([cycle_id, operation])
        if self.store.decision_questions(rid, [question_key]):
            raise StateConflict('Preserve the existing recovery question; progress handling cannot supersede consent')
        key = self._stop_key(cycle_id, operation)
        if self.store.get(rid, key) is None:
            self.store.checkpoint(rid, key, {'facts': self._facts(rid, failure, work, plan, author_materials=author_materials),
                'question_key': question_key, 'excluded_actions': exclusions, 'reason': reason})
        raise RecoveryPathExhausted(exclusions, reason)

    def exclusions(self, rid, failure, work, plan):
        if not self.store.get(rid, 'recovery/no-progress/owner/' + failure.origin_id):
            return {}
        context = self.inputs(rid, failure, work, plan)
        source = tree_hash(work)
        excluded = {}
        for action in ('diagnose', 'plan_patch'):
            basis = self.basis(context, source, action)
            key = f'recovery/no-progress/{action}/{basis}'
            saved = self.store.get(rid, key)
            if saved and len(saved['operations']) >= NO_PROGRESS_LIMIT:
                excluded[action] = {**saved, 'evidence_ref': key,
                    'reason': 'Two completed diagnoses without new execution evidence on identical facts' if action == 'diagnose' else
                              'Two rejected contract amendments on identical facts',
                    'resume_condition': 'New relevant source, plan, specification, design or supplied evidence; '
                                        'another wording, permission or invocation is not new evidence.'}
        return excluded
