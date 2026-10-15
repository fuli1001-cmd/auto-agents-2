import asyncio
import sqlite3

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from typer.testing import CliRunner

from auto_agents_2.cli import app
from auto_agents_2.config import ConfigStore
from auto_agents_2.errors import AA2Error
from auto_agents_2.interaction import choice_spec, is_prototype_approval, prompt_answer, question_text
from auto_agents_2.models import UserQuestion
from auto_agents_2.store import Store


async def eventually(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_choice_rejects_empty_words_and_out_of_range_then_accepts_number():
    with create_pipe_input() as pipe:
        session = PromptSession(input=pipe, output=DummyOutput())
        task = asyncio.create_task(prompt_answer(session, {"prompt": "审核原型", "interaction": choice_spec(["通过", "修改"])}))
        try:
            await eventually(lambda: session.app.is_running)
            for invalid in ("", "approve", "3"):
                pipe.send_text("\x15" + invalid + "\r")
                await eventually(lambda: session.default_buffer.validation_error is not None)
                assert not task.done()
                pipe.send_text("\x15")
                await eventually(lambda: not session.default_buffer.text)
            pipe.send_text("2\r")
            assert await asyncio.wait_for(task, 3) == "2"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_multiline_editing_paste_and_ctrl_d_submission():
    with create_pipe_input() as pipe:
        session = PromptSession(input=pipe, output=DummyOutput())
        task = asyncio.create_task(prompt_answer(session, {"prompt": "修改意见"}))
        try:
            await eventually(lambda: session.app.is_running)
            pipe.send_text("\x04")
            await eventually(lambda: session.default_buffer.validation_error is not None)
            assert not task.done()
            pipe.send_text("alpha\rbeta")
            await eventually(lambda: session.default_buffer.text == "alpha\nbeta")
            assert not task.done()  # Enter inserted a newline.
            pipe.send_text("\x01\x7f")  # Home, backspace merges two lines.
            await eventually(lambda: session.default_buffer.text == "alphabeta")
            pipe.send_text("\x1b[3~")  # Delete the b after the cursor.
            await eventually(lambda: session.default_buffer.text == "alphaeta")
            pipe.send_text("\x05\r")
            pipe.send_text("\x1b[200~one\ntwo\x1b[201~")
            await eventually(lambda: session.default_buffer.text == "alphaeta\none\ntwo")
            assert not task.done()  # Bracketed multiline paste cannot submit.
            pipe.send_text("\x1b[A\x01X")  # Up, Home, insert on the previous line.
            await eventually(lambda: session.default_buffer.text == "alphaeta\nXone\ntwo")
            pipe.send_text("\x04")
            assert await asyncio.wait_for(task, 3) == "alphaeta\nXone\ntwo"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_store_migrates_old_questions_without_losing_text(tmp_path):
    root = tmp_path / ".aa2"
    root.mkdir()
    with sqlite3.connect(root / "state.sqlite") as db:
        db.execute("CREATE TABLE questions(id TEXT PRIMARY KEY,run_id TEXT,key TEXT,prompt TEXT,artifact_hash TEXT,answer TEXT,created REAL, UNIQUE(run_id,key,artifact_hash))")
        db.execute("INSERT INTO questions VALUES('q','r','prototype','old question','v1',NULL,1)")
    store = Store(tmp_path)
    store.migrate()
    assert store.pending("r")[0]["prompt"] == "old question"
    store.supersede_questions("r", "prototype")
    assert store.pending("r") == []
    with store.connect() as db:
        assert db.execute("SELECT prompt,answer FROM questions WHERE id='q'").fetchone()[:] == ("old question", None)


def test_external_choice_validation_version_binding_and_multiline_file(tmp_path):
    ConfigStore(tmp_path).initialize()
    store = Store(tmp_path)
    rid = store.admit("test", {})["id"]
    question = store.request(rid, "prototype-approval", "审核", "v1", choice_spec(["通过", "修改"]))
    runner = CliRunner()
    prefix = ["respond", "--project", str(tmp_path), "--request", question["id"]]
    for args in (["--answer", "1"], ["--artifact", "wrong", "--answer", "1"],
                 ["--artifact", "v1", "--answer", "approve"], ["--artifact", "v1", "--answer", "3"]):
        assert runner.invoke(app, prefix + args).exit_code == 2
        assert store.pending(rid)
    assert runner.invoke(app, prefix + ["--artifact", "v1", "--answer", "2"]).exit_code == 0
    feedback = store.request(rid, "prototype-feedback", "修改意见", "v1")
    file = tmp_path / "feedback.txt"
    file.write_text("缩小间距\n按钮右对齐\n")
    assert runner.invoke(app, ["respond", "--project", str(tmp_path), "--answer-file", str(file)]).exit_code == 0
    with store.connect() as db:
        assert db.execute("SELECT answer FROM questions WHERE id=?", (feedback["id"],)).fetchone()[0] == file.read_text()


def test_superseded_version_cannot_be_answered(tmp_path):
    store = Store(tmp_path)
    rid = store.admit("test", {})["id"]
    old = store.request(rid, "prototype-approval", "old", "v1", choice_spec(["yes", "no"]))
    new = store.request(rid, "prototype-approval", "new", "v2", choice_spec(["yes", "no"]))
    with pytest.raises(AA2Error):
        store.answer(old["id"], "1", "v1")
    assert [q["id"] for q in store.pending(rid)] == [new["id"]]


async def test_explicit_pause_preserves_answer_and_reasks_after_resume(tmp_path):
    from test_workflow import CodingAgent, engine
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("test", {})["id"]
    asked = []

    async def pause(question):
        asked.append(question)
        return "n" if len(asked) == 1 else "y"

    workflow.question_handler = pause
    with pytest.raises(asyncio.CancelledError):
        await workflow._ask(rid, "provider/test", "修复后回复 retry。", "v1")
    assert await workflow._ask(rid, "provider/test", "修复后回复 retry。", "v1") == "continue"
    assert asked[0]["id"] != asked[1]["id"]
    with workflow.store.connect() as db:
        assert db.execute("SELECT answer,superseded FROM questions WHERE id=?", (asked[0]["id"],)).fetchone()[:] == ("n", 1)


async def test_choice_value_named_pause_is_not_a_control_instruction(tmp_path):
    from test_workflow import CodingAgent, engine
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("test", {})["id"]

    async def choose(question):
        return "1"

    workflow.question_handler = choose
    assert await workflow._ask(rid, "delivery-target", "选择分支", "v1", choice_spec(["pause", "main"])) == "pause"


def test_structured_clarification_is_not_silently_treated_as_approval():
    question = UserQuestion(prompt="原型中的审核功能需要哪些角色？", options=["管理员", "全部用户"])
    assert not is_prototype_approval(question)
    assert is_prototype_approval(UserQuestion(prompt="是否通过？", purpose="prototype_approval"))
    assert is_prototype_approval("R008 要求先取得人工批准：请审阅原型是否通过？")


def test_question_cleanup_preserves_usable_urls_and_choice_values():
    assert question_text("R008 请查看原型\n预览：http://127.0.0.1:1234/R008.html") == "请查看原型\n预览：http://127.0.0.1:1234/R008.html"
    assert choice_spec(["R008"])["options"][0] == {"key": "1", "label": "R008", "value": "R008"}
