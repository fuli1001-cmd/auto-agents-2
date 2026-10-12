from __future__ import annotations

import asyncio
import json
import shlex
import shutil
import sys
import time
from pathlib import Path
from typing import Annotated

import typer
from prompt_toolkit import PromptSession
from rich.console import Console

from .config import ConfigStore
from .engine import WorkflowEngine
from .errors import AA2Error, BusyProject, IncompleteIteration
from .models import ReviewResult
from .providers import ProviderRouter
from .resources import ResourceManager
from .sandbox import SandboxRunner
from .store import Store
from .util import file_lock, write_json
from .workspace import WorkspaceManager

app = typer.Typer(help="aa2 — 可恢复的软件开发编排", no_args_is_help=True)
provider_app = typer.Typer(help="配置中的 provider 实例")
app.add_typer(provider_app, name="provider")
console = Console()
Project = Annotated[Path, typer.Option("--project", "-p", help="目标项目目录")]
STAGE_LABELS = {"clarify": "需求澄清", "coverage_review": "需求覆盖审查", "provider_research": "外部服务研究",
                "prototype": "前端原型", "design": "架构设计", "plan": "任务规划", "implement": "开发实现",
                "review": "独立审查", "verify": "最终验收", "readme": "更新文档"}


def configuration(project: Path):
    if not ConfigStore(project).path.is_file():
        raise AA2Error(f"项目尚未初始化。执行：aa2 init --project {shlex.quote(str(project))}")
    return ConfigStore(project)


def fail(exc: Exception):
    console.print(f"[red]{getattr(exc, 'code', 'ERROR')}[/red]: {exc}", markup=True)
    raise typer.Exit(2)


@app.command()
def init(project: Project = Path("."), from_config: Annotated[Path | None, typer.Option("--from-config")] = None):
    """初始化项目配置，不安装目标依赖或改动全局设置。"""
    try:
        project = project.resolve()
        project.mkdir(parents=True, exist_ok=True)
        config = ConfigStore(project)
        config.initialize()
        if from_config:
            imported = json.loads(from_config.expanduser().read_text())
            body = json.loads(config.path.read_text())
            fields = {"kind", "binary", "environment", "profile_map", "extra_args"}
            body["providers"] = {name: {k: v for k, v in value.items() if k in fields}
                                 for name, value in imported["providers"].items()}
            body["active_provider"] = imported["active_provider"]
            body["efforts"].update(imported.get("efforts", {}))
            from .config import Config
            Config.model_validate(body)
            with file_lock(config.path.with_suffix(".lock")):
                write_json(config.path, body)
        Store(project)
        WorkspaceManager(project).initialize()
        console.print(f"配置已创建：{config.path}")
        console.print("请检查 providers/profile_map，然后运行 aa2 doctor。")
    except AA2Error as exc:
        fail(exc)


async def render(store: Store, stop: asyncio.Event, paused: asyncio.Event, last: int):
    operation = "启动"
    started = time.monotonic()
    while not stop.is_set():
        for event in store.events(after=last):
            last = event["seq"]
            kind, data = event["kind"], event["data"]
            if kind == "agent.started":
                label = STAGE_LABELS.get(data["stage"], data["stage"])
                if "test-change-review" in data.get("operation", ""):
                    label = "测试与配置变更审查"
                operation = f"{label}｜{data['provider']}｜{data['level']}"
                started = time.monotonic()
            elif kind == "task.integrated":
                operation = f"任务验收 {data['done']}/{data['total']}"
            elif kind == "run.updated" and data.get("status") == "WAITING_USER":
                operation = "等待用户输入：可使用 aa2 respond"
            elif kind == "resources.collected":
                operation = f"清理 {data['removed']} 项资源，释放 {data['bytes'] / 1024**2:.1f} MiB"
            elif kind == "provider.changed":
                operation = f"provider 已切换：{data['to']}"
            elif kind == "environment.preparing":
                operation = f"准备项目环境｜{data['profile']}"
                started = time.monotonic()
            elif kind == "environment.ready":
                operation = "项目环境已就绪，执行验证"
            else:
                continue
            if not sys.stdout.isatty():
                console.print(f"[{time.strftime('%H:%M:%S', time.localtime(event['time']))}] {operation}", markup=False)
        if sys.stdout.isatty() and not paused.is_set():
            console.file.write(f"\r\033[K[{time.strftime('%H:%M:%S')}] {operation}｜{int(time.monotonic()-started)}s")
            console.file.flush()
        await asyncio.sleep(1)
    if sys.stdout.isatty():
        console.file.write("\r\033[K")


async def execute(project, requirements=None, provider=None, run_id=None):
    configuration(project)
    store = Store(project)
    if requirements is not None and store.current(unfinished=True):
        raise IncompleteIteration(f"上一轮迭代尚未完成。请先执行：aa2 run --project {shlex.quote(str(project))}")
    stop = asyncio.Event()
    paused = asyncio.Event()
    with store.connect() as db:
        cursor = db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
    display = asyncio.create_task(render(store, stop, paused, cursor))
    session = PromptSession() if sys.stdin.isatty() and sys.stdout.isatty() else None

    async def question(request):
        paused.set()
        console.file.write("\r\033[K")
        try:
            return await session.prompt_async(f"\n{request['prompt']}\n> ")
        finally:
            paused.clear()

    engine = WorkflowEngine(project, question_handler=question if session else None)
    try:
        try:
            result = await engine.start(requirements, provider, run_id)
        except BusyProject:
            if provider:
                engine.config.resolve("clarify", provider)
                old, new = engine.config.activate(provider)
                store.event(None, "provider.changed", {"from": old, "to": new, "reason": "explicit"})
            console.print("已连接当前控制进程；使用 aa2 respond 回复待处理问题。")
            while True:
                await asyncio.sleep(1)
                try:
                    result = await engine.start(run_id=run_id)
                    break
                except BusyProject:
                    continue
    finally:
        stop.set()
        await display
    if result:
        summary = store.usage_summary(result["id"])
        stamp = time.strftime("%H:%M:%S")
        outcome = result["data"].get("outcome")
        label = "需求已经实现，无需重复开发" if outcome == "NO_CHANGES" else result["data"].get("branch") or result["status"]
        console.print(f"[{stamp}] {label}｜{result['id']}", markup=False)
        qualifier = "" if summary["complete"] else "至少 "
        missing = "" if summary["complete"] else "（部分调用未提供完整用量）"
        console.print(f"[{stamp}] Token：{qualifier}{summary['total_tokens']:,}；输入 {summary['input_tokens']:,}，输出 {summary['output_tokens']:,}，缓存命中 {summary['cached_tokens']:,}{missing}", markup=False)


@app.command()
def run(project: Project = Path("."), requirements: Annotated[str | None, typer.Option("--requirements")] = None,
        provider: Annotated[str | None, typer.Option("--provider")] = None):
    """带 --requirements 开启新迭代；省略时续跑当前迭代。"""
    try:
        asyncio.run(execute(project.resolve(), requirements, provider))
    except AA2Error as exc:
        fail(exc)
    except KeyboardInterrupt:
        console.print("已暂停并保存检查点；使用相同项目的 aa2 run 续跑。")


@app.command()
def resume(project: Project = Path("."), run_id: Annotated[str | None, typer.Option("--run")] = None,
           provider: Annotated[str | None, typer.Option("--provider")] = None):
    """显式恢复入口，RUN_ID 可省略。"""
    try:
        asyncio.run(execute(project.resolve(), provider=provider, run_id=run_id))
    except AA2Error as exc:
        fail(exc)
    except KeyboardInterrupt:
        console.print("已保存检查点。")


@app.command()
def status(project: Project = Path("."), run_id: Annotated[str | None, typer.Option("--run")] = None):
    try:
        configuration(project)
        store = Store(project)
        current = store.run(run_id) if run_id else store.current()
        console.print_json(data={"run": current, "questions": store.pending(current["id"]) if current else [],
                                "tokens": store.usage_summary(current["id"]) if current else {}})
    except AA2Error as exc:
        fail(exc)


@app.command()
def logs(project: Project = Path("."), run_id: Annotated[str | None, typer.Option("--run")] = None,
         after: int = 0):
    try:
        configuration(project)
        store = Store(project)
        current = store.run(run_id) if run_id else store.current()
        if current:
            for event in store.events(current["id"], after):
                print(json.dumps(event, ensure_ascii=False))
    except AA2Error as exc:
        fail(exc)


@app.command()
def respond(answer: Annotated[str, typer.Option("--answer")], project: Project = Path("."),
            request: Annotated[str | None, typer.Option("--request")] = None,
            run_id: Annotated[str | None, typer.Option("--run")] = None,
            artifact: Annotated[str | None, typer.Option("--artifact")] = None):
    try:
        configuration(project)
        store = Store(project)
        current = store.run(run_id) if run_id else store.current(unfinished=True)
        pending = store.pending(current["id"]) if current else []
        if request:
            pending = [q for q in pending if q["id"] == request]
        if len(pending) != 1:
            raise AA2Error("需要唯一待处理问题；使用 --request 指定 status 显示的问题")
        if pending[0]["key"] == "prototype-approval" and answer.lower() == "approve" and artifact is None:
            raise AA2Error("脚本审批需要 --artifact 指定 status 显示的原型版本")
        store.answer(pending[0]["id"], answer, artifact)
        console.print("回复已保存。")
    except AA2Error as exc:
        fail(exc)


@app.command()
def cancel(project: Project = Path(".")):
    """明确取消当前迭代，保留正式产物和历史记录。"""
    try:
        cfg = configuration(project)
        store = Store(project)
        current = store.current(unfinished=True)
        if not current:
            raise AA2Error("没有未完成迭代")
        store.checkpoint(current["id"], "control/cancel", True)
        try:
            with file_lock(store.root / "controller.lock", blocking=False):
                SandboxRunner(store.root, cfg.load().sandbox_binary).reap_orphans()
                WorkflowEngine(project).archive_workspaces(current["id"])
                store.update(current["id"], status="CANCELLED")
                resources = ResourceManager(store, cfg.load().cleanup)
                resources.release_run(current["id"])
                resources.collect()
        except BusyProject:
            pass
        console.print("取消请求已保存，执行中的进程将停止。")
    except AA2Error as exc:
        fail(exc)


@app.command()
def gc(project: Project = Path("."), dry_run: bool = False):
    try:
        cfg = configuration(project).load()
        console.print_json(data=ResourceManager(Store(project), cfg.cleanup).collect(dry_run))
    except AA2Error as exc:
        fail(exc)


@provider_app.command("use")
def provider_use(name: str, project: Project = Path(".")):
    try:
        config = configuration(project)
        config.resolve("clarify", name)
        old, new = config.activate(name)
        Store(project).event(None, "provider.changed", {"from": old, "to": new, "reason": "explicit"})
        console.print(f"active_provider = {new}")
    except AA2Error as exc:
        fail(exc)


@app.command()
def doctor(project: Project = Path("."), live: bool = False):
    """检查配置、工具和实际只读执行边界；--live 才调用真实 agent。"""
    try:
        config = configuration(project)
        cfg, store = config.load(), Store(project)
        runner = SandboxRunner(store.root, cfg.sandbox_binary)
        sandbox_ok, explanation = runner.probe()
        checks = {"sandbox": {"ok": sandbox_ok, "detail": explanation}, "conda": bool(shutil.which(cfg.environment.conda)), "providers": {}}
        for name in cfg.providers:
            try:
                effective = config.resolve("review", name)
                checks["providers"][name] = {k: v for k, v in effective.items() if k not in {"environment", "args"}}
            except AA2Error as exc:
                checks["providers"][name] = {"error": str(exc)}
        console.print_json(data=checks)
        if live:
            if store.current(unfinished=True):
                raise IncompleteIteration("请先完成当前迭代，再执行 live doctor")
            if not sandbox_ok:
                raise AA2Error("Sandbox probe must pass before live checks")
            run_record = store.admit("doctor://live", cfg.model_dump(mode="json"))
            try:
                result = asyncio.run(ProviderRouter(config, store, runner).invoke(
                    "review", {"task": "Return approved=true, summary='provider contract verified', findings=[]. Do not execute tools."},
                    ReviewResult, project.resolve(), run_record["id"], "doctor", True))
                store.update(run_record["id"], status="SUCCEEDED", outcome="DIAGNOSTIC")
                console.print_json(data=result.model_dump())
            except BaseException:
                store.update(run_record["id"], status="CANCELLED", outcome="DIAGNOSTIC_FAILED")
                raise
            finally:
                resources = ResourceManager(store, cfg.cleanup)
                resources.release_run(run_record["id"])
                resources.collect()
        if not sandbox_ok:
            raise typer.Exit(1)
    except AA2Error as exc:
        fail(exc)
