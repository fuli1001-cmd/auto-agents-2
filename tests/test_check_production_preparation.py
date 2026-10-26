"""Explicit file obligations follow only preparations actually scheduled here."""
import sys
from pathlib import Path

import pytest
from evidence_test_fixtures import evidence_review
from test_check_production import plan_and_review
from test_workflow import CodingAgent, engine

from auto_agents_2.check_production import activation_updates
from auto_agents_2.environment import Environment
from auto_agents_2.evidence_admission import admission_record, validate_admission_review
from auto_agents_2.models import Command, EvidenceObligation
from auto_agents_2.recovery import RecoveryNeeded
from auto_agents_2.sandbox import SandboxRunner


@pytest.mark.parametrize('selection', ['dependency', 'duplicate', 'unselected'])
async def test_task_closes_files_of_selected_preparation_only(tmp_path, selection):
    workflow = engine(tmp_path, CodingAgent())
    workflow.verifier.sandbox = SandboxRunner(workflow.store.root)
    rid = workflow.store.admit('selected preparation files', {})['id']
    (tmp_path/'source.txt').write_text('fixture build input')
    plan, _, context = plan_and_review()
    preparation = Command(argv=[sys.executable, '-c', "from pathlib import Path; Path('dist').mkdir(exist_ok=True); Path('dist/result.js').write_text('actual build output')"],
                          inputs=['source.txt'], outputs=['dist'], timeout=60)
    if selection == 'dependency':
        command = Command(argv=[sys.executable, '-c', "from pathlib import Path; assert Path('dist/result.js').is_file()"], inputs=['dist'])
    elif selection == 'duplicate':
        command = preparation.model_copy(update={'inputs': [], 'timeout': 120})
    else:
        command = Command(argv=[sys.executable, '-c', 'print("task without build input")'])
    plan.tasks[0].checks, plan.runtime_prepare = [command], [preparation]
    plan.evidence_contract.obligations = [EvidenceObligation(id='prepare-files', target='runtime_prepare', index=0,
        value=preparation, origin='model_derived', requirement_ids=['R1'], capability='test_file_attachments',
        capability_version=1, producer='VerificationService', production_stage='verification', required_stage='verification')]
    context['plan'] = plan.model_dump()
    review = evidence_review(context)
    validate_admission_review(plan, review, context)
    workflow.store.checkpoints(rid, {'plan': plan.model_dump(), **activation_updates(admission_record(plan, review, context))})
    env = Environment(Path(sys.prefix), 'selected-preparation-fixture')
    task = plan.tasks[0]
    commands = workflow._task_commands(rid, task, tmp_path, plan)
    checks = await workflow._checks(rid, commands, tmp_path, env, task)
    assert all(check.exit_code == 0 for check in checks)
    assert not any(check.attachments for check in checks)
    if selection == 'unselected':
        assert commands == [command]
        result = workflow._require_task_evidence(rid, task, {}, commands, checks, tmp_path, env)
        assert not any(row.get('obligation_id') == 'prepare-files' for row in result['check_production']['closed'])
        final_commands = workflow._candidate_checks(plan, tmp_path)
        final = await workflow._checks(rid, final_commands, tmp_path, env)
        with pytest.raises(RecoveryNeeded) as error:
            workflow._require_candidate_evidence(rid, final_commands, final, tmp_path, env, phase='final')
        assert error.value.failure.kind == 'evidence'
        return
    assert commands[0].inputs == ['source.txt']
    if selection == 'duplicate':
        assert len(commands) == 1 and commands[0].timeout == 120
    with pytest.raises(RecoveryNeeded) as error:
        workflow._require_task_evidence(rid, task, {}, commands, checks, tmp_path, env)
    assert error.value.failure.kind == 'evidence'
    # Real archived-byte identity fixture; the production collector is exercised
    # separately by test_real_report_and_attachment_collector_feed_engine_closure.
    attachment = {'path': 'actual.json', 'artifact': workflow.artifacts.put(b'{"build":true}', 'test_diagnostic')}
    if selection == 'dependency':
        checks[1].attachments = [attachment]
        with pytest.raises(RecoveryNeeded):
            workflow._require_task_evidence(rid, task, {}, commands, checks, tmp_path, env)
        checks[1].attachments = []
    checks[0].attachments = [attachment]
    observed = workflow._require_task_evidence(rid, task, {}, commands, checks, tmp_path, env)
    row = next(row for row in observed['check_production']['closed'] if row.get('obligation_id') == 'prepare-files')
    assert row['check_indexes'] == [0] and row['attachment_hashes'] == [attachment['artifact']['hash']]
