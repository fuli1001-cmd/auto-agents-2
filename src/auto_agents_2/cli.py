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

from .config import Config, ConfigStore, default_config
from .display import ProgressDisplay
from .engine import WorkflowEngine
from .errors import AA2Error, BusyProject, IncompleteIteration
from .interaction import prompt_answer
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
        if from_config:
            try:
                imported = json.loads(from_config.expanduser().read_text())
                with file_lock(config.path.with_suffix(".lock")):
                    body = (config.load() if config.path.exists() else default_config()).model_dump(mode="json")
                    fields = {"kind", "binary", "environment", "profile_map", "extra_args"}
                    body["providers"] = {name: {k: v for k, v in value.items() if k in fields}
                                         for name, value in imported["providers"].items()}
                    body["active_provider"] = imported["active_provider"]
                    body["efforts"].update(imported.get("efforts", {}))
                    Config.model_validate(body)
                    write_json(config.path, body)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                raise AA2Error("Cannot read, validate or persist imported provider configuration") from exc
        else:
            config.initialize()
        Store(project)
        WorkspaceManager(project).initialize()
        console.print(f"配置已创建：{config.path}")
        console.print("请检查 providers/profile_map，然后运行 aa2 doctor。")
    except AA2Error as exc:
        fail(exc)


async def render(store: Store, stop: asyncio.Event, paused: asyncio.Event, last: int, input_ready=None):
    display = ProgressDisplay(console, sys.stdout.isatty())
    try:
        while not stop.is_set():
            if not paused.is_set() or input_ready is None or not input_ready.is_set():
                for event in store.events(after=last):
                    last = event["seq"]
                    display.event(event)
            if paused.is_set():
                display.pause()
                if input_ready is not None:
                    input_ready.set()
            else:
                display.refresh()
            await asyncio.sleep(1)
        # Calls can finish while the controller is stopping.
        for event in store.events(after=last):
            display.event(event)
    finally:
        display.close()


async def execute(project, requirements=None, provider=None, run_id=None):
    configuration(project)
    store = Store(project)
    if requirements is not None and store.current(unfinished=True):
        raise IncompleteIteration(f"上一轮迭代尚未完成。请先执行：aa2 run --project {shlex.quote(str(project))}")
    if requirements is None and run_id is None:
        attached = store.current()
        if not attached:
            raise AA2Error("尚无可恢复迭代，请通过 --requirements 提供原始需求")
        run_id = attached["id"]
    stop = asyncio.Event()
    paused = asyncio.Event()
    input_ready = asyncio.Event()
    with store.connect() as db:
        cursor = db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
    session = PromptSession() if sys.stdin.isatty() and sys.stdout.isatty() else None
    display = asyncio.create_task(render(store, stop, paused, cursor, input_ready))

    async def question(request):
        input_ready.clear()
        paused.set()
        try:
            await input_ready.wait()
            return await prompt_answer(session, request)
        finally:
            paused.clear()

    async def supervised_start(*args, **kwargs):
        owner = asyncio.create_task(engine.start(*args, **kwargs))
        try:
            await asyncio.wait([owner, display], return_when=asyncio.FIRST_COMPLETED)
            if display.done():
                try:
                    await display
                except Exception as exc:
                    raise AA2Error("Progress display failed; workflow paused") from exc
                raise AA2Error("Progress display stopped before the workflow")
            return await owner
        finally:
            if not owner.done():
                owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)

    try:
        engine = WorkflowEngine(project, question_handler=question if session else None)
        try:
            result = await supervised_start(requirements, provider, run_id)
        except BusyProject:
            if requirements is not None:
                raise IncompleteIteration(f"项目已有控制进程。请先执行：aa2 run --project {project}")
            if provider:
                engine.config.resolve("clarify", provider)
                old, new = engine.config.activate(provider)
                store.event(None, "provider.changed", {"from": old, "to": new, "reason": "explicit"})
            console.print("已连接当前控制进程；使用 aa2 respond 回复待处理问题。")
            while True:
                await asyncio.sleep(1)
                attached = store.run(run_id)
                if attached["status"] in {"SUCCEEDED", "CANCELLED"}:
                    result = attached
                    break
                try:
                    result = await supervised_start(run_id=run_id)
                    break
                except BusyProject:
                    continue
    finally:
        stop.set()
        if display.done() and not display.cancelled() and display.exception() is not None:
            display.exception()  # Already propagated by supervised_start.
        else:
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
    except (KeyboardInterrupt, asyncio.CancelledError):
        console.print("已暂停并保存检查点；使用相同项目的 aa2 run 续跑。")


@app.command()
def resume(project: Project = Path("."), run_id: Annotated[str | None, typer.Option("--run")] = None,
           provider: Annotated[str | None, typer.Option("--provider")] = None):
    """显式恢复入口，RUN_ID 可省略。"""
    try:
        asyncio.run(execute(project.resolve(), provider=provider, run_id=run_id))
    except AA2Error as exc:
        fail(exc)
    except (KeyboardInterrupt, asyncio.CancelledError):
        console.print("已保存检查点。")


@app.command()
def status(project: Project = Path("."), run_id: Annotated[str | None, typer.Option("--run")] = None):
    try:
        configuration(project)
        store = Store(project, readonly=True)
        current = store.run(run_id) if run_id else store.current()
        console.print_json(data={"run": current, "questions": store.pending(current["id"]) if current else [],
                                "tokens": store.usage_summary(current["id"]) if current else {},
                                "replanning": store.get(current["id"], "replanning/pending", {}) if current else {},
                                "recovery": store.get(current["id"], "recovery/active", {}) if current else {},
                                "issues": store.get(current["id"], "issues", {}) if current else {},
                                "action_budgets": store.recovery_budgets(current["id"]) if current else {},
                                "recovery_observations": {t.get("origin_id") or t["id"]: store.get(current["id"],
                                    "recovery/observations/" + (t.get("origin_id") or t["id"]), [])
                                    for t in (store.get(current["id"], "plan", {}) or {}).get("tasks", [])} if current else {},
                                "plan_revision": store.get(current["id"], "plan_revision/current", {}) if current else {},
                                "tasks": {t["id"]: store.get(current["id"], "task_state/" + t["id"], {})
                                          for t in (store.get(current["id"], "plan", {}) or {}).get("tasks", [])} if current else {},
                                "delivery": store.get(current["id"], "delivery/journal", store.get(current["id"], "delivery/receipt", {})) if current else {}})
    except AA2Error as exc:
        fail(exc)


@app.command()
def logs(project: Project = Path("."), run_id: Annotated[str | None, typer.Option("--run")] = None,
         after: int = 0):
    try:
        configuration(project)
        store = Store(project, readonly=True)
        current = store.run(run_id) if run_id else store.current()
        if current:
            for event in store.events(current["id"], after):
                print(json.dumps(event, ensure_ascii=False))
    except AA2Error as exc:
        fail(exc)


@app.command()
def respond(answer: Annotated[str | None, typer.Option("--answer")] = None, project: Project = Path("."),
            request: Annotated[str | None, typer.Option("--request")] = None,
            run_id: Annotated[str | None, typer.Option("--run")] = None,
            artifact: Annotated[str | None, typer.Option("--artifact")] = None,
            answer_file: Annotated[Path | None, typer.Option("--answer-file", help="UTF-8 多行回复文件")] = None):
    try:
        if (answer is None) == (answer_file is None):
            raise AA2Error("请指定 --answer 或 --answer-file，二选一。")
        if answer_file is not None:
            try:
                answer = answer_file.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise AA2Error("无法读取 UTF-8 回复文件") from exc
        configuration(project)
        store = Store(project)
        current = store.run(run_id) if run_id else store.current(unfinished=True)
        pending = store.pending(current["id"]) if current else []
        if request:
            pending = [q for q in pending if q["id"] == request]
        if len(pending) != 1:
            raise AA2Error("需要唯一待处理问题；使用 --request 指定 status 显示的问题")
        if pending[0]["key"] == "prototype-approval" and artifact is None:
            raise AA2Error("脚本审批需要 --artifact 指定 status 显示的原型版本")
        store.answer(pending[0]["id"], answer, artifact)
        console.print("回复已保存。")
    except AA2Error as exc:
        fail(exc)


@app.command()
def cancel(project: Project = Path(".")):
    """发布前取消迭代；发布开始后暂停收尾并保留恢复记录。"""
    try:
        cfg = configuration(project)
        store = Store(project)
        current = store.current(unfinished=True)
        if not current:
            raise AA2Error("没有未完成迭代")
        store.checkpoint(current["id"], "control/cancel", True)
        try:
            with file_lock(store.root / "controller.lock", blocking=False):
                active = store.current(unfinished=True)
                if store.run(current["id"])["status"] in {"SUCCEEDED", "CANCELLED"} or not active or active["id"] != current["id"]:
                    console.print("所选迭代已结束；未修改其状态。")
                    return
                SandboxRunner(store.root, cfg.load().sandbox_binary).reap_orphans()
                published = store.get(current["id"], "delivery/journal", {}).get("publication_started", False)
                if published:
                    store.update(current["id"], status="PAUSED")
                else:
                    WorkflowEngine(project).archive_workspaces(current["id"])
                    store.update(current["id"], status="CANCELLED")
                    resources = ResourceManager(store, cfg.load().cleanup)
                    resources.release_run(current["id"])
                    resources.collect()
                    resources.collect_artifacts_safely()
        except BusyProject:
            pass
        console.print("停止请求已保存；若已开始发布则保留现场并暂停，使用 aa2 run 继续完成交付。")
    except AA2Error as exc:
        fail(exc)


@app.command()
def gc(project: Project = Path("."), dry_run: bool = False):
    try:
        cfg = configuration(project).load()
        store = Store(project)
        resources = ResourceManager(store, cfg.cleanup)
        if not dry_run:
            try:
                with file_lock(store.root / "controller.lock", blocking=False):
                    resources.finish_pending()
                    resources.collect_artifacts_safely()
                    result = resources.collect()
            except BusyProject:
                raise AA2Error("控制器正在运行；本次未执行资源删除，请稍后重试") from None
        else:
            result = resources.collect(True)
        console.print_json(data=result)
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
            with file_lock(store.root / "controller.lock", blocking=False):
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
