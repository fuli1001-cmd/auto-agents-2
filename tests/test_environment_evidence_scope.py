"""Coverage boundaries travel with real observations and do not waive existing gates."""
import asyncio
import json
import shlex
import sys

import pytest
from test_environment_resolution_evidence import installed as installed_fixture
from test_stage_contract_corrections import fixture_patch, review_for, setup_correction
from test_workflow import CodingAgent, engine

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.contract_corrections import validate_contract_review
from auto_agents_2.engine import WorkflowEngine
from auto_agents_2.environment import Environment
from auto_agents_2.environment_contract import resolution_capture_scope
from auto_agents_2.errors import AA2Error
from auto_agents_2.models import ReviewResult
from auto_agents_2.prompts import PromptCompiler
from auto_agents_2.util import digest, write_json

installed = installed_fixture


async def test_capture_explicitly_separates_installed_facts_from_unrecorded_solver_settings(tmp_path, installed):
    manager, env, _, calls = installed
    before = manager.integrity(env.prefix)
    first = await manager.with_resolution(tmp_path, env)
    facts = ArtifactStore(manager.root).json(first.resolution)
    assert facts['capture_scope'] == resolution_capture_scope()
    assert facts['capture_scope']['not_captured']['effective_solver_configuration']['status'] == 'not_collected'
    # Existing package constraints are real facts; they are not the effective solver pins.
    assert facts['conda_packages'][0]['constrains'] == ['python_abi 3.13.*']
    assert facts['environment_hash'] == env.fingerprint
    assert facts['pip_lock'] and facts['conda_explicit_lock']
    assert facts['installation']['status'] == 'not_recorded'
    again = await manager.with_resolution(tmp_path, env)
    assert again.resolution == first.resolution and len(calls) == 2
    assert manager.integrity(env.prefix) == before
    # The fixture only permits the original selected-Python metadata probe: no
    # conda config, host files, settings mutation or resolver invocation was added.


async def test_recapture_is_new_bound_observation_and_does_not_rewrite_legacy_artifact(tmp_path, installed):
    manager, env, _, _ = installed
    artifacts = ArtifactStore(manager.root)
    current = artifacts.json((await manager.with_resolution(tmp_path, env)).resolution)
    old = {k: v for k, v in current.items() if k != 'capture_scope'}
    old_ref = artifacts.put(old, 'environment_resolution')
    old_bytes = artifacts.bytes(old_ref)
    fresh = artifacts.json((await manager.with_resolution(tmp_path, env)).resolution)
    assert artifacts.bytes(old_ref) == old_bytes and 'capture_scope' not in artifacts.json(old_ref)
    assert fresh['environment_hash'] == old['environment_hash']
    assert {k: v for k, v in fresh.items() if k != 'capture_scope'} == old


@pytest.mark.parametrize('stage,context', [
    ('design', {}), ('plan', {}), ('review', {'review_kind': 'plan'}),
    ('plan', {'plan_patch': True}), ('review', {'review_kind': 'plan_patch'}),
    ('implement', {}), ('review', {'review_kind': 'implementation'}),
    ('recovery_diagnosis', {}), ('decision', {'decision_request': {}}), ('verify', {}),
])
def test_all_responsible_roles_share_actual_coverage_and_formal_correction_boundary(stage, context):
    prompt, _ = PromptCompiler().compile(stage, context)
    assert json.dumps(resolution_capture_scope(), ensure_ascii=False) in prompt
    assert 'not an evidence ID or project acceptance' in prompt
    assert 'explicit user obligation or genuinely necessary missing evidence remains blocking' in prompt
    assert 'formal plan_patch.contract_corrections' in prompt
    assert 'Do not silently ignore or delete it' in prompt


@pytest.mark.parametrize('context', [{'input_contract': {'purpose': 'provider_probe'}}, {}])
def test_material_probes_do_not_receive_workflow_repair_instructions(context):
    prompt, _ = PromptCompiler().compile('material_probe', context)
    assert 'The environment_resolution collector has this finite coverage' not in prompt


async def test_real_engine_pause_resume_final_and_nochange_receive_captured_scope(tmp_path, installed):
    manager, env, _, calls = installed
    # The metadata-only fixture normally has a non-executable interpreter marker.
    # This workflow also executes real pytest checks through its selected prefix.
    interpreter = env.prefix / 'bin/python'
    interpreter.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' "$@"\n')
    interpreter.chmod(0o700)
    ready_path = env.prefix / 'aa2-ready.json'
    ready = json.loads(ready_path.read_text())
    ready['integrity'] = manager.integrity(env.prefix)
    write_json(ready_path, ready)
    env = Environment(env.prefix, manager.installed_identity(env.prefix))

    class Reviewer(CodingAgent):
        def __init__(self):
            super().__init__()
            self.paused = False
            self.seen = []

        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            prepared = ContextPlanner(workflow.artifacts).prepare(stage, context, workspace, run_id=run_id)
            if stage in {'plan', 'implement', 'verify'} or stage == 'review' and context.get('review_kind') in {'plan', 'implementation'}:
                prompt, _ = PromptCompiler().compile(stage, prepared.payload)
                assert 'installed_dependency_snapshot' in prompt
            for row in context.get('checks', context.get('evidence', [])):
                facts = workflow.artifacts.json(row['environment_resolution'])
                assert facts['capture_scope'] == resolution_capture_scope()
                assert facts['environment_hash'] == row['environment_hash']
                self.seen.append(operation)
            if operation == 'develop/task/T1/review' and not self.paused:
                self.paused = True
                raise asyncio.CancelledError()
            return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)

    agent = Reviewer()
    workflow = engine(tmp_path, agent)
    workflow.environments = manager
    # Restore the actual Engine environment entrypoints. Only Conda installation
    # and its interpreter probe use the existing offline installed-prefix fixture;
    # no test injects a prebuilt environment_resolution artifact into the caller.
    workflow._environment = WorkflowEngine._environment.__get__(workflow)
    async def ensure(*args, **kwargs):
        return env
    manager.ensure = ensure
    requirements = tmp_path.parent / (tmp_path.name + '-input.md')
    requirements.write_text('Add two numbers')
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(requirements))
    rid = workflow.store.current()['id']
    assert workflow.store.current()['status'] == 'PAUSED'
    rounds = workflow.store.get(rid, 'rounds/T1')['round']
    result = await workflow.start()
    assert result['status'] == 'SUCCEEDED'
    assert workflow.store.get(rid, 'rounds/T1')['round'] == rounds
    assert 'develop/task/T1/review' in agent.seen and 'final-review' in agent.seen
    receipt = workflow.store.get(rid, 'delivery/receipt')
    assert workflow.artifacts.json(receipt['runtime']['resolution'])['capture_scope'] == resolution_capture_scope()
    agent.seen.clear()
    assert (await workflow.start(str(requirements)))['data']['outcome'] == 'NO_CHANGES'
    assert agent.seen and calls


@pytest.mark.parametrize('rejected_duty', ['user_constraints_preserved', 'assertions_preserved', 'collection_preserved'])
async def test_scope_statement_cannot_waive_independent_substance_review(tmp_path, rejected_duty):
    workflow, rid, _, original, _, agent = setup_correction(tmp_path, flag=rejected_duty)
    # A truthful scope declaration is no permission to drop user obligations,
    # assertions or necessary collection. Existing semantic veto remains binding.
    original_hash = digest(workflow.store.get(rid, 'plan'))
    await workflow.recovery.resume(rid)
    assert digest(workflow.store.get(rid, 'plan')) == original_hash
    assert not workflow.store.get(rid, 'plan_revision/current')
    assert agent.views and workflow.store.get(rid, 'rounds/T3') == {'round': 17}
    _, patch = fixture_patch(original, target='acceptance')
    with pytest.raises(AA2Error, match='substance or scope'):
        validate_contract_review(patch, review_for(patch, flag=rejected_duty))


def test_prompt_never_turns_approval_into_an_automatic_acceptance():
    prompt, _ = PromptCompiler().compile('review', {'review_kind': 'plan_patch'})
    assert 'Counts and the author' in prompt
    assert 'cannot certify installation, passing tests or final acceptance' in prompt
    assert ReviewResult(approved=False, summary='Required evidence unavailable').approved is False
