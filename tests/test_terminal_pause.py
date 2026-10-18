"""Exercise Ctrl+C in a real PTY, including prompt_toolkit's raw input mode."""
import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time

import pytest

from auto_agents_2.store import Store

SCRIPT = '''
import sys
from pathlib import Path
from auto_agents_2 import cli
from auto_agents_2.config import ConfigStore
from auto_agents_2.interaction import choice_spec
project=Path(sys.argv[1])
ConfigStore(project).initialize()
async def waiting(self, rid):
    kind=sys.argv[2]
    interaction=choice_spec(["通过", "修改"]) if kind=="choice" else None
    await self._ask(rid, "provider/test" if kind=="confirm" else "fixture", "等待测试输入", "v1", interaction)
cli.WorkflowEngine._execute=waiting
cli.run(project=project, requirements="fixture" if sys.argv[3]=="new" else None, provider=None)
'''


def pause_once(project, kind, resume=False):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 160, 0, 0))
    process = subprocess.Popen([sys.executable, "-c", SCRIPT, str(project), kind, "resume" if resume else "new"],
                               stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    output = bytearray()
    sent = False
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
            text = output.decode(errors="replace")
            if not sent and ("Ctrl+D" in text if kind == "text" else "请选择" in text):
                os.write(master, b"n\r" if kind == "confirm" else b"unfinished\x03")
                sent = True
            if process.poll() is not None:
                break
        process.wait(timeout=3)
        assert sent and process.returncode == 0, output.decode(errors="replace")
        assert "已暂停并保存检查点" in output.decode(errors="replace")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)


@pytest.mark.parametrize("kind", ["choice", "text"])
def test_ctrl_c_pauses_and_same_run_resumes_from_input(tmp_path, kind):
    pause_once(tmp_path, kind)
    store = Store(tmp_path)
    run = store.current()
    assert run["status"] == "PAUSED"
    question = store.pending(run["id"])[0]
    assert question["answer"] is None
    pause_once(tmp_path, kind, resume=True)
    assert store.current()["id"] == run["id"]
    assert store.current()["status"] == "PAUSED"
    assert store.pending(run["id"])[0]["id"] == question["id"]


def test_pause_menu_exits_cleanly_and_does_not_reuse_negative_answer(tmp_path):
    pause_once(tmp_path, "confirm")
    store = Store(tmp_path)
    rid = store.current()["id"]
    assert store.current()["status"] == "PAUSED"
    pause_once(tmp_path, "confirm", resume=True)
    assert store.current()["id"] == rid
    assert store.current()["status"] == "PAUSED"
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM questions WHERE answer='n' AND superseded=1").fetchone()[0] == 2


@pytest.mark.parametrize("action", ["decline", "choice_interrupt", "text_interrupt", "submit", "submitted_interrupt"])
def test_completed_iteration_prompt_in_real_terminal(tmp_path, action):
    script = '''
import sys
from pathlib import Path
from auto_agents_2 import cli
from auto_agents_2.config import ConfigStore
from auto_agents_2.requirements import capture
from auto_agents_2.store import Store
project = Path(sys.argv[1])
ConfigStore(project).initialize()
store = Store(project)
previous = store.admit("previous.md", {})
store.update(previous["id"], status="SUCCEEDED", outcome="DELIVERED")
async def finish(self, rid):
    raw = await capture(self.store.run(rid)["data"]["source"], self.artifacts)
    self.store.checkpoint(rid, "capture", raw)
    if sys.argv[2] == "submitted_interrupt":
        await self._ask(rid, "clarify/0", "等待新迭代澄清", "v1")
    self.store.update(rid, status="SUCCEEDED", outcome="DELIVERED")
    return self.store.run(rid)
cli.WorkflowEngine._execute = finish
cli.run(project=project, requirements=None, provider=None)
'''
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 160, 0, 0))
    process = subprocess.Popen([sys.executable, "-c", script, str(tmp_path), action],
                               stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    output = bytearray()
    step = 0
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
            text = output.decode(errors="replace")
            if step == 0 and "请选择 [y/n]" in text:
                assert text.index("如何运行项目") < text.index("是否开始新的迭代")
                os.write(master, b"n\r" if action == "decline" else b"\x03" if action == "choice_interrupt" else b"y\r")
                step = 1
            elif step == 1 and "Ctrl+D" in text:
                os.write(master, b"draft\x03" if action == "text_interrupt" else "需求第一行\r需求第二行\x04".encode())
                step = 2
            elif step == 2 and action == "submitted_interrupt" and "等待新迭代澄清" in text:
                os.write(master, b"\x03")
                step = 3
            if process.poll() is not None:
                break
        process.wait(timeout=3)
        rendered = output.decode(errors="replace")
        assert process.returncode == 0 and step, rendered
        assert rendered.count("是否开始新的迭代") == 1
        store = Store(tmp_path)
        if action in {"submit", "submitted_interrupt"}:
            current = store.current()
            from pathlib import Path
            assert Path(current["data"]["source"]).read_text() == "需求第一行\n需求第二行"
            assert store.get(current["id"], "capture")
            assert current["status"] == ("PAUSED" if action == "submitted_interrupt" else "SUCCEEDED")
            assert ("已暂停并保存检查点" in rendered) == (action == "submitted_interrupt")
            assert "未创建新迭代" not in rendered
        else:
            assert "已退出，未创建新迭代" in rendered and "已暂停" not in rendered
            assert len(store.successful()) == 1
            assert not (store.root / "requirements").exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)


@pytest.mark.parametrize("repetitions", [30, 1000])
def test_long_chinese_question_and_complete_scope_are_visible_in_a_real_terminal(repetitions):
    script = '''
import asyncio, sys
from prompt_toolkit import PromptSession
from auto_agents_2.interaction import prompt_answer, choice_spec
repetitions = int(sys.argv[1])
question = {"prompt": "QUESTION_START 允许范围：" + "任务范围说明" * repetitions + " SCOPE_END_MARKER\\n恢复依据：" + "诊断依据说明" * repetitions + " REASON_END_MARKER",
            "interaction": choice_spec(["授权恢复", "暂停"], yes_no=True)}
asyncio.run(prompt_answer(PromptSession(), question))
'''
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 80, 0, 0))
    process = subprocess.Popen([sys.executable, "-c", script, str(repetitions)], stdin=slave, stdout=slave, stderr=slave,
                               env={**os.environ, "TERM": "xterm-256color"}, start_new_session=True)
    os.close(slave)
    output = bytearray()
    sent = False
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break
            if not sent and "请选择" in output.decode(errors="replace"):
                os.write(master, b"n\r")
                sent = True
            if process.poll() is not None:
                break
        process.wait(timeout=3)
        rendered = output.decode(errors="replace")
        assert sent and process.returncode == 0, rendered
        unwrapped = rendered.replace("\r", "").replace("\n", "")
        assert all(marker in unwrapped for marker in ("QUESTION_START", "SCOPE_END_MARKER", "REASON_END_MARKER"))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
