"""Source-view absence is not execution absence; real verification remains mandatory."""

import json
import shutil
import subprocess
import sys

import pytest
from authority_review_fixtures import authority_review, seed_original
from test_verification_input_identity import task_fixture
from test_workflow import CodingAgent, engine

from auto_agents_2.context import ContextBuilder
from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.models import Finding
from auto_agents_2.prompts import PromptCompiler
from auto_agents_2.readview import VIEW_VERSION, create_view
from auto_agents_2.recovery import RecoveryNeeded
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.util import digest
from auto_agents_2.workspace import tree_hash


@pytest.mark.parametrize('stage,kind', [
    ('implement', None), ('review', None), ('review', 'test_changes'),
    ('verify', None), ('recovery_diagnosis', None), ('review', 'preview_contract'),
])
def test_shared_public_view_explains_generated_omissions_without_exposing_them(tmp_path, stage, kind):
    project = tmp_path / 'project'
    project.mkdir()
    workflow = engine(project, CodingAgent())
    for name in ['frontend/src/main.js', 'frontend/dist/app.js', 'frontend/.next/server.js',
                 'frontend/node_modules/pkg/index.js', '.env', 'private/secret.txt']:
        file = project / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text('fixture contents')
    context = {'project': ContextBuilder().build(project)}
    if kind:
        context['review_kind'] = kind
    prepared = ContextPlanner(workflow.artifacts, ['private']).prepare(stage, context, project)
    boundary = prepared.payload['project']['view_contract']
    assert boundary['version'] == VIEW_VERSION
    assert boundary['kind'] == 'filtered_source_snapshot'
    assert not boundary['original_execution_workspace_mounted']
    assert {'dist', '.next', 'node_modules'} <= set(boundary['excluded_generated_names'])
    assert 'not evidence of absence' in boundary['absence_semantics']
    view = create_view(project, tmp_path / 'view', ['private'])
    assert (view / 'frontend/src/main.js').is_file()
    for name in ['frontend/dist', 'frontend/.next', 'frontend/node_modules', '.env', 'private']:
        assert not (view / name).exists()
    prompt, _ = PromptCompiler().compile(stage, prepared.payload, '', '')
    assert 'filtered source snapshot' in prompt
    assert 'not prove' in prompt
    # Reading this policy does not mint a successful verification observation.
    assert 'verification_observation' not in prepared.payload


async def test_task_review_receives_real_verification_despite_filtered_dist(tmp_path):
    workflow, rid, work, base, task, plan = await task_fixture(tmp_path)
    seed_original(workflow, rid)
    observed = []

    async def review(stage, context, schema, workspace, run_id, operation, fresh=False):
        assert stage == 'review' and operation.endswith('/review')
        observation = context['verification_observation']
        checks = context['checks']
        commands = context['verification_contract']['commands']
        assert observation['status'] == 'valid_at_gate_entry'
        assert observation['source_hash'] == tree_hash(work)
        assert observation['environment_hash'] == checks[0]['environment_hash']
        assert observation['checks_hash'] == digest(checks)
        assert observation['commands_hash'] == digest(commands)
        assert observation['generated_outputs'] == [{
            'command_index': 0, 'command_hash': checks[0]['command_hash'],
            'outputs': checks[0]['generated_outputs']}]
        assert checks[0]['exit_code'] == 0 and checks[1]['tests'] == 1
        prepared = ContextPlanner(workflow.artifacts).prepare(stage, context, work, operation, run_id=rid)
        assert prepared.payload['verification_observation'] == observation
        assert prepared.payload['project']['view_contract']['kind'] == 'filtered_source_snapshot'
        view = create_view(work, workflow.store.root / 'tmp/model-review-view')
        script = ('from pathlib import Path; import json; print(json.dumps(['
                  'Path("frontend/source.txt").exists(), Path("frontend/dist").exists(), '
                  f'Path({str(work / "frontend/dist")!r}).exists()]))')
        result = subprocess.run(SandboxRunner(workflow.store.root).command(
            [sys.executable, '-B', '-c', script], view, [], hidden=[work]),
            capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == [True, False, False]
        assert (work / 'frontend/dist/index.html').read_text() == 'current build'
        observed.append(observation)
        return authority_review(context, approved=True, summary='Reviewed source and actual passing reports')

    workflow.agent.invoke = review
    assert await workflow._task(rid, task, base, plan, {'specification': {}})
    assert len(observed) == 1
    assert workflow.store.get(rid, 'rounds/T1')['round'] == 6


@pytest.mark.parametrize('gate', ['task', 'final', 'reuse'])
@pytest.mark.parametrize('invalid', ['missing', 'tampered', 'old_checker', 'different_command', 'failed_report'])
async def test_invalid_real_evidence_cannot_produce_gate_observation(tmp_path, gate, invalid):
    workflow, rid, work, base, task, plan = await task_fixture(tmp_path)
    environment = await workflow._environment(work, rid, plan)
    commands = workflow._task_commands(rid, task, work, plan)
    results = await workflow._checks(rid, commands, work, environment, task)
    saved = workflow.store.get(rid, 'develop/task/T1')
    saved.update(phase='tested', checks=[r.model_dump() for r in results])
    workflow.store.checkpoint(rid, 'develop/task/T1', saved)
    if invalid == 'missing':
        shutil.rmtree(work / 'frontend/dist')
    elif invalid == 'tampered':
        (work / 'frontend/dist/index.html').write_text('altered output')
    elif invalid == 'old_checker':
        results[0] = results[0].model_copy(update={'checker_version': 'old-verifier'})
    elif invalid == 'different_command':
        commands[1] = commands[1].model_copy(update={'expected_tests': 2})
    elif invalid == 'failed_report':
        results[1] = results[1].model_copy(update={'report_status': 'invalid'})
    with pytest.raises(RecoveryNeeded):
        if gate == 'task':
            workflow._require_task_evidence(rid, task, saved, commands, results, work, environment)
        else:
            workflow._require_candidate_evidence(rid, commands, results, work, environment,
                                                  phase=gate, expected_source=saved['source_hash'])
    assert workflow.store.get(rid, 'task_state/T1')['implementation'] != 'accepted'
    assert workflow.store.get(rid, 'rounds/T1')['round'] == 6


async def test_current_observation_does_not_override_independent_semantic_rejection(tmp_path):
    workflow, rid, work, base, task, plan = await task_fixture(tmp_path)
    seed_original(workflow, rid)

    async def reject(stage, context, *args, **kwargs):
        assert context['verification_observation']['status'] == 'valid_at_gate_entry'
        return authority_review(context, approved=False, summary='Separate business obligation remains unverified', findings=[
            Finding(severity='blocking', location='backend/test_app.py', description='Missing business case',
                    evidence='The supplied test covers only one input', resolution='Preserve and extend assertions')])

    workflow.agent.invoke = reject
    with pytest.raises(RecoveryNeeded) as caught:
        await workflow._task(rid, task, base, plan, {'specification': {}})
    assert caught.value.failure.kind == 'review'
    assert not workflow.store.get(rid, 'develop/task/T1')['review']['approved']
    assert workflow.store.get(rid, 'task_state/T1')['implementation'] != 'accepted'


async def test_final_and_no_change_review_receive_current_binding_without_authorizing_generation(tmp_path):
    class Agent(CodingAgent):
        def __init__(self):
            super().__init__()
            self.gates = []

        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == 'verify':
                observation = context['verification_observation']
                assert observation['checks_hash'] == digest(context['evidence'])
                assert observation['commands_hash'] == digest(context['verification_contract']['commands'])
                assert observation['source_hash'] == tree_hash(workspace)
                prepared = ContextPlanner(workflow.artifacts).prepare(stage, context, workspace, operation)
                assert prepared.payload['verification_observation'] == observation
                self.gates.append(operation)
            elif stage == 'implement' or context.get('review_kind') == 'test_changes':
                assert 'verification_observation' not in context
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    project = tmp_path / 'project'
    project.mkdir()
    requirements = tmp_path / 'requirements.md'
    requirements.write_text('Add two numbers')
    agent = Agent()
    workflow = engine(project, agent)
    assert (await workflow.start(str(requirements)))['status'] == 'SUCCEEDED'
    workflow = engine(project, agent)
    assert (await workflow.start(str(requirements)))['data']['outcome'] == 'NO_CHANGES'
    assert agent.gates == ['final-review', 'no-change-review']


def test_pending_old_presentation_upgrade_preserves_original_and_completed_records(tmp_path):
    from auto_agents_2.context_plan import CONTEXT_VERSION
    from auto_agents_2.providers import ProviderRouter

    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("filtered source observation", {})["id"]
    router = ProviderRouter(workflow.config, workflow.store, workflow.sandbox)
    context = {"project": ContextBuilder().build(tmp_path)}
    operation, identity = "develop/task/T1/review", "frozen-input-identity"
    key = "presentation/" + operation + "/" + identity
    record = {"version": "context-v5", "purpose": "implementation", "payload": {"project": {}},
              "selections": [], "reference_map": {}}
    original = {"identity": identity, "artifact": workflow.artifacts.put(record, "prepared_context")}
    completed = {"result": {"approved": False, "summary": "Old view lacked output boundary"}}
    workflow.store.checkpoints(rid, {key: original, "invocation/" + operation: completed,
        "rounds/T1": {"round": 4}, "recovery/permits/previous": {"status": "completed"}})
    upgraded = router._prepare_context("review", context, tmp_path, rid, operation, identity)
    assert upgraded.version == CONTEXT_VERSION
    assert upgraded.payload['project']['view_contract']['version'] == VIEW_VERSION
    assert workflow.store.get(rid, key) == original
    assert workflow.artifacts.json(original['artifact']) == record
    assert workflow.store.get(rid, 'invocation/' + operation) == completed
    assert workflow.store.get(rid, 'rounds/T1') == {'round': 4}
    assert workflow.store.get(rid, 'recovery/permits/previous') == {'status': 'completed'}
    assert workflow.store.get(rid, key + '/' + CONTEXT_VERSION)
    assert router._prepare_context('review', context, tmp_path, rid, operation, identity).record() == upgraded.record()
