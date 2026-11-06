import copy
import sys

import pytest

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.context import ContextBuilder
from auto_agents_2.context_plan import CONTEXT_VERSION, ContextPlanner
from auto_agents_2.errors import ProviderError
from auto_agents_2.evidence import invocation_identity
from auto_agents_2.models import IntakeResult
from auto_agents_2.prompts import PROMPT_VERSION
from auto_agents_2.providers import VIEW_VERSION, ProviderRouter
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.transports import CodexTransport
from auto_agents_2.util import digest


@pytest.mark.parametrize('with_file', [False, True])
def test_complete_public_snapshot_does_not_request_directory_discovery(tmp_path, with_file):
    if with_file:
        (tmp_path / 'requirements.md').write_text('FULL_FIXTURE_REQUIREMENTS')
    view = ContextBuilder().select(tmp_path)
    assert len(view['files']) == view['public_file_count'] == int(with_file)
    assert view['read_on_demand'] == []
    assert 'do not list directories, run commands' in view['discovery']
    assert 'excluded files in the execution workspace' in view['discovery']
    assert view['view_contract']['original_execution_workspace_mounted'] is False


@pytest.mark.parametrize('mode', ['oversize', 'binary'])
def test_incomplete_public_snapshot_keeps_native_discovery_available(tmp_path, mode):
    (tmp_path / 'app.py').write_bytes(b'x' * 100001 if mode == 'oversize' else b'\xff\xfe')
    view = ContextBuilder().select(tmp_path, paths=['app.py'])
    assert view['public_file_count'] == 1 and view['files'] == []
    assert view['read_on_demand'][0]['path'] == 'app.py'
    assert view['discovery'].startswith('Search the mounted public workspace')


def test_completeness_includes_mandatory_instructions_appended_after_selection(tmp_path):
    (tmp_path / 'src').mkdir()
    (tmp_path / 'src/app.py').write_text('VALUE = 1\n')
    (tmp_path / 'src/AGENTS.md').write_text('Keep the fixture contract.\n')
    view = ContextBuilder().select(tmp_path)
    assert {item['path'] for item in view['files']} == {'src/app.py', 'src/AGENTS.md'}
    assert view['public_file_count'] == 2
    assert 'do not list directories, run commands' in view['discovery']


def fixture_router(tmp_path):
    from test_runtime import configured

    (tmp_path / 'requirements.md').write_text('FULL_FIXTURE_REQUIREMENTS')
    config, store = configured(tmp_path)
    rid = store.admit('fixture', {})['id']
    router = ProviderRouter(config, store, SandboxRunner(store.root))
    context = {'raw_requirements': 'FULL_FIXTURE_REQUIREMENTS', 'raw_requirements_truncated': False,
               'project': ContextBuilder().build(tmp_path), 'context_files': []}
    return config, store, rid, router, context


def old_presentation(router, context, workspace, rid, identity, version='context-v7'):
    artifacts = ArtifactStore(router.store.root)
    record = ContextPlanner(artifacts).prepare('clarify', context, workspace, 'clarify', run_id=rid).record()
    record['version'] = version
    record['payload']['project']['discovery'] = 'LEGACY_V7_DIRECTORY_DISCOVERY'
    ref = artifacts.put(record, 'prepared_context')
    binding = {'identity': identity, 'artifact': ref}
    router.store.checkpoint(rid, 'presentation/clarify/' + identity, binding)
    return binding, record


def test_v7_presentation_is_retained_and_rerendered_without_resetting_budget(tmp_path):
    config, store, rid, router, context = fixture_router(tmp_path)
    identity = invocation_identity('clarify', context, IntakeResult, tmp_path, config.load())
    binding, legacy = old_presentation(router, context, tmp_path, rid, identity)
    budget = {'failures': 2, 'grant': 1, 'pending': False, 'error_kind': 'unavailable'}
    store.checkpoint(rid, 'infra/clarify/fixture', budget)
    current = router._prepare_context('clarify', context, tmp_path, rid, 'clarify', identity)
    assert current.version == CONTEXT_VERSION == 'context-v8'
    assert 'do not list directories, run commands' in current.payload['project']['discovery']
    assert current.payload['project']['files'][0]['content'] == 'FULL_FIXTURE_REQUIREMENTS'
    assert current.payload.get('context_files', []) == []
    assert store.get(rid, 'presentation/clarify/' + identity) == binding
    assert ArtifactStore(store.root).json(binding['artifact']) == legacy
    assert store.get(rid, 'presentation/clarify/' + identity + '/' + CONTEXT_VERSION)
    assert store.get(rid, 'infra/clarify/fixture') == budget
    assert router._prepare_context('clarify', context, tmp_path, rid, 'clarify', identity).record() == current.record()
    assert invocation_identity('clarify', context, IntakeResult, tmp_path, config.load()) == identity


@pytest.mark.parametrize('version', ['context-v6', 'context-v999'])
def test_unreviewed_presentation_versions_remain_blocked(tmp_path, version):
    _, store, rid, router, context = fixture_router(tmp_path)
    binding, _ = old_presentation(router, context, tmp_path, rid, 'identity', version)
    with pytest.raises(ProviderError, match='Unsupported frozen input presentation'):
        router._prepare_context('clarify', context, tmp_path, rid, 'clarify', 'identity')
    assert store.get(rid, 'presentation/clarify/identity') == binding
    assert store.get(rid, 'presentation/clarify/identity/' + CONTEXT_VERSION) is None


def test_corrupt_v7_presentation_cannot_be_replaced_by_a_new_render(tmp_path):
    _, store, rid, router, context = fixture_router(tmp_path)
    binding, _ = old_presentation(router, context, tmp_path, rid, 'identity')
    sha = binding['artifact']['hash']
    (store.root / 'artifacts' / sha[:2] / sha).write_text('corrupt')
    with pytest.raises(ValueError, match='integrity'):
        router._prepare_context('clarify', context, tmp_path, rid, 'clarify', 'identity')
    assert store.get(rid, 'presentation/clarify/identity/' + CONTEXT_VERSION) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['codex', 'antigravity'])
async def test_v8_native_argv_does_not_resume_retained_v7_conversation(tmp_path, monkeypatch, kind):
    config, store, rid, router, context = fixture_router(tmp_path)
    monkeypatch.setattr(router, 'execution_identity', lambda stage: 'fixture-execution')
    effective = {'kind': kind, 'provider': 'fixture', 'model': 'fixture-model',
                 'fingerprint': 'fixture-settings', 'binary': sys.executable,
                 'prompt_profile': 'auto', 'level': 'deep'}
    identity = invocation_identity('clarify', context, IntakeResult, tmp_path, config.load(),
                                   execution='fixture-execution')
    binding, _ = old_presentation(router, context, tmp_path, rid, identity)
    old_key = '/'.join(['session', 'clarify', 'fixture', VIEW_VERSION, PROMPT_VERSION,
                        'context-v7', effective['fingerprint'], digest(config.load().sensitive_paths)])
    store.checkpoint(rid, old_key, 'fixture-previous-denied-conversation')
    seen = []

    class CapturedNativeArguments(Exception):
        pass

    class Capture(CodexTransport):
        def argv(self, effective, workspace, schema, output, prompt, previous, images, context_files=()):
            seen.append(previous)
            assert 'do not list directories, run commands' in prompt
            assert 'FULL_FIXTURE_REQUIREMENTS' in prompt
            assert list(context_files) == []
            raise CapturedNativeArguments

    router.transports[kind] = Capture()
    with pytest.raises(CapturedNativeArguments):
        await router._call(effective, 'clarify', copy.deepcopy(context), IntakeResult,
                           tmp_path, rid, 'clarify', fresh=False)
    assert seen == [None]
    assert store.get(rid, old_key) == 'fixture-previous-denied-conversation'
    assert store.get(rid, 'presentation/clarify/' + identity) == binding
    assert store.get(rid, 'presentation/clarify/' + identity + '/' + CONTEXT_VERSION)
    assert store.get(rid, 'provider-result/clarify') is None
