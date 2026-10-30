import asyncio
import sys

import pytest
from test_workflow import engine

from auto_agents_2.models import Change, DocumentResult, IntakeResult
from auto_agents_2.providers import ProviderRouter
from auto_agents_2.util import digest, write_json
from auto_agents_2.workspace import git

PAGE = "design/prototypes/task/index.html"


def fixture(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    workflow = engine(project, None)
    page = project / PAGE
    page.parent.mkdir(parents=True)
    page.write_text("CURRENT_PAGE_MARKER")
    (project / "DESIGN.md").write_text("CURRENT_DESIGN_MARKER")
    git(project, "add", "DESIGN.md", "design/prototypes")
    git(project, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-m", "existing prototype")
    native = tmp_path / "native"
    native.mkdir()
    binary = tmp_path / "provider"
    response = DocumentResult(summary="revised prototype", document="# Current prototype",
        files=[Change(path=PAGE, content="REVISED_PAGE_MARKER")]).model_dump(mode="json")
    response["design_selection"] = []  # New strict wire field; legacy saved records still omit it.
    binary.write_text(f"#!{sys.executable}\nRESPONSE = {response!r}\n" + r'''
import json, pathlib, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("fixture-cli 1")
    sys.exit(0)
wire = sys.stdin.read()
schema = json.loads(pathlib.Path(args[args.index("--output-schema") + 1]).read_text())
context = json.loads(wire.split("<context>\n", 1)[1].split("\n</context>", 1)[0])
if "text_token" in schema["properties"]:
    result = {"text_token": pathlib.Path(context["context_files"][0]).read_text(), "image_token": ""}
else:
    if "resume" in args:
        print(json.dumps({"type": "turn.failed", "error": {"message": "context_window_exceeded"}}), flush=True)
        sys.exit(1)
    if context.get("feedback", {}).get("round") != "seed":
        materials = wire + "".join(pathlib.Path(p).read_text() for p in context.get("context_files", []))
        for marker in ["ORIGINAL_OBLIGATION", "LATEST_FEEDBACK", "CURRENT_SPEC_MARKER", "CURRENT_DESIGN_MARKER"]:
            assert marker in materials, marker
        workspace = pathlib.Path(args[args.index("-C") + 1])
        assert (workspace / "design/prototypes/task/index.html").read_text() == "CURRENT_PAGE_MARKER"
    result = RESPONSE
    print(json.dumps({"type": "thread.started", "thread_id": "fixture-prototype-thread"}), flush=True)
pathlib.Path(args[args.index("-o") + 1]).write_text(json.dumps(result))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}), flush=True)
''')
    binary.chmod(0o755)
    body = workflow.config.load().model_dump(mode="json")
    body["providers"] = {"codex": body["providers"]["codex"]}
    body["providers"]["codex"].update(binary=str(binary), environment={"CODEX_HOME": str(native)},
        profile_map={level: {"model": "test"} for level in ("balanced", "deep", "max")})
    body["execution"]["infra_retries"] = 3
    write_json(workflow.config.path, body)
    workflow.agent = ProviderRouter(workflow.config, workflow.store, workflow.sandbox)
    rid = workflow.store.admit("original prototype", {})["id"]
    spec = IntakeResult(summary="CURRENT_SPEC_MARKER", document="# UI", frontend=True, prototype_paths=[PAGE])
    workflow.store.checkpoints(rid, {
        "baseline": git(project, "rev-parse", "HEAD"), "clarify": spec.model_dump(),
        "capture": {"artifact": workflow.artifacts.put("ORIGINAL_OBLIGATION", "requirements")},
    })
    return workflow, project, rid, spec


def author_starts(store, rid):
    return [e for e in store.events(rid) if e["kind"] == "agent.started" and e["data"]["stage"] == "prototype"]


@pytest.mark.parametrize("interrupt_after_response", [False, True])
async def test_new_prototype_avoids_old_session_and_preserves_feedback_cache_and_gates(tmp_path, monkeypatch, interrupt_after_response):
    workflow, project, rid, spec = fixture(tmp_path)
    store = workflow.store
    await workflow.agent.invoke("prototype", {"feedback": {"round": "seed"}},
                                DocumentResult, project, rid, "prototype")
    feedback = store.request(rid, "prototype-feedback", "Requested revision", digest("previous prototype"))
    store.answer(feedback["id"], "LATEST_FEEDBACK: simplify the current prototype")
    store.update(rid, replans=12)
    budget = {"failures": 2, "grant": 4, "pending": False}
    store.checkpoint(rid, "infra/prototype/codex", budget)
    context = {"specification": spec.model_dump(), "project": workflow.contexts.build(project),
               "feedback": {"reason": "LATEST_FEEDBACK: simplify the current prototype"}}

    class BrowserReached(Exception):
        pass

    async def browser(*args):
        assert (project / PAGE).read_text() == "REVISED_PAGE_MARKER"
        raise BrowserReached()

    workflow._prototype_browser_report = browser
    if interrupt_after_response:
        checkpoint = store.checkpoint

        def stop_before_pending(run_id, key, data):
            if key == "prototype/pending":
                raise asyncio.CancelledError()
            return checkpoint(run_id, key, data)

        with monkeypatch.context() as patch:
            patch.setattr(store, "checkpoint", stop_before_pending)
            with pytest.raises(asyncio.CancelledError):
                await workflow._prepare_prototype(rid, project, spec, spec.model_dump(), context, "UI")
        assert store.get(rid, "prototype/pending") is None
        assert store.get(rid, "invocation/prototype")["result"]["files"]
        assert (project / PAGE).read_text() == "CURRENT_PAGE_MARKER"

    with pytest.raises(BrowserReached):
        await workflow._prepare_prototype(rid, project, spec, spec.model_dump(), context, "UI")
    starts = author_starts(store, rid)
    assert len(starts) == 2  # prior round plus one new proposal, including interrupted receipt recovery
    pending = store.get(rid, "prototype/pending")
    assert pending["document"]["summary"] == "revised prototype"

    # Restart after application reuses the saved candidate and still needs browser/review/human approval.
    workflow.agent = ProviderRouter(workflow.config, store, workflow.sandbox)
    with pytest.raises(BrowserReached):
        await workflow._prepare_prototype(rid, project, spec, spec.model_dump(), context, "UI")
    assert author_starts(store, rid) == starts
    assert store.get(rid, "infra/prototype/codex") == budget
    assert store.run(rid)["data"]["replans"] == 12
    assert store.question(feedback["id"])["answer"] == "LATEST_FEEDBACK: simplify the current prototype"
    assert store.get(rid, "prototype_approval") is None
    assert not store.pending(rid)
    assert not list((store.root / "processes").glob("*.json"))
