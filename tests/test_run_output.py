import asyncio
import shlex
from io import StringIO

import pytest
from rich.console import Console
from test_workflow import CodingAgent, engine

from auto_agents_2 import cli
from auto_agents_2.config import ConfigStore
from auto_agents_2.display import ProgressDisplay
from auto_agents_2.models import Command, PlanResult, PreviewSpec, TaskSpec
from auto_agents_2.store import Store


def task(number, dependencies=()):
    return TaskSpec(id=f"T{number}", title=f"任务 {number}", description="implement",
                    requirement_ids=["R1"], depends_on=list(dependencies),
                    allowed_paths=[f"part{number}"], acceptance=["works"],
                    checks=[Command(argv=["python", "-m", "pytest"])])


@pytest.mark.parametrize("tty", [False, True])
async def test_task_numbers_survive_parallel_failure_and_resume(tmp_path, monkeypatch, tty):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("progress", {})["id"]
    (tmp_path / "README.md").write_text("# Fixture\n")
    workflow.workspaces.checkpoint(tmp_path, f"refs/aa2/{rid}/baseline", "fixture baseline")
    plan = PlanResult(summary="three tasks", document="plan",
                      tasks=[task(1), task(2), task(3, ["T1", "T2"])],
                      final_checks=[Command(argv=["python", "-m", "pytest"])])
    monkeypatch.setattr(workflow.workspaces, "integrate", lambda *args: None)
    monkeypatch.setattr(workflow.workspaces, "checkpoint", lambda *args: None)
    failed = False

    async def implement(rid, task, *args):
        nonlocal failed
        await asyncio.sleep(0)
        if task.id == "T2" and not failed:
            failed = True
            raise RuntimeError("interrupted task")
        return task.id

    monkeypatch.setattr(workflow, "_task", implement)
    with pytest.raises(RuntimeError, match="interrupted task"):
        await workflow._develop(rid, tmp_path, plan, {})
    assert workflow.store.get(rid, "develop/done") == ["T1"]
    await workflow._develop(rid, tmp_path, plan, {})
    events = workflow.store.events(rid)
    starts = [e["data"] for e in events if e["kind"] == "task.started"]
    assert [(e["task"], e["index"], e["total"]) for e in starts] == [
        ("T1", 1, 3), ("T2", 2, 3), ("T2", 2, 3), ("T3", 3, 3)]
    assert [e["data"] for e in events if e["kind"] == "tasks.planned"] == [
        {"total": 3, "done": 0}, {"total": 3, "done": 1}]
    output = StringIO()
    display = ProgressDisplay(Console(file=output, width=200), tty)
    for event in events:
        display.event(event)
    display.close()
    rendered = output.getvalue()
    assert "共 3 个任务｜已验收 1/3" in rendered
    assert rendered.count("正在执行第 2/3 个任务｜T2｜任务 2") == 2
    assert "正在执行第 3/3 个任务｜T3｜任务 3" in rendered
    assert "任务验收 3/3" in rendered


@pytest.mark.parametrize("status, outcome, preview, runtime_ready", [
    ("SUCCEEDED", "DELIVERED", True, True),
    ("SUCCEEDED", "DELIVERED", False, True),
    ("SUCCEEDED", "NO_CHANGES", False, True),
    ("SUCCEEDED", "DELIVERED", True, False),
    ("PAUSED", "", True, True),
    ("CANCELLED", "", True, True),
])
async def test_execute_prints_launch_guidance_only_after_success(
        tmp_path, monkeypatch, status, outcome, preview, runtime_ready):
    project = tmp_path / "project [demo] with spaces"
    project.mkdir()
    ConfigStore(project).initialize()
    store = Store(project)
    rid = store.admit("guide", {})["id"]
    store.update(rid, status=status, outcome=outcome)
    command = Command(argv=["python", "-m", "tools.preview", "--port", "{port}", "--label", "demo [local]"],
                      cwd="backend app")
    if preview:
        store.checkpoint(rid, "plan", {"preview": PreviewSpec(command=command, routes=["/tasks/"]).model_dump()})
    if runtime_ready:
        (project / ".aa2/runtime.sh").write_text("# prepared environment\n")

    class FinishedEngine:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self, *args, **kwargs):
            return store.run(rid)

    output = StringIO()
    monkeypatch.setattr(cli, "WorkflowEngine", FinishedEngine)
    monkeypatch.setattr(cli, "console", Console(file=output, width=200))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False)
    await cli.execute(project)
    rendered = output.getvalue()
    assert "Token：" in rendered
    if status != "SUCCEEDED":
        assert "如何运行项目" not in rendered
        return
    assert "如何运行项目（Bash）" in rendered
    assert f"cd {shlex.quote(str(project))}" in rendered
    assert str(project / "README.md") in rendered
    if not runtime_ready:
        assert "未找到已准备的环境入口" in rendered
        assert "source .aa2/runtime.sh" not in rendered
        assert "tools.preview" not in rendered
    elif preview:
        assert "source .aa2/runtime.sh" in rendered
        assert "cd 'backend app'" in rendered
        line = next(line for line in rendered.splitlines() if line.startswith("python -m"))
        assert shlex.split(line) == [arg.replace("{port}", "8000") for arg in command.argv]
        assert "http://127.0.0.1:8000/tasks/" in rendered
        assert "预览可能使用临时数据" in rendered
        assert "Ctrl+C" in rendered
    else:
        assert "source .aa2/runtime.sh" in rendered
        assert "随后执行 README.md 中的项目启动命令" in rendered


def test_zero_task_plan_is_displayed_without_an_active_task():
    output = StringIO()
    display = ProgressDisplay(Console(file=output), False)
    display.event({"kind": "tasks.planned", "time": 100, "data": {"total": 0, "done": 0}})
    assert "共 0 个任务｜已验收 0/0" in output.getvalue()
    assert "正在执行" not in output.getvalue()
