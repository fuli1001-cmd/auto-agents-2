"""Actual sandbox regression for evidence sets exceeding bwrap's argument limit."""
import asyncio
import copy
import json
import sys

import pytest
from test_provider_prompt_delivery import fixture_router

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.errors import PolicyViolation, ProviderError
from auto_agents_2.models import ReviewResult
from auto_agents_2.providers import MOUNT_LIMIT_DIAGNOSTIC, diagnostic_summary
from auto_agents_2.readview import create_artifact_view
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.util import digest


def install_reader(binary):
    binary.write_text(f"#!{sys.executable}\n" + '''
import hashlib, json, pathlib, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("fixture-cli 1")
    sys.exit(0)
schema = json.loads(pathlib.Path(args[args.index("--output-schema")+1]).read_text())
wire = sys.stdin.read()
if "<request_file>\\n" in wire:
    ref = json.loads(wire.split("<request_file>\\n",1)[1].split("\\n</request_file>",1)[0])
    raw = pathlib.Path(ref["path"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == ref["sha256"]
    wire = raw.decode()
context = json.loads(wire.split("<context>\\n",1)[1].split("\\n</context>",1)[0])
if "text_token" in schema["properties"]:
    result = {"text_token": pathlib.Path(context["context_files"][0]).read_text(), "image_token": ""}
else:
    docs = context["documents"]
    seen = set()
    for ref in docs:
        path = pathlib.Path(ref["location"])
        data = path.read_bytes()
        assert hashlib.sha256(data).hexdigest() == ref["hash"]
        seen.add(json.loads(data)["case"])
        try:
            path.write_bytes(b"forbidden")
        except OSError:
            pass
        else:
            raise AssertionError("An admitted artifact became writable")
    assert seen == set(range(context["expected_count"]))
    assert not pathlib.Path(context["unselected_path"]).exists()
    assert not pathlib.Path(context["controller_state"]).exists()
    result = {"approved": False, "summary": str(len(seen)), "findings": []}
pathlib.Path(args[args.index("-o")+1]).write_text(json.dumps(result))
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}))
''')


def fixture_inputs(store, count):
    artifacts = ArtifactStore(store.root)
    docs = [artifacts.put({"case": i}, "document") for i in range(count)]
    unselected = artifacts.put("UNSELECTED_MUST_STAY_HIDDEN", "document")
    return {
        "documents": docs, "expected_count": count,
        "unselected_path": str(artifacts.root / unselected["hash"][:2] / unselected["hash"]),
        "controller_state": str(store.root / "state.sqlite"),
    }


async def test_more_than_three_thousand_artifacts_remain_complete_read_only_and_selective(tmp_path):
    router, project, store, rid = fixture_router(tmp_path)
    install_reader(tmp_path / "provider")
    context = fixture_inputs(store, 3055)
    original = copy.deepcopy(context)
    result = await router.invoke("verify", context, ReviewResult, project, rid, "large-evidence", fresh=True)
    assert not result.approved and result.summary == "3055"
    assert context == original
    material = store.get(rid, "materials/large-evidence")
    assert {r["hash"] for r in context["documents"]} <= {r["hash"] for r in material["materials"]}
    assert material["omitted_mandatory_content"] == []
    assert not list((store.root / "attempts" / rid).iterdir())
    assert not list((store.root / "processes").glob("*.json"))


async def test_concurrent_material_views_have_separate_lifetimes(tmp_path):
    router, project, store, rid = fixture_router(tmp_path)
    install_reader(tmp_path / "provider")
    context = fixture_inputs(store, 3)
    # Certify the provider once before exercising two overlapping evidence consumers.
    await router.invoke("verify", context, ReviewResult, project, rid, "probe-and-read", fresh=True)
    results = await asyncio.gather(*[
        router.invoke("verify", context, ReviewResult, project, rid, operation, fresh=True)
        for operation in ["read-a", "read-b"]
    ])
    assert [r.summary for r in results] == ["3", "3"]
    for ref in context["documents"]:
        assert ArtifactStore(store.root).bytes(ref) == json.dumps(
            {"case": json.loads(ArtifactStore(store.root).bytes(ref))["case"]}, sort_keys=True
        ).encode()
    assert not list((store.root / "attempts" / rid).iterdir())
    assert not list((store.root / "processes").glob("*.json"))


@pytest.mark.parametrize("fault", ["missing", "corrupt", "leaf_link", "parent_link"])
async def test_projection_damage_cannot_start_provider_or_accept_result(tmp_path, monkeypatch, fault):
    router, project, store, rid = fixture_router(tmp_path)
    install_reader(tmp_path / "provider")
    context = fixture_inputs(store, 2)
    original = create_artifact_view

    def damaged(source, destination, files):
        victim = files[-1]
        if fault == "missing":
            victim.unlink()
        elif fault == "corrupt":
            victim.write_text("changed-after-admission")
        elif fault == "leaf_link":
            backup = tmp_path / "held-body"
            victim.replace(backup)
            victim.symlink_to(backup)
        else:
            backup = tmp_path / "held-shard"
            victim.parent.replace(backup)
            victim.parent.symlink_to(backup, target_is_directory=True)
        return original(source, destination, files)

    # Material capability probes precede the tested dispatch.
    await router.invoke("verify", context, ReviewResult, project, rid, "baseline", fresh=True)
    monkeypatch.setattr("auto_agents_2.providers.create_artifact_view", damaged)
    with pytest.raises(ProviderError, match="projected safely"):
        await router.invoke("verify", context, ReviewResult, project, rid, "damaged", fresh=True)
    assert store.get(rid, "invocation/damaged") is None
    assert store.get(rid, "provider-result/damaged") is None
    assert not [e for e in store.events(rid) if e["kind"] == "agent.started" and e["data"]["operation"] == "damaged"]
    assert not list((store.root / "attempts" / rid).iterdir())
    assert not list((store.root / "processes").glob("*.json"))


@pytest.mark.parametrize("fault", ["source_outside", "source_link", "target_outside", "target_root", "target_link"])
def test_readonly_overlay_cannot_escape_owned_state(tmp_path, fault):
    state = tmp_path / "state"
    state.mkdir()
    source = state / "projected"
    source.mkdir()
    target = state / "artifacts"
    target.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if fault == "source_outside":
        source = outside
    elif fault == "source_link":
        link = state / "source-link"
        link.symlink_to(source, target_is_directory=True)
        source = link
    elif fault == "target_outside":
        target = outside
    elif fault == "target_root":
        target = state
    else:
        link = state / "target-link"
        link.symlink_to(target, target_is_directory=True)
        target = link
    with pytest.raises(PolicyViolation, match="Read-only overlays"):
        SandboxRunner(state).command(
            [sys.executable, "-c", "pass"], tmp_path, [], readonly_overlays=[(source, target)]
        )


def test_mount_limit_is_distinguished_from_native_provider_configuration():
    assert diagnostic_summary(
        "bwrap: Exceeded maximum number of arguments 9000\nconfig retry API_KEY=EXAMPLE_PLACEHOLDER"
    ) == MOUNT_LIMIT_DIAGNOSTIC


async def test_running_consumers_pin_only_their_selected_diagnostic_files(tmp_path):
    from auto_agents_2.config import Cleanup
    from auto_agents_2.resources import ResourceManager

    router, project, store, rid = fixture_router(tmp_path)
    binary = tmp_path / "provider"
    binary.write_text(f"#!{sys.executable}\n" + '''
import json, pathlib, sys, time
args = sys.argv[1:]
if args == ["--version"]:
    print("fixture-cli 1")
    sys.exit(0)
schema = json.loads(pathlib.Path(args[args.index("--output-schema")+1]).read_text())
wire = sys.stdin.read()
context = json.loads(wire.split("<context>\\n",1)[1].split("\\n</context>",1)[0])
source = pathlib.Path(context["context_files"][0])
out = pathlib.Path(args[args.index("-o")+1])
if "text_token" in schema["properties"]:
    result = {"text_token": source.read_text(), "image_token": ""}
else:
    out.with_name("ready").write_text("ready")
    while not out.with_name("release").exists():
        time.sleep(0.01)
    result = {"approved": False, "summary": source.read_text(), "findings": []}
out.write_text(json.dumps(result))
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}))
''')
    artifacts = ArtifactStore(store.root)
    resources = ResourceManager(store, Cleanup(min_free_bytes=0))
    refs = [artifacts.put(body, "test_diagnostic") for body in ("selected-a", "selected-b", "unselected")]
    store.checkpoint(rid, "finished-diagnostics", {"logs": refs})
    store.invalidate(rid, ["finished-diagnostics"])

    def context(ref):
        # A plain explicit path has no structured reference/reader lease. The
        # live process's precise read declaration must retain it.
        return {"context_files": [str(artifacts.root / ref["hash"][:2] / ref["hash"])]}

    async def ready(operation):
        async def wait():
            while True:
                starts = [e["data"] for e in store.events(rid)
                          if e["kind"] == "agent.started" and e["data"]["operation"] == operation]
                if starts:
                    out = store.root / "attempts" / rid / starts[-1]["attempt"] / "out"
                    if (out / "ready").exists():
                        return out
                await asyncio.sleep(0.01)
        return await asyncio.wait_for(wait(), 15)

    tasks = []
    try:
        tasks.append(asyncio.create_task(router.invoke(
            "verify", context(refs[0]), ReviewResult, project, rid, "consumer-a", fresh=True)))
        first = await ready("consumer-a")
        tasks.append(asyncio.create_task(router.invoke(
            "verify", context(refs[1]), ReviewResult, project, rid, "consumer-b", fresh=True)))
        second = await ready("consumer-b")
        resources.collect_artifacts()
        assert artifacts.read(refs[0]) == "selected-a"
        assert artifacts.read(refs[1]) == "selected-b"
        assert artifacts.released(refs[2])

        (first / "release").write_text("release")
        assert (await asyncio.wait_for(tasks[0], 15)).summary == "selected-a"
        resources.collect_artifacts()
        assert artifacts.released(refs[0])
        assert artifacts.read(refs[1]) == "selected-b"

        (second / "release").write_text("release")
        assert (await asyncio.wait_for(tasks[1], 15)).summary == "selected-b"
        resources.collect_artifacts()
        assert artifacts.released(refs[1])
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert not list((store.root / "attempts" / rid).iterdir())
    assert not list((store.root / "processes").glob("*.json"))
