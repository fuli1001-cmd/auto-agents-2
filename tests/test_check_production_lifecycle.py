"""Production native parser -> Engine recovery -> actual check closure -> delivery.

The provider is a local deterministic CLI, not a live model. Commands, schema
validation, reference remapping, journal activation and evidence gates are real.
"""
import asyncio
import json
import sys

import pytest
import test_dispatched_recheck_order as recheck_fixture
import test_dispatched_repair_order as repair_fixture
from test_workflow import CodingAgent
from test_workflow import engine as base_engine

from auto_agents_2.check_production import ACTIVE_KEY, REQUIRED_KEY, active_receipt
from auto_agents_2.models import CheckProducer
from auto_agents_2.providers import ProviderRouter
from auto_agents_2.util import write_json


class NativeFixture:
    def __init__(self, workflow, semantic_agent):
        self.inner = semantic_agent
        self.observations = []
        root = workflow.store.root
        binary, self.response = root/'closure-native-provider', root/'closure-native-response.json'
        native = root/'closure-native-home'
        native.mkdir(exist_ok=True)
        binary.write_text(f'#!{sys.executable}\n' + '''import sys,json,pathlib
args=sys.argv[1:]
if args == ['--version']:
    print('closure-native-fixture 1'); sys.exit(0)
wire=sys.stdin.read()
schema=json.loads(pathlib.Path(args[args.index('--output-schema')+1]).read_text())
if 'text_token' in schema['properties']:
    context=json.loads(wire.split('<context>\\n',1)[1].split('\\n</context>',1)[0])
    response=json.dumps({'text_token':pathlib.Path(context['context_files'][0]).read_text(),'image_token':''})
else:
    response=pathlib.Path(__RESPONSE__).read_text()
pathlib.Path(args[args.index('-o')+1]).write_text(response)
print(json.dumps({'type':'thread.started','thread_id':'closure-native-fixture'}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))
'''.replace('__RESPONSE__', repr(str(self.response))))
        binary.chmod(0o755)
        config = workflow.config.load().model_dump(mode='json')
        codex = config['providers']['codex']
        codex.update(binary=str(binary), environment={'CODEX_HOME': str(native)},
                     profile_map={key: {'model': 'test'} for key in ('balanced', 'deep', 'max')})
        config['execution']['infra_retries'] = 0
        config.update(active_provider='codex', providers={'codex': codex})
        write_json(workflow.config.path, config)
        self.router = ProviderRouter(workflow.config, workflow.store, workflow.sandbox)

    async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
        if context.get('verification_observation', {}).get('check_production'):
            assert context['verification_observation']['check_production']['closed']
            self.observations.append(operation)
        response = await self.inner.invoke(stage, context, schema, workspace, run_id, operation, fresh)
        assessment = getattr(response, 'evidence_assessment', None)
        if assessment and assessment.check_production:
            from auto_agents_2.models import PlanResult
            plan = PlanResult.model_validate(context['plan'])
            tasks = {t.id: t for t in plan.tasks}
            for row in assessment.check_production.bindings:
                if row.target == 'acceptance':
                    row.kind = 'command'
                    row.producers = [CheckProducer(index=i, command=c) for i,c in enumerate(tasks[row.task_id].checks)]
                    row.reason = 'Scenario explicitly requires all declared source checks'
        self.response.write_text(json.dumps(response.model_dump(mode='json')))
        return await self.router.invoke(stage, context, schema, workspace, run_id, operation, fresh=fresh)


def native_engine(project, agent):
    workflow = base_engine(project, agent)
    workflow.agent = NativeFixture(workflow, agent)
    return workflow


def install_native(monkeypatch):
    workflows = []
    def create(project, agent):
        workflow = native_engine(project, agent)
        workflows.append(workflow)
        return workflow
    monkeypatch.setattr(repair_fixture, 'engine', create)
    monkeypatch.setattr(recheck_fixture, 'engine', create)
    return workflows


@pytest.mark.asyncio
async def test_native_repair_patch_and_final_closure_deliver_without_repeating_parent(tmp_path, monkeypatch):
    workflows = install_native(monkeypatch)
    await repair_fixture.test_real_patch_repair_runs_before_stale_parent_and_delivers(tmp_path)
    from auto_agents_2.store import Store
    store = Store(tmp_path/'project')
    rid = store.current()['id']
    receipt = active_receipt(lambda k: store.get(rid, k))
    assert receipt and store.get(rid, REQUIRED_KEY)
    # Successful delivery intentionally cleans native temporary prompts. Observe
    # the actual production call boundary instead of assuming those files survive.
    operations = [op for workflow in workflows for op in workflow.agent.observations]
    assert 'develop/task/C/review' in operations and 'final-review' in operations
    parsed = [e for e in store.events(rid) if e['kind'] == 'agent.finished']
    assert parsed  # Native process -> actual production parser, not model_validate_json only.


@pytest.mark.parametrize('boundary', ['applied', 'tested', 'accepted', 'integrated'])
async def test_native_recheck_interruptions_preserve_readonly_closure_and_outcome(tmp_path, monkeypatch, boundary):
    install_native(monkeypatch)
    await recheck_fixture.test_recheck_interruption_resumes_without_implementation(tmp_path, monkeypatch, boundary)
    from auto_agents_2.store import Store
    store = Store(tmp_path/'project')
    rid = store.current()['id']
    assert store.current()['data']['outcome'] == 'DELIVERED'
    assert active_receipt(lambda k: store.get(rid,k))
    assert store.get(rid, 'task_state/P')['evidence'] == 'valid'
    assert store.get(rid, 'rounds/P')['round'] == 1


@pytest.mark.parametrize('damage', ['missing_receipt', 'stale_receipt', 'missing_outcome'])
async def test_native_retained_state_cannot_bypass_receipt_or_atomic_outcome(tmp_path, monkeypatch, damage):
    install_native(monkeypatch)
    workflow, rid, plan, base, agent, _ = await recheck_fixture.paused_at_recheck(tmp_path, monkeypatch)
    before = list(agent.implementations)
    if damage == 'missing_receipt':
        workflow.store.checkpoint(rid, ACTIVE_KEY, None)
    elif damage == 'stale_receipt':
        receipt = workflow.store.get(rid, ACTIVE_KEY)
        receipt['candidate_hash'] = 'stale'
        workflow.store.checkpoint(rid, ACTIVE_KEY, receipt)
    else:
        saved = workflow.store.get(rid, 'develop/task/C')
        permit = workflow.store.get(rid, saved['recovery_permit'])
        permit['phase'], permit['outcome'] = 'completed', {'tasks': {}}
        workflow.store.checkpoint(rid, saved['recovery_permit'], permit)
    resumed = native_engine(workflow.project, agent)
    async def stop_question(question):
        raise asyncio.CancelledError()
    resumed.question_handler = stop_question
    try:
        result = await resumed.start()
        assert result['status'] != 'SUCCEEDED'
    except asyncio.CancelledError:
        pass
    assert resumed.store.current()['status'] != 'SUCCEEDED'
    assert agent.implementations == before


@pytest.mark.parametrize('change', ['legacy_migration', 'new_producer', 'unchanged'])
async def test_real_prepared_patch_rebinds_mapping_without_reopening_unrelated_implementation(tmp_path, monkeypatch, change):
    from test_contract_activation_contexts import prepare
    from test_stage_contract_corrections import setup_correction

    from auto_agents_2.plan_revision import EvidencePatchReview
    workflow, rid, work, old, record, agent = setup_correction(tmp_path)
    if change == 'legacy_migration':
        workflow.store.checkpoints(rid, {ACTIVE_KEY: None, REQUIRED_KEY: None})
    invoke = agent.invoke
    async def mapped(stage, context, schema, *args, **kwargs):
        result = await invoke(stage, context, schema, *args, **kwargs)
        if change == 'new_producer' and issubclass(schema, EvidencePatchReview):
            row = next(r for r in result.evidence_assessment.check_production.bindings if r.task_id == 'T1')
            from auto_agents_2.models import Command
            row.kind, row.producers = 'command', [CheckProducer(index=0, command=Command.model_validate(context['plan']['tasks'][0]['checks'][0]))]
        return result
    agent.invoke = mapped
    identity, key, journal = await prepare(workflow, rid, record, monkeypatch)
    assert journal['phase'] == 'prepared' and workflow.store.get(rid, 'plan') == old.model_dump()
    assert ('T1' in journal['affected']) == (change != 'unchanged')
    assert 'T1' not in journal['reopen']
    workflow.store.activate_plan_patch(rid, identity)
    workflow.store.activate_plan_patch(rid, identity)
    assert workflow.store.get(rid, 'plan_revision/current')['revision'] == 2
    assert workflow.store.get(rid, 'rounds/T3') == {'round':17}
    assert ('T1' in workflow.store.get(rid, 'develop/done')) == (change == 'unchanged')
    assert active_receipt(lambda key: workflow.store.get(rid, key))


@pytest.mark.parametrize('advance', [False, True])
async def test_final_postreview_gate_keeps_originals_and_nochange_preserves_admission_source(tmp_path, monkeypatch, advance):
    from test_delivery import commit, fixture

    from auto_agents_2.models import PlanResult
    from auto_agents_2.review_evidence import consumer_key, review_consumer
    project, requirements = fixture(tmp_path)
    class FilesAgent(CodingAgent):
        final_returned = False
        advanced = False
        async def invoke(self, stage, context, schema, *args, **kwargs):
            result = await super().invoke(stage, context, schema, *args, **kwargs)
            if getattr(result, 'evidence_assessment', None):
                plan = PlanResult.model_validate(context['plan'])
                for row in result.evidence_assessment.check_production.bindings:
                    row.kind, row.attachment_formats = 'attachments', ['json']
                    if row.target == 'acceptance':
                        row.producers = [CheckProducer(index=0, command=plan.tasks[0].checks[0])]
            if stage == 'verify':
                self.final_returned = True
                operation = args[2]
                if advance and not self.advanced and operation == 'final-review':
                    self.advanced = True
                    (project/'user.txt').write_text('concurrent user work')
                    commit(project)
            return result
    agent = FilesAgent()
    workflow = base_engine(project, agent)
    (project/'initial.txt').write_text('initial')
    commit(project)
    from auto_agents_2 import check_production
    real_observe, phases = check_production.observe, []
    def record_observe(*args, **kwargs):
        result = real_observe(*args, **kwargs)
        if result:
            phases.append(kwargs['phase'])
        return result
    monkeypatch.setattr(check_production, 'observe', record_observe)
    checked = workflow.verifier._check
    async def with_file(*args, **kwargs):
        result = await checked(*args, **kwargs)
        if review_consumer.get():
            result.attachments = [{'path': 'state.json', 'artifact': workflow.artifacts.put(b'{"actual":true}', 'test_diagnostic')}]
        return result
    monkeypatch.setattr(workflow.verifier, '_check', with_file)
    gate = workflow._require_candidate_evidence
    observed = []
    def final_gate(rid, *args, **kwargs):
        if agent.final_returned and kwargs.get('phase') in {'final', 'reuse'}:
            owner = workflow.store.get(rid, consumer_key())
            assert owner['phase'] == 'pending'
            workflow.resources.collect_artifacts()
            observed.append(kwargs['phase'])
        return gate(rid, *args, **kwargs)
    monkeypatch.setattr(workflow, '_require_candidate_evidence', final_gate)
    first = await workflow.start(str(requirements))
    assert first['data']['outcome'] == 'DELIVERED' and 'final' in observed
    assert ('reintegration' in phases) == advance
    receipt = workflow.store.get(first['id'], ACTIVE_KEY)
    agent.final_returned = False
    second = await workflow.start(str(requirements))
    assert second['data']['outcome'] == 'NO_CHANGES' and 'reuse' in observed
    assert workflow.store.get(second['id'], ACTIVE_KEY) == receipt
    assert workflow.store.get(second['id'], 'evidence_admission/inherited')['run_id'] == first['id']
