from __future__ import annotations

import asyncio
import json
import shlex
import shutil
import sys
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import quote

from .artifacts import ArtifactStore
from .config import ConfigStore
from .context import ContextBuilder
from .delivery import DeliveryCoordinator, RuntimeVerificationError
from .design import (
    PROTOTYPE_BROWSER_VERSION,
    DesignCatalog,
    PreviewServer,
    browser_environment,
    inspect_application,
    inspect_prototype,
)
from .environment import EnvironmentManager
from .errors import (
    AA2Error,
    DependencyResolutionError,
    PolicyViolation,
    ProtectedChange,
    PrototypeBrowserError,
    ProviderError,
)
from .evidence import invocation_identity, reusable
from .interaction import choice_spec, is_prototype_approval, question_text
from .issues import IssueLedger
from .models import (
    CheckResult,
    Command,
    DocumentResult,
    ImplementationResult,
    IntakeResult,
    PlanResult,
    ReviewResult,
)
from .planning import ready_batch, validate_plan
from .profiles import ProjectProfile
from .progress import capture_output, observe_activity
from .providers import Agent, ProviderRouter
from .replanning import browser_criteria, check_criteria
from .requirements import capture, validate_coverage
from .research import ResearchService
from .resources import ResourceManager
from .sandbox import SandboxRunner
from .store import Store
from .util import atomic_write, digest, file_lock, identifier, scrub_text
from .verification import VerificationService, passed
from .workspace import WorkspaceManager, apply_prepared, git, legacy_tree_hash, prepare_changes, tree_hash

STAGES = ["clarify", "provider_research", "prototype", "design", "plan", "develop", "readme", "verify", "deliver"]


class Replan(AA2Error):
    def __init__(self, stage: str, reason: str):
        self.stage, self.reason = stage, reason
        super().__init__(reason)


class WorkflowEngine:
    def __init__(self, project: Path, *, agent: Agent | None = None,
                 sandbox: SandboxRunner | None = None,
                 question_handler: Callable[[dict], Awaitable[str]] | None = None):
        self.project = project.resolve()
        self.config = ConfigStore(self.project)
        self.store = Store(self.project)
        cfg = self.config.load()
        self.sandbox = sandbox or SandboxRunner(self.store.root, cfg.sandbox_binary)
        self.agent = agent or ProviderRouter(self.config, self.store, self.sandbox)
        self.artifacts = ArtifactStore(self.store.root)
        self.workspaces = WorkspaceManager(self.project)
        self.contexts = ContextBuilder(self.config.load().sensitive_paths)
        self.resources = ResourceManager(self.store, cfg.cleanup)
        self.environments = EnvironmentManager(self.config, self.sandbox)
        self.verifier = VerificationService(self.store.root, self.sandbox, self.resources)
        self.delivery = DeliveryCoordinator(self.store, self.workspaces, self.resources, self.environments,
                                            self._reintegrate_delivery, self._runtime_checks)
        self.question_handler = question_handler
        self.question_lock = asyncio.Lock()
        browser_environment(self.store.root / "cache/browsers")

    async def start(self, source: str | None = None, provider: str | None = None, run_id: str | None = None):
        # Admission check precedes source reads and provider persistence.
        if source is not None and self.store.current(unfinished=True):
            from .errors import IncompleteIteration
            raise IncompleteIteration(f"上一轮迭代尚未完成。请先执行：aa2 run --project {self.project}")
        with file_lock(self.store.root / "controller.lock", blocking=False):
            self.sandbox.reap_orphans()
            self.resources.recover_disposable()
            self.resources.finish_pending()
            if provider:
                self.config.resolve("clarify", provider)
                old, new = self.config.activate(provider)
                self.store.event(None, "provider.changed", {"from": old, "to": new, "reason": "explicit"})
            if source is not None:
                if not source.startswith(("http://", "https://")):
                    source = str(Path(source).expanduser().resolve())
                run = self.store.admit(source, self.config.load().model_dump(mode="json"))
            else:
                run = self.store.run(run_id) if run_id else self.store.current()
                if not run:
                    raise AA2Error("尚无可恢复迭代，请通过 --requirements 提供原始需求")
                if run["status"] in {"SUCCEEDED", "CANCELLED"}:
                    return run
            rid = run["id"]
            if self.store.get(rid, "control/cancel"):
                self.store.checkpoint(rid, "control/cancel", False)
            self.store.update(rid, status="RUNNING")
            if self.store.get(rid, "replanning/evidence/contracts") is None:
                self._replan_evidence(rid, "contracts", {"coverage": False, "prototype_scope": False, "plan": False})
            collector = asyncio.create_task(self._collect_periodically(rid, asyncio.current_task()))
            try:
                while True:
                    try:
                        if self.store.get(rid, "replanning/pending"):
                            await self._resume_replan(rid)
                        return await self._execute(rid)
                    except Replan as exc:
                        self.store.prepare_replan(rid, exc.stage, exc.reason)
                    except Exception as exc:
                        incidents = self.store.get(rid, "incidents", 0) + 1
                        self.store.checkpoint(rid, "incidents", incidents)
                        await self._ask(rid, f"incident/{incidents}", f"执行遇到阻塞：{exc}\n修复配置或环境后回复 retry；也可以通过 aa2 cancel 取消本轮。", digest(str(exc)))
            except asyncio.CancelledError:
                if self.store.get(rid, "control/cancel"):
                    journal = self.store.get(rid, "delivery/journal", {})
                    published = journal.get("publication_started", False)
                    self.store.update(rid, status="PAUSED" if published else "CANCELLED")
                    return self.store.run(rid)
                self.store.update(rid, status="PAUSED")
                raise
            except BaseException as exc:
                self.store.update(rid, status="PAUSED", last_error=str(exc)[:2000])
                raise
            finally:
                collector.cancel()
                await asyncio.gather(collector, return_exceptions=True)
                if self.store.run(rid)["status"] in {"SUCCEEDED", "CANCELLED"}:
                    if self.store.run(rid)["status"] == "CANCELLED":
                        self.archive_workspaces(rid)
                    self.resources.release_run(rid)
                self.resources.collect()
                self.resources.collect_artifacts_safely()

    def _replan_evidence(self, rid, scope, criteria, **identity):
        self.store.checkpoint(rid, f"replanning/evidence/{scope}", {"criteria": criteria, **identity})

    def _replan_contract(self, rid, name, valid):
        saved = self.store.get(rid, "replanning/evidence/contracts", {})
        criteria = {"coverage": False, "prototype_scope": False, "plan": False, **saved.get("criteria", {})}
        criteria[name] = valid
        self._replan_evidence(rid, "contracts", criteria)

    def _replan_checks(self, rid, scope, commands, results, work, environment):
        source = tree_hash(work)
        self._replan_evidence(rid, scope, check_criteria(commands, results, source, environment.fingerprint),
                              source_hash=source, environment_hash=environment.fingerprint)

    async def _resume_replan(self, rid):
        pending = self.store.get(rid, "replanning/pending")
        decision = pending["decision"]
        if decision["stalled"] >= self.config.load().execution.max_stalled_replans and not pending["granted"]:
            interaction = choice_spec(["已处理，再尝试一次", "暂停，稍后续跑"], ["continue", "pause"], yes_no=True)
            interaction["pause_on"] = "n"
            await self._ask(rid, f"replan-stalled/{pending['evaluation']}",
                            f"连续 {decision['stalled']} 次回退未检测到可验证改进，已停止自动执行并保留现场。\n"
                            f"回退目标：{pending['stage']}\n原因：{pending['reason']}\n"
                            "请处理阻塞后确认再尝试一次，或暂停。确认不会清零历史计数。",
                            digest(pending), interaction)
            pending["granted"] = True
            self.store.checkpoint(rid, "replanning/pending", pending)
        # Include already archived refs when recovering an interruption mid-archive.
        archived = self.archive_workers(rid, pending["generation"])
        self.store.supersede_questions(rid, f"replan-stalled/{pending['evaluation']}")
        self.store.complete_replan(rid, STAGES[STAGES.index(pending["stage"]):], archived)

    async def _collect_periodically(self, rid, owner):
        elapsed = 0
        while True:
            await asyncio.sleep(1)
            if self.store.get(rid, "control/cancel"):
                owner.cancel()
                return
            elapsed += 1
            if elapsed >= self.config.load().cleanup.interval_seconds:
                self.resources.collect()
                self.resources.collect_artifacts_safely()
                elapsed = 0

    async def _ask(self, rid, key, prompt, artifact_hash="", interaction=None) -> str:
        if interaction is None and (key.startswith(("incident/", "provider/", "round-budget/"))
                                    or key in {"token-budget", "coverage-evidence", "disk-space"}):
            prompt = prompt.replace("回复 retry", "确认重试").replace("回复 continue", "确认继续")
            interaction = choice_spec(["已处理，继续", "暂停，稍后续跑"], ["continue", "pause"], yes_no=True)
            interaction["pause_on"] = "n"
        prompt = question_text(prompt)
        interaction = interaction or {"kind": "text"}
        question = self.store.request(rid, key, prompt, artifact_hash, interaction)
        if question["answer"] is None and self.store.run(rid)["status"] != "WAITING_USER":
            self.store.update(rid, status="WAITING_USER")
        if question["answer"] is None:
            hint = (f"aa2 respond --project {shlex.quote(str(self.project))} --request {question['id']}"
                    + (f" --artifact {artifact_hash}" if key == "prototype-approval" else "")
                    + (" --answer <选项>" if interaction["kind"] == "choice" else " --answer-file <文本文件>"))
            self.store.event(rid, "question.waiting", {"label": "等待用户审核原型" if key == "prototype-approval" else "等待用户输入",
                             "prompt": prompt, "interaction": interaction, "reply_hint": hint})
        while question["answer"] is None:
            if self.question_handler:
                async with self.question_lock:
                    async def external_answer():
                        while True:
                            current = self.store.request(rid, key, prompt, artifact_hash, interaction)
                            if current["answer"] is not None:
                                return current["answer"]
                            await asyncio.sleep(self.config.load().execution.wait_poll_seconds)
                    tasks = [asyncio.create_task(self.question_handler(question)), asyncio.create_task(external_answer())]
                    try:
                        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        answer = next(iter(done)).result()
                        try:
                            self.store.answer(question["id"], answer, artifact_hash)
                        except AA2Error:
                            # Only a competing valid response may win this race.
                            current = self.store.request(rid, key, prompt, artifact_hash, interaction)
                            if current["answer"] is None:
                                raise
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
            else:
                await asyncio.sleep(self.config.load().execution.wait_poll_seconds)
            question = self.store.request(rid, key, prompt, artifact_hash, interaction)
            if self.store.run(rid)["status"] == "CANCELLED":
                raise AA2Error("Iteration cancelled")
        self.store.update(rid, status="RUNNING")
        answer = question["answer"]
        if question["interaction"].get("kind") == "choice":
            answer = next(o["value"] for o in question["interaction"]["options"] if o["key"] == answer)
        if question["interaction"].get("pause_on") == question["answer"]:
            # A pause is not a reusable confirmation on the next resume.
            self.store.supersede_questions(rid, key, include_answered=True)
            raise asyncio.CancelledError()
        return answer

    async def _invoke(self, stage, context, schema, workspace, rid, key, fresh=False):
        invocation_key = "invocation/" + key
        source_before = tree_hash(workspace)
        identity = invocation_identity(stage, context, schema, workspace, self.config.load(), source_before)
        saved = self.store.get(rid, invocation_key, {})
        if saved.get("identity") == identity and saved.get("result"):
            return schema.model_validate(saved["result"])
        self.store.update(rid, stage=stage)
        try:
            result = await self.agent.invoke(stage, context, schema, workspace, rid, key, fresh)
        except ProviderError as exc:
            await self._ask(rid, f"provider/{key}", f"可用 provider 暂时无法完成任务：{exc}\n修复配置或认证后回复 retry。", digest(str(exc)))
            if isinstance(self.agent, ProviderRouter):
                self.agent.grant_retry(rid, key)
            result = await self.agent.invoke(stage, context, schema, workspace, rid, key, fresh)
        if tree_hash(workspace) != source_before:
            raise PolicyViolation("Workspace changed while generating or reviewing a candidate; preserving user changes")
        artifact = self.artifacts.put(result.model_dump(mode="json"), stage)
        if isinstance(result, ReviewResult):
            IssueLedger(self.store, self.artifacts).record(rid, key, result, context)
        self.store.checkpoint(rid, invocation_key, {"identity": identity, "result": result.model_dump(mode="json"),
                                                  "artifact": artifact})
        self.store.event(rid, "artifact.created", {"stage": stage, "operation": key, "artifact": artifact})
        return result

    async def _questions(self, rid, key, questions, fingerprint):
        answers = []
        for index, question in enumerate(questions):
            prompt = question if isinstance(question, str) else question.prompt
            options = [] if isinstance(question, str) else question.options
            answer = await self._ask(rid, f"{key}/{index}", prompt, fingerprint,
                                     choice_spec(options) if options else None)
            answers.append({"question": prompt, "answer": answer})
        return answers

    async def _approve_prototype(self, rid, work, fingerprint):
        content_hash = self._prototype_hash(work)
        directory = work / "design/prototypes"
        pages = sorted(directory.rglob("*.html"))
        if not pages:
            raise AA2Error("原型页面尚未准备好，不能请求审核")
        with PreviewServer(directory) as preview:
            links = []
            for page in pages:
                name = page.relative_to(directory).as_posix()
                label = "原型说明" if page.name == "review.html" else "界面预览"
                links.append(f"{label}（{name}）：{preview.url}{quote(name)}")
            prompt = ("等待用户审核原型\n原型已准备好，请在浏览器中查看：\n" + "\n".join(links)
                      + "\n通过后继续开发；不通过则填写修改意见，修改后重新审核。"
                      + "\n等待期间预览保持可用，后续开发暂不继续。")
            answer = await self._ask(rid, "prototype-approval", prompt, fingerprint,
                                     choice_spec(["通过，继续开发", "不通过，填写修改意见"], ["approve", "reject"]))
            if self._prototype_hash(work) != content_hash:
                raise AA2Error("原型在审核期间发生变化，已保留现场；请核对修改后再续跑")
            if answer not in {"approve", "通过", "批准"}:  # Includes saved legacy approvals.
                feedback = await self._ask(rid, "prototype-feedback", "请输入原型修改意见：", fingerprint)
                return feedback
        return None

    def _legacy_prototype(self, rid):
        pending = next((q for q in self.store.pending(rid) if q["key"] == "prototype"), None)
        if not pending:
            return None
        # Reuse the immutable candidate behind the old, premature question.
        for event in reversed(self.store.events(rid)):
            if event["kind"] == "artifact.created" and event["data"].get("operation") == "prototype":
                data = self.artifacts.json(event["data"]["artifact"])
                if digest(data) == pending["artifact_hash"]:
                    return DocumentResult.model_validate(data)
        return None

    async def _prototype_browser_report(self, rid, work, document):
        await self._ensure_browser(rid, work)
        fingerprint = self._prototype_hash(work)
        results, error = [], ""
        try:
            with observe_activity(self.store, rid, "原型浏览器检查"):
                results = await inspect_prototype(work / "design/prototypes",
                    self.store.root / "visuals" / rid / "prototype" / identifier("check"), document.browser_actions)
        except PrototypeBrowserError as exc:
            results, error = exc.results, str(exc)
        if self._prototype_hash(work) != fingerprint:
            raise AA2Error("原型在检查期间被修改，需要重新检查")
        results = self._archive_visuals(results)
        report = {"version": PROTOTYPE_BROWSER_VERSION, "source_hash": fingerprint,
                  "actions_hash": digest([a.model_dump() for a in document.browser_actions]),
                  "results": results, "error": error}
        artifact = self.artifacts.put(report, "prototype-browser")
        self.store.event(rid, "artifact.created", {"stage": "prototype", "operation": "prototype-browser", "artifact": artifact})
        # Keep the report across prototype replans, so the next proposal gets actual failures.
        self.store.checkpoint(rid, "prototype_browser_report", artifact)
        self._replan_evidence(rid, "prototype-browser", browser_criteria(document.browser_actions, results),
                              source_hash=fingerprint)
        return report

    async def _prepare_prototype(self, rid, work, specification, assessment, context, original):
        key = "prototype/pending"
        identity = digest([assessment, self.store.get(rid, "baseline"), self.store.get(rid, "replan_feedback")])
        pending = self.store.get(rid, key, {})
        report_ref = self.store.get(rid, "prototype_browser_report")
        if report_ref:
            context["controller_browser_report"] = self.artifacts.json(report_ref)
        if pending and pending["identity"] != identity:
            def legacy_defaults(value):
                if isinstance(value, dict):
                    return {k: legacy_defaults(v) for k, v in value.items()
                            if not (k == "inputs" and v == [] and "argv" in value)}
                if isinstance(value, list):
                    return [legacy_defaults(v) for v in value]
                return value
            legacy_identity = digest([legacy_defaults(assessment), self.store.get(rid, "baseline"),
                                      self.store.get(rid, "replan_feedback")])
            if pending["identity"] == legacy_identity:
                pending.update(legacy_identity=legacy_identity, identity=identity)
                self.store.checkpoint(rid, key, pending)
            else:
                # Preserve the old record as evidence, but do not apply a candidate for different inputs.
                self.artifacts.put(pending, "prototype-recovery")
                pending = {}
        if not pending:
            document = self._legacy_prototype(rid)
            if (work / "DESIGN.md").is_file():
                context["design_baseline"] = (work / "DESIGN.md").read_text()
            else:
                self.resources.register(self.store.root / "cache/design", "cache", rid)
                with observe_activity(self.store, rid, "准备原型设计参考"):
                    context["design_candidates"] = await DesignCatalog(self.store.root / "cache/design", self.sandbox).candidates(original)
            if document is None:
                document = await self._invoke("prototype", context, DocumentResult, work, rid, "prototype")
            pending = {"identity": identity, "document": document.model_dump(),
                       "design_candidates": context.get("design_candidates", []),
                       "question_refresh_version": PROTOTYPE_BROWSER_VERSION}
            self.store.checkpoint(rid, key, pending)
        document = DocumentResult.model_validate(pending["document"])
        # Re-evaluate legacy unanswered questions with real controller evidence.
        # No candidate changes are applied before resolving genuine user decisions.
        if (pending.get("question_refresh_version", 0) < PROTOTYPE_BROWSER_VERSION
                and not document.files and document.browser_actions and "prepared" not in pending
                and any(q["key"].startswith("prototype-clarification/") for q in self.store.pending(rid))):
            context["controller_browser_report"] = await self._prototype_browser_report(rid, work, document)
            context["previous_prototype"] = document.model_dump()
            with self.store.connect() as db:
                context["prototype_answer"] = [dict(row) for row in db.execute(
                    "SELECT prompt AS question,answer FROM questions WHERE run_id=? "
                    "AND key LIKE 'prototype-clarification/%' AND artifact_hash=? AND answer IS NOT NULL",
                    (rid, digest(document.model_dump())))]
            context["controller_feedback"] = (
                "The controller browser executor has been repaired. Its actual report is attached. "
                "Resolve internal evidence requests from that report; do not ask the user to supply controller logs. "
                "Preserve genuine product questions and all existing acceptance obligations.")
            refreshed = await self._invoke("prototype", context, DocumentResult, work, rid, "prototype", fresh=True)
            self.artifacts.put(pending, "prototype-recovery")
            pending.update(document=refreshed.model_dump(), question_refresh_version=PROTOTYPE_BROWSER_VERSION,
                           refreshed_question_hash=digest(document.model_dump()))
            self.store.checkpoint(rid, key, pending)
            document = refreshed
        if pending.get("refreshed_question_hash"):
            for question in self.store.pending(rid):
                if (question["key"].startswith("prototype-clarification/")
                        and question["artifact_hash"] == pending["refreshed_question_hash"]):
                    self.store.supersede_questions(rid, question["key"])
        if pending["design_candidates"]:
            context["design_candidates"] = pending["design_candidates"]
        self.store.supersede_questions(rid, "prototype")
        for correction in range(2):
            genuine = [q for q in document.questions if not is_prototype_approval(q)]
            premature = len(genuine) != len(document.questions)
            if genuine:
                context["prototype_answer"] = await self._questions(rid, "prototype-clarification", genuine, digest(document.model_dump()))
            elif not premature or document.files:
                break
            else:
                context["controller_feedback"] = "Generate prototype files now. Human approval is requested by the controller after files, checks and preview exist. Do not ask for it in questions."
            document = await self._invoke("prototype", context, DocumentResult, work, rid, "prototype")
            pending["document"] = document.model_dump()
            self.store.checkpoint(rid, key, pending)
        else:
            raise AA2Error("原型尚有未解决的问题，未进入人工审核")
        document = document.model_copy(update={"questions": []})
        if "prepared" not in pending:
            self._replan_contract(rid, "prototype_scope", False)
            candidates = {item["url"] for item in pending["design_candidates"]}
            selected = [source for source in document.sources if source.url in candidates]
            if candidates and not selected:
                raise Replan("prototype", "Cite the chosen pinned DESIGN.md candidate URL in sources before producing a new design")
            retrieved = await ResearchService(self.artifacts).verify(selected) if selected else []
            prior_pages = git(work, "ls-tree", "-r", "--name-only", self.store.get(rid, "baseline"), "--", "design/prototypes")
            scopes = specification.prototype_paths if prior_pages else ["design/prototypes", "DESIGN.md"]
            if prior_pages and document.files and not scopes:
                raise Replan("clarify", "Existing prototypes require explicit prototype_paths; preserve unrelated designs")
            if any(not (p == "DESIGN.md" or p.startswith("design/prototypes/")) for p in specification.prototype_paths):
                raise Replan("clarify", "prototype_paths must refer only to prototype pages or DESIGN.md")
            pending.update(document=document.model_dump(), retrieved=retrieved,
                           prepared=prepare_changes(work, document.files, scopes, protected=False, contracts=True))
            self.store.checkpoint(rid, key, pending)
        self._replan_contract(rid, "prototype_scope", True)
        if "fingerprint" not in pending:
            apply_prepared(work, pending["prepared"])
            pending["fingerprint"] = self._prototype_hash(work)
            self.store.checkpoint(rid, key, pending)
        fingerprint = self._prototype_hash(work)
        legacy_fingerprint = digest([legacy_tree_hash(work / "design/prototypes"),
                                    (work / "DESIGN.md").read_text() if (work / "DESIGN.md").exists() else ""])
        if pending["fingerprint"] == legacy_fingerprint:
            pending["legacy_fingerprint"] = legacy_fingerprint
            pending["fingerprint"] = fingerprint
            pending.pop("browser_version", None)
            self.store.checkpoint(rid, key, pending)
        if fingerprint != pending["fingerprint"]:
            raise AA2Error("待审核原型已被修改，已保留现场；请核对修改后再续跑")
        if "visuals" in pending and pending.get("browser_version") != PROTOTYPE_BROWSER_VERSION:
            self.artifacts.put(pending, "prototype-recovery")
            pending.pop("visuals")
            pending.pop("review", None)
            self.store.invalidate(rid, ["invocation/prototype-review"])
            self.store.checkpoint(rid, key, pending)
        if "visuals" not in pending:
            report = await self._prototype_browser_report(rid, work, document)
            if report["error"]:
                raise Replan("prototype", report["error"])
            pending["visuals"] = report["results"]
            pending["browser_version"] = PROTOTYPE_BROWSER_VERSION
            self.store.checkpoint(rid, key, pending)
        if self._prototype_hash(work) != fingerprint:
            raise AA2Error("原型在检查期间被修改，需要重新检查")
        pending["visuals"] = self._archive_visuals(pending["visuals"])
        self._replan_evidence(rid, "prototype-browser", browser_criteria(document.browser_actions, pending["visuals"]),
                              source_hash=fingerprint)
        if "review" not in pending:
            review = await self._invoke("review", {"specification": assessment, "prototype": document.model_dump(),
                "review_kind": "prototype", "design_evidence": pending["retrieved"], "browser_evidence": pending["visuals"],
                "project": self.contexts.build(work)}, ReviewResult, work, rid, "prototype-review", fresh=True)
            if not review.approved:
                raise Replan("prototype", review.summary)
            pending["review"] = review.model_dump()
            self.store.checkpoint(rid, key, pending)
        if self._prototype_hash(work) != fingerprint:
            raise AA2Error("原型在检查期间被修改，需要重新检查")
        actions_hash = digest([action.model_dump() for action in document.browser_actions])
        approval_identity = digest([fingerprint, actions_hash])
        def same_approval(run_id):
            approval = self.store.get(run_id, "prototype_approval", {})
            if approval.get("hash") not in {fingerprint, legacy_fingerprint}:
                return False
            if approval.get("actions_hash") == actions_hash:
                return True
            legacy = self.store.get(run_id, "prototype")
            return bool(legacy and digest(legacy.get("browser_actions", [])) == actions_hash)
        prior = same_approval(rid) or any(same_approval(p["id"]) for p in self.store.successful())
        if not prior:
            feedback = await self._approve_prototype(rid, work, approval_identity)
            if feedback is not None:
                raise Replan("prototype", feedback)
        self.store.checkpoint(rid, "prototype_approval", {"hash": fingerprint, "identity": approval_identity,
                              "actions_hash": actions_hash, "browser_version": PROTOTYPE_BROWSER_VERSION,
                              "review": pending["review"], "visuals": pending["visuals"]})
        return document, pending["retrieved"]

    async def _execute(self, rid):
        if self.store.run(rid)["status"] == "SUCCEEDED":
            self.resources.finish_delivery(rid)
            return self.store.run(rid)
        if self.store.get(rid, "delivery/journal"):
            return await self.delivery.finish(rid)
        run = self.store.run(rid)
        raw = self.store.get(rid, "capture")
        if not raw:
            raw = await capture(run["data"]["source"], self.artifacts)
            self.store.checkpoint(rid, "capture", raw)
        if not self.store.get(rid, "baseline"):
            self.workspaces.initialize()
            target = run["data"].get("target_branch") or self.config.load().delivery.target_branch
            if not target:
                choices = self.workspaces.target_candidates()
                if len(choices) == 1:
                    target = choices[0]
                else:
                    target = (await self._ask(rid, "delivery-target", "请选择交付目标分支：", digest(choices),
                                             choice_spec(choices))).strip()
                    if target not in choices:
                        raise AA2Error("请选择已存在的本地分支，或在 delivery.target_branch 中明确配置")
                self.config.select_target(target)
            self.store.update(rid, target_branch=target)
            base = self.workspaces.committed_baseline(rid, target)
            self.store.checkpoint(rid, "target_base", self.workspaces.head(target))
            self.store.checkpoint(rid, "baseline", base)
            self.store.update(rid, project_source_hash=tree_hash(self.project))
        base = self.store.get(rid, "baseline")
        work = self.workspaces.allocate(rid, "integration", base)
        self.resources.register(work, "worktree", rid)
        source_hash = tree_hash(work)
        original = self.artifacts.read(raw["artifact"])
        raw_hash = raw["artifact"]["hash"]
        history = []
        for previous in self.store.successful():
            if previous["id"] == rid:
                continue
            assessment = self.store.get(previous["id"], "clarify")
            previous_raw = self.store.get(previous["id"], "capture")
            if assessment:
                history.append({"run": previous["id"], "specification": assessment,
                                "research": self.store.get(previous["id"], "provider_research", {}),
                                "delivery": previous["data"].get("delivery_commit", "")})
            # Only a byte-identical input AND verified current source/lockfiles can take the fast path.
            if previous_raw and previous_raw["hash"] == raw["hash"] and previous["data"].get("verified_source_hash") == source_hash and not previous["data"].get("external_dependencies"):
                self.store.checkpoint(rid, "clarify", assessment)
                return await self._finish_no_changes(rid, work, previous["id"])
        context = {**self.store.get(rid, "clarify_context", {}), "raw_requirements": original,
                   "context_files": [str(self.artifacts.root / raw_hash[:2] / raw_hash)],
                   "raw_requirements_truncated": False, "history": history,
                   "project": self.contexts.build(work), "feedback": self.store.get(rid, "replan_feedback", {})}
        assessment = self.store.get(rid, "clarify")
        if not assessment:
            for turn in range(20):
                result = await self._invoke("clarify", context, IntakeResult, work, rid, "clarify")
                if result.questions:
                    response = await self._questions(rid, f"clarify/{turn}", result.questions, digest(result.model_dump()))
                    context.setdefault("decisions", []).append(response)
                    self.store.checkpoint(rid, "clarify_context", context)
                    continue
                if not result.requirements:
                    raise AA2Error("Requirements assessment returned no acceptance criteria")
                review = await self._invoke("coverage_review", {**context, "assessment": result.model_dump()},
                                            ReviewResult, work, rid, f"coverage-review/{turn}", fresh=True)
                if not review.approved:
                    context["review_feedback"] = review.model_dump()
                    continue
                assessment = result.model_dump()
                self.store.checkpoint(rid, "clarify", assessment)
                break
            else:
                raise AA2Error("需求澄清未能收敛，已保存现场")
        result = IntakeResult.model_validate(assessment)
        if ProjectProfile.detect(work, self.config.load().project_profile).has_frontend:
            result.frontend = True
            assessment = result.model_dump()
            self.store.checkpoint(rid, "clarify", assessment)
        all_satisfied, problems = validate_coverage(result, work)
        self._replan_contract(rid, "coverage", not problems and all(c.status != "unknown" for c in result.coverage))
        if problems:
            raise Replan("clarify", "; ".join(problems))
        if any(c.status == "unknown" for c in result.coverage):
            if result.verification:
                environment = await self._environment(work, rid)
                evidence = await self.verifier.run(result.verification, work, environment)
                self._replan_checks(rid, "coverage", result.verification, evidence, work, environment)
                self.store.checkpoint(rid, "clarify_context", {**context, "coverage_verification": [r.model_dump() for r in evidence]})
                raise Replan("clarify", "Resolve unknown coverage using the persisted verification evidence before planning changes")
            answer = await self._ask(rid, "unknown-coverage", "部分需求证据不足，不能确认已实现或直接重复开发。请补充可验证行为或现有实现的位置。", digest(assessment))
            self.store.checkpoint(rid, "clarify_context", {**context, "additional_evidence": answer})
            raise Replan("clarify", "Resolve unknown coverage using additional user evidence")
        context["specification"] = assessment
        if all_satisfied:
            checks = result.verification or self.config.load().verification
            if not checks:
                await self._ask(rid, "coverage-evidence", "已有实现缺少可执行核验证据。请在 verification 配置中补充检查命令后回复 continue。", source_hash)
                checks = self.config.load().verification
                if not checks:
                    raise AA2Error("不能仅凭语义判断宣称需求已经实现")
            environment = await self._environment(work, rid)
            evidence = await self.verifier.run(checks, work, environment)
            self._replan_checks(rid, "coverage", checks, evidence, work, environment)
            self.store.checkpoint(rid, "coverage_checks", [r.model_dump() for r in evidence])
            if passed(evidence):
                return await self._finish_no_changes(rid, work, "current-baseline")
            self.store.checkpoint(rid, "clarify_context", {**context, "regression_evidence": [r.model_dump() for r in evidence]})
            raise Replan("clarify", "Coverage verification failed. Distinguish actual regressions from missing test infrastructure; do not reimplement satisfied behaviour.")
        spec_dir = work / "docs/requirements"
        acceptance = "\n\n".join(f"### {r.id}: {r.description}\n" + "\n".join(f"- {a}" for a in r.acceptance) for r in result.requirements)
        atomic_write(spec_dir / f"{rid}.md", result.document + "\n\n## 验收条目\n\n" + acceptance + f"\n\n原始输入指纹：`{raw['hash']}`\n")
        for stage in ("provider_research", "prototype", "design", "plan"):
            if stage == "prototype" and not result.frontend:
                continue
            cached = self.store.get(rid, stage)
            if cached:
                context[stage] = cached
                continue
            if stage == "prototype":
                current_prototype = self._prototype_hash(work)
                reused = None
                if not result.ui_changes:
                    for previous in self.store.successful():
                        approval = self.store.get(previous["id"], "prototype_approval", {})
                        if approval.get("hash") == current_prototype:
                            reused = self.store.get(previous["id"], "prototype")
                            if reused:
                                self.store.checkpoint(rid, "prototype_approval", approval)
                                break
                if reused:
                    self.store.checkpoint(rid, stage, reused)
                    context[stage] = reused
                    continue
            schema = PlanResult if stage == "plan" else DocumentResult
            if stage == "plan":
                origin_state = self.store.task_origin_state(rid)
                if set(origin_state["budgets"]) - set(origin_state["origins"].values()):
                    raise Replan("plan", "Task origin history is missing for consumed task budgets; restore the registry before planning")
                context["task_origins"] = origin_state["origins"]
                context["task_budgets"] = origin_state["budgets"]
                context["unresolved_issues"] = [issue for issue in IssueLedger(self.store, self.artifacts).unresolved(rid)
                                                if issue["scope"] == "plan-review"]
            if stage == "prototype":
                document, retrieved = await self._prepare_prototype(rid, work, result, assessment, context, original)
            else:
                pending_key = "candidate/" + stage
                pending = self.store.get(rid, pending_key, {})
                document = (schema.model_validate(pending["document"]) if pending.get("document") else
                            await self._invoke(stage, context, schema, work, rid, stage))
                self.store.checkpoint(rid, pending_key, {"document": document.model_dump()})
                if isinstance(document, DocumentResult) and document.questions:
                    answer = await self._questions(rid, stage, document.questions, digest(document.model_dump()))
                    context[stage + "_answer"] = answer
                    document = await self._invoke(stage, context, schema, work, rid, stage)
                    self.store.checkpoint(rid, pending_key, {"document": document.model_dump()})
                    if document.questions:
                        raise AA2Error(f"{stage} still has unresolved questions")
                retrieved = []
            if stage == "provider_research" and document.sources:
                retrieved = await ResearchService(self.artifacts).verify(document.sources)
                review = await self._invoke("review", {"review_kind": "research", "research": document.model_dump(),
                                                     "retrieved_sources": retrieved}, ReviewResult,
                                            work, rid, "research-review", fresh=True)
                if not review.approved:
                    raise Replan(stage, review.summary)
            if stage == "plan":
                self._replan_contract(rid, "plan", False)
                unmet = {c.requirement_id for c in result.coverage if c.status != "satisfied"} or {r.id for r in result.requirements}
                try:
                    validate_plan(document, unmet)
                except AA2Error as exc:
                    raise Replan("plan", str(exc)) from exc
                if not document.runtime_probes:
                    raise Replan("plan", "Declare lightweight runtime_probes that exercise the implemented application without user/production data writes")
                prior = self.store.get(rid, "task_origins", {})
                # Validate against history, not tasks newly registered in this plan.
                origins = dict(prior)
                for task in document.tasks:
                    if task.id in prior:
                        task.origin_id = prior[task.id]
                    elif prior and task.origin_id not in set(prior.values()):
                        raise Replan("plan", f"Replacement task {task.id} must retain an existing origin_id so budgets cannot reset")
                    else:
                        task.origin_id = task.origin_id or task.id
                    origins[task.id] = task.origin_id
                self.store.checkpoint(rid, "task_origins", origins)
                # Registration changes the controller state after the planner's
                # input was built. Review the validated state, not that stale input.
                context["task_origins"] = origins
                context["task_origin_validation"] = {
                    "status": "passed", "registration": "existing" if prior else "initial",
                    "prior_origins": prior, "budget_counters_unchanged": True,
                    "scope": "task origin registration and budget inheritance only; independent plan review still required",
                }
                if result.frontend and document.preview is None:
                    raise Replan("plan", "Frontend plan requires a runnable preview and route mapping")
                self._replan_contract(rid, "plan", True)
                review = await self._invoke("review", {**context, "plan": document.model_dump(), "review_kind": "plan"}, ReviewResult,
                                            work, rid, "plan-review", fresh=True)
                if not review.approved:
                    details = "".join(f"\n\n[{f.severity}] {f.location}\n{f.description}\n证据：{f.evidence}\n修复要求：{f.resolution}"
                                      for f in review.findings)
                    raise Replan("plan", review.summary + details)
            atomic_write(work / "docs/aa2" / f"{stage}.md", document.document)
            stage_data = document.model_dump()
            if retrieved:
                stage_data["verified_sources"] = [{k: v for k, v in source.items() if k != "excerpt"} for source in retrieved]
                context.pop("selected_design_sources", None)
            self.store.checkpoint(rid, stage, stage_data)
            context[stage] = stage_data
        plan = PlanResult.model_validate(self.store.get(rid, "plan"))
        self.workspaces.checkpoint(work, f"refs/aa2/{rid}/design", "aa2: requirements and design baseline")
        await self._develop(rid, work, plan, context)
        if not self.store.get(rid, "readme"):
            pending = self.store.get(rid, "candidate/readme", {})
            docs = (DocumentResult.model_validate(pending["document"]) if pending.get("document") else
                    await self._invoke("readme", {**context, "project": self.contexts.build(work)}, DocumentResult,
                                       work, rid, "readme"))
            self.store.checkpoint(rid, "candidate/readme", {**pending, "document": docs.model_dump()})
            if any(c.path.startswith(("docs/requirements/", "docs/aa2/")) for c in docs.files):
                raise Replan("plan", "Documentation changes cannot overwrite accepted requirements, architecture or controller evidence")
            patch = pending.get("patch") or prepare_changes(work, docs.files, ["README.md", "docs", ".env.example"], protected=False)
            self.store.checkpoint(rid, "candidate/readme", {"document": docs.model_dump(), "patch": patch})
            apply_prepared(work, patch)
            self.store.checkpoint(rid, "readme", docs.model_dump())
        self._agent_instructions(work, plan)
        self._runtime_instructions(work)
        if ProjectProfile.detect(work, self.config.load().project_profile).has_frontend and not plan.runtime_prepare:
            raise Replan("plan", "Frontend delivery requires runtime_prepare commands to rebuild primary-workdir outputs after frozen dependency installation")
        environment = await self._environment(work, rid, plan)
        self._export_environment(work, environment)
        candidate = self.workspaces.checkpoint(work, f"refs/aa2/{rid}/candidate", "aa2: delivery candidate")
        checkout_policy = self.workspaces.normalize_checkout(work, candidate)
        self.store.checkpoint(rid, "verify/checkout_policy", checkout_policy)
        candidate_hash = tree_hash(work)
        verify_key = digest([candidate_hash, environment.fingerprint, plan.model_dump(), assessment,
                             self.store.get(rid, "prototype_approval", {}), context.get("provider_research", {})])
        previous_checks = self.store.get(rid, "verify/checks", []) if self.store.get(rid, "verify/identity") == verify_key else []
        evidence = [CheckResult.model_validate(c) for c in previous_checks]
        if not reusable(self._candidate_checks(plan), evidence, work, environment, self.artifacts).allowed:
            evidence = await self.verifier.run(self._candidate_checks(plan), work, environment)
        self.store.checkpoint(rid, "verify/identity", verify_key)
        self.store.checkpoint(rid, "verify/checks", [r.model_dump() for r in evidence])
        self._replan_checks(rid, "final-checks", self._candidate_checks(plan), evidence, work, environment)
        if not passed(evidence):
            raise Replan("plan", "Final verification failed: " + json.dumps([r.model_dump() for r in evidence]))
        visuals = []
        if ProjectProfile.detect(work, self.config.load().project_profile).has_frontend and not result.frontend:
            raise Replan("clarify", "Implementation introduced a frontend without a prototype and browser verification contract")
        if result.frontend:
            if self.store.get(rid, "prototype_approval", {}).get("hash") != self._prototype_hash(work):
                raise Replan("prototype", "Prototype changed after human approval")
            await self._ensure_browser(rid, work)
            with observe_activity(self.store, rid, "实现界面浏览器验收"):
                visuals = await inspect_application(plan.preview, work, self.store.root / "visuals" / rid / "implemented",
                                                    environment, self.sandbox, self.store.root)
            visuals = self._archive_visuals(visuals)
            self.store.checkpoint(rid, "verify/visuals", visuals)
            self._replan_evidence(rid, "application-browser", browser_criteria(plan.preview.actions, visuals),
                                  source_hash=tree_hash(work))
        review_key = digest([verify_key, [r.model_dump() for r in evidence], visuals])
        cached_review = self.store.get(rid, "verify/review") if self.store.get(rid, "verify/review_identity") == review_key else None
        review = ReviewResult.model_validate(cached_review) if cached_review else await self._invoke("verify", {**context, "project": self.contexts.build(work),
                                               "evidence": [r.model_dump() for r in evidence],
                                               "implemented_visuals": visuals,
                                               "prototype_approval": self.store.get(rid, "prototype_approval", {})},
                                    ReviewResult, work, rid, "final-review", fresh=True)
        if not review.approved:
            raise Replan("plan", review.summary)
        self.store.checkpoint(rid, "verify/review", review.model_dump())
        self.store.checkpoint(rid, "verify/review_identity", review_key)
        if (git(work, "rev-parse", "HEAD") != candidate or tree_hash(work) != candidate_hash
                or git(work, "diff", "--name-only", "HEAD")):
            raise AA2Error("Candidate changed during verification")
        self.store.update(rid, external_dependencies=bool(context.get("provider_research", {}).get("sources")))
        return await self.delivery.finish(rid, candidate=candidate, work=work, environment=environment, plan=plan)

    async def _environment(self, work, rid, plan=None):
        profile = ProjectProfile.detect(work, self.config.load().project_profile)
        with observe_activity(self.store, rid, f"准备项目环境（{work.name}）｜{profile.name}"):
            self.store.event(rid, "environment.preparing", {"profile": profile.name})
            for name in ("conda", "npm", "design"):
                cache = self.store.root / "cache" / name
                cache.mkdir(parents=True, exist_ok=True)
                self.resources.register(cache, "cache", rid)
            environment = await self.environments.ensure(work, profile.name, plan.environment_file if plan else "",
                                                          plan.requirements_file if plan else "")
            self.resources.register(environment.prefix, "environment", rid)
            for directory in profile.node_roots:
                await self.environments.install_node(directory, environment)
            self.store.event(rid, "environment.ready", {"fingerprint": environment.fingerprint})
            return environment

    async def _develop(self, rid, integration, plan, context):
        cfg = self.config.load()
        done = set(self.store.get(rid, "develop/done", []))
        while len(done) < len(plan.tasks):
            if shutil.disk_usage(self.store.root).free < cfg.cleanup.min_free_bytes:
                self.resources.collect()
                if shutil.disk_usage(self.store.root).free < cfg.cleanup.min_free_bytes:
                    await self._ask(rid, "disk-space", "可用磁盘不足，请释放空间或调整配额后回复 continue。")
                    if shutil.disk_usage(self.store.root).free < self.config.load().cleanup.min_free_bytes:
                        raise AA2Error("磁盘空间仍不足")
            batch = ready_batch(plan.tasks, done, cfg.execution.max_parallel_tasks)
            if not batch:
                raise AA2Error("Task graph cannot make progress")
            base = git(integration, "rev-parse", "HEAD")
            outcomes = await asyncio.gather(*(self._task(rid, task, base, plan, context) for task in batch), return_exceptions=True)
            failures = []
            for task, outcome in zip(batch, outcomes):
                if isinstance(outcome, BaseException):
                    failures.append(outcome)
                    continue
                try:
                    self.workspaces.integrate(integration, outcome)
                except AA2Error as exc:
                    raise Replan("plan", str(exc)) from exc
                done.add(task.id)
                self.store.checkpoint(rid, "develop/done", sorted(done))
                self.store.event(rid, "task.integrated", {"task": task.id, "done": len(done), "total": len(plan.tasks)})
                saved_task = self.store.get(rid, f"develop/task/{task.id}", {})
                if saved_task.get("workspace") and saved_task.get("recovery_ref"):
                    self.resources.retire_worktree(Path(saved_task["workspace"]), saved_task["recovery_ref"])
            self.workspaces.checkpoint(integration, f"refs/aa2/{rid}/integration", "aa2: integrated tasks")
            if failures:
                raise failures[0]

    async def _task(self, rid, task, base, plan, context):
        key = f"develop/task/{task.id}"
        saved = self.store.get(rid, key, {})
        if saved.get("commit"):
            return saved["commit"]
        generation = self.store.run(rid)["data"].get("replans", 0)
        work = self.workspaces.allocate(rid, f"{task.id}-p{generation}", base)
        self.resources.register(work, "worktree", rid)
        # Round counters live outside invalidated plan checkpoints.
        counter_key = f"rounds/{task.origin_id or task.id}"
        counters = self.store.get(rid, counter_key, {"round": 0, "stalled": 0, "failure": ""})
        while True:
            if saved.get("phase") not in {"implementing", "applied", "tested"}:
                counters = self.store.get(rid, counter_key, counters)
                if counters["round"] >= self.config.load().execution.max_rounds:
                    break
                counters["round"] += 1
                saved = {"phase": "implementing", "feedback": {k: v for k, v in saved.items() if k in {"checks", "review", "environment_error", "candidate_error"}}}
                self.store.checkpoints(rid, {key: saved, counter_key: counters})
            previous_feedback = saved.get("feedback", saved)
            feedback = {k: previous_feedback[k] for k in ("phase", "checks", "review", "environment_error", "candidate_error") if k in previous_feedback}
            feedback = json.loads(json.dumps(feedback))
            task_context = {"task": task.model_dump(), "specification": context["specification"],
                            "design": context.get("design"), "feedback": feedback, "unresolved_issues": IssueLedger(self.store, self.artifacts).unresolved(rid),
                            "project": self.contexts.build(work, scopes=task.allowed_paths)}
            change = (ImplementationResult.model_validate(saved["implementation"]) if saved.get("implementation") else
                      await self._invoke("implement", task_context, ImplementationResult, work, rid, key))
            if change.replan_stage:
                raise Replan(change.replan_stage, change.replan_reason)
            if saved.get("phase") == "implementing":
                # Persist the candidate before applying it. Full writes are idempotent;
                # replace changes use a postimage check when recovering an interrupted apply.
                saved["implementation"] = change.model_dump()
                self.store.checkpoint(rid, key, saved)
                try:
                    prepared = saved.get("patch") or prepare_changes(work, change.changes, task.allowed_paths)
                except ProtectedChange:
                    review = await self._invoke("review", {**task_context, "candidate_changes": change.model_dump(),
                                                           "review_kind": "test_changes",
                                                           "purpose": "Authorize proposed protected test/config changes against requirements"},
                                                ReviewResult, work, rid, key + "/test-change-review", fresh=True)
                    if not review.approved:
                        saved = {"review": review.model_dump(), "phase": "rejected"}
                        self.store.checkpoint(rid, key, saved)
                        continue
                    prepared = prepare_changes(work, change.changes, task.allowed_paths, protected=False)
                except PolicyViolation as exc:
                    saved = {"phase": "rejected", "candidate_error": str(exc)}
                    self.store.checkpoint(rid, key, saved)
                    continue
                saved["patch"] = prepared
                self.store.checkpoint(rid, key, saved)
                apply_prepared(work, prepared)
                saved.update(phase="applied", source_hash=tree_hash(work))
                self.store.checkpoint(rid, key, saved)
            try:
                environment = await self._environment(work, rid, plan)
            except DependencyResolutionError as exc:
                saved.update(phase="rejected", environment_error=str(exc), source_hash=tree_hash(work))
                fingerprint = digest({"environment_error": str(exc)})
                counters["stalled"] = counters["stalled"] + 1 if fingerprint == counters["failure"] else 0
                counters["failure"] = fingerprint
                # Retain the failed proposal even when replanning invalidates develop/.
                self.store.checkpoints(rid, {
                    key: saved, counter_key: counters,
                    f"candidate/environment-failures/{task.id}/p{generation}/r{counters['round']}": {
                        **saved, "workspace": str(work)},
                })
                if counters["stalled"] >= self.config.load().execution.max_stalled_rounds:
                    raise Replan("plan", f"Task {task.id} dependency resolution stalled: {exc}") from exc
                continue
            prior_checks = [CheckResult.model_validate(c) for c in saved.get("checks", [])]
            reuse = reusable(task.checks, prior_checks, work, environment, self.artifacts)
            if saved.get("phase") == "tested" and reuse.allowed:
                checks = prior_checks
            else:
                checks = await self.verifier.run(task.checks, work, environment)
                saved.update(phase="tested", checks=[c.model_dump() for c in checks], source_hash=tree_hash(work))
                self.store.checkpoint(rid, key, saved)
            self._replan_checks(rid, f"task/{task.origin_id or task.id}", task.checks, checks, work, environment)
            if passed(checks):
                review = await self._invoke("review", {**task_context, "implementation": change.model_dump(),
                                                       "project": self.contexts.build(work, scopes=task.allowed_paths),
                                                       "checks": [c.model_dump() for c in checks],
                                                       "source_hash": saved["source_hash"]}, ReviewResult, work, rid, key + "/review", fresh=True)
                saved["review"] = review.model_dump()
                if review.approved:
                    recovery_ref = f"refs/aa2/{rid}/tasks/{task.id}/p{generation}"
                    commit = self.workspaces.checkpoint(work, recovery_ref, f"aa2: {task.title}")
                    self.store.checkpoint(rid, key, {**saved, "commit": commit, "workspace": str(work), "recovery_ref": recovery_ref})
                    return commit
            failure = {"checks": [{"argv": c.argv, "exit_code": c.exit_code, "tests": c.tests} for c in checks],
                       "review": [{"severity": f["severity"], "location": f["location"]}
                                  for f in saved.get("review", {}).get("findings", [])]}
            fingerprint = digest(failure)
            counters["stalled"] = counters["stalled"] + 1 if fingerprint == counters["failure"] else 0
            counters["failure"] = fingerprint
            saved["phase"] = "rejected"
            self.store.checkpoints(rid, {counter_key: counters, key: saved})
            if counters["stalled"] >= self.config.load().execution.max_stalled_rounds:
                raise Replan("plan", f"Task {task.id} stalled: {json.dumps(saved)}")
        await self._ask(rid, f"round-budget/{task.id}", f"任务 {task.id} 已达到实现轮数上限。调整配置后回复 continue。", str(counters["round"]))
        if counters["round"] >= self.config.load().execution.max_rounds:
            raise AA2Error("Implementation round budget remains exhausted")
        return await self._task(rid, task, base, plan, context)

    async def _ensure_browser(self, rid, workspace):
        cache = self.store.root / "cache/browsers"
        cache.mkdir(parents=True, exist_ok=True)
        self.resources.register(cache, "cache", rid)
        if any(cache.glob("chromium-*/chrome-linux*/chrome")):
            return
        import os
        with observe_activity(self.store, rid, "准备浏览器环境"), self.resources.temporary("browser-install") as scratch:
            env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(cache), "TMPDIR": str(scratch)}
            process = await self.sandbox.spawn([sys.executable, "-m", "playwright", "install", "chromium"],
                                               workspace, env, [cache, scratch])
            try:
                async with asyncio.timeout(900):
                    _, stderr = await capture_output(process)
                if process.returncode:
                    raise AA2Error("Project-local browser installation failed: " + stderr.decode(errors="replace")[-2000:])
            finally:
                await self.sandbox.stop(process)

    def _agent_instructions(self, work, plan):
        path = work / "AGENTS.md"
        previous = path.read_bytes().decode("utf-8") if path.exists() else "# Project instructions\n"
        start, end = "<!-- aa2:begin -->", "<!-- aa2:end -->"
        if start in previous and end in previous:
            before, rest = previous.split(start, 1)
            _, after = rest.split(end, 1)
            previous = before + after
        commands = "\n".join(f"- `{shlex.join(command.argv)}` (cwd: {command.cwd})" for command in plan.final_checks)
        block = f"\n{start}\nUse the versioned requirements in docs/requirements and architecture in docs/aa2.\nPreserve accepted behaviour and the DESIGN.md baseline. Run checks relevant to the change;\ncomplete the delivery checks below before shipping. Test expectation changes need independent review.\nUse project-local dependencies. Do not install into system Python, Conda base or global npm.\n\nDelivery checks:\n{commands}\n{end}\n"
        atomic_write(path, previous.rstrip() + "\n" + block)

    def _export_environment(self, work, environment):
        for source, name in (("aa2-pip.lock", "requirements.lock"), ("aa2-conda.lock", "conda-explicit.lock")):
            file = environment.prefix / source
            if file.exists():
                text = file.read_text().replace(str(environment.prefix / "aa2-source"), ".")
                atomic_write(work / "docs/aa2/environments" / environment.fingerprint / name, scrub_text(text), 0o644)

    def _prototype_hash(self, work):
        return digest([tree_hash(work / "design/prototypes"), (work / "DESIGN.md").read_text() if (work / "DESIGN.md").exists() else ""])

    def archive_workspaces(self, rid):
        root = self.store.root / "worktrees" / rid
        if root.exists():
            for workspace in root.iterdir():
                if workspace.is_dir() and not workspace.is_symlink():
                    self.workspaces.checkpoint(workspace, f"refs/aa2/{rid}/cancelled/{workspace.name}", "aa2: cancelled iteration checkpoint")

    def archive_workers(self, rid, generation):
        prefix = f"refs/aa2/{rid}/replans/{generation}/"
        archived = git(self.project, "for-each-ref", "--format=%(refname)", prefix).splitlines()
        root = self.store.root / "worktrees" / rid
        for workspace in root.iterdir() if root.exists() else []:
            if workspace.name == "integration" or not workspace.is_dir() or workspace.is_symlink():
                continue
            ref = f"refs/aa2/{rid}/replans/{generation}/{workspace.name}"
            self.workspaces.checkpoint(workspace, ref, "aa2: recovery snapshot before replanning")
            self.resources.retire_worktree(workspace, ref)
            if ref not in archived:
                archived.append(ref)
        return archived

    async def _finish_no_changes(self, rid, work, evidence_run):
        source_hash = tree_hash(work)
        source_run = evidence_run
        if source_run == "current-baseline":
            source_run = next((r["id"] for r in self.store.successful()
                               if r["data"].get("verified_source_hash") == source_hash), None)
        inherited = self.store.run(source_run)["data"] if source_run else {"external_dependencies": True}
        plan_data = self.store.get(source_run, "plan") if source_run else None
        if source_run:
            for key in ("plan", "prototype", "prototype_approval", "provider_research", "verify/identity", "verify/checks", "verify/review"):
                value = self.store.get(source_run, key)
                if value is not None:
                    self.store.checkpoint(rid, key, value)
        plan = PlanResult.model_validate(plan_data) if plan_data else None
        environment = await self._environment(work, rid, plan)
        # A source-only match cannot establish current environment or evidence identity.
        candidate = self.workspaces.checkpoint(work, f"refs/aa2/{rid}/candidate", "aa2: dependency lock verification")
        policy = self.workspaces.normalize_checkout(work, candidate)
        self.store.checkpoint(rid, "verify/checkout_policy", policy)
        checks = self._candidate_checks(plan) if plan else (
            IntakeResult.model_validate(self.store.get(rid, "clarify")).verification or self.config.load().verification)
        evidence = await self.verifier.run(checks, work, environment)
        if not passed(evidence):
            raise AA2Error("当前环境未通过验收；保留已有实现和失败证据")
        review = await self._invoke("verify", {"project": self.contexts.build(work),
                                    "specification": self.store.get(rid, "clarify"),
                                    "evidence": [c.model_dump() for c in evidence]},
                                    ReviewResult, work, rid, "no-change-review", fresh=True)
        if not review.approved:
            raise AA2Error(review.summary)
        self.store.checkpoint(rid, "verify/checks", [c.model_dump() for c in evidence])
        self.store.checkpoint(rid, "verify/review", review.model_dump())
        self.store.checkpoint(rid, "verify/identity", digest([tree_hash(work), environment.fingerprint,
                              [c.model_dump() for c in evidence], review.model_dump()]))
        self.store.update(rid, coverage_evidence=evidence_run, external_dependencies=inherited.get("external_dependencies", False))
        return await self.delivery.finish(rid, candidate=candidate, work=work, environment=environment, plan=plan, outcome="NO_CHANGES")

    async def _runtime_checks(self, rid, journal, environment):
        plan = PlanResult.model_validate(journal["plan"]) if journal["plan"] else None
        commands = (plan.runtime_prepare + plan.runtime_probes) if plan else []
        if not commands:
            commands = [Command(argv=[str(environment.prefix / "bin/python"), "-c", "import sys; assert sys.prefix == " + repr(str(environment.prefix))])]
        checks = []
        for index, command in enumerate(commands):
            identity = digest([journal["candidate"], environment.fingerprint, command.model_dump()])
            key = f"delivery/runtime/{index}"
            # Outputs and runtime caches may change without a source commit. Probe every handoff.
            results = await self.verifier.run([command], self.project, environment)
            if not passed(results):
                raise RuntimeVerificationError([c.model_dump() for c in results])
            checks.extend(results)
            self.store.checkpoint(rid, key, {"identity": identity, "checks": [c.model_dump() for c in results]})
        evidence = {"checks": [c.model_dump() for c in checks]}
        if plan and plan.preview:
            await self._ensure_browser(rid, self.project)
            with observe_activity(self.store, rid, "交付界面运行检查"):
                evidence["visuals"] = await inspect_application(plan.preview, self.project,
                    self.store.root / "visuals" / rid / "runtime", environment, self.sandbox, self.store.root)
        if "visuals" in evidence:
            evidence["visuals"] = self._archive_visuals(evidence["visuals"])
        return evidence

    def _archive_visuals(self, rows):
        for row in rows:
            if row.get("screenshot"):
                path = Path(row["screenshot"])
                ref = self.artifacts.put_file(path, "screenshot")
                row["image_artifact"] = ref
                row["screenshot"] = str(self.artifacts.root / ref["hash"][:2] / ref["hash"])
        return rows

    def _runtime_instructions(self, work):
        path = work / "README.md"
        text = path.read_bytes().decode("utf-8") if path.exists() else "# Project\n"
        start, end = "<!-- aa2:runtime:begin -->", "<!-- aa2:runtime:end -->"
        if start in text and end in text:
            before, rest = text.split(start, 1)
            text = before + rest.split(end, 1)[1]
        block = (f"\n{start}\n## aa2 已准备的运行环境\n\n交付成功后在项目根目录执行：\n\n"
                 "```bash\nsource .aa2/runtime.sh\n```\n\n随后执行本文的启动命令。依赖位于本项目内；"
                 "新克隆目录没有 `.aa2` 时，请按本文独立安装说明创建项目内环境。\n"
                 f"{end}\n")
        atomic_write(path, text.rstrip() + "\n" + block, 0o644)

    async def _reintegrate_delivery(self, rid, journal):
        work = Path(journal["work"])
        incoming = journal["incoming"]
        if journal.get("runtime_failure"):
            failure = digest(journal["runtime_failure"])
            if self.store.get(rid, "delivery/runtime_repaired") != failure:
                await self._delivery_repair(rid, work, {"runtime_failure": journal["runtime_failure"]},
                                            [p for p in git(work, "ls-files", "-z").split("\0") if p])
                self.store.checkpoint(rid, "delivery/runtime_repaired", failure)
        # A cancelled merge remains isolated and can be continued using its conflict index.
        if not self.workspaces.is_ancestor(incoming, git(work, "rev-parse", "HEAD")):
            unmerged = git(work, "diff", "--name-only", "--diff-filter=U").splitlines()
            if not unmerged:
                try:
                    git(work, "-c", "user.name=aa2", "-c", "user.email=aa2@localhost", "merge", "--no-edit", "--no-ff", incoming)
                except AA2Error:
                    unmerged = git(work, "diff", "--name-only", "--diff-filter=U").splitlines()
                    if not unmerged:
                        raise
            if unmerged:
                await self._delivery_repair(rid, work, {"conflicts": unmerged, "incoming": incoming}, unmerged)
        plan = PlanResult.model_validate(journal["plan"]) if journal["plan"] else None
        checks = self._candidate_checks(plan) if plan else IntakeResult.model_validate(self.store.get(rid, "clarify")).verification
        if not checks:
            checks = self.config.load().verification
        while True:
            environment = await self._environment(work, rid, plan)
            self._export_environment(work, environment)
            candidate = self.workspaces.checkpoint(work, f"refs/aa2/{rid}/candidate", "aa2: reintegrated delivery")
            checkout_policy = self.workspaces.normalize_checkout(work, candidate)
            self.store.checkpoint(rid, "verify/checkout_policy", checkout_policy)
            before = tree_hash(work)
            evidence = await self.verifier.run(checks, work, environment)
            if not passed(evidence):
                await self._delivery_repair(rid, work, {"checks": [c.model_dump() for c in evidence]},
                                            [p for p in git(work, "ls-files", "-z").split("\0") if p])
                continue
            visuals = []
            if plan and plan.preview:
                prototype_hash = self._prototype_hash(work)
                if self.store.get(rid, "prototype_approval", {}).get("hash") != prototype_hash:
                    await self._ensure_browser(rid, work)
                    with observe_activity(self.store, rid, "集成后原型浏览器检查"):
                        prototype = await inspect_prototype(work / "design/prototypes", self.store.root / "visuals" / rid / "reintegration-prototype",
                            DocumentResult.model_validate({k: v for k, v in self.store.get(rid, "prototype", {}).items()
                                                           if k in DocumentResult.model_fields}).browser_actions)
                    prototype = self._archive_visuals(prototype)
                    review = await self._invoke("review", {"review_kind": "prototype", "project": self.contexts.build(work), "visuals": prototype},
                                                ReviewResult, work, rid, "reintegration-prototype", fresh=True)
                    if not review.approved:
                        raise AA2Error(review.summary)
                    actions_hash = digest(self.store.get(rid, "prototype", {}).get("browser_actions", []))
                    approval_identity = digest([prototype_hash, actions_hash])
                    feedback = await self._approve_prototype(rid, work, approval_identity)
                    if feedback is not None:
                        await self._delivery_repair(rid, work, {"prototype_feedback": feedback},
                            [p for p in git(work, "ls-files", "design/prototypes", "DESIGN.md").splitlines() if p])
                        continue
                    self.store.checkpoint(rid, "prototype_approval", {"hash": prototype_hash, "identity": approval_identity,
                                         "actions_hash": actions_hash, "browser_version": PROTOTYPE_BROWSER_VERSION})
                with observe_activity(self.store, rid, "集成后界面浏览器验收"):
                    visuals = await inspect_application(plan.preview, work, self.store.root / "visuals" / rid / "reintegrated",
                                                        environment, self.sandbox, self.store.root)
            review = await self._invoke("verify", {"project": self.contexts.build(work), "specification": self.store.get(rid, "clarify"),
                "evidence": [c.model_dump() for c in evidence], "implemented_visuals": visuals, "incoming": incoming},
                ReviewResult, work, rid, f"reintegration-review/{journal['reintegrations']}", fresh=True)
            if not review.approved:
                await self._delivery_repair(rid, work, {"review": review.model_dump()},
                                            [p for p in git(work, "ls-files", "-z").split("\0") if p])
                continue
            if (tree_hash(work) != before or git(work, "rev-parse", "HEAD") != candidate
                    or git(work, "diff", "--name-only", "HEAD")):
                raise AA2Error("Reintegrated candidate changed during verification")
            identity = digest([candidate, before, environment.fingerprint, [c.model_dump() for c in evidence], review.model_dump()])
            self.store.checkpoint(rid, "verify/identity", identity)
            self.store.checkpoint(rid, "verify/checks", [c.model_dump() for c in evidence])
            self.store.checkpoint(rid, "verify/review", review.model_dump())
            visuals = self._archive_visuals(visuals)
            self.store.checkpoint(rid, "verify/visuals", visuals)
            return {"candidate": candidate, "candidate_tree": git(work, "rev-parse", f"{candidate}^{{tree}}"),
                    "source_hash": before, "validation": identity,
                    "environment": {"prefix": str(environment.prefix), "fingerprint": environment.fingerprint}}

    async def _delivery_repair(self, rid, work, feedback, scopes):
        key = "rounds/delivery"
        identity = digest([feedback, scopes])
        pending_key = "candidate/delivery-repair"
        while True:
            pending = self.store.get(rid, pending_key, {})
            rejection = pending.get("rejection", {})
            if pending.get("identity") != identity or pending.get("phase") == "rejected":
                count = self.store.get(rid, key, {"round": 0})["round"]
                if count >= self.config.load().execution.max_rounds:
                    raise AA2Error("主分支集成修复预算耗尽，请人工核对规划")
                if pending:
                    self.artifacts.put(pending, "delivery-repair-history")
                pending = {"identity": identity, "round": count + 1, "rejection": rejection}
                self.store.checkpoints(rid, {key: {"round": count + 1}, pending_key: pending})
            if pending.get("commit"):
                return
            count = pending["round"]
            context = {"task": {"description": "Repair the isolated reintegration without weakening accepted behaviour", "allowed_paths": scopes},
                       "specification": self.store.get(rid, "clarify"), "project": self.contexts.build(work),
                       "feedback": {**feedback, "repair_feedback": rejection}}
            proposal = (ImplementationResult.model_validate(pending["proposal"]) if pending.get("proposal") else
                        await self._invoke("implement", context, ImplementationResult, work, rid, f"delivery-repair/{count}"))
            if proposal.replan_stage or not proposal.changes:
                raise AA2Error("集成修复需要人工核对规划：" + proposal.replan_reason)
            pending["proposal"] = proposal.model_dump()
            self.store.checkpoint(rid, pending_key, pending)
            contracts = "prototype_feedback" in feedback and all(p == "DESIGN.md" or p.startswith("design/prototypes/") for p in scopes)
            try:
                pending["patch"] = pending.get("patch") or prepare_changes(work, proposal.changes, scopes, protected=False, contracts=contracts)
            except PolicyViolation as exc:
                pending.update(phase="rejected", rejection={"candidate_error": str(exc)})
                self.store.checkpoint(rid, pending_key, pending)
                continue
            self.store.checkpoint(rid, pending_key, pending)
            review = (ReviewResult.model_validate(pending["review"]) if pending.get("review") else
                      await self._invoke("review", {**context, "review_kind": "test_changes", "candidate_changes": proposal.model_dump()},
                                         ReviewResult, work, rid, f"delivery-repair-review/{count}", fresh=True))
            if not review.approved:
                pending.update(phase="rejected", rejection={"review": review.model_dump()})
                self.store.checkpoint(rid, pending_key, pending)
                continue
            pending["review"] = review.model_dump()
            self.store.checkpoint(rid, pending_key, pending)
            apply_prepared(work, pending["patch"])
            pending["commit"] = self.workspaces.checkpoint(work, f"refs/aa2/{rid}/candidate", "aa2: reviewed integration repair")
            self.store.checkpoint(rid, pending_key, pending)
            return

    def _candidate_checks(self, plan):
        # Legacy task gates cannot supply v2 proof. Recheck their fixed obligations,
        # without rerunning implementation or resetting task budgets.
        legacy = []
        run = self.store.current(unfinished=True)
        if run:
            for task in plan.tasks:
                evidence = self.store.get(run["id"], f"develop/task/{task.id}", {}).get("checks", [])
                if not evidence or any(row.get("checker_version") != "verification-v2" for row in evidence):
                    legacy.extend(task.checks)
        return list({digest(c.model_dump()): c for c in [*legacy, *plan.final_checks, *plan.runtime_prepare, *plan.runtime_probes]}.values())
