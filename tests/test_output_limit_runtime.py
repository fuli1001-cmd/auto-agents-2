"""Real native-process counts and changed-process incident receipt/proof replay."""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_output_limit_recovery import OP, RESULT, admit, approve_runtime_messages, invoke, seed
from test_provider_terminal_state import COMPLETE, START, TURN, fixture_router
from authority_review_fixtures import authority_review, seed_original

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.errors import AA2Error, PostCommitError, RecoveryPathExhausted
from auto_agents_2.engine import WorkflowEngine
from auto_agents_2.models import ImplementationResult, ReviewResult
from auto_agents_2.output_limit_recovery import CALL, KEY, ModelCall, authorize, prepare, raw_hash
from auto_agents_2.output_limit_runtime import controller_identity, publish_activation, quote_paths
from auto_agents_2.util import digest, file_lock
from auto_agents_2.workspace import apply_prepared, prepare_changes, tree_hash


def native_fixture(tmp_path, *, materials=True, probes=True):
    router, project, store, rid = fixture_router(tmp_path, [START, TURN, COMPLETE],
        file_response=json.dumps(RESULT.model_dump()), retries=2)
    binary = Path(router.config.resolve("implement", "codex")["binary"])
    response, counter = tmp_path / "business-response.json", tmp_path / "physical-model-spawns"
    response.write_text(json.dumps(RESULT.model_dump(mode="json")))
    binary.write_text(f"#!{sys.executable}\nRESPONSE={str(response)!r}\nCOUNTER={str(counter)!r}\n" + r'''
import json, pathlib, re, sys
# incident-version-fixture
args = sys.argv[1:]
if args == ['--version']:
    print('codex-cli 0.165.0')
    raise SystemExit(0)
prompt = sys.stdin.read()
schema = json.loads(pathlib.Path(args[args.index('--output-schema')+1]).read_text())
probe = set(schema['properties']) == {'text_token','image_token'}
with open(COUNTER, 'a') as stream:
    stream.write('probe\n' if probe else 'business\n')
if probe:
    paths = re.findall(r'/[^"\s<>]+/artifacts/[0-9a-f]{2}/[0-9a-f]{64}', prompt)
    existing = [pathlib.Path(name) for name in paths if pathlib.Path(name).is_file()]
    assert existing
    result = {'text_token':existing[0].read_text(), 'image_token':''}
else:
    result = json.loads(pathlib.Path(RESPONSE).read_text())
pathlib.Path(args[args.index('-o')+1]).write_text(json.dumps(result))
for event in [{'type':'thread.started','thread_id':'counted-fixture'},
              {'type':'turn.started'}, {'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}]:
    print(json.dumps(event), flush=True)
''')
    extra = {}
    if materials:
        artifacts = ArtifactStore(store.root)
        ref = artifacts.put("Required public fixture material", "document")
        extra["context_files"] = [str(artifacts.root / ref["hash"][:2] / ref["hash"])]
    workflow = WorkflowEngine(project, agent=router, sandbox=router.sandbox)
    seed_original(workflow, rid)
    f = seed(router, project, store, rid, extra)
    f.engine = workflow
    f.workflow = workflow
    if probes:
        f.fields["material_probe_authorization_reference"] = "fixture separate two-probe decision"
        f.request = prepare(project, **f.fields)
        f.path.write_text(json.dumps(f.request))
        f.sha = raw_hash(f.path.read_bytes())
    f.response, f.counter = response, counter
    return f


def physical(f):
    return f.counter.read_text().splitlines() if f.counter.exists() else []


async def engine_correction(f):
    with file_lock(f.store.root / "controller.lock", blocking=False):
        async with approve_runtime_messages(f.store, f.rid):
            with f.incident.workflow_call(f.engine, "implement", f.context, ImplementationResult, f.project, OP):
                return await f.engine._invoke_once("implement", f.context, ImplementationResult, f.project, f.rid, OP,
                                                   interactive_retry=False)


async def review_call(f, role):
    acceptance = "test_changes" if role == "protected_review" else "implementation"
    operation = OP + ("/test-change-review" if role == "protected_review" else "/review")
    saved = f.store.get(f.rid, OP)
    saved.update(implementation=RESULT.model_dump(), phase="implementing")
    if role == "implementation_review":
        patch = prepare_changes(f.project, RESULT.changes, ["app.py"])
        f.incident.prepared("T1", f.project, patch)
        apply_prepared(f.project, patch)
        f.incident.applied("T1", f.project, patch)
        saved.update(phase="tested", source_hash=tree_hash(f.project), checks=[])
    f.store.checkpoint(f.rid, OP, saved)
    context = {**f.context, "review_kind": acceptance}
    if role == "protected_review":
        context["candidate_changes"] = RESULT.model_dump()
    else:
        context.update(source_hash=tree_hash(f.project), implementation=RESULT.model_dump(), checks=[])
    context, schema = f.engine.authority.prepare(f.rid, "review", context, ReviewResult, f.project, operation)
    result = authority_review(context, summary="counted review")
    f.response.write_text(json.dumps(result.model_dump(mode="json")))
    with file_lock(f.store.root / "controller.lock", blocking=False):
        async with approve_runtime_messages(f.store, f.rid):
            with f.incident.workflow_call(f.engine, "review", context, schema, f.project, operation, acceptance):
                await f.engine._invoke_once("review", context, schema, f.project, f.rid, operation, True,
                                            interactive_retry=False)
    return context, schema, operation


def worker(f, payload):
    """A separate interpreter deliberately changes its effective provider environment."""
    input_path = f.project.parent / "restart-input.json"
    input_path.write_text(json.dumps(payload))
    code = r'''
import json,os,sys
from pathlib import Path
from types import SimpleNamespace
from auto_agents_2.config import ConfigStore
from auto_agents_2.engine import WorkflowEngine
from auto_agents_2.models import ImplementationResult,ReviewResult
from auto_agents_2.output_limit_recovery import Incident
from auto_agents_2.providers import ProviderRouter
from auto_agents_2.review_authority import review_schema
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.store import Store
from auto_agents_2.util import digest
sys.path.insert(0,sys.argv[2])
p=json.loads(Path(sys.argv[1]).read_text())
store=Store(Path(p['project']))
sandbox=SandboxRunner(store.root,_test_unconfined=True)
router=ProviderRouter(ConfigStore(Path(p['project'])),store,sandbox)
workflow=WorkflowEngine(Path(p['project']),agent=router,sandbox=sandbox)
incident=Incident(store,p['rid'])
record=incident.require_active()
group='review' if p.get('role') in {'protected_review','implementation_review','material_probe_review'} or p['action'] in {'new_implementation_review','stopped_implementation_review'} else 'implement'
old=record['runtime_binding']['runtime']['profiles'][group]
new=router.effective(group,'codex')['fingerprint']
assert old['fingerprint']!=new and old['execution_component']!=router.execution_identity(group)
if p['action']=='probe':
 incident.reconcile_probe(p['role'],sandbox)
elif p['action']=='new_correction':
 import asyncio
 from auto_agents_2.errors import RecoveryPathExhausted
 from auto_agents_2.util import file_lock
 async def attempt():
  with file_lock(store.root/'controller.lock',blocking=False):
   with incident.workflow_call(workflow,'implement',p['context'],ImplementationResult,Path(p['workspace']),p['operation']):
    await router.invoke('implement',p['context'],ImplementationResult,Path(p['workspace']),p['rid'],p['operation'])
 try:asyncio.run(attempt())
 except RecoveryPathExhausted:pass
 else:raise AssertionError('occupied old probe must not permit new-key correction')
elif p['action'] in {'new_implementation_review','stopped_implementation_review'}:
 import asyncio
 from test_output_limit_runtime import review_call
 f=SimpleNamespace(project=Path(p['project']),store=store,rid=p['rid'],router=router,incident=incident,
   context=p['context'],response=Path(p['response']),
   engine=workflow,workflow=workflow)
 if p['action']=='stopped_implementation_review':
  from auto_agents_2.errors import RecoveryPathExhausted
  try:asyncio.run(review_call(f,'implementation_review'))
  except RecoveryPathExhausted:pass
  else:raise AssertionError('used review probe cannot be replaced by a new key')
 else:asyncio.run(review_call(f,'implementation_review'))
else:
 schema=ImplementationResult if p['role']=='correction' else review_schema(ReviewResult,False)
 import asyncio
 from auto_agents_2.util import file_lock
 async def replay():
  acceptance={'protected_review':'test_changes','implementation_review':'implementation'}.get(p['role'])
  with file_lock(store.root/'controller.lock',blocking=False):
   with incident.workflow_call(workflow,p['stage'],p['context'],schema,Path(p['workspace']),p['operation'],acceptance):
    return await workflow._invoke_once(p['stage'],p['context'],schema,Path(p['workspace']),p['rid'],p['operation'],interactive_retry=False)
 result=asyncio.run(replay())
 assert result is not None
after=incident.record()
saved=store.get(p['rid'],'invocation/'+p.get('operation',''),{})
print(json.dumps({'pid':os.getpid(),'profile_changed':True,'execution_changed':True,'group':group,
 'nested_receipt':bool(saved.get('provider_receipt')),
 'state':after['state'],
 'calls':{k:v['state'] for k,v in after['calls'].items()},
 'capabilities':{k:v['capability_key'] for k,v in after['calls'].items() if k.startswith('material_probe_')}}))
'''
    environment = {**os.environ, "AA2_INCIDENT_RESTART_TEST": "different-process-profile"}
    run = subprocess.run([sys.executable, "-B", "-c", code, str(input_path), str(Path(__file__).parent)], env=environment,
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["pid"] != os.getpid() and result["profile_changed"] and result["execution_changed"]
    return result


async def test_explicit_probe_expansion_is_separate_and_cannot_be_silently_inferred(tmp_path):
    f = native_fixture(tmp_path)
    with pytest.raises(AA2Error, match="two conditional material probes"):
        authorize(f.project, f.path, f.sha, confirm_one_call=True)
    assert f.store.get(f.rid, KEY) is None
    admit(f)
    record = f.incident.record()
    assert record["request"]["authorization_reference"] != record["request"]["material_probe_authorization_reference"]
    assert set(record["limits"]) == {"correction", "protected_review", "implementation_review",
                                     "material_probe_implement", "material_probe_review"}
    assert sum(record["limits"].values()) == 5 and record["runtime_binding"] == {"state": "unbound"}


async def test_required_probe_has_own_slot_before_the_single_correction(tmp_path):
    f = native_fixture(tmp_path)
    admit(f)
    before = f.store.get(f.rid, f.request["infra_key"])
    await invoke(f)
    assert physical(f) == ["probe", "business"]
    calls = f.incident.record()["calls"]
    assert calls["material_probe_implement"]["state"] == "proof_confirmed"
    assert calls["correction"]["state"] == "completed"
    assert f.store.get(f.rid, f.request["infra_key"]) == before
    assert f.store.get(f.rid, "materials/" + OP) == {"original": "retained"}
    with f.store.connect() as db:
        assert tuple(db.execute("SELECT answer,superseded FROM questions WHERE id=?",
                               (f.fields["question_id"],)).fetchone()) == (None, 1)


@pytest.mark.parametrize("role", ["correction", "protected_review", "implementation_review"])
async def test_each_business_receipt_replays_in_a_real_new_process_without_native_spawn(tmp_path, role):
    f = native_fixture(tmp_path, materials=False)
    admit(f)
    await engine_correction(f)
    context, stage, operation = f.context, "implement", OP
    if role != "correction":
        context, _, operation = await review_call(f, role)
        stage = "review"
    assert f.store.get(f.rid, "invocation/" + operation).get("provider_receipt")
    before = physical(f)
    result = worker(f, {"action": "business", "project": str(f.project), "rid": f.rid,
        "role": role, "stage": stage, "context": context, "workspace": str(f.project), "operation": operation})
    assert result["calls"][role] == "completed"
    assert result["nested_receipt"] and result["group"] == ("implement" if role == "correction" else "review")
    assert physical(f) == before


@pytest.mark.parametrize("phase", ["claimed", "native_result", "proof_confirmed"])
@pytest.mark.parametrize("role", ["material_probe_implement", "material_probe_review"])
async def test_probe_reconciliation_in_a_real_new_process_keeps_original_key_and_spawn_count(tmp_path, monkeypatch, phase, role):
    f = native_fixture(tmp_path)
    admit(f)
    if role == "material_probe_review":
        await engine_correction(f)
    original = f.store.confirm_output_limit_probe
    def lost(*args, **kwargs):
        if args[2] != role:
            return original(*args, **kwargs)
        if phase == "proof_confirmed":
            original(*args, **kwargs)
        raise asyncio.CancelledError("fixture interrupts native-result/proof commit boundary")
    if phase == "claimed":
        complete = f.store.complete_output_limit_call
        def lost_completion(*args, **kwargs):
            if args[2] == role:
                raise OSError("fixture native receipt committed before completion notification")
            return complete(*args, **kwargs)
        monkeypatch.setattr(f.store, "complete_output_limit_call", lost_completion)
    else:
        monkeypatch.setattr(f.store, "confirm_output_limit_probe", lost)
    with pytest.raises(PostCommitError if phase == "claimed" else asyncio.CancelledError):
        if role == "material_probe_implement":
            await invoke(f)
        else:
            await review_call(f, "protected_review")
    assert physical(f) == (["probe"] if role == "material_probe_implement" else ["probe", "business", "probe"])
    assert f.incident.record()["calls"][role]["state"] == phase
    before = physical(f)
    old_key = f.incident.record()["calls"][role]["capability_key"]
    result = worker(f, {"action": "probe", "project": str(f.project), "rid": f.rid,
                       "role": role})
    assert result["calls"][role] == "proof_confirmed"
    assert result["capabilities"][role] == old_key and physical(f) == before
    monkeypatch.setattr(f.store, "confirm_output_limit_probe", original)
    stopped = worker(f, {"action": "new_correction" if role == "material_probe_implement" else "stopped_implementation_review",
        "project": str(f.project), "rid": f.rid, "role": role, "response": str(f.response),
        "context": f.context, "workspace": str(f.project), "operation": OP})
    assert stopped["state"] == "stopped" and physical(f) == before


async def test_both_required_probe_groups_and_both_reviews_have_exactly_five_physical_calls(tmp_path):
    f = native_fixture(tmp_path)
    admit(f)
    await invoke(f)
    await review_call(f, "protected_review")
    await review_call(f, "implementation_review")
    assert physical(f) == ["probe", "business", "probe", "business", "business"]
    calls = f.incident.record()["calls"]
    assert set(calls) == set(f.incident.record()["limits"])
    assert all(row["state"] == ("proof_confirmed" if role.startswith("material_probe_") else "completed")
               for role, row in calls.items())
    assert calls["material_probe_implement"]["capability_key"] != calls["material_probe_review"]["capability_key"]


async def test_first_review_cache_hit_leaves_one_probe_for_new_runtime_second_review(tmp_path):
    f = native_fixture(tmp_path)
    admit(f)
    await invoke(f)
    key = f.incident.record()["runtime_binding"]["runtime"]["profiles"]["review"]["capability_key"]
    f.store.checkpoint(f.rid, "material-capability/" + key, {"status": "observed_read", "capability": key})
    await review_call(f, "protected_review")
    assert "material_probe_review" not in f.incident.record()["calls"]
    before = physical(f)
    result = worker(f, {"action": "new_implementation_review", "project": str(f.project), "rid": f.rid,
        "context": f.context, "response": str(f.response)})
    assert before == ["probe", "business", "business"]
    assert physical(f) == before + ["probe", "business"]
    assert result["calls"]["protected_review"] == "completed" and result["calls"]["implementation_review"] == "completed"
    assert result["capabilities"]["material_probe_review"] != key
    assert f.store.get(f.rid, "material-capability/" + key) == {"status": "observed_read", "capability": key}


async def test_wrong_random_proof_stops_without_correction_or_capability(tmp_path):
    f = native_fixture(tmp_path)
    # Modify before preparing the original failed request/profile, never after a quote.
    binary = Path(f.router.config.resolve("implement", "codex")["binary"])
    binary.write_text(binary.read_text().replace("existing[0].read_text()", "'wrong-random-token'"))
    f.request = prepare(f.project, **f.fields)
    f.path.write_text(json.dumps(f.request))
    f.sha = raw_hash(f.path.read_bytes())
    admit(f)
    with pytest.raises(RecoveryPathExhausted):
        await invoke(f)
    assert physical(f) == ["probe"]
    calls = f.incident.record()["calls"]
    assert set(calls) == {"material_probe_implement"} and calls["material_probe_implement"]["state"] == "native_result"
    assert f.store.get(f.rid, "material-capability/" + calls["material_probe_implement"]["capability_key"]) is None


@pytest.mark.parametrize("drift", ["prompt", "schema", "attachment", "view-add", "git-metadata", "projection-add", "native-add", "env"])
async def test_prepared_bytes_and_sets_cannot_change_between_bundle_and_managed_claim(tmp_path, monkeypatch, drift):
    import auto_agents_2.output_limit_runtime as runtime
    f = native_fixture(tmp_path, materials=True)
    admit(f)
    prepare_launch = runtime.prepare_launch
    async def mutate(*args, **kwargs):
        launch = await prepare_launch(*args, **kwargs)
        if drift == "prompt":
            path = next(Path(p) for p in launch["read_files"] if Path(p).name == "prompt.txt")
            path.write_text(path.read_text() + " changed")
        elif drift == "schema":
            path = next(Path(p) for p in launch["read_files"] if Path(p).name == "schema.json")
            path.write_text("{}")
        elif drift == "attachment":
            path = next(Path(p) for p in launch["read_files"] if "/artifacts/" in p)
            path.write_text("changed material")
        elif drift == "view-add":
            (Path(launch["workspace_view_root"]) / "added.py").write_text("unexpected")
        elif drift == "git-metadata":
            path = Path(launch["workspace_view_root"]) / ".git/config"
            path.write_text(path.read_text() + "\n[core]\n    bare = false\n")
        elif drift == "projection-add":
            root = Path(next(iter(launch["artifact_projection_manifests"])))
            (root / "unexpected").write_text("not admitted")
        elif drift == "native-add":
            (Path(launch["native_projection_root"]) / "unexpected.toml").write_text("model='other'")
        else:
            args[8]["AA2_UNEXPECTED_ENV"] = "changed"
        return launch
    monkeypatch.setattr(runtime, "prepare_launch", mutate)
    with pytest.raises(RecoveryPathExhausted):
        await invoke(f)
    assert physical(f) == [] and f.incident.record()["calls"] == {}


async def test_quote_requires_actual_lock_owner_and_message_does_not_write_database(tmp_path):
    f = native_fixture(tmp_path, materials=False)
    admit(f)
    with pytest.raises(AA2Error, match="own controller.lock"):
        controller_identity(f.store.root)
    with file_lock(f.store.root / "controller.lock", blocking=False):
        assert controller_identity(f.store.root)["pid"] == os.getpid()
    assert physical(f) == [] and f.incident.record()["runtime_quotes"] == {}


@pytest.mark.parametrize("field", ["capability_key", "challenge_key", "operation", "launch_profile_key"])
async def test_probe_result_cannot_be_confirmed_to_a_different_key(tmp_path, monkeypatch, field):
    f = native_fixture(tmp_path)
    admit(f)
    def interrupt(*args, **kwargs):
        raise asyncio.CancelledError
    monkeypatch.setattr(f.store, "confirm_output_limit_probe", interrupt)
    with pytest.raises(asyncio.CancelledError):
        await invoke(f)
    record = f.incident.record()
    claim = record["calls"]["material_probe_implement"]
    new_key = "0" * 64
    if field == "capability_key":
        claim["capability_key"] = new_key
    elif field == "challenge_key":
        claim["challenge"]["capability_key"] = new_key
    elif field == "operation":
        claim["operation"] = "material-probe/" + new_key
    else:
        claim["launch"]["profile"]["capability_key"] = new_key
    f.store.checkpoint(f.rid, KEY, record)
    with pytest.raises(AA2Error):
        f.incident.reconcile_probe("material_probe_implement", f.router.sandbox)
    assert f.store.get(f.rid, "material-capability/" + new_key) is None
    assert physical(f) == ["probe"]


async def wait_quote(f):
    for _ in range(1000):
        record = f.incident.record()
        for nonce, entry in record["runtime_quotes"].items():
            path, message = quote_paths(f.store, record["request"], nonce)
            if not entry["revoked"] and not entry["consumed"] and path.is_file():
                return record, nonce, entry, path, message
        await asyncio.sleep(0.01)
    raise AssertionError("controller did not publish its bounded runtime quote")


async def test_external_activation_only_writes_message_and_owner_consumes_nonce_once(tmp_path):
    f = native_fixture(tmp_path, materials=False)
    admit(f)
    with file_lock(f.store.root / "controller.lock", blocking=False):
        with f.incident.workflow_call(f.engine, "implement", f.context, ImplementationResult, f.project, OP):
            task = asyncio.create_task(CALL.get().ensure_runtime(f.router, f.context, ImplementationResult, f.project))
            try:
                record, nonce, entry, path, message = await wait_quote(f)
                before = f.incident.record()
                output = publish_activation(f.project, path, entry["sha256"], "fixture concrete operator decision")
                assert output["database_written"] is False and f.incident.record() == before
                assert message.is_file() and not before["runtime_quotes"][nonce]["consumed"]
                await task
                bound = f.incident.record()
                assert bound["runtime_binding"]["nonce"] == nonce and bound["runtime_quotes"][nonce]["consumed"]
                publish_activation(f.project, path, entry["sha256"], "fixture concrete operator decision")
                assert f.incident.record() == bound
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert physical(f) == [] and f.incident.record()["calls"] == {}


@pytest.mark.parametrize("field", ["nonce", "quote_sha256", "request_hash", "controller", "version"])
async def test_stale_or_changed_activation_never_dispatches_or_consumes_slots(tmp_path, field):
    f = native_fixture(tmp_path, materials=False)
    admit(f)
    with file_lock(f.store.root / "controller.lock", blocking=False):
        with f.incident.workflow_call(f.engine, "implement", f.context, ImplementationResult, f.project, OP):
            task = asyncio.create_task(CALL.get().ensure_runtime(f.router, f.context, ImplementationResult, f.project))
            try:
                _, nonce, entry, path, message = await wait_quote(f)
                publish_activation(f.project, path, entry["sha256"], "fixture concrete operator decision")
                body = json.loads(message.read_text())
                body[field] = {"pid": -1} if field == "controller" else "changed"
                message.write_text(json.dumps(body))
                with pytest.raises(RecoveryPathExhausted):
                    await task
                assert f.incident.record()["runtime_quotes"][nonce]["revoked"]
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert physical(f) == [] and f.incident.record()["calls"] == {}


async def test_cancelled_unclaimed_quote_can_rebind_without_refilling_or_migrating_proof(tmp_path, monkeypatch):
    f = native_fixture(tmp_path, materials=False)
    admit(f)
    with file_lock(f.store.root / "controller.lock", blocking=False):
        with f.incident.workflow_call(f.engine, "implement", f.context, ImplementationResult, f.project, OP):
            task = asyncio.create_task(CALL.get().ensure_runtime(f.router, f.context, ImplementationResult, f.project))
            _, nonce, old, _, _ = await wait_quote(f)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert f.incident.record()["runtime_quotes"][nonce]["revoked"]
            monkeypatch.setenv("AA2_UNUSED_ROLE_QUOTE_RENEWAL", "new-runtime-environment")
            async with approve_runtime_messages(f.store, f.rid):
                await CALL.get().ensure_runtime(f.router, f.context, ImplementationResult, f.project)
            record = f.incident.record()
            assert record["runtime_binding"]["nonce"] != nonce
            assert old["quote"]["runtime"]["profiles"]["implement"]["capability_key"] != record["runtime_binding"]["runtime"]["profiles"]["implement"]["capability_key"]
            assert not record["calls"] and sum(record["limits"].values()) == 5
    assert physical(f) == []


async def test_runtime_and_probe_bookkeeping_do_not_change_source_history_or_original_logical_input(tmp_path, monkeypatch):
    from auto_agents_2.source_history import SourceHistory
    from auto_agents_2.providers import invocation_identity
    f = native_fixture(tmp_path)
    engine = SimpleNamespace(store=f.store, config=f.router.config, artifacts=ArtifactStore(f.store.root))
    history = SourceHistory(engine)
    original = history.collect_task(f.rid, f.project, f.task)
    logical = invocation_identity("implement", f.context, ImplementationResult, f.project, f.router.config.load())
    admit(f)
    original_claim = ModelCall.claim
    observations = []
    def claim(scope, *args, **kwargs):
        if scope.role == "correction":
            observations.append((history.collect_task(f.rid, f.project, f.task),
                invocation_identity("implement", f.context, ImplementationResult, f.project, f.router.config.load())))
        return original_claim(scope, *args, **kwargs)
    monkeypatch.setattr(ModelCall, "claim", claim)
    await invoke(f)
    assert observations == [(original, logical)] and physical(f) == ["probe", "business"]


@pytest.mark.parametrize("materials", [False, True])
async def test_unused_probe_budget_is_not_spent_without_a_required_cache_miss(tmp_path, materials):
    f = native_fixture(tmp_path, materials=materials)
    if materials:
        eff = f.router.effective("implement", "codex")
        key = (await f.router._material_identity(eff, f.project))["key"]
        f.store.checkpoint(f.rid, "material-capability/" + key,
                           {"status": "observed_read", "capability": key, "fixture": "existing exact-key proof"})
    admit(f)
    await invoke(f)
    assert physical(f) == ["business"]
    assert set(f.incident.record()["calls"]) == {"correction"}


@pytest.mark.parametrize("drift", ["execution-source", "native-source", "actual-child-env", "view-after-thread"])
async def test_last_managed_boundary_rejects_drift_after_sandbox_command_preparation(tmp_path, monkeypatch, drift):
    import auto_agents_2.managed_process as managed
    f = native_fixture(tmp_path, materials=False)
    admit(f)
    if drift == "actual-child-env":
        original = managed.spawn_managed
        async def mutate(command, cwd, env, state_root, paths, **kwargs):
            if kwargs.get("_incident_launch") is not None:
                env["AA2_MUTATED_CHILD"] = "changed after sandbox transform"
            return await original(command, cwd, env, state_root, paths, **kwargs)
        monkeypatch.setattr(managed, "spawn_managed", mutate)
    else:
        original = f.router.sandbox.command
        def mutate(argv, cwd, *args, **kwargs):
            command = original(argv, cwd, *args, **kwargs)
            if "--version" not in argv:
                if drift == "execution-source":
                    (f.project / "user-change.py").write_text("preserve this unexpected source")
                elif drift == "native-source":
                    native = f.router.transports["codex"].native_home(f.router.effective("implement", "codex")["environment"])
                    (native / "unexpected.toml").write_text("model='test'")
                else:
                    (cwd / "unexpected-view.py").write_text("changed after to_thread")
            return command
        monkeypatch.setattr(f.router.sandbox, "command", mutate)
    with pytest.raises(RecoveryPathExhausted):
        await invoke(f)
    assert physical(f) == [] and not f.incident.record()["calls"]


async def test_incident_views_do_not_start_background_git_maintenance(tmp_path, monkeypatch):
    from auto_agents_2 import readview
    from auto_agents_2.workspace import git_environment
    f = native_fixture(tmp_path)
    admit(f)
    trace = tmp_path / "temporary-view-git-trace.jsonl"
    operations = []
    def traced_git(work, *args, **kwargs):
        # Trace only owned fixture snapshots, after the existing environment
        # isolation. This does not alter real repository or global Git settings.
        assert Path(work).resolve().is_relative_to((f.store.root / "attempts").resolve())
        environment = git_environment(kwargs.get("env"))
        environment["GIT_TRACE2_EVENT"] = str(trace)
        run = subprocess.run(["git", "--literal-pathspecs", "-C", str(work), *args],
                             env=environment, capture_output=True, timeout=30)
        assert run.returncode == 0
        operations.append((str(work), "commit" in args))
        return os.fsdecode(run.stdout).removesuffix("\n")
    monkeypatch.setattr(readview, "git", traced_git)
    # The ordinary default keeps its established Git behavior. Observe that
    # counterfactual separately; only the two incident views must suppress it.
    readview.create_view(f.project, f.store.root / "attempts" / "default-maintenance-control")
    control_rows = [json.loads(line) for line in trace.read_text().splitlines()]
    control_maintenance = [row for row in control_rows if row.get("event") == "child_start"
                           and any(word in row.get("argv", []) for word in ("maintenance", "gc"))]
    assert control_maintenance
    trace = tmp_path / "incident-view-git-trace.jsonl"
    operations.clear()
    await invoke(f)
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    maintenance = [row for row in rows if row.get("event") == "child_start"
                   and any(word in row.get("argv", []) for word in ("maintenance", "gc"))]
    assert sum(commit for _, commit in operations) == 2  # business and its read-challenge view
    assert maintenance == [] and physical(f) == ["probe", "business"]
