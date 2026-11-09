"""Behaviour and fault injection for disposable, proven native cache leaves."""
import asyncio
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_runtime import configured

from auto_agents_2 import native_cache
from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.models import DocumentResult
from auto_agents_2.providers import ProviderRouter
from auto_agents_2.resources import ResourceManager
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.state_codec import delete_query_db
from auto_agents_2.transports import CodexTransport
from auto_agents_2.util import digest, file_lock, write_json

LAYOUT = native_cache.observed_layout(CodexTransport(), b"codex-cli 0.163.0\n", "a" * 64)


def setup_scene(tmp_path, *, joined=True, version=True, seal=True):
    config, store = configured(tmp_path)
    rid = store.admit("synthetic native-cache lifecycle", {})["id"]
    manager = ResourceManager(store, config.load().cleanup)
    root = store.root / "sessions" / rid / digest("codex")[:16] / digest("operation")[:20]
    invocation = {"attempt": "attempt_fixture", "operation": "fixture",
                  "provider": "codex", "session_key": "session/fixture", "input_identity": "fixture-input"}
    handle = native_cache.begin(manager, root, rid, invocation, LAYOUT if version else None)
    for name in native_cache.FILES:
        directory = root / "cache" / name
        directory.mkdir(parents=True)
        leaf = ("a" * (16 if name == "remote_plugin_catalog" else 40)) + ".json"
        (directory / leaf).write_text("synthetic regenerable discovery data")
    # These are deliberately outside the allowlist, with unmistakable placeholders.
    preserved = ["sessions/history.jsonl", "auth.json", "config.toml",
                 "cache/unknown/cache.json", "audit/approval.json"]
    for name in preserved:
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("EXAMPLE_PLACEHOLDER " + name)
    artifact = ArtifactStore(store.root).put({"approved": False, "summary": "fixture"}, "review")
    store.checkpoints(rid, {
        "provider-result/fixture": {"attempt": invocation["attempt"], "artifact": artifact},
        "invocation/fixture": {"artifact": artifact, "result": {"approved": False},
                               "attempt": invocation["attempt"], "identity": invocation["input_identity"],
                               "cleanup_record": str(store.root / "processes/process_fixture.json")},
        "infra/fixture/codex": {"pending": False, "failures": 3, "grant": 4},
        "session/fixture": "fixture-session",
        "budget/fixture": {"calls": 7, "replans": 27},
        "prototype/approval": {"approved": True, "binding": "fixture-original"},
    })
    process = SimpleNamespace(cleanup_confirmed=joined, returncode=0,
                              supervisor=SimpleNamespace(returncode=0), pid=12345,
                              record=store.root / "processes/process_fixture.json")
    if seal:
        native_cache.seal(manager, handle, process)
    return SimpleNamespace(config=config, store=store, rid=rid, manager=manager, root=root,
                           handle=handle, process=process, artifact=artifact, preserved=preserved)


def metadata(scene):
    with scene.store.connect() as db:
        return json.loads(db.execute("SELECT data FROM resources WHERE id=?",
                                    (scene.handle["resource"],)).fetchone()[0])


def frozen_state(scene):
    return {key: scene.store.get(scene.rid, key) for key in (
        "provider-result/fixture", "invocation/fixture", "infra/fixture/codex",
        "session/fixture", "budget/fixture", "prototype/approval")}


def test_reclaims_only_proven_cache_and_preserves_session_and_gate_evidence(tmp_path):
    scene = setup_scene(tmp_path)
    before = frozen_state(scene)
    bodies = {name: (scene.root / name).read_bytes() for name in scene.preserved}
    first = scene.manager.collect(dry_run=True)
    assert len(first["session_cache"]["candidates"]) == 3
    assert all((scene.root / "cache" / name).is_dir() for name in native_cache.FILES)
    result = scene.manager.collect()
    assert result["session_cache"]["files_removed"] == 3
    assert result["session_cache"]["bytes"] > 0 and not result["errors"]
    assert frozen_state(scene) == before
    assert {name: (scene.root / name).read_bytes() for name in scene.preserved} == bodies
    assert ArtifactStore(scene.store.root).bytes(scene.artifact)
    with scene.store.connect() as db:
        row = db.execute("SELECT refs,state FROM resources WHERE id=?", (scene.handle["resource"],)).fetchone()
        assert tuple(row) == (1, "live")
    assert metadata(scene)["native_cache"]["phase"] == "collected"
    assert metadata(scene)["native_cache"]["caches"] == {}
    assert scene.manager.collect()["session_cache"]["files_removed"] == 0


@pytest.mark.parametrize("case", [
    "active", "unjoined", "unknown-version", "legacy", "pending", "response-drift",
    "session-drift", "missing-infra", "hold", "pin", "registered-child", "unknown-process",
])
def test_unfinished_unknown_or_referenced_cache_is_retained(tmp_path, case):
    scene = setup_scene(tmp_path, joined=case != "unjoined",
                        version=case != "unknown-version", seal=case != "active")
    if case == "legacy":
        with scene.store.connect() as db:
            db.execute("UPDATE resources SET data='{}' WHERE id=?", (scene.handle["resource"],))
    elif case == "pending":
        scene.store.checkpoint(scene.rid, "infra/fixture/codex", {"pending": True})
    elif case == "response-drift":
        scene.store.checkpoint(scene.rid, "provider-result/fixture", {"attempt": "other", "artifact": scene.artifact})
    elif case == "session-drift":
        scene.store.checkpoint(scene.rid, "session/fixture", "different-native-session")
    elif case == "missing-infra":
        with scene.store.connect() as db:
            delete_query_db(db, "SELECT slot_order FROM checkpoint_records WHERE run_id=? AND key='infra/fixture/codex'", (scene.rid,))
    elif case == "hold":
        scene.manager.hold(scene.root, "explicit recovery consumer", scene.rid)
    elif case == "pin":
        with scene.store.connect() as db:
            db.execute("INSERT INTO delivery_pins VALUES(?,?)", (str(scene.root), scene.rid))
    elif case == "registered-child":
        scene.manager.register(scene.root / "cache/remote_plugin_catalog", "cache", scene.rid, refs=0)
    elif case == "unknown-process":
        directory = scene.store.root / "processes"
        directory.mkdir(exist_ok=True)
        (directory / "process_unknown.json").write_text("{}")
    before = frozen_state(scene)
    result = scene.manager.collect()
    assert result["session_cache"]["files_removed"] == 0
    assert all((scene.root / "cache" / name).is_dir() for name in native_cache.FILES)
    assert frozen_state(scene) == before


@pytest.mark.parametrize("case", ["symlink", "hardlink", "unknown-name", "nested-dir", "oversized", "too-many"])
def test_unknown_cache_layout_is_not_adopted(tmp_path, case):
    scene = setup_scene(tmp_path, seal=False)
    directory = scene.root / "cache/remote_plugin_catalog"
    ordinary = next(directory.iterdir())
    target = tmp_path / "user-owned.txt"
    target.write_text("EXAMPLE_PLACEHOLDER preserve")
    if case == "symlink":
        ordinary.unlink()
        ordinary.symlink_to(target)
    elif case == "hardlink":
        ordinary.unlink()
        os.link(target, ordinary)
    elif case == "unknown-name":
        (directory / "unexpected-state.bin").write_text("preserve")
    elif case == "nested-dir":
        (directory / "recovery").mkdir()
    elif case == "oversized":
        with ordinary.open("r+b") as stream:
            stream.truncate(64 * 1024 * 1024 + 1)
    else:
        for index in range(native_cache.MAX_FILES + 1):
            (directory / f"{index:016x}.json").touch()
    native_cache.seal(scene.manager, scene.handle, scene.process)
    result = scene.manager.collect()
    assert directory.exists()
    assert target.read_text() == "EXAMPLE_PLACEHOLDER preserve"
    assert "remote_plugin_catalog" in metadata(scene)["native_cache"]["retained_layouts"]
    assert result["session_cache"]["files_removed"] == 2


@pytest.mark.parametrize("case", ["root", "cache-parent", "class-dir", "file", "new-file"])
def test_path_or_inventory_drift_after_seal_retains_cache(tmp_path, case):
    scene = setup_scene(tmp_path)
    directory = scene.root / "cache/remote_plugin_catalog"
    if case == "root":
        old = scene.root.with_name("saved-root")
        scene.root.rename(old)
        scene.root.mkdir()
    elif case == "cache-parent":
        parent = scene.root / "cache"
        parent.rename(scene.root / "saved-cache")
        parent.mkdir()
    elif case == "class-dir":
        directory.rename(directory.with_name("saved-class"))
        directory.mkdir()
    elif case == "file":
        leaf = next(directory.iterdir())
        leaf.write_text("changed content")
    else:
        (directory / ("b" * 16 + ".json")).write_text("new consumer data")
    result = scene.manager.collect()
    assert result["session_cache"]["files_removed"] == 0
    assert result["session_cache"]["errors"]
    assert metadata(scene)["native_cache"]["phase"] == "sealed"


def test_partial_unlink_failure_keeps_durable_intent_and_replays_only_manifest(tmp_path, monkeypatch):
    scene = setup_scene(tmp_path)
    original = native_cache.os.unlink
    calls = 0

    def fail_second(name, *args, **kwargs):
        nonlocal calls
        if str(name).endswith(".json") and kwargs.get("dir_fd") is not None:
            calls += 1
            if calls == 2:
                raise OSError("fixture unlink failure")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(native_cache.os, "unlink", fail_second)
    result = scene.manager.collect()
    assert result["session_cache"]["errors"]
    assert result["session_cache"]["files_removed"] == 1
    assert metadata(scene)["native_cache"]["phase"] == "collecting"
    monkeypatch.setattr(native_cache.os, "unlink", original)
    retry = scene.manager.collect()
    assert retry["session_cache"]["files_removed"] == 2
    assert not retry["session_cache"]["errors"]
    assert metadata(scene)["native_cache"]["phase"] == "collected"
    assert all((scene.root / name).is_file() for name in scene.preserved)


def test_failed_intent_write_never_unlinks(tmp_path, monkeypatch):
    scene = setup_scene(tmp_path)
    original = native_cache._write

    def fail_intent(db, row, data, record, event=None):
        if record["phase"] == "collecting":
            raise OSError("fixture intent persistence failure")
        return original(db, row, data, record, event)

    monkeypatch.setattr(native_cache, "_write", fail_intent)
    result = scene.manager.collect()
    assert result["session_cache"]["files_removed"] == 0
    assert all((scene.root / "cache" / name).is_dir() for name in native_cache.FILES)
    assert metadata(scene)["native_cache"]["phase"] == "sealed"


def test_new_dispatch_waits_for_gc_lock_before_creating_mirror(tmp_path):
    config, store = configured(tmp_path)
    rid = store.admit("lock fixture", {})["id"]
    manager = ResourceManager(store, config.load().cleanup)
    root = store.root / "sessions" / rid / ("a" * 16) / ("b" * 20)
    started = threading.Event()

    def dispatch():
        started.set()
        return native_cache.begin(manager, root, rid,
            {"attempt": "new", "operation": "new", "provider": "codex", "session_key": "new"}, LAYOUT)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with file_lock(store.root / "gc.lock"):
            future = pool.submit(dispatch)
            assert started.wait(2)
            assert not future.done()
            assert not root.exists()
        handle = future.result(timeout=2)
    assert handle["metadata"]["phase"] == "active"


def test_unseal_precedes_new_resume_and_excludes_old_manifest(tmp_path):
    scene = setup_scene(tmp_path)
    before = frozen_state(scene)
    next_call = {**scene.handle["metadata"], "attempt": "next-attempt"}
    next_call.pop("version")
    next_call.pop("phase")
    next_call.pop("root")
    next_call.pop("layout")
    native_cache.begin(scene.manager, scene.root, scene.rid, next_call, LAYOUT)
    assert scene.manager.collect()["session_cache"]["files_removed"] == 0
    assert frozen_state(scene) == before


def test_only_observed_supported_builtin_version_has_cache_contract():
    assert LAYOUT and LAYOUT["cli_version"] == "0.163.0"
    assert native_cache.observed_layout(CodexTransport(), "codex-cli 0.164.0", "a" * 64) is None
    assert native_cache.observed_layout(CodexTransport(), "codex-cli 0.163.0", "bad") is None

    class Custom(CodexTransport):
        pass

    assert native_cache.observed_layout(Custom(), "codex-cli 0.163.0", "a" * 64) is None


@pytest.mark.parametrize("gateway", ["router", "engine"])
async def test_real_managed_provider_seals_after_join_and_recreates_cache_on_resume(tmp_path, monkeypatch, gateway):
    config, store = configured(tmp_path)
    rid = store.admit("synthetic provider cache recreation", {})["id"]
    native = tmp_path / "native"
    native.mkdir()
    (native / "config.toml").write_text("# EXAMPLE_PLACEHOLDER\n")
    binary = tmp_path / "fixture-provider"
    binary.write_text(f"#!{sys.executable}\nROOT={str(store.root)!r}\n" + r'''
import json,pathlib,sys
args=sys.argv[1:]
if "--version" in args:
    print("codex-cli 0.163.0")
    raise SystemExit(0)
sys.stdin.read()
roots=list((pathlib.Path(ROOT)/"sessions").glob("*/*/*"))
assert len(roots)==1
root=roots[0]
history=root/"fixture-history.json"
old=json.loads(history.read_text()) if history.exists() else []
if old:
    assert "resume" in args
    assert not (root/"cache/remote_plugin_catalog").exists()
history.write_text(json.dumps(old+["completed synthetic turn"]))
directory=root/"cache/remote_plugin_catalog"
directory.mkdir(parents=True,exist_ok=True)
(directory/("a"*16+".json")).write_text("synthetic catalog recreated")
result={"summary":"synthetic response","document":"synthetic document",
        "questions":[],"sources":[],"files":[],"browser_actions":[],"design_selection":[]}
pathlib.Path(args[args.index("-o")+1]).write_text(json.dumps(result))
for event in [
 {"type":"thread.started","thread_id":"fixture-native-session"},
 {"type":"turn.started"},
 {"type":"turn.completed","usage":{"input_tokens":11,"output_tokens":3}}
]:
 print(json.dumps(event),flush=True)
''')
    binary.chmod(0o755)
    body = config.load().model_dump(mode="json")
    body["providers"] = {"codex": body["providers"]["codex"]}
    body["providers"]["codex"].update(binary=str(binary), environment={"CODEX_HOME": str(native)})
    write_json(config.path, body)
    router = ProviderRouter(config, store, SandboxRunner(store.root, _test_unconfined=True))
    ref = ArtifactStore(store.root).put("synthetic read attachment", "fixture")
    context = {"context_files": [str(store.root / "artifacts" / ref["hash"][:2] / ref["hash"])]}
    workflow = None
    if gateway == "engine":
        from auto_agents_2.engine import WorkflowEngine
        workflow = WorkflowEngine(tmp_path, agent=router, sandbox=router.sandbox)

        async def space(**kwargs):
            return {"version": 1, "observed_at": 123.0, "platform": "Linux",
                    "volumes": [{"name": "filesystem", "free": 50 * 1024**3, "total": 100 * 1024**3}],
                    "errors": []}

        monkeypatch.setattr(workflow.storage, "sample", space)

    # Establish capability after Engine configures the final runtime environment.
    # The real bounded --version invocation still runs; only the independent
    # material-read challenge is already certified for this synthetic transport.
    eff = router.effective("readme", "codex")
    from auto_agents_2.readview import VIEW_VERSION
    from auto_agents_2.prompts import PROMPT_VERSION
    from auto_agents_2.workspace import file_digest
    identity = digest([eff["fingerprint"], file_digest(binary), digest(b"codex-cli 0.163.0\n"),
                       VIEW_VERSION, PROMPT_VERSION, config.load().sensitive_paths, False])
    store.checkpoint(rid, "material-capability/" + identity, {"status": "observed_read"})

    async def invoke(count):
        current = {**context, "turn": count}
        if workflow:
            return await workflow._invoke_once("readme", current, DocumentResult, tmp_path, rid, "fixture-native")
        return await router.invoke("readme", current, DocumentResult, tmp_path, rid, "fixture-native")

    for count in (1, 2):
        await invoke(count)
        assert not list((store.root / "processes").glob("*.json"))
        with store.connect() as db:
            row = db.execute("SELECT path,data FROM resources WHERE kind='session'").fetchone()
        mirror = Path(row["path"])
        record = json.loads(row["data"])["native_cache"]
        if workflow:
            receipt = store.get(rid, "invocation/fixture-native")["provider_receipt"]
            assert receipt["version"] == 1 and receipt["attempt"] == record["attempt"]
            assert receipt["identity"] == record["input_identity"]
            assert receipt["cleanup_record"] == record["joined"]["record"]
        assert record["phase"] == "sealed" and record["joined"]["cleanup_confirmed"]
        assert record["joined"]["supervisor_returncode"] == 0
        assert not store.get(rid, "infra/fixture-native/codex")["pending"]
        history = (mirror / "fixture-history.json").read_bytes()
        assert ResourceManager(store, config.load().cleanup).collect()["session_cache"]["files_removed"] == 1
        assert (mirror / "fixture-history.json").read_bytes() == history
        assert len(json.loads(history)) == count
    if workflow:
        await invoke(2)  # Reuse the Engine result after its caches were reclaimed.
        assert len(json.loads((mirror / "fixture-history.json").read_bytes())) == 2
    assert store.usage_summary(rid)["total_tokens"] == 28


def test_old_completion_cannot_reseal_a_new_active_generation(tmp_path):
    scene = setup_scene(tmp_path)
    next_call = {key: scene.handle["metadata"][key] for key in
                 ("attempt", "operation", "provider", "session_key", "input_identity")}
    next_call["attempt"] = "new-active-attempt"
    native_cache.begin(scene.manager, scene.root, scene.rid, next_call, LAYOUT)
    native_cache.seal(scene.manager, scene.handle, scene.process)
    assert metadata(scene)["native_cache"]["phase"] == "active"
    assert metadata(scene)["native_cache"]["attempt"] == "new-active-attempt"
    assert scene.manager.collect()["session_cache"]["files_removed"] == 0


@pytest.mark.parametrize("change", [
    {"attempt": "unrelated-attempt"}, {"identity": "different-input"},
    {"cleanup_record": "/unrelated/process.json"},
])
def test_changed_completion_binding_preserves_the_cache(tmp_path, change):
    scene = setup_scene(tmp_path)
    invocation = scene.store.get(scene.rid, "invocation/fixture")
    scene.store.checkpoint(scene.rid, "invocation/fixture", {**invocation, **change})
    assert scene.manager.collect()["session_cache"]["files_removed"] == 0
    assert all((scene.root / "cache" / name).is_dir() for name in native_cache.FILES)


@pytest.mark.parametrize("failure", ["workload", "supervisor", "artifact"])
def test_unsuccessful_or_unverifiable_completion_cannot_seal(tmp_path, failure):
    scene = setup_scene(tmp_path, seal=False)
    if failure == "workload":
        scene.process.returncode = 1
    elif failure == "supervisor":
        scene.process.supervisor.returncode = 1
    else:
        sha = scene.artifact["hash"]
        (scene.store.root / "artifacts" / sha[:2] / sha).write_text("corrupt synthetic response")
    if failure == "artifact":
        with pytest.raises(ValueError, match="integrity"):
            native_cache.seal(scene.manager, scene.handle, scene.process)
    else:
        native_cache.seal(scene.manager, scene.handle, scene.process)
    assert metadata(scene)["native_cache"]["phase"] == "active"
    assert scene.manager.collect()["session_cache"]["files_removed"] == 0


def test_intent_transaction_rollback_keeps_all_files_and_prior_seal(tmp_path, monkeypatch):
    from contextlib import contextmanager
    scene = setup_scene(tmp_path)
    original = scene.store.connect

    @contextmanager
    def fail_before_commit():
        with original() as db:
            yield db
            if db.in_transaction:
                row = db.execute("SELECT data FROM resources WHERE id=?", (scene.handle["resource"],)).fetchone()
                if row and json.loads(row[0])["native_cache"]["phase"] == "collecting":
                    raise OSError("fixture durability failure before intent commit")

    monkeypatch.setattr(scene.store, "connect", fail_before_commit)
    result = scene.manager.collect()
    assert result["session_cache"]["files_removed"] == 0
    assert result["session_cache"]["errors"]
    assert metadata(scene)["native_cache"]["phase"] == "sealed"
    assert all((scene.root / "cache" / name).is_dir() for name in native_cache.FILES)


@pytest.mark.parametrize("receipt", [None, [], {}, {"version": 2}])
def test_unknown_engine_receipt_never_falls_back_to_old_transport_fields(tmp_path, receipt):
    scene = setup_scene(tmp_path)
    original = scene.store.get(scene.rid, "invocation/fixture")
    scene.store.checkpoint(scene.rid, "invocation/fixture", {**original, "provider_receipt": receipt})
    assert scene.manager.collect()["session_cache"]["files_removed"] == 0
    assert all((scene.root / "cache" / name).is_dir() for name in native_cache.FILES)
