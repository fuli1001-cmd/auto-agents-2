"""Retained step history and a separate live area for active steps."""
from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass

from rich.console import Group
from rich.live import Live
from rich.text import Text

from .interaction import user_text

STAGE_LABELS = {"clarify": "需求澄清", "coverage_review": "需求覆盖审查", "provider_research": "外部服务研究",
                "prototype": "生成界面原型", "design": "架构设计", "plan": "任务规划", "implement": "开发实现",
                "review": "独立审查", "verify": "最终验收", "readme": "更新文档",
                "recovery_diagnosis": "故障独立诊断", "decision": "自动决策"}


def runtime_instructions(project, preview=None, *, runtime_ready=True):
    """Display the delivered environment and preview contract without running commands."""
    lines = ["如何运行项目（Bash）：", f"cd {shlex.quote(str(project))}"]
    if runtime_ready:
        lines.append("source .aa2/runtime.sh")
        if preview:
            lines.append("\n启动已验收的应用预览（示例端口 8000，可自行修改）：")
            if preview.command.cwd != ".":
                lines.append(f"cd {shlex.quote(preview.command.cwd)}")
            lines.append(shlex.join([arg.replace("{port}", "8000") for arg in preview.command.argv]))
            lines.append("启动后访问：http://127.0.0.1:8000" + preview.routes[0])
            lines.append("按 Ctrl+C 停止预览。预览可能使用临时数据；日常运行和数据保存方式请按 README.md 的说明。")
        else:
            lines.append("随后执行 README.md 中的项目启动命令。")
    else:
        lines.append("未找到已准备的环境入口，请按 README.md 的安装与启动说明运行项目。")
    lines.append(f"运行文档：{project / 'README.md'}")
    return "\n".join(lines)


def agent_label(data):
    stage, operation = data["stage"], data.get("operation", "")
    label = STAGE_LABELS.get(stage, stage)
    if stage == "implement":
        label = {"dependencies": "修复依赖清单", "checks": "修复检查问题"}.get(data.get("repair_kind"), label)
    for key, value in (("prototype-review", "原型自动审查"), ("reintegration-prototype", "集成后原型审查"),
                       ("recovery/diagnose", "故障独立诊断"), ("recovery/patch-review", "局部计划修订审查"),
                       ("recovery/patch/", "修订局部计划"),
                       ("plan-review", "任务规划审查"), ("test-change-review", "测试与配置变更审查")):
        if key in operation:
            label = value
    if data.get("task_title"):
        label += "（" + user_text(data["task_title"]) + "）"
    return (f"{label}｜{data['provider']}｜模型：{data.get('model') or 'CLI 默认（未知）'}"
            f"｜推理：{data.get('effort') or 'CLI 默认（未知）'}")


@dataclass
class Step:
    title: str
    started: float
    output: float | None = None
    detail: str = ""
    done: int = 0
    total: int | None = None

    def line(self, now, outcome=None):
        stamp = time.strftime("%H:%M:%S", time.localtime(self.started))
        prefix = f"[{stamp}] {user_text(self.title)}"
        duration = max(0, int(now - self.started))
        if outcome:
            state = {"success": "完成", "error": "失败", "interrupted": "已暂停",
                     "verified": "检查通过", "unpassed": "复测结束，仍有检查未通过"}.get(outcome, outcome)
            return f"{prefix}｜{state}｜耗时 {duration}s"
        suffix = f"｜已运行 {duration}s"
        if self.total is not None:
            suffix += f"｜进度 {self.done}/{self.total}"
        suffix += ("｜尚未收到输出" if self.output is None
                   else f"｜最近输出 {max(0, int(now - self.output))} 秒前")
        if self.detail:
            suffix += "｜" + user_text(self.detail)
        if now - (self.output or self.started) >= 300:
            suffix += "｜⚠ 长时间无输出，状态待确认"
        return prefix + suffix


class ProgressDisplay:
    def __init__(self, console, tty):
        self.console, self.tty = console, tty
        self.steps = {}
        self.live = None

    def pause(self):
        if self.live:
            self.live.stop()
            self.live = None

    def message(self, text):
        if not re.match(r"\[\d{2}:\d{2}:\d{2}\]", text):
            text = f"[{time.strftime('%H:%M:%S')}] {text}"
        self.console.print(Text(user_text(text)))

    def start(self, key, label, when, total=None):
        self.steps[key] = Step(label, when, total=total)
        if not self.tty:
            self.message(f"[{time.strftime('%H:%M:%S', time.localtime(when))}] {label}")

    def finish(self, key, when, outcome):
        step = self.steps.pop(key, None)
        if step:
            self.message(step.line(when, outcome))

    def event(self, event):
        kind, data, when = event["kind"], event["data"], event["time"]
        key = data.get("attempt", data.get("activity", "agent"))
        if kind == "agent.started":
            self.start(key, agent_label(data), when)
        elif kind == "activity.started":
            self.start(key, data["label"], when, data.get("total"))
        elif kind in {"agent.output", "activity.output", "activity.progress"}:
            step = self.steps.get(key)
            if step:
                if data.get("last_output") is not None:
                    step.output = data["last_output"]
                if data.get("detail"):
                    step.detail = data["detail"]
                step.done = data.get("done", step.done)
                step.total = data.get("total", step.total)
        elif kind == "agent.spawned" and key in self.steps:
            self.steps[key].detail = "进程已启动"
        elif kind in {"agent.finished", "activity.finished"}:
            self.finish(key, when, data.get("outcome", "success" if data.get("complete") else "error"))
        elif kind == "agent.retrying":
            self.message(f"[{time.strftime('%H:%M:%S', time.localtime(when))}] 调用失败，准备重试"
                         f"｜{data['provider']}｜第 {data['retry']} 次｜等待 {data['delay']}s")
        elif kind == "question.waiting":
            self.message(data["label"])
            if not self.tty:
                self.message(data["prompt"])
                for option in data.get("interaction", {}).get("options", []):
                    self.message(f"{option['key']}. {option['label']}")
                self.message(data["reply_hint"])
        elif kind == "tasks.planned":
            self.message(f"任务计划｜共 {data['total']} 个任务｜已验收 {data['done']}/{data['total']}")
        elif kind == "task.started":
            self.message(f"正在执行第 {data['index']}/{data['total']} 个任务｜{data['task']}｜{data['title']}")
        elif kind == "task.integrated":
            self.message(f"任务验收 {data['done']}/{data['total']}")
        elif kind == "task.baseline_refreshed":
            action = "保留实现并重新验证" if data["action"] == "reverify" else "保留旧候选并在新基线上修复"
            self.message(f"更新任务依赖基线｜{data['task']}｜{action}")
        elif kind == "provider.changed":
            self.message(f"provider 已切换：{data['to']}")
        elif kind == "recovery.requested":
            self.message(f"记录故障｜归属：{data['owner']}｜{data['component']}｜{data['kind']}")
        elif kind == "recovery.observed":
            checks = sorted(data["checks"], key=lambda row: row["passed"])
            for row in checks:
                label = "通过" if row["passed"] else "未通过" if row["valid"] else "待核验"
                count = f"｜{row['passed_tests']}/{row['total_tests']} 用例通过" if row["total_tests"] else ""
                self.message(f"检查证据｜{data['scope']}｜{row['cwd']}｜{' '.join(row['argv'])}｜{label}{count}")
                if row["unpassed_test_ids"]:
                    self.message("未通过用例：\n" + "\n".join(row["unpassed_test_ids"][:6]))
        elif kind == "recovery.decided":
            action = {"recheck": "重新采集证据", "repair": "修复责任任务", "plan_patch": "局部计划修订",
                      "blocked": "缺少证据，等待处理"}.get(data["action"], STAGE_LABELS.get(data["action"], data["action"]))
            owners = ", ".join(data.get("budget_owners", [])) or data["budget_owner"]
            self.message(f"恢复裁决｜{action}｜影响任务：{', '.join(data['task_ids']) or '最终验收'}｜预算归属：{owners}\n{data['reason']}")
        elif kind == "decision.applied":
            actor = {"human": "用户", "rule": "控制器规则", "agent": "决策代理"}.get(data["actor"], data["actor"])
            budget = data.get("budget", {})
            detail = f"｜本次授权第 {budget['grant']} 次" if "grant" in budget else ""
            self.message(f"决策已应用｜{actor}｜{data['owner']}{detail}\n{data['reason']}")
        elif kind == "automation.policy":
            self.message(data["message"])
        elif kind == "recovery.authorized":
            actor = {"rule": "控制器规则", "agent": "决策代理", "human": "用户"}[data["actor"]]
            self.message(f"恢复授权｜{data['owner']}｜{data['action']}｜{actor}\n{data['reason']}")
        elif kind == "evidence.invalidated":
            codes = ", ".join(row["code"] for row in data.get("issues", [])) or data.get("code", "evidence_unavailable")
            self.message(f"证据待复验｜{data['phase']}｜{codes}｜候选及原始证据已保留")
        elif kind == "delivery.inputs_preserved":
            self.message(f"发布输入已核对｜保留 {data['retained_inputs']} 个原有输入｜继续主目录运行验收")
        elif kind == "recovery.classified":
            route = {"rule": "控制器恢复", "decision": "自动决策", "human": "需要外部处理"}.get(data["route"], data["route"])
            self.message(f"故障分类｜{data['phase']}｜{data['reason_code']}｜{route}")
        elif kind == "workflow.internal_error":
            self.message(f"aa2 内部错误｜{data['exception_type']}｜故障编号 {data['incident_id']}｜已保留现场并停止本次执行")
        elif kind == "automation.engineering":
            if data["improved"]:
                self.message(f"工程进展｜{data['owner']}｜{data['milestone']}｜业务验收仍须独立验证")
        elif kind == "automation.progress":
            self.message(f"进度评估｜{data['owner']}｜连续无新增通过 {data['stalled']} 次"
                         f"｜连续缺少有效证据 {data['missing']} 次")
        elif kind == "decision.deferred":
            self.message(f"转人工处理｜{data['owner']}｜{data['reason']}")
        elif kind == "plan.patch_activated":
            self.message(f"计划修订已生效｜版本 {data['revision']}｜复验任务：{', '.join(data['affected'])}")
        elif kind == "replan.assessed":
            outcome = ("已建立验证基线" if data["baseline"] else
                       f"新增通过 {len(data['gains'])} 项" if data["improved"] else
                       f"连续 {data['stalled']} 次无可验证改进")
            if data["regressions"]:
                outcome += f"｜{len(data['regressions'])} 项退步或缺失"
            if data.get("deferred"):
                outcome += f"｜{len(data['deferred'])} 个范围证据待复验"
            self.message(f"回退评估｜{STAGE_LABELS.get(data['stage'], data['stage'])}｜{outcome}")
        elif kind == "run.updated" and data.get("delivery_phase"):
            self.message("交付｜" + {"ready": "核对目标分支", "reintegrating": "隔离集成与复测", "publishing": "更新主目录",
                "published": "主目录已更新", "runtime": "准备主目录运行环境", "repairing_runtime": "隔离修复运行检查",
                "complete": "交付完成"}.get(data["delivery_phase"], data["delivery_phase"]))
        elif kind == "delivery.completed":
            self.message(f"交付完成｜{data['target']}｜{data['commit'][:12]}")

    def refresh(self, now=None):
        now = time.time() if now is None else now
        if not self.tty:
            return
        # Let Rich account for wrapped rows on narrow terminals; never hide the
        # output age or warning behind a long model/profile name.
        lines = Group(*(Text(s.line(now), overflow="fold") for s in self.steps.values()))
        if not self.steps:
            self.pause()
        elif self.live is None:
            self.live = Live(lines, console=self.console, auto_refresh=False, transient=True,
                             redirect_stdout=False, redirect_stderr=False)
            self.live.start(refresh=True)
        else:
            self.live.update(lines, refresh=True)

    def close(self):
        self.pause()
        for key in list(self.steps):
            self.finish(key, time.time(), "interrupted")
