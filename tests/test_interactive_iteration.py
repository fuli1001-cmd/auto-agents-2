import asyncio
from contextlib import contextmanager
from io import StringIO
from pathlib import Path

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from test_interaction import eventually
from test_workflow import CodingAgent, engine

from auto_agents_2 import cli
from auto_agents_2.config import ConfigStore
from auto_agents_2.errors import AA2Error, BusyProject, IncompleteIteration
from auto_agents_2.models import IntakeResult
from auto_agents_2.requirements import MAX_REQUIREMENTS_BYTES, save_interactive_requirements
from auto_agents_2.store import Store


def terminal(monkeypatch, *, stdin=True, stdout=True):
    output = StringIO()
    monkeypatch.setattr(cli, "console", Console(file=output, width=200))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: stdin)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: stdout)
    monkeypatch.setattr(cli, "PromptSession", lambda: object())
    return output


def completed(project, *, status="SUCCEEDED", outcome="DELIVERED"):
    ConfigStore(project).initialize()
    store = Store(project)
    run = store.admit("previous.md", {})
    store.update(run["id"], status=status, outcome=outcome)
    return store, run["id"]


class ClarifyingAgent(CodingAgent):
    def __init__(self, *, interrupt=False):
        super().__init__()
        self.interrupt = interrupt
        self.inputs = []

    async def invoke(self, stage, context, *args, **kwargs):
        if stage == "clarify":
            self.inputs.append(context["raw_requirements"])
            count = len(context.get("decisions", []))
            if count == 1 and self.interrupt:
                self.interrupt = False
                raise asyncio.CancelledError()
            if count < 2:
                return IntakeResult(summary="clarification", document="", questions=[f"澄清问题 {count + 1}"])
        return await super().invoke(stage, context, *args, **kwargs)


@pytest.mark.parametrize("outcome", ["DELIVERED", "NO_CHANGES"])
async def test_completed_project_starts_one_iteration_with_two_clarification_turns(tmp_path, monkeypatch, outcome):
    store, previous = completed(tmp_path, outcome=outcome)
    output = terminal(monkeypatch)
    agent = ClarifyingAgent()
    workflows = []

    def make_engine(project, *, question_handler):
        workflow = engine(project, agent)
        workflow.question_handler = question_handler
        workflows.append(workflow)
        return workflow

    monkeypatch.setattr(cli, "WorkflowEngine", make_engine)
    original = "  实现加法。\n保留已有功能。\n"
    replies = iter(["y", original, "整数加法", "返回相加结果"])
    prompts = []

    async def answer(session, question):
        assert "如何运行项目" in output.getvalue()
        prompts.append(question["prompt"])
        return next(replies)

    monkeypatch.setattr(cli, "prompt_answer", answer)
    result = await cli.run_session(tmp_path)
    assert result["status"] == "SUCCEEDED" and result["id"] != previous
    assert prompts == ["是否开始新的迭代？", "请描述本次迭代的需求：", "澄清问题 1", "澄清问题 2"]
    assert len(workflows) == 2 and len(store.successful()) == 2
    assert set(agent.inputs) == {original}
    assert "implement" in [stage for stage, _ in agent.calls]
    source = Path(result["data"]["source"])
    assert source.parent == store.root / "requirements"
    assert source.read_text() == original
    assert source.stat().st_mode & 0o777 == 0o600
    raw = store.get(result["id"], "capture")
    assert workflows[-1].artifacts.read(raw["artifact"]) == original
    assert store.pending(previous) == []
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM questions WHERE run_id=?", (previous,)).fetchone()[0] == 0
        answers = [row[0] for row in db.execute("SELECT answer FROM questions WHERE run_id=? ORDER BY created", (result["id"],))]
    assert answers == ["整数加法", "返回相加结果"]
    assert output.getvalue().count("如何运行项目") == 2


@pytest.mark.parametrize("phase", ["choice", "description"])
@pytest.mark.parametrize("interruption", [KeyboardInterrupt, asyncio.CancelledError, EOFError])
async def test_cancel_before_submission_does_not_create_or_pause_a_run(tmp_path, monkeypatch, phase, interruption):
    store, previous = completed(tmp_path)
    output = terminal(monkeypatch)

    async def answer(session, question):
        if phase == "description" and question.get("interaction"):
            return "y"
        raise interruption()

    monkeypatch.setattr(cli, "prompt_answer", answer)
    result = await cli.run_session(tmp_path)
    assert result["id"] == previous and store.current()["status"] == "SUCCEEDED"
    assert "已退出，未创建新迭代" in output.getvalue()
    assert "已暂停" not in output.getvalue()
    assert not (store.root / "requirements").exists()
    assert store.pending(previous) == []


async def test_choice_validates_before_declining(tmp_path, monkeypatch):
    store, previous = completed(tmp_path)
    output = terminal(monkeypatch)
    with create_pipe_input() as pipe:
        session = PromptSession(input=pipe, output=DummyOutput())
        monkeypatch.setattr(cli, "PromptSession", lambda: session)
        task = asyncio.create_task(cli.run_session(tmp_path))
        try:
            await eventually(lambda: session.app.is_running)
            for invalid in ("", "yes", "Y"):
                pipe.send_text(invalid + "\r")
                await eventually(lambda: session.default_buffer.validation_error is not None)
                assert not task.done() and store.current()["id"] == previous
                pipe.send_text("\x15")
                await eventually(lambda: not session.default_buffer.text)
            pipe.send_text("n\r")
            assert (await asyncio.wait_for(task, 3))["id"] == previous
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert "已退出，未创建新迭代" in output.getvalue()
    assert not (store.root / "requirements").exists()


@pytest.mark.parametrize("invalid", [" \n\t", "界" * (MAX_REQUIREMENTS_BYTES // 3 + 1)], ids=["blank", "too-large"])
async def test_invalid_description_requires_new_input_before_admission(tmp_path, monkeypatch, invalid):
    store, previous = completed(tmp_path)
    output = terminal(monkeypatch)
    calls = []
    replies = iter(["y", invalid, "新的需求"])

    async def answer(session, question):
        assert store.current()["id"] == previous
        return next(replies)

    async def execute(project, *args, **kwargs):
        calls.append(kwargs)
        return store.run(previous)

    monkeypatch.setattr(cli, "execute", execute)
    monkeypatch.setattr(cli, "prompt_answer", answer)
    await cli.run_session(tmp_path)
    assert len(calls) == 2 and calls[-1]["requirements_text"] == "新的需求"
    assert "不能为空" in output.getvalue() or "4 MiB" in output.getvalue()
    assert not (store.root / "requirements").exists()


@pytest.mark.parametrize("status", ["RUNNING", "PAUSED", "WAITING_USER", "CANCELLED", None])
async def test_other_initial_states_never_offer_a_new_iteration(tmp_path, monkeypatch, status):
    ConfigStore(tmp_path).initialize()
    store = Store(tmp_path)
    if status:
        _, rid = completed(tmp_path, status=status)
    terminal(monkeypatch)

    async def execute(*args, **kwargs):
        if status is None:
            raise AA2Error("尚无可恢复迭代")
        store.update(rid, status="SUCCEEDED" if status != "CANCELLED" else status)
        return store.run(rid)

    async def unexpected_prompt(*args):
        pytest.fail("Only an iteration already successful at startup may offer a new run")

    monkeypatch.setattr(cli, "execute", execute)
    monkeypatch.setattr(cli, "prompt_answer", unexpected_prompt)
    if status is None:
        with pytest.raises(AA2Error, match="尚无可恢复"):
            await cli.run_session(tmp_path)
    else:
        await cli.run_session(tmp_path)


@pytest.mark.parametrize("stdin, stdout, requirements", [
    (False, True, None), (True, False, None), (False, False, None), (True, True, "next.md"),
])
async def test_noninteractive_and_explicit_sources_keep_the_existing_entrypoint(
        tmp_path, monkeypatch, stdin, stdout, requirements):
    store, previous = completed(tmp_path)
    terminal(monkeypatch, stdin=stdin, stdout=stdout)
    calls = []

    async def execute(*args, **kwargs):
        calls.append((args, kwargs))
        return store.run(previous)

    async def unexpected_prompt(*args):
        pytest.fail("This invocation must not prompt")

    monkeypatch.setattr(cli, "execute", execute)
    monkeypatch.setattr(cli, "prompt_answer", unexpected_prompt)
    await cli.run_session(tmp_path, requirements, "selected-provider")
    assert calls == [((tmp_path, requirements, "selected-provider"), {})]


@pytest.mark.parametrize("explicit_id", [False, True])
def test_resume_of_completed_run_never_offers_new_iteration(tmp_path, monkeypatch, explicit_id):
    store, previous = completed(tmp_path)
    output = terminal(monkeypatch)

    async def unexpected_prompt(*args):
        pytest.fail("resume must not offer a new iteration")

    monkeypatch.setattr(cli, "prompt_answer", unexpected_prompt)
    cli.resume(project=tmp_path, run_id=previous if explicit_id else None, provider=None)
    assert "如何运行项目" in output.getvalue()
    assert store.current()["id"] == previous
    assert not (store.root / "requirements").exists()


async def test_newer_iteration_prevents_prompt_for_old_completion(tmp_path, monkeypatch):
    store, previous = completed(tmp_path)
    terminal(monkeypatch)

    async def execute(project, **kwargs):
        assert kwargs["run_id"] == previous
        store.admit("competing.md", {})
        return store.run(previous)

    async def unexpected_prompt(*args):
        pytest.fail("The completed run is no longer the current iteration")

    monkeypatch.setattr(cli, "execute", execute)
    monkeypatch.setattr(cli, "prompt_answer", unexpected_prompt)
    await cli.run_session(tmp_path)
    assert store.current()["data"]["source"] == "competing.md"
    assert store.current()["status"] == "RUNNING"


@pytest.mark.parametrize("capture_interrupted", [False, True])
async def test_submitted_text_and_answers_survive_restart(tmp_path, monkeypatch, capture_interrupted):
    agent = ClarifyingAgent(interrupt=not capture_interrupted)
    workflow = engine(tmp_path, agent)
    answers = []

    async def answer(question):
        answers.append(question["prompt"])
        return "answer " + question["prompt"]

    workflow.question_handler = answer
    if capture_interrupted:
        from auto_agents_2 import engine as module
        capture = module.capture

        async def interrupt(*args):
            raise asyncio.CancelledError()

        monkeypatch.setattr(module, "capture", interrupt)
    original = "实现加法\n保留已有行为\n"
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(requirements_text=original)
    interrupted = workflow.store.current()
    assert interrupted["status"] == "PAUSED"
    source = Path(interrupted["data"]["source"])
    assert source.read_text() == original
    if capture_interrupted:
        assert workflow.store.get(interrupted["id"], "capture") is None
        monkeypatch.setattr(module, "capture", capture)
    else:
        assert len(workflow.store.get(interrupted["id"], "clarify_context")["decisions"]) == 1
    resumed = engine(tmp_path, agent)
    resumed.question_handler = answer
    result = await resumed.start()
    assert result["status"] == "SUCCEEDED" and result["id"] == interrupted["id"]
    assert answers == ["澄清问题 1", "澄清问题 2"]
    assert list((workflow.store.root / "requirements").glob("*.md")) == [source]


@pytest.mark.parametrize("race", [False, True])
async def test_unfinished_iteration_is_rechecked_under_lock_before_saving_or_switching(tmp_path, monkeypatch, race):
    workflow = engine(tmp_path, CodingAgent())
    if race:
        @contextmanager
        def locked(*args, **kwargs):
            workflow.store.admit("competing.md", {})
            yield
        monkeypatch.setattr("auto_agents_2.engine.file_lock", locked)
    else:
        workflow.store.admit("competing.md", {})
    with pytest.raises(IncompleteIteration):
        await workflow.start(provider="not-configured", requirements_text="新需求")
    assert workflow.store.current()["data"]["source"] == "competing.md"
    assert not (workflow.store.root / "requirements").exists()
    assert workflow.config.load().active_provider == "codex"
    assert not workflow.agent.calls


async def test_text_submission_does_not_attach_to_busy_controller(tmp_path, monkeypatch):
    completed(tmp_path)
    terminal(monkeypatch, stdin=False, stdout=False)

    class BusyEngine:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self, *args, **kwargs):
            raise BusyProject("busy")

    monkeypatch.setattr(cli, "WorkflowEngine", BusyEngine)
    with pytest.raises(IncompleteIteration):
        await cli.execute(tmp_path, requirements_text="新需求")


@pytest.mark.parametrize("options", [{"source": "source.md"}, {"run_id": "previous"}])
async def test_text_is_mutually_exclusive_with_other_input_modes(tmp_path, options):
    workflow = engine(tmp_path, CodingAgent())
    with pytest.raises(AA2Error, match="不能与"):
        await workflow.start(requirements_text="新需求", **options)
    assert workflow.store.current() is None
    assert not (workflow.store.root / "requirements").exists()


@pytest.mark.parametrize("text", ["", " \n", "界" * (MAX_REQUIREMENTS_BYTES // 3 + 1)],
                         ids=["empty", "whitespace", "too-large"])
async def test_engine_rejects_invalid_text_before_creating_a_run(tmp_path, text):
    workflow = engine(tmp_path, CodingAgent())
    with pytest.raises(AA2Error):
        await workflow.start(requirements_text=text)
    assert workflow.store.current() is None
    assert not (workflow.store.root / "requirements").exists()


def test_saved_sources_are_unique_and_preserve_exact_text(tmp_path):
    original = "  multiline\n需求\r\n"
    first = save_interactive_requirements(tmp_path, original)
    second = save_interactive_requirements(tmp_path, original)
    assert first != second
    assert first.read_bytes() == second.read_bytes() == original.encode()


def test_requirements_directory_cannot_redirect_writes(tmp_path):
    root = tmp_path / ".aa2"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "requirements").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(AA2Error, match="无法保存"):
        save_interactive_requirements(root, "new requirement")
    assert not list(elsewhere.iterdir())


async def test_failed_source_write_does_not_admit_or_change_provider(tmp_path, monkeypatch):
    workflow = engine(tmp_path, CodingAgent())

    def failure(*args):
        raise OSError("injected write failure")

    monkeypatch.setattr("auto_agents_2.requirements.write_regular", failure)
    with pytest.raises(AA2Error, match="无法保存"):
        await workflow.start(requirements_text="新需求")
    assert workflow.store.current() is None
    assert not workflow.agent.calls
