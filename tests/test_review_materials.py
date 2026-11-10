"""Exact public review inputs, independent of controller/runtime writes."""
import os
import sys

import pytest
from test_provider_terminal_state import COMPLETE, MESSAGE, START, TURN, fixture_router

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.models import ReviewResult
from auto_agents_2.prompt_rules import REVIEW_RESPONSIBILITY
from auto_agents_2.prompts import PromptCompiler
from auto_agents_2.providers import EventDecoder
from auto_agents_2.review_materials import ReviewMaterialChanged, ReviewMaterials
from auto_agents_2.sandbox import SandboxRunner


@pytest.mark.parametrize("stage,kind", [
    ("coverage_review", ""), ("verify", ""), ("recovery_diagnosis", ""),
    *(("review", k) for k in ("research", "design", "plan", "plan_patch", "implementation",
      "test_changes", "preview_contract", "prototype", "recovery_diagnosis")),
])
def test_review_roles_share_responsibility_without_replacing_domain_contract(stage, kind):
    prompt, _ = PromptCompiler().compile(stage, {"review_kind": kind}, "gpt-fixture")
    assert prompt.count(REVIEW_RESPONSIBILITY) == 1
    assert "not impose a blanket network ban" in prompt
    assert "replace human prototype approval" in prompt
    assert "Return proposed\nfile changes" not in prompt
    if kind == "plan":
        assert "PRE-IMPLEMENTATION planning gate" in prompt
    if kind == "prototype":
        assert "static interactive prototype" in prompt
    if stage == "recovery_diagnosis":
        assert "diagnos" in prompt.lower()


@pytest.mark.parametrize("stage,purpose", [("review", "provider_probe"), ("material_probe", "material_probe"),
                                            ("implement", "implementation"), ("decision", "decision")])
def test_probes_and_authors_do_not_become_product_reviewers(stage, purpose):
    prompt, _ = PromptCompiler().compile(stage, {"input_contract": {"purpose": purpose}}, "gpt-fixture")
    assert REVIEW_RESPONSIBILITY not in prompt
    if purpose == "provider_probe":
        assert "Do not execute tools" in prompt


def package(tmp_path):
    view = tmp_path / "workspace"
    view.mkdir()
    (view / "source.py").write_text("original")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("complete prompt")
    guard = ReviewMaterials(view)
    guard.admit_files([prompt])
    return guard, view, prompt


@pytest.mark.parametrize("damage", ["rewrite", "delete", "add", "leaf_link", "parent_link", "fifo", "mode"])
def test_snapshot_damage_is_rejected_without_following_links(tmp_path, damage):
    guard, view, prompt = package(tmp_path)
    source = view / "source.py"
    if damage == "rewrite":
        source.write_text("changed")
    elif damage == "delete":
        source.unlink()
    elif damage == "add":
        (view / "extra").write_text("unadmitted")
    elif damage == "leaf_link":
        source.unlink()
        source.symlink_to(prompt)
    elif damage == "parent_link":
        other = tmp_path / "original-view"
        view.rename(other)
        view.symlink_to(other, target_is_directory=True)
    elif damage == "fifo":
        os.mkfifo(view / "pipe")
    else:
        source.chmod(0o777)
    with pytest.raises(ReviewMaterialChanged):
        guard.verify()


def test_only_selected_files_are_frozen_and_timestamps_are_not_identity(tmp_path):
    guard, view, prompt = package(tmp_path)
    os.utime(view / "source.py", (1, 1))
    (tmp_path / "result.json").write_text("output")
    (tmp_path / "unrelated-report").write_text("other task")
    guard.verify()
    prompt.write_text("changed")
    with pytest.raises(ReviewMaterialChanged):
        guard.verify()


@pytest.mark.parametrize("damage", ["missing", "extra", "changed", "link"])
def test_projected_materials_match_exact_selected_set(tmp_path, damage):
    guard, view, prompt = package(tmp_path)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    selected = artifacts / "body"
    selected.write_text("selected")
    projected = tmp_path / "projected"
    projected.mkdir()
    guard.admit_files([prompt, selected])
    target = projected / "body"
    target.write_text("selected")
    guard.admit_projection(projected, artifacts)
    if damage == "missing":
        target.unlink()
    elif damage == "extra":
        (projected / "unadmitted").write_text("bad")
    elif damage == "changed":
        target.write_text("different")
    else:
        target.unlink()
        target.symlink_to(selected)
    with pytest.raises(ReviewMaterialChanged):
        guard.verify()


async def no_probe(*args):
    pass


@pytest.mark.parametrize("when", ["dispatch", "return"])
@pytest.mark.parametrize("target", ["snapshot", "prompt", "schema", "selected", "projection_extra"])
async def test_damage_never_publishes_success_or_retries(tmp_path, monkeypatch, when, target):
    router, project, store, rid = fixture_router(tmp_path, [START, TURN, MESSAGE, COMPLETE], retries=3)
    (project / "source.py").write_text("original")
    ref = ArtifactStore(store.root).put("admitted fixture evidence", "document")
    monkeypatch.setattr(router, "_ensure_material_read", no_probe)
    view = None
    overlay = router._session_overlay

    def damage():
        root = view.parent
        victim = {"snapshot": view / "source.py", "prompt": root / "prompt.txt",
                  "schema": root / "schema.json", "selected": ArtifactStore(store.root).root / ref["hash"][:2] / ref["hash"],
                  "projection_extra": root / "artifact-view" / "extra"}[target]
        victim.write_text("changed by synthetic fixture")

    def controlled_overlay(*args, **kwargs):
        nonlocal view
        attempt = kwargs["cache_invocation"]["attempt"]
        view = store.root / "attempts" / rid / attempt / "workspace"
        result = overlay(*args, **kwargs)
        if when == "dispatch":
            damage()
        return result

    class DamagingDecoder(EventDecoder):
        def feed(self, event):
            super().feed(event)
            if when == "return" and event.get("type") == "turn.completed":
                damage()

    monkeypatch.setattr(router, "_session_overlay", controlled_overlay)
    router.decoders["codex"] = DamagingDecoder
    with pytest.raises(ReviewMaterialChanged) as first_failure:
        await router.invoke("review", {"document": ref}, ReviewResult, project, rid, "guarded")
    assert store.get(rid, "invocation/guarded") is None
    assert store.get(rid, "provider-result/guarded") is None
    state = store.get(rid, "infra/guarded/codex")
    assert state["failures"] == 1 and not state["pending"]
    assert state["error_kind"] == "review_material_changed"
    with pytest.raises(ReviewMaterialChanged) as replayed_failure:
        await router.invoke("review", {"document": ref}, ReviewResult, project, rid, "guarded")
    assert replayed_failure.value.requirement == first_failure.value.requirement
    assert store.get(rid, "infra/guarded/codex")["failures"] == 1
    events = store.events(rid)
    assert not any(e["kind"] == "agent.retrying" for e in events)
    assert sum(e["kind"] == "agent.spawned" for e in events) == (when == "return")
    if when == "return":
        assert store.usage_summary(rid)["total_tokens"] == 14
        assert any(e["kind"] == "review.result_unaccepted" for e in events)
    assert not list((store.root / "processes").glob("*.json"))


async def test_real_bwrap_protects_all_review_materials_and_allows_result(tmp_path, monkeypatch):
    router, project, store, rid = fixture_router(tmp_path, [])
    router.sandbox = SandboxRunner(store.root)
    monkeypatch.setattr(router, "_ensure_material_read", no_probe)
    (project / "source.py").write_text("source")
    ref = ArtifactStore(store.root).put("admitted fixture evidence", "document")
    binary = tmp_path / "provider"
    binary.write_text(f"#!{sys.executable}\n" + r"""
import json, pathlib, sys
args=sys.argv[1:]
wire=sys.stdin.read()
schema=pathlib.Path(args[args.index('--output-schema')+1])
context=json.loads(wire.split('<context>\n',1)[1].split('\n</context>',1)[0])
for path in [pathlib.Path.cwd()/'source.py', schema, schema.parent/'prompt.txt', pathlib.Path(context['document']['location'])]:
    original=path.read_bytes()
    try:
        path.write_bytes(b'forbidden')
    except OSError:
        pass
    else:
        raise AssertionError('read-only input became writable')
    assert path.read_bytes() == original
pathlib.Path(args[args.index('-o')+1]).write_text(json.dumps({'approved': True, 'summary': 'sandbox verified', 'findings': []}))
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}))
""")
    result = await router.invoke("review", {"document": ref}, ReviewResult, project, rid, "sandbox")
    assert result.approved and result.summary == "sandbox verified"
    record = store.get(rid, "materials/sandbox")["review_inputs"]
    assert store.get(rid, "invocation/sandbox")["review_inputs_hash"] == record["hash"]
    assert "source.py" in record["snapshot"]
    assert len(record["files"]) >= 3
    assert str(store.root / "state.sqlite") not in record["files"]


def test_material_failure_uses_existing_state_conflict_boundary():
    from auto_agents_2.faults import StateConflict, known_exception
    failure = ReviewMaterialChanged("Delivered source changed")
    assert isinstance(failure, StateConflict)
    assert known_exception(failure, "review") is failure
    assert failure.requirement.kind == "state_conflict"


async def test_real_source_change_is_stale_not_delivered_material_damage(tmp_path, monkeypatch):
    from test_runtime import configured

    from auto_agents_2.engine import WorkflowEngine
    from auto_agents_2.errors import PolicyViolation

    config, store = configured(tmp_path)
    source = tmp_path / "source.py"
    source.write_text("before review")

    class ConcurrentEdit:
        async def invoke(self, *args):
            source.write_text("user concurrent edit")
            return ReviewResult(approved=True, summary="old candidate")

    workflow = WorkflowEngine(tmp_path, agent=ConcurrentEdit(),
                              sandbox=SandboxRunner(store.root, _test_unconfined=True))
    monkeypatch.setattr(workflow, "_storage_boundary", no_probe)
    rid = store.admit("synthetic source drift", {})["id"]
    with pytest.raises(PolicyViolation, match="Review source is stale"):
        await workflow._invoke_once("review", {}, ReviewResult, tmp_path, rid, "stale", interactive_retry=False)
    assert source.read_text() == "user concurrent edit"
    assert store.get(rid, "invocation/stale") is None
    retained = store.get(rid, "invocation-drift/stale")
    assert ArtifactStore(store.root).json(retained["proposal"])["summary"] == "old candidate"


async def test_other_task_artifacts_logs_and_output_do_not_change_inputs(tmp_path, monkeypatch):
    router, project, store, rid = fixture_router(tmp_path, [START, TURN, MESSAGE, COMPLETE])

    class ConcurrentProgress(EventDecoder):
        def feed(self, event):
            super().feed(event)
            if event.get("type") == "turn.completed":
                ArtifactStore(store.root).put("unrelated new material", "document")
                store.event(rid, "fixture.other_task", {"progress": "new output"})
                store.checkpoint(rid, "unrelated-task", {"status": "complete"})

    router.decoders["codex"] = ConcurrentProgress
    result = await router.invoke("review", {}, ReviewResult, project, rid, "concurrent")
    assert result.summary == "Fixture review rejected"
    assert store.get(rid, "invocation/concurrent")["review_inputs_hash"]
    assert store.get(rid, "unrelated-task") == {"status": "complete"}


@pytest.mark.parametrize("changed", [False, True])
async def test_normal_prerequisite_grants_only_exact_integrity_failure(tmp_path, monkeypatch, changed):
    from auto_agents_2.engine import WorkflowEngine
    from auto_agents_2.faults import StateConflict

    router, project, store, rid = fixture_router(tmp_path, [START, TURN, MESSAGE, COMPLETE], retries=3)
    key = "infra/review/codex"
    failed = {"pending": False, "failures": 4, "grant": 2,
              "error_kind": "review_material_changed", "error_input": "bound-input"}
    store.checkpoint(rid, key, failed)
    store.checkpoint(rid, "infra/other/codex", {"pending": False, "failures": 7, "grant": 0})
    error = ReviewMaterialChanged("Damaged review inputs").bind_failure(key, failed)
    workflow = WorkflowEngine(project, agent=router, sandbox=router.sandbox)
    answers = []

    async def answer(question):
        answers.append(question)
        if changed:
            store.checkpoint(rid, key, {**failed, "failures": 5})
        return "y"

    workflow.question_handler = answer
    if changed:
        with pytest.raises(StateConflict, match="exact failed invocation"):
            await workflow._wait_prerequisite(rid, error)
        assert store.get(rid, key)["failures"] == 5
        assert store.get(rid, key)["grant"] == 2
    else:
        await workflow._wait_prerequisite(rid, error)
        assert store.get(rid, key) == {k: v for k, v in {**failed, "grant": 3}.items() if k != "error_input"}
        result = await router.invoke("review", {}, ReviewResult, project, rid, "review")
        assert result.summary == "Fixture review rejected"
        assert store.get(rid, key)["failures"] == 4
        assert store.get(rid, key)["grant"] == 3
    assert len(answers) == 1
    assert store.get(rid, "infra/other/codex") == {"pending": False, "failures": 7, "grant": 0}


async def test_corrupt_material_with_invalid_json_never_becomes_format_retry(tmp_path, monkeypatch):
    original = fixture_router

    def invalid_response(*args, **kwargs):
        return original(*args, **kwargs, file_response="not valid JSON")

    monkeypatch.setitem(globals(), "fixture_router", invalid_response)
    await test_damage_never_publishes_success_or_retries(tmp_path, monkeypatch, "return", "snapshot")


async def test_pending_integrity_question_is_reused_after_restart(tmp_path):
    import asyncio

    from auto_agents_2.engine import WorkflowEngine

    router, project, store, rid = fixture_router(tmp_path, [], retries=3)
    key = "infra/review/codex"
    state = {"pending": False, "failures": 1, "grant": 0, "error_kind": "review_material_changed",
             "error_input": "same-input", "material_failure_reason": "Original exact integrity failure"}
    store.checkpoint(rid, key, state)
    seen = []

    async def pause(question):
        seen.append(question["id"])
        raise asyncio.CancelledError()

    workflow = WorkflowEngine(project, agent=router, sandbox=router.sandbox, question_handler=pause)
    first = ReviewMaterialChanged("Original exact integrity failure").bind_failure(key, state)
    with pytest.raises(asyncio.CancelledError):
        await workflow._wait_prerequisite(rid, first)

    async def resume(question):
        seen.append(question["id"])
        return "y"

    restarted = WorkflowEngine(project, agent=router, sandbox=router.sandbox, question_handler=resume)
    replay = ReviewMaterialChanged("Saved review material failure").bind_failure(key, store.get(rid, key))
    await restarted._wait_prerequisite(rid, replay)
    assert len(seen) == 2 and seen[0] == seen[1]
    assert store.get(rid, key)["failures"] == 1 and store.get(rid, key)["grant"] == 1
    assert "error_input" not in store.get(rid, key)
