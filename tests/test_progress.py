import asyncio
import json
import sys
from io import StringIO

import pytest
from rich.console import Console
from test_runtime import configured

from auto_agents_2 import cli
from auto_agents_2.display import ProgressDisplay
from auto_agents_2.models import ReviewResult
from auto_agents_2.providers import ProviderRouter, diagnostic_summary
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.transports import CodexTransport
from auto_agents_2.util import write_json


def event(kind, when, **data):
    return {"kind": kind, "time": when, "data": data}


def test_live_steps_keep_history_and_isolate_parallel_output(monkeypatch):
    frames = []

    class Live:
        def __init__(self, value, **kwargs):
            self.value = value

        def start(self, **kwargs):
            self.update(self.value)

        def stop(self):
            pass

        def update(self, value, **kwargs):
            frames.append([v.plain for v in value.renderables])

    monkeypatch.setattr("auto_agents_2.display.Live", Live)
    output = StringIO()
    display = ProgressDisplay(Console(file=output, width=200), True)
    display.event(event("activity.started", 100, activity="a", label="原型检查", total=3))
    display.event(event("activity.started", 101, activity="b", label="独立审查"))
    display.event(event("activity.output", 105, activity="a", last_output=105))
    display.event(event("activity.progress", 106, activity="a", done=1, total=3))
    for now in range(110, 120):
        display.refresh(now)
    assert output.getvalue() == ""  # No appended heartbeat lines.
    assert "最近输出 14 秒前" in frames[-1][0] and "进度 1/3" in frames[-1][0]
    assert "尚未收到输出" in frames[-1][1]
    display.event(event("activity.finished", 120, activity="a", outcome="success"))
    display.refresh(121)
    assert "原型检查｜完成｜耗时 20s" in output.getvalue()
    assert len(frames[-1]) == 1 and "独立审查" in frames[-1][0]
    display.event(event("activity.output", 122, activity="a", last_output=122))  # Late event ignored.
    display.refresh(450)
    assert "长时间无输出，状态待确认" in frames[-1][0]
    display.event(event("activity.finished", 451, activity="b", outcome="error"))
    display.close()
    assert output.getvalue().count("原型检查") == 1
    assert output.getvalue().count("独立审查") == 1
    assert "最近输出" not in output.getvalue()


def test_plain_output_only_records_step_transitions():
    output = StringIO()
    display = ProgressDisplay(Console(file=output, width=200), False)
    display.event(event("activity.started", 100, activity="a", label="执行测试", total=2))
    before = output.getvalue()
    for now in range(101, 110):
        display.event(event("activity.output", now, activity="a", last_output=now))
        display.refresh(now)
    assert output.getvalue() == before
    display.event(event("activity.finished", 110, activity="a", outcome="success"))
    assert "执行测试｜完成｜耗时 10s" in output.getvalue()
    assert "最近输出" not in output.getvalue()


async def test_renderer_flushes_history_before_prompt_and_stops_writing_while_editing(tmp_path, monkeypatch):
    from auto_agents_2.store import Store
    store = Store(tmp_path)
    output = StringIO()
    monkeypatch.setattr(cli, "console", Console(file=output, width=200))
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False)
    store.event(None, "activity.started", {"activity": "a", "label": "原型检查"})
    store.event(None, "activity.finished", {"activity": "a", "outcome": "success"})
    stop, paused, ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    paused.set()
    display = asyncio.create_task(cli.render(store, stop, paused, 0, ready))
    try:
        await asyncio.wait_for(ready.wait(), 3)
        assert "完成" in output.getvalue()
        before = output.getvalue()
        store.event(None, "activity.started", {"activity": "b", "label": "并行任务"})
        await asyncio.sleep(1.1)
        assert output.getvalue() == before
    finally:
        stop.set()
        await display


@pytest.mark.parametrize("interrupt", [False, True])
async def test_provider_stream_observation_and_interrupt_cleanup(tmp_path, interrupt):
    config, store = configured(tmp_path)
    native = tmp_path / "native"
    native.mkdir()
    binary = tmp_path / "fixture"
    binary.write_text(f"#!{sys.executable}\n" + '''import sys,json,time
sys.stdin.read()
sys.stdout.write('{"type":"thread.started",')
sys.stdout.flush()
time.sleep(1.3)
print('"thread_id":"fixture"}',flush=True)
sys.stderr.write('API_KEY=never-log-this\\n')
sys.stderr.write('internal worker timed out; timeout=5\\n')
sys.stderr.flush()
time.sleep(0.1)
result={"approved":True,"summary":"done","findings":[]}
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":json.dumps(result)}}),flush=True)
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}),flush=True)
''')
    binary.chmod(0o755)
    body = config.load().model_dump(mode="json")
    body["providers"] = {"codex": body["providers"]["codex"]}
    body["active_provider"] = "codex"
    body["providers"]["codex"]["binary"] = str(binary)
    write_json(config.path, body)
    adapter = CodexTransport()
    adapter.native_home = lambda _: native
    rid = store.admit("test", {})["id"]
    router = ProviderRouter(config, store, SandboxRunner(store.root, _test_unconfined=True), transports={"codex": adapter})
    task = asyncio.create_task(router.invoke("review", {}, ReviewResult, tmp_path, rid, "review"))
    try:
        async with asyncio.timeout(3):
            while not any(e["kind"] == "agent.output" for e in store.events(rid)):
                await asyncio.sleep(0.01)
        assert not task.done()  # Partial output is observed before its newline.
        if interrupt:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await asyncio.wait_for(task, 5)).approved
        events = store.events(rid)
        assert "never-log-this" not in json.dumps(events)
        assert "provider 报告等待超时" not in json.dumps(events, ensure_ascii=False)
        finished = next(e["data"] for e in events if e["kind"] == "agent.finished")
        assert finished["outcome"] == ("interrupted" if interrupt else "success")
        assert finished["last_output"]
        assert not finished["diagnostic"]
        assert not list((store.root / "processes").glob("*.json"))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_timeout_keyword_is_not_a_provider_timeout_verdict():
    assert diagnostic_summary("internal worker timed out; timeout=5") == "provider 诊断输出"
