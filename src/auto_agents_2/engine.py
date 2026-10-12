from __future__ import annotations

import asyncio
import json
import shlex
import shutil
import sys
from pathlib import Path
from typing import Awaitable, Callable

from .artifacts import ArtifactStore
from .config import ConfigStore
from .context import ContextBuilder
from .design import DesignCatalog, PreviewServer, browser_environment, inspect_application, inspect_prototype
from .environment import EnvironmentManager
from .errors import AA2Error, PolicyViolation, ProviderError
from .models import CheckResult, DocumentResult, ImplementationResult, IntakeResult, PlanResult, ReviewResult
from .planning import ready_batch, validate_plan
from .profiles import ProjectProfile
from .providers import Agent, ProviderRouter
from .requirements import capture, validate_coverage
from .research import ResearchService
from .resources import ResourceManager
from .sandbox import SandboxRunner
from .store import Store
from .util import atomic_write, digest, file_lock, scrub_text
from .verification import VerificationService, passed
from .workspace import WorkspaceManager, apply_changes, apply_prepared, git, prepare_changes, tree_hash

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
        self.contexts = ContextBuilder()
        self.resources = ResourceManager(self.store, cfg.cleanup)
        self.environments = EnvironmentManager(self.config, self.sandbox)
        self.verifier = VerificationService(self.store.root, self.sandbox, self.resources)
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
            self.store.update(rid, status="RUNNING")
            collector = asyncio.create_task(self._collect_periodically(rid, asyncio.current_task()))
            try:
                while True:
                    try:
                        return await self._execute(rid)
                    except Replan as exc:
                        count = self.store.run(rid)["data"].get("replans", 0)
                        while count >= self.config.load().execution.max_replans:
                            incident = self.store.get(rid, "budget_requests", 0) + 1
                            self.store.checkpoint(rid, "budget_requests", incident)
                            await self._ask(rid, f"replan-budget/{incident}", "重规划预算已耗尽。请调整配置后回复 continue，或取消迭代。", digest(count))
                        self.store.update(rid, replans=count + 1)
                        archived = self.archive_workers(rid, count)
                        index = STAGES.index(exc.stage)
                        self.store.invalidate(rid, STAGES[index:])
                        self.store.checkpoint(rid, "replan_feedback", {"reason": exc.reason, "stage": exc.stage, "recovery_refs": archived})
                    except Exception as exc:
                        incidents = self.store.get(rid, "incidents", 0) + 1
                        self.store.checkpoint(rid, "incidents", incidents)
                        await self._ask(rid, f"incident/{incidents}", f"执行遇到阻塞：{exc}\n修复配置或环境后回复 retry；也可以通过 aa2 cancel 取消本轮。", digest(str(exc)))
            except asyncio.CancelledError:
                if self.store.get(rid, "control/cancel"):
                    self.store.update(rid, status="CANCELLED")
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
                elapsed = 0

    async def _ask(self, rid, key, prompt, artifact_hash="") -> str:
        question = self.store.request(rid, key, prompt, artifact_hash)
        if question["answer"] is None and self.store.run(rid)["status"] != "WAITING_USER":
            self.store.update(rid, status="WAITING_USER")
        while question["answer"] is None:
            if self.question_handler:
                async with self.question_lock:
                    async def external_answer():
                        while True:
                            current = self.store.request(rid, key, prompt, artifact_hash)
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
                            pass  # Another attached client answered first.
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
            else:
                await asyncio.sleep(self.config.load().execution.wait_poll_seconds)
            question = self.store.request(rid, key, prompt, artifact_hash)
            if self.store.run(rid)["status"] == "CANCELLED":
                raise AA2Error("Iteration cancelled")
        self.store.update(rid, status="RUNNING")
        return question["answer"]

    async def _invoke(self, stage, context, schema, workspace, rid, key, fresh=False):
        maximum = self.config.load().execution.max_tokens
        if maximum and self.store.usage_summary(rid)["total_tokens"] >= maximum:
            await self._ask(rid, "token-budget", "Token 预算已用尽。调整预算后回复 continue。", str(maximum))
            updated = self.config.load().execution.max_tokens
            if updated and self.store.usage_summary(rid)["total_tokens"] >= updated:
                raise AA2Error("Token budget remains exhausted")
        self.store.update(rid, stage=stage)
        try:
            result = await self.agent.invoke(stage, context, schema, workspace, rid, key, fresh)
        except ProviderError as exc:
            await self._ask(rid, f"provider/{key}", f"可用 provider 暂时无法完成任务：{exc}\n修复配置或认证后回复 retry。", digest(str(exc)))
            if isinstance(self.agent, ProviderRouter):
                self.agent.grant_retry(rid, key)
            result = await self.agent.invoke(stage, context, schema, workspace, rid, key, fresh)
        artifact = self.artifacts.put(result.model_dump(mode="json"), stage)
        self.store.event(rid, "artifact.created", {"stage": stage, "operation": key, "artifact": artifact})
        return result

    async def _execute(self, rid):
        run = self.store.run(rid)
        raw = self.store.get(rid, "capture")
        if not raw:
            raw = await capture(run["data"]["source"], self.artifacts)
            self.store.checkpoint(rid, "capture", raw)
        if not self.store.get(rid, "baseline"):
            self.workspaces.initialize()
            base = self.workspaces.baseline(rid)
            project_hash = tree_hash(self.project)
            for previous in self.store.successful():
                delivery = previous["data"].get("delivery_commit")
                if not delivery:
                    continue
                if project_hash in {previous["data"].get("project_source_hash"), previous["data"].get("verified_source_hash")}:
                    base = delivery
                elif base != delivery:
                    choice = await self._ask(rid, "baseline-choice", "当前工作区与上轮交付版本不同。回复 current 使用当前工作区，或 delivery 沿用上轮交付版本。", digest([base, delivery]))
                    if choice.strip() == "delivery":
                        base = delivery
                    elif choice.strip() != "current":
                        raise AA2Error("Baseline selection must be current or delivery")
                break
            self.store.checkpoint(rid, "baseline", base)
            self.store.update(rid, project_source_hash=project_hash)
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
                return self._finish_no_changes(rid, source_hash, previous["id"])
        context = {**self.store.get(rid, "clarify_context", {}), "raw_requirements": original[:24000],
                   "context_files": [str(self.artifacts.root / raw_hash[:2] / raw_hash)],
                   "raw_requirements_truncated": len(original) > 24000, "history": history[:3],
                   "project": self.contexts.build(work), "feedback": self.store.get(rid, "replan_feedback", {})}
        assessment = self.store.get(rid, "clarify")
        if not assessment:
            for turn in range(20):
                result = await self._invoke("clarify", context, IntakeResult, work, rid, "clarify")
                if result.questions:
                    response = await self._ask(rid, f"clarify/{turn}", "\n".join(result.questions), digest(result.model_dump()))
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
        if problems:
            raise Replan("clarify", "; ".join(problems))
        if any(c.status == "unknown" for c in result.coverage):
            if result.verification:
                environment = await self._environment(work, rid)
                evidence = await self.verifier.run(result.verification, work, environment)
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
            self.store.checkpoint(rid, "coverage_checks", [r.model_dump() for r in evidence])
            if passed(evidence):
                return self._finish_no_changes(rid, source_hash, "current-baseline")
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
                await self._ensure_browser(rid, work)
                if (work / "DESIGN.md").is_file():
                    context["design_baseline"] = (work / "DESIGN.md").read_text()
                else:
                    self.resources.register(self.store.root / "cache/design", "cache", rid)
                    context["design_candidates"] = await DesignCatalog(self.store.root / "cache/design", self.sandbox).candidates(original)
            schema = PlanResult if stage == "plan" else DocumentResult
            if stage == "plan":
                context["task_origins"] = self.store.get(rid, "task_origins", {})
            document = await self._invoke(stage, context, schema, work, rid, stage)
            if isinstance(document, DocumentResult) and document.questions:
                answer = await self._ask(rid, stage, "\n".join(document.questions), digest(document.model_dump()))
                context[stage + "_answer"] = answer
                document = await self._invoke(stage, context, schema, work, rid, stage)
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
            if stage == "prototype":
                if "design_candidates" in context:
                    candidates = {item["url"] for item in context["design_candidates"]}
                    selected = [source for source in document.sources if source.url in candidates]
                    if not selected:
                        raise Replan("prototype", "Cite the chosen pinned DESIGN.md candidate in sources before producing a new design")
                    retrieved = await ResearchService(self.artifacts).verify(selected)
                    context["selected_design_sources"] = retrieved
                prior_pages = git(work, "ls-tree", "-r", "--name-only", self.store.get(rid, "baseline"), "--", "design/prototypes")
                scopes = result.prototype_paths if prior_pages else ["design/prototypes", "DESIGN.md"]
                if prior_pages and document.files and not scopes:
                    raise Replan("clarify", "Existing prototypes require explicit prototype_paths for this iteration; preserve unrelated approved pages and the design baseline")
                if any(not (p == "DESIGN.md" or p.startswith("design/prototypes/")) for p in result.prototype_paths):
                    raise Replan("clarify", "prototype_paths must refer only to approved prototype pages/components or DESIGN.md")
                apply_changes(work, document.files, scopes, protected=False, contracts=True)
                visuals = await inspect_prototype(work / "design/prototypes", self.store.root / "visuals" / rid / "prototype", document.browser_actions)
                review = await self._invoke("review", {"specification": assessment, "prototype": document.model_dump(),
                                                     "review_kind": "prototype",
                                                     "design_evidence": retrieved,
                                                     "browser_evidence": visuals, "project": self.contexts.build(work)},
                                            ReviewResult, work, rid, "prototype-review", fresh=True)
                if not review.approved:
                    raise Replan("prototype", review.summary)
                fingerprint = self._prototype_hash(work)
                prior_approval = any(self.store.get(p["id"], "prototype_approval", {}).get("hash") == fingerprint
                                     for p in self.store.successful())
                if not prior_approval:
                    with PreviewServer(work / "design/prototypes") as preview:
                        answer = await self._ask(rid, "prototype-approval", f"原型自动审查通过。预览：{preview.url}\n输入 approve 批准，或填写修改意见。", fingerprint)
                    if answer.strip().lower() not in {"approve", "通过", "批准"}:
                        raise Replan("prototype", answer)
                self.store.checkpoint(rid, "prototype_approval", {"hash": fingerprint, "review": review.model_dump(), "visuals": visuals})
            if stage == "plan":
                unmet = {c.requirement_id for c in result.coverage if c.status != "satisfied"} or {r.id for r in result.requirements}
                validate_plan(document, unmet)
                prior = self.store.get(rid, "task_origins", {})
                for task in document.tasks:
                    if task.id in prior:
                        task.origin_id = prior[task.id]
                    elif prior and task.origin_id not in set(prior.values()):
                        raise Replan("plan", f"Replacement task {task.id} must retain an existing origin_id so budgets cannot reset")
                    else:
                        task.origin_id = task.origin_id or task.id
                    prior[task.id] = task.origin_id
                self.store.checkpoint(rid, "task_origins", prior)
                if result.frontend and document.preview is None:
                    raise Replan("plan", "Frontend plan requires a runnable preview and route mapping")
                review = await self._invoke("review", {**context, "plan": document.model_dump(), "review_kind": "plan"}, ReviewResult,
                                            work, rid, "plan-review", fresh=True)
                if not review.approved:
                    raise Replan("plan", review.summary)
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
            docs = await self._invoke("readme", {**context, "project": self.contexts.build(work)}, DocumentResult,
                                      work, rid, "readme")
            if any(c.path.startswith(("docs/requirements/", "docs/aa2/")) for c in docs.files):
                raise Replan("plan", "Documentation changes cannot overwrite accepted requirements, architecture or controller evidence")
            apply_changes(work, docs.files, ["README.md", "docs", ".env.example"], protected=False)
            self.store.checkpoint(rid, "readme", docs.model_dump())
        self._agent_instructions(work, plan)
        environment = await self._environment(work, rid, plan)
        self._export_environment(work, environment)
        candidate = self.workspaces.checkpoint(work, f"refs/aa2/{rid}/candidate", "aa2: delivery candidate")
        candidate_hash = tree_hash(work)
        verify_key = digest([candidate_hash, environment.fingerprint, plan.model_dump(), assessment,
                             self.store.get(rid, "prototype_approval", {}), context.get("provider_research", {})])
        previous_checks = self.store.get(rid, "verify/checks", []) if self.store.get(rid, "verify/identity") == verify_key else []
        evidence = [CheckResult.model_validate(c) for c in previous_checks]
        if not passed(evidence):
            evidence = await self.verifier.run(plan.final_checks, work, environment)
        self.store.checkpoint(rid, "verify/identity", verify_key)
        self.store.checkpoint(rid, "verify/checks", [r.model_dump() for r in evidence])
        if not passed(evidence):
            raise Replan("plan", "Final verification failed: " + json.dumps([r.model_dump() for r in evidence]))
        visuals = []
        if ProjectProfile.detect(work, self.config.load().project_profile).has_frontend and not result.frontend:
            raise Replan("clarify", "Implementation introduced a frontend without a prototype and browser verification contract")
        if result.frontend:
            if self.store.get(rid, "prototype_approval", {}).get("hash") != self._prototype_hash(work):
                raise Replan("prototype", "Prototype changed after human approval")
            await self._ensure_browser(rid, work)
            visuals = await inspect_application(plan.preview, work, self.store.root / "visuals" / rid / "implemented",
                                                environment, self.sandbox, self.store.root)
            self.store.checkpoint(rid, "verify/visuals", visuals)
        cached_review = self.store.get(rid, "verify/review") if self.store.get(rid, "verify/review_identity") == verify_key else None
        review = ReviewResult.model_validate(cached_review) if cached_review else await self._invoke("verify", {**context, "project": self.contexts.build(work),
                                               "evidence": [r.model_dump() for r in evidence],
                                               "implemented_visuals": visuals,
                                               "prototype_approval": self.store.get(rid, "prototype_approval", {})},
                                    ReviewResult, work, rid, "final-review", fresh=True)
        if not review.approved:
            raise Replan("plan", review.summary)
        self.store.checkpoint(rid, "verify/review", review.model_dump())
        self.store.checkpoint(rid, "verify/review_identity", verify_key)
        if git(work, "rev-parse", "HEAD") != candidate or tree_hash(work) != candidate_hash:
            raise AA2Error("Candidate changed during verification")
        branch = self.workspaces.deliver(rid, candidate)
        self.store.update(rid, status="SUCCEEDED", stage="deliver", outcome="DELIVERED", branch=branch,
                          delivery_commit=candidate, verified_source_hash=tree_hash(work),
                          external_dependencies=bool(context.get("provider_research", {}).get("sources")))
        return self.store.run(rid)

    async def _environment(self, work, rid, plan=None):
        profile = ProjectProfile.detect(work, self.config.load().project_profile)
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
                saved = {"phase": "implementing", "feedback": saved}
                self.store.checkpoint(rid, key, saved)
                self.store.checkpoint(rid, counter_key, counters)
            previous_feedback = saved.get("feedback", saved)
            feedback = {k: previous_feedback[k] for k in ("phase", "checks", "review", "environment_error") if k in previous_feedback}
            feedback = json.loads(json.dumps(feedback))
            for check in feedback.get("checks", []):
                check["output"] = check.get("output", "")[-4000:]
            task_context = {"task": task.model_dump(), "specification": context["specification"],
                            "design": context.get("design"), "feedback": feedback,
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
                except PolicyViolation:
                    review = await self._invoke("review", {**task_context, "candidate_changes": change.model_dump(),
                                                           "review_kind": "test_changes",
                                                           "purpose": "Authorize proposed protected test/config changes against requirements"},
                                                ReviewResult, work, rid, key + "/test-change-review", fresh=True)
                    if not review.approved:
                        saved = {"review": review.model_dump(), "phase": "rejected"}
                        self.store.checkpoint(rid, key, saved)
                        continue
                    prepared = prepare_changes(work, change.changes, task.allowed_paths, protected=False)
                saved["patch"] = prepared
                self.store.checkpoint(rid, key, saved)
                apply_prepared(work, prepared)
                saved.update(phase="applied", source_hash=tree_hash(work))
                self.store.checkpoint(rid, key, saved)
            environment = await self._environment(work, rid, plan)
            if (saved.get("phase") == "tested" and saved.get("source_hash") == tree_hash(work)
                    and all(c["environment_hash"] == environment.fingerprint for c in saved.get("checks", []))):
                checks = [CheckResult.model_validate(c) for c in saved["checks"]]
            else:
                checks = await self.verifier.run(task.checks, work, environment)
                saved.update(phase="tested", checks=[c.model_dump() for c in checks], source_hash=tree_hash(work))
                self.store.checkpoint(rid, key, saved)
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
            self.store.checkpoint(rid, counter_key, counters)
            self.store.checkpoint(rid, key, saved)
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
        with self.resources.temporary("browser-install") as scratch:
            env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(cache), "TMPDIR": str(scratch)}
            process = await self.sandbox.spawn([sys.executable, "-m", "playwright", "install", "chromium"],
                                               workspace, env, [cache, scratch])
            try:
                async with asyncio.timeout(900):
                    _, stderr = await process.communicate()
                if process.returncode:
                    raise AA2Error("Project-local browser installation failed: " + stderr.decode(errors="replace")[-2000:])
            finally:
                await self.sandbox.stop(process)

    def _agent_instructions(self, work, plan):
        path = work / "AGENTS.md"
        previous = path.read_text() if path.exists() else "# Project instructions\n"
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
        archived = []
        root = self.store.root / "worktrees" / rid
        for workspace in root.iterdir() if root.exists() else []:
            if workspace.name == "integration" or not workspace.is_dir() or workspace.is_symlink():
                continue
            ref = f"refs/aa2/{rid}/replans/{generation}/{workspace.name}"
            self.workspaces.checkpoint(workspace, ref, "aa2: recovery snapshot before replanning")
            self.resources.retire_worktree(workspace, ref)
            archived.append(ref)
        return archived

    def _finish_no_changes(self, rid, source_hash, evidence_run):
        inherited = self.store.run(evidence_run)["data"] if evidence_run != "current-baseline" else {"external_dependencies": True}
        if evidence_run == "current-baseline":
            for previous in self.store.successful():
                if previous["data"].get("verified_source_hash") == source_hash:
                    inherited = previous["data"]
                    break
        self.store.update(rid, status="SUCCEEDED", stage="coverage", outcome="NO_CHANGES",
                          verified_source_hash=source_hash, coverage_evidence=evidence_run,
                          delivery_commit=inherited.get("delivery_commit", self.store.get(rid, "baseline")),
                          external_dependencies=inherited.get("external_dependencies", False))
        return self.store.run(rid)
