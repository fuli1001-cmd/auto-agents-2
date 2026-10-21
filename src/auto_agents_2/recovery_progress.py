"""Exclude repeated evidence-neutral recovery paths, not cumulative work budgets."""
from .errors import RecoveryPathExhausted
from .execution_stages import STAGE_CONTRACT_VERSION, current_stage_contract, stage_contract
from .faults import StateConflict
from .recovery import check_rows
from .source_history import SourceHistory
from .util import digest
from .workspace import tree_hash

NO_PROGRESS_LIMIT = 2


class RecoveryProgress:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def inputs(self, rid, failure, work, plan):
        scopes = sorted({p for t in plan.tasks if not failure.task_id or t.id == failure.task_id
                         for p in t.allowed_paths}) if plan else []
        return {'controller_stage_contract': stage_contract(),
                'failure': failure.model_dump(), 'plan': plan.model_dump() if plan else None,
                'specification': self.store.get(rid, 'clarify'), 'design': self.store.get(rid, 'design'),
                'prototype_approval': self.store.get(rid, 'prototype_approval'),
                'source_history': SourceHistory(self.engine).collect(rid, work, scopes)}

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
        materials = {name: context.get(name) for name in
                     ('plan', 'specification', 'design', 'prototype_approval', 'source_history')}
        # The newly supplied citation contract changes what a timing amendment can
        # actually cite. Preserve old ledgers; prose/prompt edits and diagnosis
        # counters alone cannot create fresh attempts or replace human permission.
        protocol = context.get('controller_stage_contract')
        if action == 'plan_patch' and current_stage_contract(protocol):
            materials['controller_stage_contract'] = protocol
        return digest([STAGE_CONTRACT_VERSION, action, source_hash, facts, materials])

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

    def _facts(self, rid, failure, work, plan):
        context = self.inputs(rid, failure, work, plan)
        source = tree_hash(work)
        return {action: self.basis(context, source, action) for action in ('diagnose', 'plan_patch')}

    def resume_operation(self, rid, failure, work, plan, cycle_id, operation):
        stopped = self.store.get(rid, self._stop_key(cycle_id, operation))
        if not stopped:
            return operation
        if self.store.decision_questions(rid, [stopped['question_key']]):
            raise StateConflict('A recorded user question cannot be replaced by recovery progress handling')
        facts = self._facts(rid, failure, work, plan)
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

    def stop(self, rid, failure, work, plan, cycle_id, operation, exclusions, reason):
        question_key = 'recovery-cycle/' + digest([cycle_id, operation])
        if self.store.decision_questions(rid, [question_key]):
            raise StateConflict('Preserve the existing recovery question; progress handling cannot supersede consent')
        key = self._stop_key(cycle_id, operation)
        if self.store.get(rid, key) is None:
            self.store.checkpoint(rid, key, {'facts': self._facts(rid, failure, work, plan),
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
                    'reason': 'Two inconclusive diagnoses on identical facts' if action == 'diagnose' else
                              'Two rejected timing-only amendments on identical facts',
                    'resume_condition': 'New relevant source, plan, specification, design or supplied evidence; '
                                        'another wording, permission or invocation is not new evidence.'}
        return excluded
