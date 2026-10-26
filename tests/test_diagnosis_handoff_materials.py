"""Real Engine -> provider parser -> read-only material transport -> adjudication."""
import asyncio
import sys

import pytest
from test_diagnosis_handoff import initial_recheck

from auto_agents_2.providers import ProviderRouter
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.util import write_json


async def complete_with_material_transport(tmp_path, *, damage=None, logs=None):
    project = tmp_path / 'project'
    project.mkdir()
    workflow, rid, work, _, record, first, second = await initial_recheck(
        project, **({'logs': logs} if logs is not None else {}))
    await workflow.recovery.resume(rid)
    native = tmp_path / 'fixture-native-home'
    native.mkdir()
    binary = tmp_path / 'fixture-provider'
    binary.write_text(f'#!{sys.executable}\nEXPECTED = {sorted([first["hash"], second["hash"]])!r}\n' + r'''
import hashlib, json, pathlib, sys
args = sys.argv[1:]
if args == ['--version']:
    print('fixture-cli 1')
    sys.exit(0)
assert args[args.index('-s') + 1] == 'read-only'
schema = json.loads(pathlib.Path(args[args.index('--output-schema') + 1]).read_text())
wire = sys.stdin.read()
context = json.loads(wire.split('<context>\n', 1)[1].split('\n</context>', 1)[0])
if 'text_token' in schema['properties']:
    result = {'text_token': pathlib.Path(context['context_files'][0]).read_text(), 'image_token': ''}
elif 'decision_request' in context:
    assert 'diagnose' not in context['decision_request']['scope']['candidates']
    result = {'action': 'approve', 'selected_action': 'repair', 'reason': 'Use a permitted alternative.',
              'expected_result': 'A protected candidate, still requiring fresh checks.',
              'evidence_refs': ['failure.evidence'], 'answers': []}
else:
    assert context['diagnosis_request']['proposal']['reason'] == 'Compare the two retained installation logs.'
    assert context['history']['recent']
    seen = set()
    def inspect(value):
        if isinstance(value, dict):
            if value.get('hash') in EXPECTED and 'location' in value:
                path = pathlib.Path(value['location'])
                raw = path.read_bytes()
                assert hashlib.sha256(raw).hexdigest() == value['hash']
                try:
                    path.write_bytes(b'must remain read-only')
                except OSError:
                    pass
                else:
                    raise AssertionError('Historical evidence became writable')
                seen.add(value['hash'])
            for child in value.values(): inspect(child)
        elif isinstance(value, list):
            for child in value: inspect(child)
    inspect(context)
    assert sorted(seen) == EXPECTED
    result = {'action': 'recheck', 'task_ids': ['T3'], 'reason': 'Read two original logs: ' + ','.join(EXPECTED),
              'evidence_refs': EXPECTED + ['failure.evidence'], 'repair_targets': []}
pathlib.Path(args[args.index('-o') + 1]).write_text(json.dumps(result))
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}))
''')
    binary.chmod(0o755)
    body = workflow.config.load().model_dump(mode='json')
    body['providers'] = {'codex': body['providers']['codex']}
    body['providers']['codex'].update(binary=str(binary), environment={'CODEX_HOME': str(native)})
    body['providers']['codex']['profile_map'] = {key: {'model': 'fixture'} for key in ('balanced', 'deep', 'max')}
    body['execution']['infra_retries'] = 0
    write_json(workflow.config.path, body)
    workflow.agent = ProviderRouter(workflow.config, workflow.store, SandboxRunner(workflow.store.root))
    operation = f'recovery/diagnose/{record.id}/3'
    if damage:
        path = workflow.artifacts.root / first['hash'][:2] / first['hash']
        if damage == 'missing':
            path.unlink()  # Isolated fixture only; never a delivered project's evidence.
        else:
            path.write_text('corrupt fixture')
        async def preserve(question):
            assert 'evidence material is unavailable or invalid' in question['prompt']
            raise asyncio.CancelledError()
        workflow.question_handler = preserve
        with pytest.raises(asyncio.CancelledError):
            await workflow.recovery.resume(rid)
        assert workflow.store.get(rid, 'invocation/' + operation) is None
        assert workflow.store.get(rid, 'recovery/diagnosis-progress-enrollment/' + operation) is None
    else:
        await workflow.recovery.resume(rid)
        invocation = workflow.store.get(rid, 'invocation/' + operation)
        assert invocation['result']['evidence_refs'] == sorted([first['hash'], second['hash']]) + ['failure.evidence']
        assert workflow.store.get(rid, 'recovery/active') is None
        assert workflow.store.get(rid, 'develop/task/T3')['recovery_permit']
        material = workflow.store.get(rid, 'materials/' + operation)
        assert {first['hash'], second['hash']} <= {row['hash'] for row in material['materials']}
        assert not material['omitted_mandatory_content']
    assert not list((workflow.store.root / 'processes').glob('*.json'))
    return workflow, rid, first, second


async def test_actual_diagnosis_sees_both_logs_through_readonly_provider_and_strict_parser(tmp_path):
    await complete_with_material_transport(tmp_path)


@pytest.mark.parametrize('damage', ['missing', 'corrupt'])
async def test_missing_or_corrupt_previous_log_cannot_be_replaced_by_model_claim(tmp_path, damage):
    await complete_with_material_transport(tmp_path, damage=damage)
