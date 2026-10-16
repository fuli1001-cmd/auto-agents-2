import asyncio
import gzip
import os
import subprocess
import sys
import zlib
from pathlib import Path

import httpx
import pytest
from test_recovery_contracts import failure, plan
from test_runtime import configured

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.dependencies import copy_execution_source, pip_inputs
from auto_agents_2.environment import EnvironmentManager
from auto_agents_2.errors import AA2Error
from auto_agents_2.evidence import input_identity
from auto_agents_2.http_body import read_body
from auto_agents_2.models import CheckResult, Command
from auto_agents_2.readview import public_files
from auto_agents_2.recovery import Diagnosis, RepairTarget, adjudicate
from auto_agents_2.replanning import check_criteria
from auto_agents_2.requirements import extract_document
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.workspace import WorkspaceManager, git


def test_model_admission_never_opens_secret_files_or_matches_values(tmp_path, monkeypatch):
    secret = tmp_path / 'secrets'
    secret.mkdir()
    (secret / 'config.json').write_text('{"port":123,"password":"fixture"}')
    (tmp_path / '.env').write_text('HOST=localhost')
    (tmp_path / 'main.py').write_text('PORT=123\nHOST="localhost"\n')
    os.link(secret / 'config.json', tmp_path / 'ordinary.json')
    monkeypatch.setattr('auto_agents_2.readview.sensitive_values', lambda *a: pytest.fail('Admission read secret contents'))
    assert {p.name for p in public_files(tmp_path, ['secrets'])} == {'main.py', 'ordinary.json'}


@pytest.mark.parametrize('suffix', ['--find-links /tmp/outside', '-f/tmp/outside', '-r /tmp/outside', '--constraint=/tmp/outside', '--unknown value'])
def test_every_compound_pip_option_is_validated(tmp_path, suffix):
    with pytest.raises(AA2Error):
        pip_inputs(tmp_path, additional=['--no-index --pre ' + suffix])


def test_compound_pip_includes_preserve_constraints_and_local_snapshot(tmp_path):
    (tmp_path / 'wheels').mkdir()
    (tmp_path / 'wheels/a.whl').write_bytes(b'fixture')
    (tmp_path / 'constraints.txt').write_text('sample<2\n')
    (tmp_path / 'req.txt').write_text('sample>=1\n')
    inputs = pip_inputs(tmp_path, additional=['--no-index --find-links ./wheels -r req.txt -c constraints.txt'])
    requirements, constraints = inputs.rendered(tmp_path)
    assert '--no-index' in requirements and (tmp_path / 'wheels').as_uri() in requirements
    assert 'sample>=1' in requirements and 'sample<2' in constraints


@pytest.mark.parametrize('separator', ['@', ' @ '])
def test_direct_reference_without_spaces_uses_owned_copy(tmp_path, separator):
    work = tmp_path / 'work'
    work.mkdir()
    (work / 'pkg').mkdir()
    (work / 'pkg/module.py').write_text('VALUE=1')
    value = 'sample[test]' + separator + (work / 'pkg').as_uri() + ' ; python_version >= "3.11"'
    inputs = pip_inputs(work, additional=[value])
    copied = tmp_path / 'owned'
    copy_execution_source(work, copied, inputs)
    rendered = inputs.rendered(copied)[0]
    assert (copied / 'pkg').as_uri() in rendered and 'sample[test]' in rendered and 'python_version' in rendered
    with pytest.raises(AA2Error):
        pip_inputs(work, additional=['sample@file:///tmp/external-package'])


@pytest.mark.parametrize('local', ['.', 'dist'])
def test_new_directory_members_cannot_enter_selected_dependency_snapshot(tmp_path, local):
    work = tmp_path / 'work'
    work.mkdir()
    selected = work if local == '.' else work / local
    selected.mkdir(exist_ok=True)
    (selected / 'before.py').write_text('old')
    inputs = pip_inputs(work, additional=['-e .'] if local == '.' else ['--find-links ./dist'])
    (selected / 'after.py').write_text('new')
    with pytest.raises(AA2Error, match='changed'):
        copy_execution_source(work, tmp_path / 'owned', inputs)
    assert not (tmp_path / 'owned/after.py').exists()


async def test_constraints_flag_does_not_select_machine_capture_and_failures_redact(tmp_path, monkeypatch):
    config, store = configured(tmp_path)
    runner = SandboxRunner(store.root, _test_unconfined=True)
    manager = EnvironmentManager(config, runner)
    (tmp_path / '.env').write_text('PRIVATE=fixture-confidential-value')
    monkeypatch.setattr('auto_agents_2.environment.capture_output', lambda *a: pytest.fail('Not a machine command'))
    with pytest.raises(AA2Error) as error:
        await manager._command([sys.executable, '-c', "print('fixture-confidential-value'); raise SystemExit(1)", '-c', 'constraints'],
                               tmp_path, dict(os.environ), [], timeout=5)
    assert 'fixture-confidential-value' not in str(error.value) and '<redacted>' in str(error.value)
    monkeypatch.undo()
    with pytest.raises(AA2Error) as error:
        await manager._command([sys.executable, '-c', "print('fixture-confidential-value'); raise SystemExit(1)"],
                               tmp_path, dict(os.environ), [], timeout=5, machine=True)
    assert 'fixture-confidential-value' not in str(error.value)


def test_linked_node_dependency_and_filesystem_dotdot_affect_identity(tmp_path):
    modules = tmp_path / '.cache/project/node_modules/package'
    modules.mkdir(parents=True)
    (modules / 'module.js').write_text('one')
    (tmp_path / 'backend').symlink_to('.cache/project', target_is_directory=True)
    command = Command(argv=['node', 'index.js'])
    before = input_identity(tmp_path, command)
    (modules / 'module.js').write_text('two')
    assert input_identity(tmp_path, command) != before
    (tmp_path / 'external/subdir').mkdir(parents=True)
    (tmp_path / 'external/config').write_text('one')
    (tmp_path / 'config').write_text('unrelated')
    (tmp_path / 'alias').symlink_to('external/subdir')
    (tmp_path / 'input').symlink_to('alias/../config')
    # Isolate the declared link identity from traversal of its sibling target.
    from auto_agents_2.workspace import input_entry
    before = input_entry(tmp_path / 'input', tmp_path)
    (tmp_path / 'external/config').write_text('two')
    assert input_entry(tmp_path / 'input', tmp_path) != before


def test_utf8_manifest_is_locale_independent(tmp_path):
    (tmp_path / 'package.json').write_bytes('{"name":"中文"}'.encode())
    result = subprocess.run([sys.executable, '-c',
        'from pathlib import Path; from auto_agents_2.profiles import package_manifest; package_manifest(Path("package.json"))'],
        cwd=tmp_path, env={**os.environ, 'LC_ALL': 'C', 'PYTHONUTF8': '0', 'PYTHONCOERCECLOCALE': '0'}, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_invalid_attachments_cannot_satisfy_replan_criteria():
    command = Command(argv=['pytest'], expected_tests=1)
    from auto_agents_2.evidence import CHECKER_VERSION
    from auto_agents_2.util import digest
    result = CheckResult(argv=['pytest'], cwd='.', exit_code=1, process_exit_code=0, duration=0, output='', tests=1,
        source_hash='source', environment_hash='env', command_hash=digest(command.model_dump()), checker_version=CHECKER_VERSION,
        report_status='valid', test_ids=['case'], passed_test_ids=['case'], failure_reasons=['test_attachments_invalid'])
    assert not any(check_criteria([command], [result], 'source', 'env').values())


def test_html_closing_blocks_preserve_bare_text_and_inline_words():
    text, _ = extract_document(b'<p>A</p>B<section>C</section>D<span>E</span>F', 'text/html')
    assert 'A\nB' in text and 'C\nDEF' in text


@pytest.mark.parametrize('target', [RepairTarget(task_id='T3', paths=['outside.py'], evidence_refs=['develop/task/T3'], reason='x'),
                                    RepairTarget(task_id='T2', paths=['part2.py'], evidence_refs=['develop/task/T3'], reason='x')])
def test_same_task_targets_are_validated(target):
    with pytest.raises(AA2Error):
        adjudicate(failure(), plan=plan(), diagnosis=Diagnosis(action='repair', task_ids=['T3'],
            evidence_refs=['develop/task/T3'], reason='x', repair_targets=[target]))


def test_delivery_targets_rejected_and_nonrepair_targets_not_forwarded():
    record = failure(origin_id='delivery', task_id='')
    with pytest.raises(AA2Error, match='Delivery'):
        adjudicate(record, diagnosis=Diagnosis(action='repair', evidence_refs=record.evidence_refs, reason='x',
            repair_targets=[RepairTarget(task_id='T3', paths=['part3.py'], evidence_refs=record.evidence_refs, reason='x')]))
    record = failure(evidence_refs=['develop/task/T3', 'part3.py'])
    decision = adjudicate(record, plan=plan(), diagnosis=Diagnosis(action='recheck', task_ids=['T3'], reason='x',
        evidence_refs=record.evidence_refs, repair_targets=[RepairTarget(task_id='T3', paths=['part3.py'], evidence_refs=record.evidence_refs, reason='x')]))
    assert decision.repair_targets == []


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, data, delay=0):
        self.data, self.delay = data, delay

    async def __aiter__(self):
        for offset in range(0, len(self.data), 17):
            if self.delay:
                await asyncio.sleep(self.delay)
            yield self.data[offset:offset + 17]


@pytest.mark.parametrize('encoding,compress', [('gzip', gzip.compress), ('deflate', zlib.compress)])
async def test_bounded_decoder_accepts_normal_bodies_and_rejects_bombs(encoding, compress):
    data = b'<p>hello</p>' * 50
    response = httpx.Response(200, headers={'Content-Encoding': encoding}, stream=BytesStream(compress(data)))
    assert await read_body(response) == data
    bomb = httpx.Response(200, headers={'Content-Encoding': encoding}, stream=BytesStream(compress(b'x' * (8 * 1024 * 1024))))
    with pytest.raises(AA2Error, match='limit'):
        await read_body(bomb)
    truncated = httpx.Response(200, headers={'Content-Encoding': encoding}, stream=BytesStream(compress(data)[:-3]))
    with pytest.raises(AA2Error, match='Truncated'):
        await read_body(truncated)


@pytest.mark.parametrize('entrypoint', ['requirements', 'research'])
async def test_document_acquisition_has_an_overall_deadline(tmp_path, monkeypatch, entrypoint):
    import auto_agents_2.requirements as requirements
    import auto_agents_2.research as research
    original = httpx.AsyncClient
    def client(**kwargs):
        return original(transport=httpx.MockTransport(lambda request: httpx.Response(200,
            headers={'Content-Type': 'text/plain'}, stream=BytesStream(b'x' * 100, .03))), timeout=30)
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    module = requirements if entrypoint == 'requirements' else research
    monkeypatch.setattr(module, 'ACQUISITION_TIMEOUT', .06)
    artifacts = ArtifactStore(tmp_path / '.aa2')
    with pytest.raises(TimeoutError):
        if entrypoint == 'requirements':
            await requirements.capture('https://example.com/', artifacts)
        else:
            await research.ResearchService(artifacts).capture('https://example.com/')


def test_equal_tree_merge_checkpoint_commits_required_parent(tmp_path):
    manager = WorkspaceManager(tmp_path)
    manager.initialize()
    (tmp_path / 'file').write_text('base')
    base = manager.checkpoint(tmp_path, 'refs/aa2/base', 'base')
    git(tmp_path, 'checkout', '-qb', 'other')
    (tmp_path / 'file').write_text('other')
    other = manager.checkpoint(tmp_path, 'refs/aa2/other', 'other')
    git(tmp_path, 'checkout', '--detach', base)
    (tmp_path / 'file').write_text('ours')
    ours = manager.checkpoint(tmp_path, 'refs/aa2/ours', 'ours')
    with pytest.raises(AA2Error):
        git(tmp_path, '-c', 'user.name=aa2', '-c', 'user.email=a@a', 'merge', '--no-ff', other)
    git(tmp_path, 'checkout', '--ours', 'file')
    git(tmp_path, 'add', 'file')
    intent = manager.checkpoint_intent(tmp_path, 'refs/aa2/final')
    commit = manager.finish_checkpoint(tmp_path, intent)
    assert commit != ours and git(tmp_path, 'rev-list', '--parents', '-n', '1', commit).split()[1:] == [ours, other]
    assert not Path(git(tmp_path, 'rev-parse', '--git-path', 'MERGE_HEAD')).is_file()
    assert manager.finish_checkpoint(tmp_path, intent) == commit


async def test_native_request_admits_ordinary_source_prompt_and_attachment_matching_secret_values(tmp_path, monkeypatch):
    from auto_agents_2.models import ReviewResult
    from auto_agents_2.providers import ProviderRouter
    from auto_agents_2.util import write_json
    config, store = configured(tmp_path)
    rid = store.admit('fixture', {})['id']
    (tmp_path / '.env').write_text('HOST=localhost')
    (tmp_path / 'ordinary.py').write_text("HOST='localhost'")
    artifact = ArtifactStore(store.root).put('localhost', 'raw_requirements')
    attachment = store.root / 'artifacts' / artifact['hash'][:2] / artifact['hash']
    binary = tmp_path / 'native-fixture'
    binary.write_text(f'#!{sys.executable}\n' + '''import json,sys
from pathlib import Path
prompt=sys.stdin.read()
assert 'localhost' in prompt
assert Path('ordinary.py').read_text() == "HOST='localhost'"
assert not Path('.env').exists()
''' + f'assert Path({str(attachment)!r}).read_text() == "localhost"\n' + '''args=sys.argv[1:]
open(args[args.index('-o')+1],'w').write(json.dumps({'approved':True,'summary':'all ordinary material read','findings':[]}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))
''')
    binary.chmod(0o755)
    native = tmp_path / 'native'
    native.mkdir()
    body = config.load().model_dump(mode='json')
    spec = body['providers']['codex']
    spec.update(binary=str(binary), environment={'CODEX_HOME': str(native)})
    body.update(active_provider='codex', providers={'codex': spec})
    write_json(config.path, body)
    router = ProviderRouter(config, store, SandboxRunner(store.root, _test_unconfined=True))
    async def fixture_capability(*args):
        pass  # The subprocess above asserts its actual reads; probe protocol is tested elsewhere.
    monkeypatch.setattr(router, '_ensure_material_read', fixture_capability)
    result = await router.invoke('review', {'text': 'localhost', 'evidence': artifact}, ReviewResult,
                                 tmp_path, rid, 'ordinary-material')
    assert result.approved and store.usage_summary(rid)['total_tokens'] == 2
