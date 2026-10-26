from __future__ import annotations

import asyncio
import json
import shlex
import shutil
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import quote

from .action_authority import GATES
from .approvals import APPROVAL_CONTEXT_VERSION, PrototypeApprovals, prototype_identity
from .artifacts import ArtifactStore
from .builds import (
    BUILD_CONTRACT_VERSION,
    command_hashes,
    runtime_commands,
    verification_commands,
)
from .config import ConfigStore
from .context import ContextBuilder
from .convergence import Convergence
from .decisions import DecisionService
from .delivery import DeliveryCoordinator, RuntimeVerificationError
from .design import (
    PROTOTYPE_BROWSER_VERSION,
    DesignCatalog,
    PreviewServer,
    browser_environment,
    inspect_application,
    inspect_prototype,
)
from .design_documents import DesignDocuments
from .environment import EnvironmentManager
from .errors import (
    AA2Error,
    DependencyResolutionError,
    EnvironmentCommandError,
    IncompleteIteration,
    InvalidStructuredResponse,
    PatchRepairExhausted,
    PolicyViolation,
    ProtectedChange,
    PrototypeBrowserError,
    ProviderError,
    RecoveryPathExhausted,
    RetryableInfrastructureError,
    ToolchainUnavailable,
    VerificationPreparationError,
)
from .evidence import CHECKER_VERSION, invocation_identity, reusable
from .evidence_admission import (
    admission_context,
    admission_record,
    capability_catalog,
    current_catalog,
    validate_admission_review,
    validate_obligations,
)
from .faults import (
    ApprovalBoundary,
    HumanIntervention,
    HumanRequirement,
    InternalWorkflowError,
    ResourceBoundary,
    StateConflict,
    known_exception,
)
from .interaction import choice_spec, is_prototype_approval, question_text
from .issues import IssueLedger
from .models import (
    CheckResult,
    DocumentResult,
    EvidencePlanReview,
    ImplementationResult,
    IntakeResult,
    PlanResult,
    ReviewResult,
)
from .patch_repair import PatchRepairService
from .plan_revision import materialize_plan
from .planning import ready_batch, validate_plan
from .preview_evidence import PreviewEvidence
from .profiles import ProjectProfile
from .progress import observe_activity, safe_event
from .proposal_recovery import ProposalRecovery
from .prototype_contract import prototype_context
from .prototype_review import (
    InvalidPrototypeReview,
    PrototypeReview,
    rejection,
    review_basis,
    review_valid,
    validate_review,
)
from .providers import Agent, ProviderRouter
from .recovery import (
    ExecutionContract,
    FailureRecord,
    RecoveryNeeded,
    check_failure,
    request_from,
)
from .recovery_controller import RecoveryController
from .recovery_identity import invocation_contract
from .recovery_policy import RecoveryPolicy
from .replanning import browser_criteria, check_criteria
from .requirements import (
    capture,
    save_interactive_requirements,
    validate_coverage,
    validate_requirements_text,
)
from .research import ResearchService
from .resources import ResourceManager
from .review_authority import InvalidAuthorityReview
from .review_evidence import ReviewEvidence, consumer_key, review_consumer
from .sandbox import SandboxRunner
from .source_history import SourceHistory
from .stage_responsibilities import prepare_reentry, validate_stage_writes
from .store import Store
from .task_baselines import refresh_task_baseline
from .test_reports import test_runner
from .toolchain import ToolchainPreflight
from .util import atomic_write, digest, file_lock, identifier, scrub_text
from .verification import (
    VerificationRefreshNeeded,
    VerificationService,
    passed,
    verification_operation,
)
from .workflow_authority import WorkflowAuthority
from .workspace import (
    WorkspaceManager,
    apply_prepared,
    git,
    legacy_tree_hash,
    path_allowed,
    prepare_changes,
    prepared_before_hash,
    source_manifest,
    source_manifest_hash,
    tree_hash,
)

STAGES = list(GATES)


class Replan(AA2Error):
    def __init__(self, stage: str, reason: str, *, supersede_keys=()):
        self.stage, self.reason = stage, reason
        self.supersede_keys = list(supersede_keys)
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
        self.approvals = PrototypeApprovals(self.store, self.artifacts)
        self.workspaces = WorkspaceManager(self.project)
        self.contexts = ContextBuilder(self.config.load().sensitive_paths)
        self.patch_repair = PatchRepairService(self)
        self.resources = ResourceManager(self.store, cfg.cleanup)
        self.environments = EnvironmentManager(self.config, self.sandbox)
        self.toolchain = ToolchainPreflight(self.config, self.sandbox)
        self.verifier = VerificationService(self.store.root, self.sandbox, self.resources)
        self.delivery = DeliveryCoordinator(self.store, self.workspaces, self.resources, self.environments,
                                            self._reintegrate_delivery, self._runtime_checks,
                                            browser_version=lambda: self.browser_check_version())
        self.question_handler = question_handler
        self.question_lock = asyncio.Lock()
        self.recovery = RecoveryController(self)
        self.convergence = Convergence(self.store, self.config)
        self.decisions = DecisionService(self)
        self.recovery_policy = RecoveryPolicy(self)
        self.authority = WorkflowAuthority(self)
        browser_environment(self.store.root / "cache/browsers")

    async def start(self, source: str | None = None, provider: str | None = None, run_id: str | None = None, *,
                    allow_resume: bool = True, requirements_text: str | None = None):
        if requirements_text is not None and (source is not None or run_id is not None):
            raise AA2Error("需求文本不能与需求文件或恢复迭代 ID 同时提供")
        new_iteration = source is not None or requirements_text is not None
        # Admission check precedes source reads and provider persistence.
        if new_iteration and self.store.current(unfinished=True):
            raise IncompleteIteration(f"上一轮迭代尚未完成。请先执行：aa2 run --project {self.project}")
        if requirements_text is not None:
            validate_requirements_text(requirements_text)
        with file_lock(self.store.root / "controller.lock", blocking=False):
            # Another controller may have admitted a run since the first check.
            if new_iteration and self.store.current(unfinished=True):
                raise IncompleteIteration(f"上一轮迭代尚未完成。请先执行：aa2 run --project {self.project}")
            if not allow_resume and not new_iteration:
                attached = self.store.run(run_id) if run_id else self.store.current()
                if attached and (attached["status"] == "PAUSED" or self.store.get(attached["id"], "control/cancel")):
                    return attached
            self.store.migrate()
            self.sandbox.reap_orphans()
            self.resources.recover_disposable()
            self.resources.finish_pending()
            if provider:
                self.config.resolve("clarify", provider)
            if requirements_text is not None:
                source = str(save_interactive_requirements(self.store.root, requirements_text))
            if provider:
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
            self.store.event(rid, "automation.policy", {"enabled": self.config.load().automation.enabled,
                "count_limits_enforced": False,
                "message": "生成及验收阶段的累计实现、诊断和停滞次数仅供决策参考；旧轮数上限不再触发停止。"})
            if self.store.get(rid, "control/cancel"):
                self.store.checkpoint(rid, "control/cancel", False)
            self.store.update(rid, status="RUNNING", pause_reason=None, incident_id=None)
            from .review_authority import VERSION as authority_version
            self.store.checkpoint(rid, "authority/protocol", authority_version)
            if self.store.get(rid, "replanning/evidence/contracts") is None:
                self._replan_evidence(rid, "contracts", {"coverage": False, "prototype_scope": False, "plan": False})
            owner = asyncio.current_task()
            collector = asyncio.create_task(self._collect_periodically(rid, owner))
            def supervise_monitor(task):
                if not task.cancelled() and task.exception() is not None:
                    owner.cancel()
            collector.add_done_callback(supervise_monitor)
            preflight_complete = False
            try:
                while True:
                    try:
                        if not preflight_complete:
                            report = await self.toolchain.check()
                            self.store.checkpoint(rid, "environment/preflight", report)
                            self.store.event(rid, "environment.preflight_passed", report)
                            preflight_complete = True
                        await self._resume_environment_waits(rid)
                        if self.store.get(rid, "recovery/active"):
                            await self.recovery.resume(rid)
                            if self.store.get(rid, "recovery/active") or self._environment_waits(rid):
                                continue
                        if self.store.get(rid, "replanning/pending"):
                            if self.store.get(rid, "replanning/pending").get("controller_owned"):
                                await self._resume_replan(rid)
                            elif (not await self._refresh_legacy_approval_context(rid)
                                    and not await self._refresh_legacy_build_checks(rid)
                                    and not await self._refresh_legacy_task_checks(rid)):
                                if self.store.get(rid, "plan"):
                                    self._legacy_recovery(rid)
                                    continue
                                await self._resume_replan(rid)
                        return await self._execute(rid)
                    except RecoveryNeeded as exc:
                        self.recovery.submit(rid, exc.failure)
                    except Replan as exc:
                        self._controller_replan(rid, exc)
                    except RecoveryPathExhausted as exc:
                        self.store.checkpoint(rid, "recovery/path-exhausted",
                            {"reason": str(exc), "excluded_actions": exc.evidence})
                        self.store.event(rid, "recovery.path_exhausted", {"reason": str(exc)})
                        self.store.update(rid, status="PAUSED", pause_reason=str(exc), last_error=exc.code)
                        return self.store.run(rid)
                    except HumanIntervention as exc:
                        await self._wait_prerequisite(rid, exc)
                    except Exception as exc:
                        if isinstance(exc, ToolchainUnavailable):
                            self.store.checkpoint(rid, "environment/preflight-failure", exc.report or exc.record())
                        boundary = known_exception(exc, self.store.run(rid)["stage"])
                        if boundary:
                            await self._wait_prerequisite(rid, boundary)
                        else:
                            raise self._internal_error(rid, exc) from None
            except asyncio.CancelledError:
                if self.store.get(rid, "control/cancel"):
                    journal = self.store.get(rid, "delivery/journal", {})
                    published = journal.get("publication_started", False)
                    if not published:
                        path = self.store.root / "worktrees" / rid
                        hold = self.resources.hold(path, "cancellation archive", rid)
                        self.store.checkpoint(rid, "cancellation/archive_hold", hold)
                    self.store.update(rid, status="PAUSED" if published else "CANCELLED")
                    return self.store.run(rid)
                if collector.done() and not collector.cancelled() and collector.exception() is not None:
                    raise self._internal_error(rid, collector.exception()) from None
                self.store.update(rid, status="PAUSED")
                raise
            except BaseException as exc:
                if isinstance(exc, Exception) and not isinstance(exc, (InternalWorkflowError, HumanIntervention)):
                    exc = self._internal_error(rid, exc)
                try:
                    self.store.update(rid, status="PAUSED", last_error=str(exc)[:2000] if isinstance(exc, InternalWorkflowError)
                                      else type(exc).__name__)
                except Exception:
                    pass  # Preserve the original failure when the state store is unavailable.
                raise exc from None
            finally:
                collector.cancel()
                await asyncio.gather(collector, return_exceptions=True)
                errors = []
                try:
                    if self.store.run(rid)["status"] in {"SUCCEEDED", "CANCELLED"}:
                        if self.store.run(rid)["status"] == "CANCELLED":
                            hold = self.store.get(rid, "cancellation/archive_hold")
                            if not hold:
                                hold = self.resources.hold(self.store.root / "worktrees" / rid, "cancellation archive", rid)
                                self.store.checkpoint(rid, "cancellation/archive_hold", hold)
                            self.archive_workspaces(rid)
                            self.resources.release_holds([hold])
                            self.store.checkpoint(rid, "cancellation/archive_hold", None)
                        self.resources.release_run(rid)
                except Exception as exc:
                    errors.append(type(exc).__name__)
                for cleanup in (self.resources.collect, self.resources.collect_artifacts_safely):
                    try:
                        cleanup()
                    except Exception as exc:
                        errors.append(type(exc).__name__)
                if errors:
                    from .progress import safe_event
                    safe_event(self.store, rid, "cleanup.pending", {"errors": errors})

    @staticmethod
    def _environment_boundary(pending):
        try:
            requirement = HumanRequirement.model_validate(pending["requirement"])
            boundary = HumanIntervention(requirement, reason_code=pending["reason_code"], phase=pending["phase"])
            valid = (pending["version"] == 1 and pending["identity"] ==
                     digest([boundary.reason_code, boundary.phase, requirement.model_dump()]) and
                     requirement.evidence.get("authority") == "external_command_observation")
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise StateConflict("Pending environment observation changed; preserve its original question")
        return boundary

    def _environment_waits(self, rid):
        pending = self.store.get(rid, "environment/prerequisite-queue", {})
        if not isinstance(pending, dict):
            raise StateConflict("Invalid pending environment observations; preserve their original questions")
        for identity, item in pending.items():
            self._environment_boundary(item)
            if identity != item["identity"]:
                raise StateConflict("Pending environment observation identity changed")
        return pending

    async def _resume_environment_waits(self, rid):
        # Keep the legacy current pointer/question unchanged, including after a
        # crash between answer persistence and consuming that answer.
        current = self.store.get(rid, "environment/prerequisite-wait")
        if current:
            await self._wait_prerequisite(rid, self._environment_boundary(current))
        while pending := self._environment_waits(rid):
            active = self.store.get(rid, "recovery/active") or {}
            if active.get("failure", {}).get("evidence", {}).get("resolution_diagnosis"):
                # Validate the entire queue even while its admitted read-only
                # recovery runs. Other prerequisites retain their old semantics.
                return
            await self._wait_prerequisite(rid, self._environment_boundary(next(iter(pending.values()))))

    async def _wait_prerequisite(self, rid, error):
        requirement = error.requirement.model_dump()
        identity = digest([error.reason_code, error.phase, requirement])
        # A recorded external failure must await its original answer before a
        # restart runs the installer again and manufactures a second observation.
        # Legacy unrelated prerequisite behavior is deliberately unchanged.
        observed_environment = error.requirement.evidence.get("authority") == "external_command_observation"
        wait_key = "environment/prerequisite-wait"
        if observed_environment:
            pending = {"version": 1, "identity": identity, "reason_code": error.reason_code,
                       "phase": error.phase, "requirement": requirement}
            from .resolution_diagnosis import admit
            if admit(self, rid, pending):
                return
            previous = self.store.get(rid, wait_key)
            if previous and previous != pending:
                raise StateConflict("Another environment observation is awaiting its original answer")
            self.store.checkpoint(rid, wait_key, pending)
        self.store.record_fault(rid, {"route": "human", "reason_code": error.reason_code,
                                     "phase": error.phase, "human_requirement": requirement})
        key = "prerequisite/" + identity
        attempt = self.store.get(rid, key, 0)
        interaction = choice_spec(["已处理，重新核对", "暂停，稍后续跑"], ["retry", "pause"], yes_no=True)
        interaction["pause_on"] = "n"
        await self._ask(rid, f"{key}/{attempt}", error.requirement.prompt(), identity, interaction)
        updates = {key: attempt + 1}
        if observed_environment:
            queued = self._environment_waits(rid)
            if identity in queued and queued[identity] != pending:
                raise StateConflict("Queued environment observation no longer matches its original answer")
            queued.pop(identity, None)
            updates.update({wait_key: None, "environment/prerequisite-queue": queued})
        self.store.checkpoints(rid, updates)

    def _internal_error(self, rid, error):
        import traceback
        incident = identifier("incident")
        # Exception text and locals may contain native configuration or credentials.
        frames = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                  for frame in traceback.extract_tb(error.__traceback__)]
        record = {"route": "internal_error", "reason_code": "unclassified_exception", "incident_id": incident,
                  "exception_type": type(error).__name__, "frames": frames}
        try:
            record["phase"] = self.store.run(rid)["stage"]
            self.store.record_fault(rid, record, pause=True)
        except Exception:
            return InternalWorkflowError(incident, type(error).__name__, saved=False)
        return InternalWorkflowError(incident, type(error).__name__)

    def _replan_evidence(self, rid, scope, criteria, **identity):
        self.store.checkpoint(rid, f"replanning/evidence/{scope}", {"criteria": criteria, **identity})

    def _controller_replan(self, rid, exc):
        plan = self.store.get(rid, "plan")
        if exc.stage == "plan" and plan:
            key = "recovery/controller-contract/" + digest([plan, exc.reason])
            self.store.checkpoint(rid, key, {"stage": exc.stage, "reason": exc.reason})
            correction = self.store.get(rid, "authority/correction", {}) or {}
            self.recovery.submit(rid, FailureRecord.create(kind="contract", component="controller", origin_id="plan",
                step="contract", obligations=sorted({r for t in plan["tasks"] for r in t["requirement_ids"]}),
                evidence_refs=[key, "plan"], evidence={"reason": exc.reason,
                    **({"authority_correction": correction} if correction.get("reason") == exc.reason else {})}))
        else:
            pending = self.store.prepare_replan(rid, exc.stage, exc.reason)
            self.store.checkpoint(rid, "replanning/pending", {**pending, "controller_owned": True,
                "supersede_keys": exc.supersede_keys})

    def _replan_contract(self, rid, name, valid):
        saved = self.store.get(rid, "replanning/evidence/contracts", {})
        criteria = {"coverage": False, "prototype_scope": False, "plan": False, **saved.get("criteria", {})}
        criteria[name] = valid
        self._replan_evidence(rid, "contracts", criteria)

    def _coverage_replan(self, rid, context, assessment, question_key, fingerprint, reason):
        """Missing internal evidence is a controller correction, not a user prerequisite."""
        questions = [q for q in self.store.decision_questions(rid, [question_key])
                     if q["artifact_hash"] == fingerprint]
        answered = [q for q in questions if q["answer"] is not None]
        if answered:
            context = {**context, "additional_evidence": self.store.question(answered[-1]["id"])["answer"]}
        self.store.checkpoints(rid, {
            "clarify_context": {**context, "controller_feedback": reason},
            "recovery/coverage-contract/" + digest(assessment): assessment,
        })
        raise Replan("clarify", reason, supersede_keys=[question_key] if questions and not answered else [])

    def _replan_checks(self, rid, scope, commands, results, work, environment):
        source = tree_hash(work)
        updates = IssueLedger(self.store, self.artifacts).check_updates(
            rid, scope.removeprefix("task/"), commands, results, work, environment)
        updates[f"replanning/evidence/{scope}"] = {
            "criteria": check_criteria(commands, results, source, environment.fingerprint),
            "progress_criteria": check_criteria(commands, results, source, environment.fingerprint, process_exit=True),
            "source_hash": source, "environment_hash": environment.fingerprint,
            "comparable": all(row["valid"] for row in updates["recovery/observations/" + scope.removeprefix("task/")])}
        if scope.startswith("task/"):
            origin = scope.removeprefix("task/")
            for task in self.store.get(rid, "plan", {}).get("tasks", []):
                if (task.get("origin_id") or task["id"]) == origin:
                    key = "task_state/" + task["id"]
                    state = self.store.get(rid, key, {"version": 1, "revision": 1, "implementation": "pending"})
                    observed = updates["recovery/observations/" + origin]
                    verdict = ("valid" if observed and all(r["passed"] for r in observed) else
                               "failed" if any(r["valid"] and not r["passed"] for r in observed) else "stale")
                    updates[key] = {**state, "evidence": verdict}
        elif scope == "final-checks" and updates[f"replanning/evidence/{scope}"]["comparable"]:
            plan_data = self.store.get(rid, "plan")
            if plan_data:
                plan = PlanResult.model_validate(plan_data)
                done = set(self.store.get(rid, "develop/done", []))
                for task in plan.tasks:
                    if task.id not in done:
                        continue
                    required = verification_commands(task.checks, work, plan.runtime_prepare)
                    identities = {(tuple(c.argv), c.cwd) for c in required}
                    selected = [c for c in commands if (tuple(c.argv), c.cwd) in identities]
                    rows = [r for r in results if (tuple(r.argv), r.cwd) in identities]
                    if identities != {(tuple(r.argv), r.cwd) for r in rows}:
                        continue
                    updates.update(IssueLedger(self.store, self.artifacts).check_updates(
                        rid, task.origin_id or task.id, selected, rows, work, environment, previous=updates["issues"]))
                    key = "task_state/" + task.id
                    state = self.store.get(rid, key, {"version": 1, "revision": 1, "implementation": "accepted"})
                    observed = updates["recovery/observations/" + (task.origin_id or task.id)]
                    updates[key] = {**state, "evidence": "valid" if all(r["passed"] for r in observed) else "failed"}
        self.store.checkpoints(rid, updates)
        owner = "final" if scope == "final-checks" else scope.removeprefix("task/")
        summaries = updates["recovery/observations/" + scope.removeprefix("task/")]
        proof = {"checkpoint": f"replanning/evidence/{scope}", "source_hash": source,
                 "environment_hash": environment.fingerprint}
        if commands and summaries and all(row["valid"] for row in summaries):
            self.convergence.milestone(rid, owner, "checks_valid", proof)
        producers = [c for c in commands if c.outputs]
        if producers and all(any(row["cwd"] == c.cwd and row["argv"] == c.argv and row["valid"]
                and row["passed"] for row in summaries) for c in producers):
            self.convergence.milestone(rid, owner, "build_ready", proof)
        self.store.event(rid, "recovery.observed", {"scope": scope,
            "checks": updates["recovery/observations/" + scope.removeprefix("task/")]})

    async def _grant_replan(self, rid, pending):
        # A controller-owned correction or an authorized local patch has already
        # been adjudicated. Counts do not introduce an extra approval boundary.
        key = f"replan-stalled/{pending['evaluation']}"
        answered = [q for q in self.store.decision_questions(rid, [key]) if q["answer"] is not None]
        if answered and not pending.get("granted"):
            question = self.store.question(answered[-1]["id"])
            await self._ask(rid, key, question["prompt"], question["artifact_hash"], question["interaction"])
            pending = {**pending, "granted": True, "authority_sources": self.authority.identity(rid)}
            self.store.checkpoint(rid, "replanning/pending", pending)
        if pending.get("granted"):
            permit = pending.get("correction_permit") or pending.get("permit")
            if (not permit and self.store.get(rid, "authority/protocol")
                    and pending.get("authority_sources") != self.authority.identity(rid)):
                raise StateConflict("Stage return authorization needs current source reassessment")
            self.recovery_policy.validate_consumption(rid, permit,
                expected={"plan_patch" if pending["stage"] == "plan" else pending["stage"]})
            correction = self.store.get(rid, "authority/correction", {}) or {}
            if correction and not correction.get("completed"):
                self.authority.correction_evidence(rid, pending, correction)
            return pending
        active = self.store.get(rid, "recovery/active", {}) or {}
        if active.get("permit") and active.get("decision", {}).get("action") == "plan_patch":
            return pending
        from .recovery import RecoveryDecision
        stage = "plan_patch" if pending["stage"] == "plan" else pending["stage"]
        correction = self.store.get(rid, "authority/correction", {}) or {}
        authority_evidence = self.authority.correction_evidence(rid, pending, correction)
        failure = FailureRecord.create(kind="contract", component="controller", origin_id="gates",
            step="upstream", obligations=["accepted_requirements"], evidence_refs=["replanning/pending"],
            evidence={"pending": pending, "checks": pending.get("verification_recheck", {}).get("checks", []),
                      **authority_evidence})
        action = RecoveryDecision(failure_id=failure.id, action=stage, budget_owner="gates",
                                  reason=pending["reason"], evidence_refs=failure.evidence_refs)
        work = self.store.root / "worktrees" / rid / "integration"
        if not work.is_dir():
            work = self.project
        proposed = {"attempt": pending["evaluation"], "failure": failure.model_dump()}
        _, permit = await self.recovery_policy.authorize(rid, failure, proposed, work, None, action,
            operation="controller-replan", established=pending["decision"].get("improved", False),
            candidates={stage: action}, controller_owned=True, supersede_keys=pending.get("supersede_keys", []))
        self.recovery_policy.start(rid, permit)
        pending = {**pending, "granted": True, "permit": permit}
        self.store.checkpoint(rid, "replanning/pending", pending)
        return pending

    async def _resume_replan(self, rid):
        pending = await self._grant_replan(rid, self.store.get(rid, "replanning/pending"))
        work = self.store.root / "worktrees" / rid / "integration"
        if not work.is_dir():
            work = self.project
        pending = prepare_reentry(self.store, rid, pending, work)
        # Include already archived refs when recovering an interruption mid-archive.
        archived = self.archive_workers(rid, pending["generation"])
        self.store.supersede_questions(rid, f"replan-stalled/{pending['evaluation']}")
        self.store.complete_replan(rid, STAGES[STAGES.index(pending["stage"]):], archived)
        self.recovery_policy.complete(rid, pending.get("permit"), {"status": "gate_reset", "stage": pending["stage"]})

    def _legacy_recovery(self, rid):
        pending = self.store.get(rid, "replanning/pending")
        plan = PlanResult.model_validate(self.store.get(rid, "plan"))
        for task in plan.tasks:
            key = "develop/task/" + task.id
            proposal = self.store.get(rid, "invocation/" + key, {}).get("result", {})
            if proposal.get("replan_stage") == pending["stage"] and proposal.get("replan_reason") == pending["reason"]:
                saved = self.store.get(rid, key, {})
                work = self.store.root / "worktrees" / rid / f"{task.id}-p{pending['generation']}"
                checks = pending.get("verification_recheck", {}).get("checks", saved.get("feedback", {}).get("checks", []))
                source = tree_hash(work) if work.is_dir() else None
                identity = {"source_hash": source} if checks and all(c.get("source_hash") == source for c in checks) else {}
                self.store.checkpoint(rid, key, {**saved, **identity, "workspace": saved.get("workspace", str(work))})
                failure = FailureRecord.create(kind="proposal", component="legacy_adapter", task_id=task.id,
                    origin_id=task.origin_id or task.id, step="task", obligations=task.requirement_ids,
                    evidence_refs=["replanning/pending", key, "invocation/" + key],
                    evidence={"pending": pending, "task": saved,
                              "checks": pending.get("verification_recheck", {}).get("checks", saved.get("feedback", {}).get("checks", []))},
                    proposal=proposal)
                self.recovery.submit(rid, failure)
                return
        self.recovery.submit(rid, FailureRecord.create(kind="contract", component="legacy_adapter", origin_id="plan",
            step="contract", obligations=[r for t in plan.tasks for r in t.requirement_ids],
            evidence_refs=["replanning/pending"], evidence={"pending": pending}))

    async def _refresh_legacy_task_checks(self, rid):
        """Recheck a pending task whose old checker failed to collect test evidence.

        Only an unchanged candidate and a replan bound to that task's exact saved
        response qualify. Fresh checks replace feedback, never independent review.
        """
        pending = self.store.get(rid, "replanning/pending", {})
        plan_data = self.store.get(rid, "plan")
        if pending.get("stage") != "plan" or not plan_data:
            return False
        plan = PlanResult.model_validate(plan_data)
        for task in plan.tasks:
            key = f"develop/task/{task.id}"
            saved = self.store.get(rid, key, {})
            invocation = self.store.get(rid, "invocation/" + key, {})
            proposal = invocation.get("result", {})
            if (saved.get("phase") != "implementing" or saved.get("implementation")
                    or proposal.get("replan_stage") != "plan" or proposal.get("changes")
                    or proposal.get("replan_reason") != pending["reason"]):
                continue
            previous = [CheckResult.model_validate(row) for row in saved.get("feedback", {}).get("checks", [])]
            work = self.store.root / "worktrees" / rid / f"{task.id}-p{pending['generation']}"
            if not work.is_dir() or work.is_symlink() or not previous:
                continue
            source = tree_hash(work)
            if any(row.source_hash != source for row in previous):
                continue
            affected = any(row.checker_version in {"verification-v2", "verification-v3"} and row.tests == 0
                           and (not row.report or row.report_status in {"missing", "invalid"})
                           and test_runner(row.argv, work if row.cwd == "." else work / row.cwd)
                           and any(row.command_hash in command_hashes(command) for command in task.checks)
                           for row in previous)
            if not affected:
                continue
            recovery_key = f"candidate/verification-recovery/{task.id}/p{pending['generation']}"
            record = {"pending": pending, "task": saved, "invocation": invocation,
                      "workspace": str(work), "checker_version": CHECKER_VERSION}
            self.store.checkpoint(rid, recovery_key, record)
            with observe_activity(self.store, rid, f"重新采集检查证据（{task.id}）") as activity:
                environment = await self._environment(work, rid, plan)
                checks = await self.verifier.run(task.checks, work, environment)
                activity.finish("verified" if passed(checks) else "unpassed")
            record["checks"] = [row.model_dump() for row in checks]
            self.store.checkpoint(rid, recovery_key, record)
            self._replan_checks(rid, f"task/{task.origin_id or task.id}", task.checks, checks, work, environment)
            if not reusable(task.checks, checks, work, environment, self.artifacts).allowed or tree_hash(work) != source:
                self.store.checkpoint(rid, "replanning/pending", {**pending, "verification_recheck": {
                    "checks": record["checks"], "recovery_key": recovery_key, "checker_version": CHECKER_VERSION}})
                return False
            updated = {**saved, "feedback": {**saved.get("feedback", {}), "checks": record["checks"],
                       "controller_recheck": {"previous_request": proposal, "checker_version": CHECKER_VERSION}}}
            self.store.recover_verification_replan(rid, pending, key, saved, updated, recovery_key, record)
            self._replan_checks(rid, f"task/{task.origin_id or task.id}", task.checks, checks, work, environment)
            return True
        return False

    async def _refresh_legacy_build_checks(self, rid):
        """Recheck a retained candidate with missing build prerequisites, once.

        Bind recovery to the exact old task proposal and unchanged source. All
        original checks still run; a build alone cannot cancel a pending rollback.
        """
        pending = self.store.get(rid, "replanning/pending", {})
        plan_data = self.store.get(rid, "plan")
        if pending.get("stage") != "plan" or not plan_data:
            return False
        plan = PlanResult.model_validate(plan_data)
        for task in plan.tasks:
            key = f"develop/task/{task.id}"
            saved = self.store.get(rid, key, {})
            invocation = self.store.get(rid, "invocation/" + key, {})
            proposal = invocation.get("result", {})
            if (saved.get("phase") != "implementing" or saved.get("implementation") or saved.get("patch")
                    or "verification_contract_version" in saved
                    or proposal.get("replan_stage") != "plan"
                    or proposal.get("replan_reason") != pending["reason"]):
                continue
            work = self.store.root / "worktrees" / rid / f"{task.id}-p{pending['generation']}"
            previous = [CheckResult.model_validate(c) for c in saved.get("feedback", {}).get("checks", [])]
            if not work.is_dir() or work.is_symlink() or not previous:
                continue
            source = tree_hash(work)
            if any(c.source_hash != source for c in previous):
                continue
            commands = verification_commands(task.checks, work, plan.runtime_prepare)
            inserted = [c for c in commands if c.outputs and not any(
                c.argv == original.argv and c.cwd == original.cwd for original in task.checks)]
            affected = any(c.checker_version in {"verification-v2", "verification-v3", "verification-v4"}
                           and c.exit_code and any(c.command_hash in command_hashes(original) for original in task.checks)
                           for c in previous)
            if not inserted or not affected:
                continue
            recovery_key = f"candidate/build-recovery/{task.id}/p{pending['generation']}"
            record = {"pending": pending, "task": saved, "invocation": invocation,
                      "workspace": str(work), "source_hash": source,
                      "verification_contract": self._verification_contract(commands)}
            self.store.checkpoint(rid, recovery_key, record)
            with observe_activity(self.store, rid, f"补齐构建前置并复测（{task.id}）") as activity:
                environment = await self._environment(work, rid, plan)
                checks = await self.verifier.run(commands, work, environment)
                activity.finish("verified" if passed(checks) else "unpassed")
            record["checks"] = [c.model_dump() for c in checks]
            self.store.checkpoint(rid, recovery_key, record)
            self._replan_checks(rid, f"task/{task.origin_id or task.id}", commands, checks, work, environment)
            if tree_hash(work) != source or not reusable(commands, checks, work, environment, self.artifacts).allowed:
                self.store.checkpoint(rid, "replanning/pending", {**pending, "verification_recheck": {
                    "checks": record["checks"], "recovery_key": recovery_key, "checker_version": CHECKER_VERSION,
                    "verification_contract": record["verification_contract"]}})
                return False
            updated = {**saved, "verification_contract_version": BUILD_CONTRACT_VERSION,
                       "feedback": {**saved.get("feedback", {}), "checks": record["checks"],
                           "controller_recheck": {"previous_request": proposal, "checker_version": CHECKER_VERSION,
                                                  "verification_contract": record["verification_contract"]}}}
            self.store.recover_task_replan(rid, pending, key, saved, updated, recovery_key, record, "build.recovered")
            self._replan_checks(rid, f"task/{task.origin_id or task.id}", commands, checks, work, environment)
            return True
        return False

    @staticmethod
    def _verification_contract(commands):
        return ExecutionContract(commands=commands).model_dump()

    def _preparation_failure(self, rid, commands, work, exc, task=None):
        key = "recovery/preparation/" + digest([task.origin_id or task.id if task else "final", str(work), [c.model_dump() for c in commands], str(exc)])
        evidence = {"commands": [c.model_dump() for c in commands], "error": str(exc), "source_hash": tree_hash(work)}
        self.store.checkpoint(rid, key, evidence)
        return RecoveryNeeded(FailureRecord.create(kind="contract", component="verification_preparation",
            origin_id=task.origin_id or task.id if task else "final", task_id=task.id if task else "", step="checks",
            obligations=task.requirement_ids if task else [digest([c.cwd, c.argv]) for c in commands],
            evidence_refs=[key], evidence=evidence))

    def _task_commands(self, rid, task, work, plan):
        try:
            return verification_commands(task.checks, work, plan.runtime_prepare)
        except VerificationPreparationError as exc:
            raise self._preparation_failure(rid, task.checks, work, exc, task) from exc

    def _verification_operation(self, rid, commands, work, environment, task=None, *, phase="final", operation=None):
        if phase == "runtime" and operation:
            return digest([rid, phase, operation])
        saved = self.store.get(rid, "develop/task/" + task.id, {}) if task else {}
        permit = saved.get("recovery_permit") if task else self.store.get(rid, "recovery/final-action")
        return digest([rid, phase, str(work), tree_hash(work), environment.fingerprint if environment else "host",
                       [c.model_dump() for c in commands], CHECKER_VERSION, permit, saved.get("observation_id"), operation])

    async def _checks(self, rid, commands, work, environment, task=None, *, phase="final", operation=None):
        identity = self._verification_operation(rid, commands, work, environment, task, phase=phase, operation=operation)
        token = verification_operation.set(identity)
        consumer_token = review_consumer.set(consumer_key(task, phase))
        try:
            return await self.verifier.run(commands, work, environment)
        except VerificationRefreshNeeded as exc:
            evidence = {"commands": [c.model_dump() for c in commands], "checks": exc.checks,
                "execution_interrupted": exc.reason_code == "execution_interrupted", "verification_assessment": {
                    "allowed": False, "issues": [{"code": exc.reason_code, "category": "evidence"}]}}
            if phase in {"runtime", "reintegration"}:
                raise RuntimeVerificationError(evidence) from exc
            raise RecoveryNeeded(FailureRecord.create(kind="evidence", component="verification",
                origin_id=(task.origin_id or task.id) if task else "final", task_id=task.id if task else "",
                step=exc.reason_code, phase="task" if task else phase,
                obligations=task.requirement_ids if task else [digest(c.model_dump()) for c in commands],
                evidence_refs=["verification/batches/" + identity],
                evidence=evidence)) from exc
        except VerificationPreparationError as exc:
            if phase in {"runtime", "reintegration"}:
                raise RuntimeVerificationError({"commands": [c.model_dump() for c in commands], "checks": [],
                    "verification_error": "verification contract unavailable", "verification_assessment": {
                        "allowed": False, "issues": [{"code": "contract_unavailable", "category": "contract"}]}}) from exc
            raise self._preparation_failure(rid, commands, work, exc, task) from exc
        finally:
            verification_operation.reset(token)
            review_consumer.reset(consumer_token)

    async def _refresh_legacy_approval_context(self, rid):
        """Let an old, unapplied task reassess its exact replan with proven consent.

        An actual human decision for the unchanged prototype is mandatory. Only
        requests made before approval receipts were supplied qualify, once.
        """
        pending = self.store.get(rid, "replanning/pending", {})
        plan_data, prototype = self.store.get(rid, "plan"), self.store.get(rid, "prototype")
        if pending.get("stage") != "prototype" or not plan_data or not prototype:
            return False
        actions = prototype.get("browser_actions", [])
        integration = self.store.root / "worktrees" / rid / "integration"
        if (not integration.is_dir() or integration.is_symlink()
                or not self.approvals.human(rid, integration, actions)):
            return False
        for task in PlanResult.model_validate(plan_data).tasks:
            key = f"develop/task/{task.id}"
            saved = self.store.get(rid, key, {})
            invocation = self.store.get(rid, "invocation/" + key, {})
            proposal = invocation.get("result", {})
            if (saved.get("phase") != "implementing" or saved.get("implementation") or saved.get("patch")
                    or "approval_context_version" in saved
                    or proposal.get("replan_stage") != "prototype" or proposal.get("changes")
                    or proposal.get("replan_reason") != pending["reason"]):
                continue
            work = self.store.root / "worktrees" / rid / f"{task.id}-p{pending['generation']}"
            if (not work.is_dir() or work.is_symlink()
                    or prototype_identity(work, actions) != prototype_identity(integration, actions)):
                continue
            source = tree_hash(work)
            recovery_key = f"candidate/approval-recovery/{task.id}/p{pending['generation']}"
            record = {"pending": pending, "task": saved, "invocation": invocation,
                      "workspace": str(work), "source_hash": source, "context_version": APPROVAL_CONTEXT_VERSION}
            self.store.checkpoint(rid, recovery_key, record)
            try:
                with observe_activity(self.store, rid, f"核验历史原型审批（{task.id}）"):
                    receipt = await self._ensure_prototype_approval(rid, integration, prototype)
            except Replan as exc:
                # A pending rollback cannot be replaced by raising another Replan.
                # Preserve it and expose the real failed gate without a busy loop.
                raise ApprovalBoundary(f"历史原型审批复核未通过：{exc.reason}") from exc
            if (tree_hash(work) != source or not self.approvals.current(rid, work, actions, self.browser_check_version())):
                raise StateConflict("审批恢复期间候选发生变化，已保留回退现场")
            record["approval"] = receipt
            updated = {**saved, "approval_context_version": APPROVAL_CONTEXT_VERSION,
                       "feedback": {**saved.get("feedback", {}), "controller_approval_recheck": {
                           "previous_request": proposal, "prototype_approval": receipt}}}
            self.store.recover_task_replan(rid, pending, key, saved, updated, recovery_key, record, "approval.recovered")
            return True
        return False

    async def _collect_periodically(self, rid, owner):
        elapsed = 0
        while True:
            await asyncio.sleep(1)
            if self.store.get(rid, "control/cancel"):
                owner.cancel()
                return
            elapsed += 1
            try:
                if elapsed >= self.config.load().cleanup.interval_seconds:
                    self.resources.collect()
                    self.resources.collect_artifacts_safely()
                    elapsed = 0
            except Exception as exc:
                # Maintenance cannot remove the only cancellation observer.
                from .progress import safe_event
                safe_event(self.store, rid, "maintenance.failed", {"error_type": type(exc).__name__})
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
                    question = self.store.question(question["id"])
                    if not question or question["superseded"]:
                        raise asyncio.CancelledError()
                    if question["answer"] is not None:
                        break
                    async def external_answer():
                        while True:
                            current = self.store.question(question["id"])
                            if not current or current["superseded"]:
                                raise asyncio.CancelledError()
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
                            current = self.store.question(question["id"])
                            if current["answer"] is None:
                                raise
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
            else:
                await asyncio.sleep(self.config.load().execution.wait_poll_seconds)
            question = self.store.question(question["id"])
            if not question or question["superseded"]:
                raise asyncio.CancelledError()
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

    def _prototype_review_context(self, rid, work, context):
        controlled = prototype_context(self, rid)
        spec = self.store.get(rid, "clarify", {})
        actions = context.get("prototype", {}).get("browser_actions", [])
        basis = review_basis(controlled["prototype_requirement_source"], spec,
                             prototype_identity(work, actions), context.get("browser_evidence", []))
        return {**context, **controlled, "specification": spec, "prototype_review_basis": basis}

    def _validate_stage_response(self, result, stage, context, rid):
        if stage != "review" or context.get("review_kind") != "prototype":
            if self.authority.consumes(type(result)):
                try:
                    self.authority.validate(rid, result, context)
                except InvalidAuthorityReview as exc:
                    ref = self.artifacts.put({"response": exc.response, "validation": exc.details,
                        "basis": context.get("controller_review_basis")}, "invalid-authority-review")
                    exc.details = {**exc.details, "response_ref": ref}
                    self.store.event(rid, "review.authority_invalid", {"code": exc.details["code"], "artifact": ref})
                    raise
            return result
        try:
            return validate_review(result, context.get("prototype_review_basis"),
                                   context.get("prototype_requirement_source", {}))
        except InvalidPrototypeReview as exc:
            ref = self.artifacts.put({"response": exc.response, "validation": exc.details,
                "basis": context.get("prototype_review_basis")}, "invalid-prototype-review")
            exc.details = {**exc.details, "response_ref": ref}
            self.store.event(rid, "prototype.review_invalid", {"code": exc.details["code"], "artifact": ref})
            raise

    def _prototype_rejection(self, review, *, product_replan=True):
        review = PrototypeReview.model_validate(review.model_dump() if hasattr(review, "model_dump") else review)
        owner, reason = rejection(review)
        if owner == "controller":
            ref = self.artifacts.put(review.model_dump(), "prototype-capability-gap")
            raise HumanIntervention(HumanRequirement(kind="external_input", reason=reason,
                action="核对所引用的真实要求及当前阶段；通过正式职责修订或补足能力，不能执行候选采集脚本或忽略要求。",
                resume_condition="重新核验要求来源、实际能力和原型证据；确认本身不替代验收。",
                evidence_refs=[ref["hash"]], evidence={"review": ref}),
                reason_code="prototype_evidence_capability_unavailable", phase="prototype")
        if product_replan or owner != "prototype":
            raise Replan(owner, reason)

    async def _invoke(self, stage, context, schema, workspace, rid, key, fresh=False, *, interactive_retry=True):
        if stage == "prototype":
            context = {**context, **prototype_context(self, rid)}
        elif stage == "review" and context.get("review_kind") == "prototype":
            context = self._prototype_review_context(rid, workspace, context)
            schema = PrototypeReview
        if stage in {"design", "implement", "verify", "recovery_diagnosis"} or (stage == "review" and
                context.get("review_kind", "implementation") in {
                    "design", "plan", "plan_patch", "implementation", "test_changes", "recovery_diagnosis"}):
            catalog = capability_catalog(shared_browsers=self.config.load().shared_cache.enabled)
            if stage == "recovery_diagnosis":
                frozen = self.store.get(rid, "recovery/invocation-inputs/" + digest([stage, key]))
                if frozen:
                    # Additive protocol guidance must not mutate an already frozen
                    # diagnosis (including interrupted calls predating this catalog).
                    catalog = frozen["context"].get("controller_evidence_capabilities")
            if catalog is not None:
                context = {"controller_evidence_capabilities": catalog, **context}
        if stage in {"clarify", "provider_research", "design", "plan", "implement"}:
            context = {**context, "controller_authority_scope": self.authority.author_scope(rid, stage)}
        if stage in {"design", "plan", "implement", "verify", "recovery_diagnosis", "decision", "review"}:
            from .collection import capture_contract
            frozen = (self.store.get(rid, "recovery/invocation-inputs/" + digest([stage, key]))
                      if stage in {"recovery_diagnosis", "decision"} else None)
            # An additive contract cannot rewrite or recount an old logical call.
            patch_prefix = {"plan": "recovery/patch/", "review": "recovery/patch-review/"}.get(stage)
            patch_frozen = (self.store.get(rid, "recovery/patch-context/" + key[len(patch_prefix):])
                            if patch_prefix and key.startswith(patch_prefix) else None)
            if patch_frozen is not None:
                # Only these recorded logical calls share a frozen policy.
                # A new authority-reaudit must still receive the current budget.
                # Absence in a legacy patch-context remains meaningful.
                budget = patch_frozen.get("controller_attachment_capture")
            else:
                budget = (frozen["context"].get("controller_attachment_capture") if frozen
                          else capture_contract(self.store.root))
            if budget is not None:
                context = {**context, "controller_attachment_capture": budget}
        context, schema = self.authority.prepare(rid, stage, context, schema, workspace, key)
        if not interactive_retry or stage in {"decision", "recovery_diagnosis", "material_probe"} or context.get("patch_repair"):
            result = await self._invoke_once(stage, context, schema, workspace, rid, key, fresh,
                                           interactive_retry=interactive_retry)
        else:
            result = await ProposalRecovery(self).invoke(stage, context, schema, workspace, rid, key, fresh)
        if self.authority.consumes(type(result)) and context.get("controller_review_basis"):
            self.authority.route_dispute(rid, result)
        return result

    async def _invoke_once(self, stage, context, schema, workspace, rid, key, fresh=False, *, interactive_retry=True,
                           logical_operation=None, logical_context=None):
        invocation_key = "invocation/" + key
        source_before = tree_hash(workspace)
        saved = self.store.get(rid, invocation_key, {})
        logical = None
        if stage in {"decision", "recovery_diagnosis"}:
            logical_key = "recovery/invocation-inputs/" + digest([stage, key])
            logical = self.store.get(rid, logical_key)
            if logical:
                if (logical["source_hash"] != source_before
                        or invocation_contract(logical["context"]) != invocation_contract(context)):
                    raise StateConflict("Recovery invocation source changed; preserve its frozen request and response")
                # Logical operation keys are immutable; prompt/provenance versions
                # cannot regenerate an already completed operation on restart.
                context = logical["context"]
                if (saved.get("result") and (saved.get("logical_input") == logical_key
                        or digest(saved) != logical["previous_invocation"])):
                    if saved.get("cleanup_record"):
                        self.sandbox.confirm_record(saved["cleanup_record"])
                    return schema.model_validate(saved["result"])
            else:
                logical = {"source_hash": source_before, "context": context, "previous_invocation": digest(saved)}
                self.store.checkpoint(rid, logical_key, logical)
        execution = self.agent.execution_identity(stage) if isinstance(self.agent, ProviderRouter) else None
        invocation_config = self.config.load()
        identity = invocation_identity(stage, context, schema, workspace, invocation_config, source_before, execution)
        compatible = {identity}
        if stage == "recovery_diagnosis":
            old_execution = self.agent.execution_identity("review") if isinstance(self.agent, ProviderRouter) else None
            compatible.add(invocation_identity("review", context, schema, workspace, self.config.load(), source_before, old_execution))
            if current_catalog(context.get("controller_evidence_capabilities")):
                # Completed pre-catalog receipts still require the exact original
                # source, schema, config and all factual inputs. Only this known
                # controller protocol may be absent; no evidence is discarded.
                legacy = {k: v for k, v in context.items() if k != "controller_evidence_capabilities"}
                compatible.add(invocation_identity("review", legacy, schema, workspace,
                    invocation_config, source_before, old_execution))
                compatible.add(invocation_identity(stage, legacy, schema, workspace,
                    invocation_config, source_before, execution))
        if saved.get("identity") in compatible and saved.get("result"):
            if saved.get("cleanup_record"):
                self.sandbox.confirm_record(saved["cleanup_record"])
            result = self._validate_stage_response(schema.model_validate(saved["result"]), stage, context, rid)
            self.authority.record(rid, result, context, logical_operation or key)
            return result
        self.store.update(rid, stage=stage)
        try:
            result = await self.agent.invoke(stage, context, schema, workspace, rid, key, fresh)
        except ProviderError as exc:
            if not interactive_retry or isinstance(exc, InvalidStructuredResponse):
                raise
            await self._ask(rid, f"provider/{key}", f"可用 provider 暂时无法完成任务：{exc}\n处理上述问题后回复 retry。", digest(str(exc)))
            if isinstance(self.agent, ProviderRouter):
                self.agent.grant_retry(rid, key)
            result = await self.agent.invoke(stage, context, schema, workspace, rid, key, fresh)
        if tree_hash(workspace) != source_before:
            self.store.checkpoint(rid, "invocation-drift/" + key, {"source_hash": source_before,
                "proposal": self.artifacts.put(result.model_dump(mode="json"), "unaccepted_proposal")})
            raise PolicyViolation("Workspace changed while generating or reviewing a candidate; preserving user changes")
        result = self._validate_stage_response(result, stage, context, rid)
        artifact = self.artifacts.put(result.model_dump(mode="json"), stage)
        self.authority.record(rid, result, context, logical_operation or key)
        if isinstance(result, ReviewResult):
            IssueLedger(self.store, self.artifacts).record(rid, logical_operation or key, result, context)
        record = {"identity": identity, "result": result.model_dump(mode="json"), "artifact": artifact,
                  **({"logical_input": logical_key} if logical else {})}
        updates = {invocation_key: record}
        if logical_operation and logical_operation != key:
            updates["invocation/" + logical_operation] = {**record, "recovered_from": key,
                "identity": invocation_identity(stage, logical_context, schema, workspace,
                                                invocation_config, source_before, execution)}
            self.store.checkpoints(rid, updates)
        else:
            self.store.checkpoint(rid, invocation_key, record)
        self.store.event(rid, "artifact.created", {"stage": stage, "operation": logical_operation or key,
            **({"attempt_operation": key} if logical_operation and logical_operation != key else {}), "artifact": artifact})
        return result

    async def _questions(self, rid, key, questions, fingerprint):
        answers = []
        work = self.store.root / "worktrees" / rid / "integration"
        automatic = await self.decisions.clarify(rid, key, questions, fingerprint, work) if work.is_dir() else {}
        for index, question in enumerate(questions):
            prompt = question if isinstance(question, str) else question.prompt
            options = [] if isinstance(question, str) else question.options
            answer = automatic.get(index)
            if answer is None:
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
                raise StateConflict("原型在审核期间发生变化，已保留现场；请核对修改后再续跑")
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
        browser_identity = self.browser_check_version()
        results, error = [], ""
        try:
            with observe_activity(self.store, rid, "原型浏览器检查"):
                results = await inspect_prototype(work / "design/prototypes",
                    self.store.root / "visuals" / rid / "prototype" / identifier("check"), document.browser_actions)
        except PrototypeBrowserError as exc:
            results, error = exc.results, str(exc)
        if self._prototype_hash(work) != fingerprint or self.browser_check_version() != browser_identity:
            raise StateConflict("原型或浏览器环境在检查期间被修改，需要重新检查")
        results = self._archive_visuals(results)
        report = {"version": self.browser_check_version(), "source_hash": fingerprint,
                  "actions_hash": digest([a.model_dump() for a in document.browser_actions]),
                  "results": results, "error": error}
        artifact = self.artifacts.put(report, "prototype-browser")
        self.store.event(rid, "artifact.created", {"stage": "prototype", "operation": "prototype-browser", "artifact": artifact})
        # Keep the report across prototype replans, so the next proposal gets actual failures.
        self.store.checkpoint(rid, "prototype_browser_report", artifact)
        self._replan_evidence(rid, "prototype-browser", browser_criteria(document.browser_actions, results),
                              source_hash=fingerprint)
        return report

    async def _record_prototype_approval(self, rid, work, document, review, visuals):
        actions = [action.model_dump() for action in document.browser_actions]
        identity = prototype_identity(work, actions)
        evidence = {**identity, "review": review, "visuals": visuals, "browser_version": self.browser_check_version()}
        if not self.approvals.automation_current(evidence, identity, actions, self.browser_check_version(), expected_routes=self.approvals.routes(work, actions),
                review_inputs=self.approvals.review_inputs(rid, work, actions, visuals)):
            raise Replan("prototype", "原型审批缺少完整浏览器证据或独立审查，需要重新核验")
        human = self.approvals.human(rid, work, actions)
        if not human:
            feedback = await self._approve_prototype(rid, work, identity["identity"])
            if feedback is not None:
                raise Replan("prototype", feedback)
            human = self.approvals.human(rid, work, actions)
        record = self.approvals.publish(rid, work, actions, human, review, visuals, self.browser_check_version())
        return self.approvals.context(record)

    async def _ensure_prototype_approval(self, rid, work, prototype=None):
        """Restore the real gate for cached stages as well as freshly generated ones."""
        data = prototype if prototype is not None else self.store.get(rid, "prototype")
        if not data:
            raise Replan("prototype", "Frontend development requires a verified prototype and explicit human approval")
        document = DocumentResult.model_validate({k: v for k, v in data.items() if k in DocumentResult.model_fields})
        actions = [action.model_dump() for action in document.browser_actions]
        if self.environments.shared.enabled:
            await self._ensure_browser(rid, work)
        binding = prototype_identity(work, actions)
        current = self.approvals.current(rid, work, actions, self.browser_check_version())
        if current:
            # Also attach provenance to modern receipts created before this version.
            return await self._record_prototype_approval(rid, work, document, current["review"], current["visuals"])
        previous = self.store.get(rid, "prototype_approval", {})
        verification_key = "candidate/prototype-approval-checks/" + digest([
            binding["identity"], self.store.get(rid, "clarify", {}), self.browser_check_version()])
        checked = self.store.get(rid, verification_key, {})
        if self.approvals.automation_current(checked, binding, actions, self.browser_check_version(), expected_routes=self.approvals.routes(work, actions),
                review_inputs=self.approvals.review_inputs(rid, work, actions, checked.get("visuals", []))):
            previous = checked
        if self.approvals.automation_current(previous, binding, actions, self.browser_check_version(), expected_routes=self.approvals.routes(work, actions),
                review_inputs=self.approvals.review_inputs(rid, work, actions, previous.get("visuals", []))):
            review, visuals = previous["review"], previous["visuals"]
        else:
            report = await self._prototype_browser_report(rid, work, document)
            if report["error"]:
                raise Replan("prototype", report["error"])
            visuals = report["results"]
            review = (await self._invoke("review", {"review_kind": "prototype", "prototype": data,
                "specification": self.store.get(rid, "clarify", {}), "browser_evidence": visuals,
                "controller_browser_report": report,
                "project": self.contexts.build(work)}, ReviewResult, work, rid,
                "prototype-approval-review/" + binding["identity"], fresh=True)).model_dump()
            if not review["approved"]:
                self._prototype_rejection(review)
            self.store.checkpoint(rid, verification_key, {**binding, "review": review, "visuals": visuals,
                                                         "browser_version": self.browser_check_version()})
        if prototype_identity(work, actions) != binding:
            raise StateConflict("原型在审批复核期间发生变化，已保留现场")
        return await self._record_prototype_approval(rid, work, document, review, visuals)

    def _task_approval_context(self, rid, work, context):
        if not (context.get("specification", {}).get("frontend") or self.store.get(rid, "clarify", {}).get("frontend")):
            return {}
        prototype = self.store.get(rid, "prototype")
        record = (self.approvals.current(rid, work, prototype.get("browser_actions", []), self.browser_check_version())
                  if prototype else None)
        if not record:
            raise ApprovalBoundary("当前任务缺少与原型内容及交互动作匹配的人工审批证据，已暂停派发")
        return {"prototype_approval": self.approvals.context(record)}

    def _task_design_context(self, rid, work):
        receipt = DesignDocuments(self).context(rid, work)
        pending = self.store.get(rid, "candidate/design", {})
        if (self.store.get(rid, "design") and "review_documents" in pending
                and receipt["status"] != "approved"):
            raise StateConflict("Architecture documents or their review receipt changed; preserve the task candidate")
        return {"design_review": receipt}

    async def _prepare_prototype(self, rid, work, specification, assessment, context, original):
        key = "prototype/pending"
        identity = digest([assessment, self.store.get(rid, "baseline"), self.store.get(rid, "replan_feedback")])
        pending = self.store.get(rid, key, {})
        report_ref = self.store.get(rid, "prototype_browser_report")
        if report_ref:
            context["controller_browser_report"] = self.artifacts.json(report_ref)
        if pending and pending["identity"] != identity:
            def legacy_defaults(value, fields):
                if isinstance(value, dict):
                    return {k: legacy_defaults(v, fields) for k, v in value.items()
                            if not (k in fields and v == [] and "argv" in value)}
                if isinstance(value, list):
                    return [legacy_defaults(v, fields) for v in value]
                return value
            legacy_identities = {digest([legacy_defaults(assessment, fields), self.store.get(rid, "baseline"),
                                         self.store.get(rid, "replan_feedback")])
                                 for fields in ({"outputs"}, {"inputs", "outputs"})}
            if pending["identity"] in legacy_identities:
                pending.update(legacy_identity=pending["identity"], identity=identity)
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
            try:
                files, prepared = await self.patch_repair.prepare(rid, work, document.files, scopes,
                    stage="prototype", context=context, operation=f"prototype/{identity}", contracts=True)
            except (PolicyViolation, PatchRepairExhausted) as exc:
                raise Replan("prototype", str(exc)) from exc
            document = document.model_copy(update={"files": files})
            pending.update(document=document.model_dump(), retrieved=retrieved, prepared=prepared)
            self.store.checkpoint(rid, key, pending)
        self._replan_contract(rid, "prototype_scope", True)
        if "fingerprint" not in pending:
            validate_stage_writes("prototype", pending["prepared"], specification.prototype_paths or ["design/prototypes", "DESIGN.md"])
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
            raise StateConflict("待审核原型已被修改，已保留现场；请核对修改后再续跑")
        if "visuals" in pending and pending.get("browser_version") != self.browser_check_version():
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
            pending["browser_version"] = self.browser_check_version()
            self.store.checkpoint(rid, key, pending)
        if self._prototype_hash(work) != fingerprint:
            raise StateConflict("原型在检查期间被修改，需要重新检查")
        pending["visuals"] = self._archive_visuals(pending["visuals"])
        self._replan_evidence(rid, "prototype-browser", browser_criteria(document.browser_actions, pending["visuals"]),
                              source_hash=fingerprint)
        basis, source = self.approvals.review_inputs(rid, work, document.model_dump()["browser_actions"], pending["visuals"])
        if "review" in pending and not review_valid(pending["review"], basis, source):
            self.approvals.archive_review(rid, pending)
            pending.pop("review")
            self.store.checkpoint(rid, key, pending)
        if "review" not in pending:
            review = await self._invoke("review", {"specification": assessment, "prototype": document.model_dump(),
                "review_kind": "prototype", "design_evidence": pending["retrieved"], "browser_evidence": pending["visuals"],
                "project": self.contexts.build(work)}, ReviewResult, work, rid, "prototype-review", fresh=True)
            if not review.approved:
                self._prototype_rejection(review)
            pending["review"] = review.model_dump()
            self.store.checkpoint(rid, key, pending)
        if self._prototype_hash(work) != fingerprint:
            raise StateConflict("原型在检查期间被修改，需要重新检查")
        context["prototype_approval"] = await self._record_prototype_approval(
            rid, work, document, pending["review"], pending["visuals"])
        return document, pending["retrieved"]

    async def _execute(self, rid):
        if self.store.run(rid)["status"] == "SUCCEEDED":
            self.resources.finish_delivery(rid)
            return self.store.run(rid)
        if self.store.get(rid, "delivery/journal"):
            journal = self.store.get(rid, "delivery/journal")
            if (journal.get("plan") or {}).get("preview"):
                try:
                    await self._ensure_prototype_approval(rid, Path(journal["work"]))
                except Replan as exc:
                    raise ApprovalBoundary(f"交付原型审批未通过，已保留交付现场：{exc.reason}") from exc
            await self.authority.reaudit_delivery(rid, journal)
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
            target_base = self.workspaces.head(target)
            initial_inputs = source_manifest(self.project)
            initial_hash = source_manifest_hash(initial_inputs)
            self.store.checkpoints(rid, {"target_base": target_base, "baseline": base,
                "baseline/source_snapshot": {"version": 1, "commit": base, "entries": initial_inputs,
                                             "source_hash": initial_hash}})
            self.store.update(rid, project_source_hash=initial_hash)
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
            if (not self.store.get(rid, "recovery/implementation_pending") and previous_raw and previous_raw["hash"] == raw["hash"]
                    and previous["data"].get("verified_source_hash") == source_hash and not previous["data"].get("external_dependencies")):
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
                    self.store.checkpoint(rid, "clarify_context", {**context,
                        "controller_feedback": "Requirements assessment returned no acceptance criteria"})
                    raise Replan("clarify", "Restore the original request's acceptance criteria before assessing coverage")
                review = await self._invoke("coverage_review", {**context, "assessment": result.model_dump()},
                                            ReviewResult, work, rid, f"coverage-review/{turn}", fresh=True)
                if not review.approved:
                    context["review_feedback"] = review.model_dump()
                    self.store.checkpoint(rid, "clarify_context", context)
                    continue
                assessment = result.model_dump()
                self.store.checkpoints(rid, {"clarify": assessment,
                    **self.authority.acceptance(rid, "clarify", assessment, review)})
                break
            else:
                raise Replan("clarify", "Clarification batch ended without accepted coverage. "
                             "Assess the persisted answers and review findings before another attempt.")
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
            if not context["project"]["inventory"]:
                self.store.checkpoint(rid, "recovery/coverage-contract/" + digest(assessment), assessment)
                raise Replan("clarify", "Controller inventory confirms an empty workspace. New requirements are changed/unmet, not unknown existing behaviour. "
                             "Do not attempt future product tests before implementation; verification here must inspect existing code only.")
            if result.verification:
                try:
                    verification_commands(result.verification, work)
                except AA2Error as exc:
                    self.store.checkpoint(rid, "recovery/coverage-contract/" + digest(assessment), assessment)
                    raise Replan("clarify", "Invalid existing-coverage verification contract: " + str(exc)) from exc
                environment = await self._environment(work, rid)
                evidence = await self.verifier.run(result.verification, work, environment)
                self._replan_checks(rid, "coverage", result.verification, evidence, work, environment)
                self.store.checkpoint(rid, "clarify_context", {**context, "coverage_verification": [r.model_dump() for r in evidence]})
                raise Replan("clarify", "Resolve unknown coverage using the persisted verification evidence before planning changes")
            self._coverage_replan(rid, context, assessment, "unknown-coverage", digest(assessment),
                "Inspect the available project and historical specifications to resolve unknown coverage. "
                "Declare executable checks for existing behaviour. Ask the user only for genuinely missing "
                "product information, not for code locations or controller-owned evidence.")
        context["specification"] = assessment
        verification_only = False
        retained_verification = self.store.get(rid, "plan", {}).get("mode") == "verification_only"
        if all_satisfied and (retained_verification or not self.store.get(rid, "recovery/implementation_pending")):
            checks = result.verification or self.config.load().verification
            if not checks:
                self._coverage_replan(rid, context, assessment, "coverage-evidence", source_hash,
                    "Satisfied coverage lacks executable verification. Inspect existing source and test runners "
                    "and propose verification commands for the current project. Do not claim success or "
                    "reimplement satisfied behaviour; the controller must execute and validate the checks.")
            environment = await self._environment(work, rid,
                PlanResult.model_validate(self.store.get(rid, "plan")) if retained_verification else None)
            evidence = await self.verifier.run(checks, work, environment)
            self._replan_checks(rid, "coverage", checks, evidence, work, environment)
            self.store.checkpoint(rid, "coverage_checks", [r.model_dump() for r in evidence])
            if passed(evidence):
                previous_contract = next((r["id"] for r in self.store.successful()
                    if r["data"].get("verified_source_hash") == source_hash and self.store.get(r["id"], "prototype")
                    and self.store.get(r["id"], "plan", {}).get("preview")), None)
                if not retained_verification and (not result.frontend or previous_contract):
                    return await self._finish_no_changes(rid, work, previous_contract or "current-baseline")
                verification_only = True
                context["planning_mode"] = "verification_only"
                context["coverage_verification"] = [r.model_dump() for r in evidence]
            else:
                self.store.checkpoint(rid, "clarify_context", {**context, "regression_evidence": [r.model_dump() for r in evidence]})
                raise Replan("clarify", "Coverage verification failed. Distinguish actual regressions from missing test infrastructure; do not reimplement satisfied behaviour.")
        spec_dir = work / "docs/requirements"
        acceptance = "\n\n".join(f"### {r.id}: {r.description}\n" + "\n".join(f"- {a}" for a in r.acceptance) for r in result.requirements)
        atomic_write(spec_dir / f"{rid}.md", result.document + "\n\n## 验收条目\n\n" + acceptance + f"\n\n原始输入指纹：`{raw['hash']}`\n")
        context.update(admission_context(lambda key: self.store.get(rid, key),
                                         shared_browsers=self.config.load().shared_cache.enabled))
        for stage in ("provider_research", "prototype", "design", "plan"):
            if stage == "prototype" and not result.frontend:
                continue
            cached = self.store.get(rid, stage)
            if cached:
                if stage == "design":
                    if not self.authority.accepted(rid, stage, cached):
                        await DesignDocuments(self).reaudit(rid, work, cached, context)
                    pending = self.store.get(rid, "candidate/design", {})
                    if "prepared" in pending:
                        DesignDocuments(self).verify(work, pending)
                    elif cached.get("files"):
                        raise Replan("design", "Legacy design files have no application/review receipt. "
                                     "Reconcile the archived proposal with current documents in the design gate; "
                                     "retain all accepted obligations and do not delegate these files to implementation.")
                    if "prepared" in pending:
                        context["design_review"] = DesignDocuments(self).context(rid, work)
                        if context["design_review"]["status"] != "approved":
                            raise Replan("design", "Current architecture needs a document-bound independent review receipt; "
                                         "retain the documents and review them in the design gate.")
                if stage == "plan" and not self.authority.accepted(rid, stage, cached):
                    await self.authority.reaudit_plan(rid, work, cached, context)
                context[stage] = cached
                if stage == "prototype":
                    context["prototype_approval"] = await self._ensure_prototype_approval(rid, work, cached)
                continue
            context["project"] = self.contexts.build(work)
            if stage == "prototype":
                reused = None
                if not result.ui_changes:
                    for previous in self.store.successful():
                        approval = self.store.get(previous["id"], "prototype_approval", {})
                        prior = self.store.get(previous["id"], "prototype")
                        if prior and self.approvals.human(rid, work, prior.get("browser_actions", []), approval):
                            reused = prior
                            self.store.checkpoint(rid, "prototype_approval", approval)
                            break
                if reused:
                    context["prototype_approval"] = await self._ensure_prototype_approval(rid, work, reused)
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
                if stage == "design" and "prepared" not in pending:
                    if pending.get("document") and "proposal_source_hash" not in pending:
                        raise Replan("design", "Legacy design candidate has no source binding; preserve it and "
                                     "regenerate against the current architecture before applying its files.")
                    if not pending.get("document"):
                        pending["proposal_source_hash"] = tree_hash(work)
                document = (schema.model_validate(pending["document"]) if pending.get("document") else
                            await self._invoke(stage, context, schema, work, rid, stage))
                self.store.checkpoint(rid, pending_key, {**pending, "document": document.model_dump()})
                if stage == "design" and document.questions:
                    document = await DesignDocuments(self).refresh_questions(rid, work, document, context)
                    pending = self.store.get(rid, pending_key, {})
                if isinstance(document, DocumentResult) and document.questions:
                    answer = await self._questions(rid, stage, document.questions, digest(document.model_dump()))
                    context[stage + "_answer"] = answer
                    if stage == "design":
                        context["project"] = self.contexts.build(work)
                        pending["proposal_source_hash"] = tree_hash(work)
                    document = await self._invoke(stage, context, schema, work, rid, stage)
                    self.store.checkpoint(rid, pending_key, {**pending, "document": document.model_dump()})
                    if document.questions:
                        raise AA2Error(f"{stage} still has unresolved questions")
                retrieved = []
            if stage == "design":
                try:
                    document, review = await DesignDocuments(self).prepare(rid, work, document, context)
                except (PolicyViolation, PatchRepairExhausted) as exc:
                    raise Replan("design", str(exc)) from exc
                if not review.approved:
                    details = "".join(f"\n[{f.severity}] {f.location}: {f.description}\n{f.resolution}"
                                      for f in review.findings)
                    raise Replan("design", review.summary + details)
                context["design_review"] = DesignDocuments(self).context(rid, work)
            if stage == "provider_research" and document.sources:
                retrieved = await ResearchService(self.artifacts).verify(document.sources)
                review = await self._invoke("review", {"review_kind": "research", "research": document.model_dump(),
                                                     "retrieved_sources": retrieved}, ReviewResult,
                                            work, rid, "research-review", fresh=True)
                if not review.approved:
                    raise Replan(stage, review.summary)
            if stage == "plan":
                self._replan_contract(rid, "plan", False)
                unmet = {c.requirement_id for c in result.coverage if c.status != "satisfied"}
                if not verification_only and not unmet:
                    unmet = {r.id for r in result.requirements}
                try:
                    if verification_only and document.mode != "verification_only":
                        raise AA2Error("Satisfied coverage requires a verification-only contract without implementation tasks")
                    validate_plan(document, unmet, coverage_verified=verification_only)
                    unsupported = validate_obligations(document, context)
                    context["unsupported_evidence_obligations"] = unsupported
                    for task in document.tasks:
                        verification_commands(task.checks, work, document.runtime_prepare)
                    context["verification_contract"] = self._verification_contract(verification_commands(
                        [*document.final_checks, *document.runtime_probes], work, document.runtime_prepare, prepare_all=True))
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
                review_context = {**context, "plan": document.model_dump(), "review_kind": "plan",
                                  "evidence_candidate_hash": digest(document.model_dump())}
                review = await self._invoke("review", review_context, EvidencePlanReview,
                                            work, rid, "plan-review", fresh=True)
                review_context = self.authority.context(rid, review, review_context)
                details = "".join(f"\n\n[{f.severity}] {f.location}\n{f.description}\n证据：{f.evidence}\n修复要求：{f.resolution}"
                                  for f in review.findings)
                try:
                    current_admission = admission_context(lambda key: self.store.get(rid, key),
                        shared_browsers=self.config.load().shared_cache.enabled)
                    if any(review_context.get(key) != value for key, value in current_admission.items()):
                        raise AA2Error("Plan admission source/capability changed during review; retain the stale candidate")
                    validate_admission_review(document, review, review_context)
                except HumanIntervention:
                    self.store.checkpoint(rid, "evidence_admission/blocked", {
                        "plan": document.model_dump(), "review": review.model_dump(), "context": review_context})
                    raise
                except AA2Error as exc:
                    raise Replan("plan", str(exc) + "\n" + review.summary + details) from exc
                self.store.checkpoint(rid, "evidence_admission/initial",
                                      admission_record(document, review, review_context))
            if stage != "design":
                atomic_write(work / "docs/aa2" / f"{stage}.md", document.document)
            stage_data = document.model_dump()
            if retrieved:
                stage_data["verified_sources"] = [{k: v for k, v in source.items() if k != "excerpt"} for source in retrieved]
                context.pop("selected_design_sources", None)
            updates = {stage: stage_data}
            if stage == "plan":
                from .check_production import activation_updates
                updates.update(activation_updates(self.store.get(rid, "evidence_admission/initial")))
            if stage in {"design", "plan"}:
                updates.update(self.authority.acceptance(rid, stage, stage_data, review))
            self.store.checkpoints(rid, updates)
            context[stage] = stage_data
        plan = PlanResult.model_validate(self.store.get(rid, "plan"))
        if not self.store.get(rid, "plan_revision/current"):
            self.store.checkpoint(rid, "plan_revision/current", {"version": 1, "revision": 1})
        materialize_plan(self.store, rid, work, plan)
        self.workspaces.checkpoint(work, f"refs/aa2/{rid}/design", "aa2: requirements and design baseline")
        if plan.mode == "verification_only":
            if not verification_only:
                raise AA2Error("Verification-only plan no longer has verified satisfied coverage")
            self.store.checkpoint(rid, "develop/done", [])
            safe_event(self.store, rid, "tasks.planned", {"total": 0, "done": 0})
        else:
            await self._develop(rid, work, plan, context)
        if not self.store.get(rid, "readme"):
            pending = self.store.get(rid, "candidate/readme", {})
            docs = (DocumentResult.model_validate(pending["document"]) if pending.get("document") else
                    await self._invoke("readme", {**context, "project": self.contexts.build(work)}, DocumentResult,
                                       work, rid, "readme"))
            self.store.checkpoint(rid, "candidate/readme", {**pending, "document": docs.model_dump()})
            patch = pending.get("patch")
            if patch is None:
                try:
                    files, patch = await self.patch_repair.prepare(rid, work, docs.files,
                        ["README.md", "docs", ".env.example"], stage="readme", context=context, operation="readme")
                except (PolicyViolation, PatchRepairExhausted) as exc:
                    raise Replan("readme", str(exc)) from exc
                docs = docs.model_copy(update={"files": files})
            self.store.checkpoint(rid, "candidate/readme", {"document": docs.model_dump(), "patch": patch})
            validate_stage_writes("readme", patch, ["README.md", "docs", ".env.example"])
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
        if result.frontend:
            context["prototype_approval"] = await self._ensure_prototype_approval(rid, work)
        verify_key = digest([candidate_hash, environment.fingerprint, plan.model_dump(), assessment,
                             self.store.get(rid, "prototype_approval", {}), context.get("provider_research", {})])
        previous_checks = self.store.get(rid, "verify/checks", []) if self.store.get(rid, "verify/identity") == verify_key else []
        evidence = [CheckResult.model_validate(c) for c in previous_checks]
        commands = self._candidate_checks(plan, work)
        context["verification_contract"] = self._verification_contract(commands)
        if (not reusable(commands, evidence, work, environment, self.artifacts).allowed
                or not ReviewEvidence(self.store).retain(rid, consumer_key(), [c.model_dump() for c in evidence])):
            observation_round = self.store.get(rid, "automation/final-round", 0) + 1
            self.store.checkpoint(rid, "automation/final-round", observation_round)
            evidence = await self._checks(rid, commands, work, environment)
        observation_round = self.store.get(rid, "automation/final-round", 0)
        self.store.checkpoint(rid, "verify/identity", verify_key)
        self.store.checkpoint(rid, "verify/checks", [r.model_dump() for r in evidence])
        self._replan_checks(rid, "final-checks", commands, evidence, work, environment)
        if not passed(evidence):
            self._final_progress(rid, plan, observation_round, frontend=result.frontend)
            raise RecoveryNeeded(check_failure(commands, evidence, step="final_checks", evidence_ref="verify/checks"))
        self._require_candidate_evidence(rid, commands, evidence, work, environment,
                                         phase="final", expected_source=candidate_hash)
        visuals = []
        if ProjectProfile.detect(work, self.config.load().project_profile).has_frontend and not result.frontend:
            raise Replan("clarify", "Implementation introduced a frontend without a prototype and browser verification contract")
        if result.frontend:
            self._task_approval_context(rid, work, context)
            await self._ensure_browser(rid, work)
            try:
                with observe_activity(self.store, rid, "实现界面浏览器验收"):
                    visuals = await inspect_application(plan.preview, work, self.store.root / "visuals" / rid / "implemented",
                                                        environment, self.sandbox, self.store.root)
            except PrototypeBrowserError as exc:
                visuals = self._archive_visuals(exc.results)
                self.store.checkpoint(rid, "verify/visuals", visuals)
                self._final_progress(rid, plan, observation_round, frontend=True, visuals=visuals)
                raise RecoveryNeeded(FailureRecord.create(kind="checks", component="browser", origin_id="final",
                    step="application_browser", obligations=[r.id for r in result.requirements], evidence_refs=["verify/visuals"],
                    evidence={"results": visuals, "error": str(exc)})) from exc
            visuals = self._archive_visuals(visuals)
            self.store.checkpoint(rid, "verify/visuals", visuals)
            self._replan_evidence(rid, "application-browser", browser_criteria(plan.preview.actions, visuals),
                                  source_hash=tree_hash(work))
        context["verification_observation"] = self._require_candidate_evidence(
            rid, commands, evidence, work, environment, phase="final", expected_source=candidate_hash)
        context.update(self._task_design_context(rid, work))
        context["source_history"] = SourceHistory(self).collect_plan(rid, work, plan)
        context["preview_contracts"] = PreviewEvidence(self).contracts(rid, plan)
        review_key = digest([verify_key, [r.model_dump() for r in evidence], visuals,
                             context["design_review"], context["source_history"], context["preview_contracts"],
                             context["verification_observation"]])
        cached_review = self.store.get(rid, "verify/review") if self.store.get(rid, "verify/review_identity") == review_key else None
        if cached_review and not self.authority.reusable(rid, cached_review):
            cached_review = None
        review = ReviewResult.model_validate(cached_review) if cached_review else await self._invoke("verify", {**context, "project": self.contexts.build(work),
                                               "evidence": [r.model_dump() for r in evidence],
                                               "implemented_visuals": visuals,
                                               "approved_prototype_visuals": self.store.get(rid, "prototype_approval", {}).get("visuals", []),
                                               "prototype_approval": context.get("prototype_approval", {})},
                                    ReviewResult, work, rid, "final-review", fresh=True)
        if not review.approved:
            ReviewEvidence(self.store).complete(rid, consumer_key(), [r.model_dump() for r in evidence],
                updates={"verify/review": review.model_dump()})
            self._final_progress(rid, plan, observation_round, frontend=result.frontend, visuals=visuals)
            raise RecoveryNeeded(FailureRecord.create(kind="review", component="final_review", origin_id="final",
                step="final_review", obligations=[r.id for r in result.requirements], evidence_refs=["verify/review", "verify/checks"],
                evidence={"review": review.model_dump(), "checks": [r.model_dump() for r in evidence]}))
        self._task_design_context(rid, work)
        if (git(work, "rev-parse", "HEAD") != candidate or tree_hash(work) != candidate_hash
                or git(work, "diff", "--name-only", "HEAD")):
            raise StateConflict("Candidate changed during verification")
        self._require_candidate_evidence(rid, commands, evidence, work, environment,
                                         phase="final", expected_source=candidate_hash)
        ReviewEvidence(self.store).complete(rid, consumer_key(), [r.model_dump() for r in evidence],
            updates={"verify/review": review.model_dump(), "verify/review_identity": review_key})
        self._final_progress(rid, plan, observation_round, frontend=result.frontend, visuals=visuals, approved=True)
        self._task_approval_context(rid, work, context)
        self.store.update(rid, external_dependencies=bool(context.get("provider_research", {}).get("sources")))
        return await self.delivery.finish(rid, candidate=candidate, work=work, environment=environment, plan=plan,
                                          outcome="NO_CHANGES" if verification_only else "DELIVERED")

    async def _environment(self, work, rid, plan=None):
        key = "infrastructure/environment/" + digest([str(work), tree_hash(work)])
        while True:
            failures = self.store.get(rid, key, 0)
            permit = None
            if failures > self.config.load().execution.infra_retries:
                from .recovery import RecoveryDecision
                record = FailureRecord.create(kind="environment", component="infrastructure",
                    origin_id="environment/" + digest(str(work)), step="environment_retry", obligations=[key],
                    evidence_refs=[key], evidence={"attempts": failures,
                        "last_failure": self.store.get(rid, f"recovery/infrastructure/{digest(key)}/{failures}", {})})
                active = {"attempt": failures + 1, "failure": record.model_dump()}
                suggested = RecoveryDecision(failure_id=record.id, action="recheck", budget_owner=record.origin_id,
                    reason="环境准备的单次重试已结束，评估是否再次准备同一环境", evidence_refs=[key])
                _, permit = await self.recovery_policy.authorize(rid, record, active, work, None, suggested,
                    operation=f"environment/{failures}", force_decision=True, candidates={"recheck": suggested})
                self.recovery_policy.start(rid, permit)
            try:
                environment = await self._environment_once(work, rid, plan)
                self.recovery_policy.complete(rid, permit, {"status": "verified", "environment_hash": environment.fingerprint})
                if permit:
                    self.recovery_policy.finish_cycle(rid, active, {"status": "verified"})
                return environment
            except RetryableInfrastructureError as exc:
                from .installation_failure import failure_evidence
                self.store.checkpoints(rid, {key: failures + 1,
                    f"recovery/infrastructure/{digest(key)}/{failures + 1}": {
                        "component": "environment", "workspace": str(work), "error": str(exc),
                        **failure_evidence(exc)}})
                self.store.event(rid, "environment.retrying", {"failure": failures + 1, "error": str(exc)})
                self.recovery_policy.complete(rid, permit, {"status": "failed", "attempt": failures + 1})
                if permit:
                    self.recovery_policy.finish_cycle(rid, active, {"status": "failed"})
                await asyncio.sleep(min(2 ** failures, 8))

    async def _environment_once(self, work, rid, plan=None):
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
            environment = await self.environments.with_resolution(work, environment)
            self.store.event(rid, "environment.ready", {"fingerprint": environment.fingerprint})
            return environment

    async def _develop(self, rid, integration, plan, context):
        cfg = self.config.load()
        for task in plan.tasks:
            if task.id in self.store.get(rid, "develop/done", []):
                self.authority.retain_task(rid, task, self.store.get(rid, "develop/task/" + task.id, {}))
        done = set(self.store.get(rid, "develop/done", []))
        positions = {task.id: index for index, task in enumerate(plan.tasks, 1)}
        safe_event(self.store, rid, "tasks.planned", {"total": len(plan.tasks), "done": len(done)})
        while len(done) < len(plan.tasks):
            if context.get("specification", {}).get("frontend"):
                context["prototype_approval"] = await self._ensure_prototype_approval(rid, integration)
            if shutil.disk_usage(self.store.root).free < cfg.cleanup.min_free_bytes:
                self.resources.collect()
                if shutil.disk_usage(self.store.root).free < cfg.cleanup.min_free_bytes:
                    raise ResourceBoundary("磁盘空间仍不足", action="释放项目所在文件系统的磁盘空间。",
                        condition="资源管理器核对可用空间达到当前配置要求。", phase="generation")
            base = git(integration, "rev-parse", "HEAD")
            from .task_repair_order import dispatched_repair
            repair = dispatched_repair(self, rid, plan, done, base)
            if repair:
                batch = [repair[0]]
                self.store.event(rid, "task.repair_prioritized", repair[1])
            else:
                batch = ready_batch(plan.tasks, done, cfg.execution.max_parallel_tasks)
            if not batch:
                raise Replan("plan", "Task graph cannot make progress; reconcile accepted task dependencies")
            from .task_batch import collect_batch
            for task in batch:
                safe_event(self.store, rid, "task.started", {
                    "task": task.id, "title": task.title,
                    "index": positions[task.id], "total": len(plan.tasks),
                })
            outcomes = await collect_batch([self._observed_task(rid, task, base, plan, context) for task in batch])
            failures = []
            for task, outcome in zip(batch, outcomes):
                if isinstance(outcome, BaseException):
                    failures.append(outcome)
                    continue
                try:
                    self.workspaces.integrate(integration, outcome)
                except AA2Error as exc:
                    raise RecoveryNeeded(FailureRecord.create(kind="integration", component="workspace", origin_id=task.origin_id or task.id,
                        task_id=task.id, step="integration", obligations=task.requirement_ids,
                        evidence_refs=["develop/task/" + task.id], evidence={"error": str(exc), "commit": outcome})) from exc
                done.add(task.id)
                self.store.checkpoint(rid, "develop/done", sorted(done))
                self.store.event(rid, "task.integrated", {"task": task.id, "done": len(done), "total": len(plan.tasks)})
                saved_task = self.store.get(rid, f"develop/task/{task.id}", {})
                if saved_task.get("workspace") and saved_task.get("recovery_ref"):
                    self.resources.retire_worktree(Path(saved_task["workspace"]), saved_task["recovery_ref"])
            self.workspaces.checkpoint(integration, f"refs/aa2/{rid}/integration", "aa2: integrated tasks")
            if failures:
                raise failures[0]

    async def _observed_task(self, rid, task, base, plan, context):
        try:
            return await self._task(rid, task, base, plan, context)
        except EnvironmentCommandError as exc:
            # Persist every task before aggregation. The shared waiting consumer
            # may admit a fully bound unknown npm failure to read-only diagnosis.
            if isinstance(exc, DependencyResolutionError) or exc.evidence.get("authority") != "external_command_observation":
                raise
            boundary = known_exception(exc, "develop")
            saved = self.store.get(rid, "develop/task/" + task.id, {})
            binding = {"task_id": task.id, "origin_id": task.origin_id or task.id,
                       "plan_hash": digest(plan.model_dump()), "task_hash": digest(task.model_dump()),
                       "base": base, "source_hash": saved.get("source_hash"),
                       "workspace": saved.get("workspace"),
                       "task_revision": self.store.get(rid, "task_state/" + task.id, {}).get("revision")}
            requirement = boundary.requirement.model_copy(update={
                "reason": f"Task {task.id}: {boundary.requirement.reason}",
                "evidence": {**boundary.requirement.evidence, "controller_task": binding}})
            boundary = HumanIntervention(requirement, reason_code=boundary.reason_code, phase=boundary.phase)
            identity = digest([boundary.reason_code, boundary.phase, requirement.model_dump()])
            observation = {"version": 1, "identity": identity, "reason_code": boundary.reason_code,
                           "phase": boundary.phase, "requirement": requirement.model_dump()}
            queued = self._environment_waits(rid)
            queued[identity] = observation
            # No await between catching and persisting: aggregation/cancellation
            # must not discard another task's independently observed failure.
            self.store.checkpoints(rid, {"environment/prerequisite-queue": queued,
                "environment/prerequisite-observations/" + identity: observation})
            raise boundary from exc

    def _task_progress(self, rid, task, saved, *, checked=False, approved=False):
        owner = task.origin_id or task.id
        counters = self.store.get(rid, "rounds/" + owner, {"round": 0})
        token = saved.get("observation_id", ["implementation", owner, counters["round"]])
        observation = self.store.get(rid, "replanning/evidence/task/" + owner) if checked else None
        phase = "checks" if checked else "environment" if saved.get("environment_error") else (
            "candidate" if saved.get("candidate_error") else "proposal")
        progress = self.convergence.observe(rid, owner, token, observation, approved=approved, phase=phase)
        self.store.checkpoint(rid, "rounds/" + owner, {**counters, "stalled": progress.get("stalled", 0)})
        return progress

    def _task_failure(self, rid, task, saved, *, kind=None, step="checks", proposal=None, verification_error=None,
                      record_outcome=True):
        feedback = saved.get("feedback", {})
        current = saved if any(k in saved for k in ("checks", "review", "environment_error", "candidate_error")) else feedback
        evidence = {k: current.get(k) for k in
                    ("checks", "review", "environment_error", "candidate_error")}
        if current.get("recording_review"):
            evidence["recording_review"] = current["recording_review"]
        if current.get("environment_failure"):
            evidence["environment_failure"] = current["environment_failure"]
        evidence["checks"] = evidence["checks"] or []
        evidence["task"] = task.model_dump()
        if verification_error is not None:
            evidence["verification_error"] = verification_error
        if kind is None:
            kind = ("integration" if feedback.get("baseline_refresh", {}).get("retained_candidate") else
                    "environment" if evidence["environment_error"] else "proposal" if evidence["candidate_error"]
                    else "review" if evidence["review"] and not evidence["review"].get("approved") else "checks")
        if kind == "integration":
            evidence["baseline_refresh"] = feedback["baseline_refresh"]
        if record_outcome:
            self.recovery_policy.task_outcome(rid, saved, task.id, {"status": "failed", "kind": kind,
                "evidence": "develop/task/" + task.id, "observation": digest(evidence)})
        return FailureRecord.create(kind=kind, component="implementation", origin_id=task.origin_id or task.id,
            task_id=task.id, step=step, obligations=task.requirement_ids, evidence_refs=["develop/task/" + task.id],
            evidence=evidence, proposal=proposal or {})

    def _require_task_evidence(self, rid, task, saved, commands, checks, work, environment):
        production = {}
        failure = self._verification_problem(rid, commands, checks, work, environment, phase="task",
                                             task=task, expected_source=saved.get("source_hash"), production_out=production)
        if failure is None:
            return self._verification_observation(commands, checks, environment, production)
        # Verification drift is a new evidence observation, not an implementation
        # defect or implicit permission to accept a stale review.
        saved["phase"] = "reverify"
        state_key = "task_state/" + task.id
        state = self.store.get(rid, state_key, {})
        self.store.checkpoints(rid, {"develop/task/" + task.id: saved,
            state_key: {**state, "evidence": "stale"}})
        raise RecoveryNeeded(self._task_failure(rid, task, saved, kind="contract" if failure.kind == "contract" else "evidence", step="verification_inputs",
            verification_error=failure.evidence["verification_error"]).model_copy(update={
                "reason_code": failure.reason_code,
                "evidence": {**self._task_failure_evidence(task, saved), **failure.evidence}}))

    @staticmethod
    def _task_failure_evidence(task, saved):
        return {"task": task.model_dump(), "review": saved.get("review"),
                **({"recording_review": saved["recording_review"]} if saved.get("recording_review") else {}),
                "environment_error": saved.get("environment_error"), "candidate_error": saved.get("candidate_error"),
                **({"environment_failure": saved["environment_failure"]} if saved.get("environment_failure") else {})}

    def _verification_problem(self, rid, commands, checks, work, environment, *, phase, task=None, expected_source=None, production_out=None):
        """Shared gate assessment. Stage adapters own the recovery dispatch, never its classification."""
        reuse = reusable(commands, checks, work, environment, self.artifacts)
        source = tree_hash(work)
        if (expected_source and source != expected_source
                or any(row.source_hash and row.source_hash != source for row in checks)):
            raise StateConflict("Verified task source changed; preserve the candidate and user edits"
                                if task else "Verified candidate source changed; preserve the candidate and user edits", phase=phase)
        if reuse.allowed:
            from .check_production import ProductionGap, observe
            from .evidence import EvidenceIssue, ReuseDecision
            try:
                production = observe(lambda key: self.store.get(rid, key), commands, checks, work, environment,
                                     self.artifacts, task=task, phase=phase)
                if production_out is not None and production is not None:
                    production_out["check_production"] = production
                return None
            except ProductionGap as exc:
                reuse = ReuseDecision(False, str(exc), (EvidenceIssue("check_production_gap", -1, ".", (), exc.kind),))
        evidence = {"commands": [c.model_dump() for c in commands], "checks": [c.model_dump() for c in checks],
                    "verification_error": reuse.reason, "verification_assessment": reuse.record()}
        key = "develop/task/" + task.id if task else "verify/checks"
        previous_review = self.store.get(rid, "develop/task/" + task.id, {}).get("review") if task else self.store.get(rid, "verify/review")
        history = {"phase": phase, **evidence, "review": previous_review}
        updates = {f"recovery/verification-history/{task.id if task else phase}/{digest(history)}": history}
        if not task and phase not in {"runtime", "completion"}:
            updates.update({"verify/checks": evidence["checks"], "verify/identity": None, "verify/review_identity": None,
                            "verify/review": None})
        self.store.checkpoints(rid, updates)
        self.store.event(rid, "evidence.invalidated", {"phase": phase, **reuse.record()})
        code = reuse.issues[0].code if reuse.issues else "evidence_unavailable"
        return FailureRecord.create(kind=("contract" if any(i.category == "contract" for i in reuse.issues) else
            "checks" if reuse.issues and all(i.category == "checks" for i in reuse.issues) else "evidence"),
            component="verification", origin_id=(task.origin_id or task.id) if task else "final", task_id=task.id if task else "",
            step="verification_inputs", phase=phase, reason_code=code,
            obligations=task.requirement_ids if task else [digest(c.model_dump()) for c in commands], evidence_refs=[key], evidence=evidence)

    def _require_candidate_evidence(self, rid, commands, checks, work, environment, *, phase, expected_source=None):
        production = {}
        failure = self._verification_problem(rid, commands, checks, work, environment,
                                             phase=phase, expected_source=expected_source, production_out=production)
        if failure:
            raise RecoveryNeeded(failure)
        return self._verification_observation(commands, checks, environment, production)

    @staticmethod
    def _verification_observation(commands, checks, environment, production=None):
        """Present only the successful gate assessment just performed by the caller.

        This is an observation, not a reusable permit or an independent approval.
        Gate entry and post-review checks still use reusable() against real inputs.
        """
        return {"version": 1, "status": "valid_at_gate_entry", **(production or {}),
                "source_hash": checks[0].source_hash,
                "environment_hash": environment.fingerprint,
                "commands_hash": digest([command.model_dump() for command in commands]),
                "checks_hash": digest([check.model_dump() for check in checks]),
                "generated_outputs": [{"command_index": index, "command_hash": check.command_hash,
                                       "outputs": dict(check.generated_outputs)}
                                      for index, check in enumerate(checks) if check.generated_outputs],
                "scope": "Controller validated the supplied checks against current execution inputs, environment, "
                         "reports and generated outputs. This does not approve implementation semantics or "
                         "replace required browser evidence. All acceptance gates revalidate before advancing."}

    def _final_progress(self, rid, plan, token, *, frontend=False, visuals=(), approved=False):
        observation = self.store.get(rid, "replanning/evidence/final-checks", {})
        if frontend and plan.preview:
            browser = browser_criteria(plan.preview.actions, visuals)
            observation = {**observation, "criteria": {**observation.get("criteria", {}), **browser},
                "progress_criteria": {**observation.get("progress_criteria", observation.get("criteria", {})), **browser}}
        self.convergence.observe(rid, "final", ["final", token], observation, approved=approved)
        permit = self.store.get(rid, "recovery/final-action")
        if permit:
            outcome = {"status": "verified" if approved else "unpassed", "observation": ["final", token],
                       "evidence": "verify/checks"}
            self.recovery_policy.complete(rid, permit, outcome)
            record = self.store.get(rid, permit, {})
            cycle_key = "recovery/cycles/" + record.get("cycle_id", "")
            cycle = self.store.get(rid, cycle_key)
            if cycle:
                self.store.checkpoint(rid, cycle_key, {**cycle, "phase": "completed", "outcomes": outcome})
            self.store.checkpoint(rid, "recovery/final-action", None)

    async def _task(self, rid, task, base, plan, context):
        key = f"develop/task/{task.id}"
        saved = self.store.get(rid, key, {})
        saved = self.authority.retain_task(rid, task, saved)
        if saved.get("commit"):
            self.recovery_policy.require_task_outcome(rid, saved, task.id)
            return saved["commit"]
        seed = saved.get("resume_base", base)
        generation = self.store.run(rid)["data"].get("replans", 0)
        state_key = "task_state/" + task.id
        state = self.store.get(rid, state_key, {"version": 1, "revision": 1, "implementation": "pending", "evidence": "absent"})
        legacy = self.store.root / "worktrees" / rid / f"{task.id}-p{generation}"
        name = f"{task.id}-r{state['revision']}-{base[:8]}"
        work = (Path(saved["workspace"]) if saved.get("workspace") else legacy if legacy.is_dir() and not saved.get("revision")
                else self.workspaces.allocate(rid, name, seed))
        if not work.is_dir():
            raise StateConflict("Task workspace missing; restore its recovery snapshot before retrying")
        saved["workspace"] = str(work)
        self.resources.register(work, "worktree", rid)
        saved = await self.authority.reaudit_task(rid, saved, task, work)
        if saved.get("phase") == "implementing" and saved.get("patch"):
            self._task_approval_context(rid, work, context)
            if not saved.get("source_hash") and not self.workspaces.is_ancestor(base, git(work, "rev-parse", "HEAD")):
                raise PolicyViolation("Legacy partial patch lacks a source identity for dependency refresh; preserve it for reconciliation")
            if any(not path_allowed(item["path"], task.allowed_paths) for item in saved["patch"]):
                raise PolicyViolation("Prepared candidate belongs to an earlier scope; preserve it for reconciliation")
            if saved.get("source_hash") and prepared_before_hash(work, saved["patch"]) != saved["source_hash"]:
                raise PolicyViolation("Task inputs changed outside its prepared patch; preserve user edits")
            from .recording_recovery import validate_prepared as validate_recording_prepared
            validate_recording_prepared(self, rid, saved, task, plan)
            self.authority.prepared_task(rid, saved, task)
            apply_prepared(work, saved["patch"])
            saved.update(phase="applied", source_hash=tree_hash(work))
            self.store.checkpoint(rid, key, saved)
        saved, work = refresh_task_baseline(self, rid, task, saved, work, base, state["revision"])
        # Round counters live outside invalidated plan checkpoints.
        counter_key = f"rounds/{task.origin_id or task.id}"
        counters = self.store.get(rid, counter_key, {"round": 0, "stalled": 0, "failure": ""})
        while True:
            approval_context = self._task_approval_context(rid, work, context)
            design_context = self._task_design_context(rid, work)
            self.store.checkpoint(rid, key, saved)
            commands = self._task_commands(rid, task, work, plan)
            if saved.get("phase") == "reverify":
                saved.update(phase="applied", implementation=ImplementationResult(summary="Controller re-verification of retained implementation").model_dump())
                self.store.checkpoint(rid, key, saved)
            if saved.get("phase") not in {"implementing", "applied", "tested"}:
                counters = self.store.get(rid, counter_key, counters)
                if saved.get("phase") == "rejected":
                    if saved.get("source_hash") and saved["source_hash"] != tree_hash(work):
                        raise PolicyViolation("Retained rejected candidate changed; preserve user edits before recovery")
                    if not self.recovery_policy.task_authorized(rid, saved, task):
                        raise RecoveryNeeded(self._task_failure(rid, task, saved))
                counters["round"] += 1
                saved = {"phase": "implementing", "workspace": str(work), "source_hash": tree_hash(work),
                    "authority_sources": self.authority.identity(rid),
                    "preview_permit": saved.get("preview_permit"),
                    "recovery_permit": saved.get("recovery_permit"),
                    "observation_id": ["implementation", task.origin_id or task.id, counters["round"]], "feedback": {
                    **saved.get("feedback", {}), **{k: v for k, v in saved.items() if k in {
                        "checks", "review", "recording_review", "environment_error", "environment_failure", "candidate_error", "controller_recheck", "controller_approval_recheck"}}}}
                self.store.checkpoints(rid, {key: saved, counter_key: counters,
                    state_key: {**state, "implementation": "in_progress"}})
            previous_feedback = saved.get("feedback", saved)
            feedback = {k: previous_feedback[k] for k in ("phase", "checks", "review", "recording_review", "environment_error", "environment_failure", "candidate_error", "controller_recheck", "controller_approval_recheck", "controller_recovery", "upstream_recovery", "failure", "baseline_refresh") if k in previous_feedback}
            feedback = json.loads(json.dumps(feedback))
            task_context = {**approval_context, **design_context, "task": task.model_dump(), "specification": context["specification"],
                            "verification_contract": self._verification_contract(commands),
                            "design": context.get("design"), "feedback": feedback, "unresolved_issues": IssueLedger(self.store, self.artifacts).unresolved(rid),
                            "project": self.contexts.build(work, scopes=task.allowed_paths)}
            from . import recording_recovery
            recording = recording_recovery.task_record(self, rid, saved, task)
            if recording:
                task_context['controller_recording_recovery'] = recording_recovery.context(recording)
            task_context["source_history"] = SourceHistory(self).collect_task(rid, work, task)
            if plan.preview:
                task_context["preview_contracts"] = PreviewEvidence(self).contracts(rid, plan)
            retained = feedback.get("baseline_refresh", {}).get("retained_candidate")
            if retained:
                task_context["context_files"] = [str(self.artifacts.root / retained["hash"][:2] / retained["hash"])]
            task_context["execution_contract"] = ExecutionContract(
                plan_revision=self.store.get(rid, "plan_revision/current", {}).get("revision", 1),
                task_revision=state["revision"], task_id=task.id, origin_id=task.origin_id or task.id,
                allowed_paths=task.allowed_paths, approval=approval_context,
                commands=commands).model_dump()
            if not saved.get("implementation"):
                saved["verification_contract_version"] = BUILD_CONTRACT_VERSION
                if approval_context:
                    saved["approval_context_version"] = APPROVAL_CONTEXT_VERSION
                self.store.checkpoint(rid, key, saved)
            change = (ImplementationResult.model_validate(saved["implementation"]) if saved.get("implementation") else
                      await self._invoke("implement", task_context, ImplementationResult, work, rid, key))
            if request_from(change):
                self._task_progress(rid, task, saved)
                raise RecoveryNeeded(self._task_failure(rid, task, saved, kind="proposal", step="task",
                    proposal=change.model_dump()))
            self._task_approval_context(rid, work, context)
            if saved.get("phase") == "implementing":
                # Persist the candidate before applying it. Full writes are idempotent;
                # replace changes use a postimage check when recovering an interrupted apply.
                saved["implementation"] = change.model_dump()
                self.store.checkpoint(rid, key, saved)
                try:
                    prepared = saved.get("patch")
                    if prepared is None:
                        changes, _ = await self.patch_repair.prepare(rid, work, change.changes, task.allowed_paths,
                            stage="implement", context=task_context, operation=f"{key}/r{counters['round']}")
                        change = change.model_copy(update={"changes": changes})
                        saved["implementation"] = change.model_dump()
                        self.store.checkpoint(rid, key, saved)
                        if recording:
                            preliminary = prepare_changes(work, change.changes, task.allowed_paths, protected=False)
                            recording_recovery.check_changes(recording, preliminary)
                        prepared = prepare_changes(work, change.changes, task.allowed_paths)
                except ProtectedChange:
                    review_context = {**task_context, "candidate_changes": change.model_dump(),
                        "review_kind": "test_changes", "purpose": "Authorize proposed protected test/config changes against requirements"}
                    recording_error = None
                    if recording:
                        review_context['recording_review_binding'] = recording_recovery.review_binding(
                            recording, saved['source_hash'], plan, preliminary, phase='implementation')
                        review_context['recording_review_instruction'] = (
                            'Independently inspect this exact before/after diff and all accepted upstream methods. '
                            'Approve only recording representation changes preserving every assertion, executed test, '
                            'viewport and required PNG/state/download bytes. A path alone is not semantic proof. '
                            'Do not approve an unactivated method change or override adopted design choices.')
                    result = await self._invoke("review", review_context,
                        recording_recovery.RecordingReview if recording else ReviewResult,
                        work, rid, key + "/test-change-review", fresh=True)
                    review = result.review if recording else result
                    if recording:
                        saved['recording_review'] = result.model_dump()
                        try:
                            recording_recovery.require_review(result.model_dump(), review_context['recording_review_binding'])
                        except PolicyViolation as exc:
                            recording_error = str(exc)
                    if not review.approved or recording_error:
                        saved = {**saved, "review": review.model_dump(), "candidate_error": recording_error or "Protected candidate rejected",
                                 "phase": "rejected", "workspace": str(work)}
                        self._task_progress(rid, task, saved)
                        self.store.checkpoint(rid, key, saved)
                        raise RecoveryNeeded(self._task_failure(rid, task, saved, kind="proposal", step="candidate"))
                    prepared = prepare_changes(work, change.changes, task.allowed_paths, protected=False)
                    saved["protected_review"] = review.model_dump()
                    saved["protected_patch_hash"] = digest(prepared)
                except (PolicyViolation, PatchRepairExhausted) as exc:
                    saved = {**saved, "phase": "rejected", "candidate_error": str(exc), "workspace": str(work)}
                    self._task_progress(rid, task, saved)
                    self.store.checkpoint(rid, key, saved)
                    raise RecoveryNeeded(self._task_failure(rid, task, saved, kind="proposal", step="candidate"))
                validate_stage_writes("develop", prepared, task.allowed_paths)
                saved["patch"] = prepared
                self.store.checkpoint(rid, key, saved)
                recording_recovery.validate_prepared(self, rid, saved, task, plan)
                self.authority.prepared_task(rid, saved, task)
                apply_prepared(work, prepared)
                saved.update(phase="applied", source_hash=tree_hash(work))
                self.store.checkpoint(rid, key, saved)
                self.convergence.milestone(rid, task.origin_id or task.id, "candidate_applied",
                    {"source_hash": saved["source_hash"], "checkpoint": key})
            try:
                environment = await self._environment(work, rid, plan)
            except DependencyResolutionError as exc:
                saved.update(phase="rejected", environment_error=str(exc), source_hash=tree_hash(work))
                saved.pop("environment_failure", None)
                if exc.evidence:
                    saved["environment_failure"] = {**exc.evidence, "source_hash": saved["source_hash"]}
                self._task_progress(rid, task, saved)
                counters = self.store.get(rid, counter_key)
                # Retain the failed proposal even when replanning invalidates develop/.
                self.store.checkpoints(rid, {
                    key: saved, counter_key: counters,
                    f"candidate/environment-failures/{task.id}/p{generation}/r{counters['round']}": {
                        **saved, "workspace": str(work)},
                })
                raise RecoveryNeeded(self._task_failure(rid, task, saved, kind="environment", step="environment")) from exc
            self.convergence.milestone(rid, task.origin_id or task.id, "environment_ready",
                {"source_hash": tree_hash(work), "environment_hash": environment.fingerprint})
            prior_checks = [CheckResult.model_validate(c) for c in saved.get("checks", [])]
            commands = self._task_commands(rid, task, work, plan)
            task_context["verification_contract"] = self._verification_contract(commands)
            reuse = reusable(commands, prior_checks, work, environment, self.artifacts)
            if (saved.get("phase") == "tested" and reuse.allowed
                    and ReviewEvidence(self.store).retain(rid, consumer_key(task), [c.model_dump() for c in prior_checks])):
                checks = prior_checks
            else:
                if any(row.checker_version != CHECKER_VERSION for row in prior_checks):
                    previous = {"checks": saved["checks"], "review": saved.get("review"),
                        "source_hash": saved.get("source_hash"), "reason": reuse.reason}
                    self.store.checkpoint(rid,
                        f"recovery/verification-history/{task.origin_id or task.id}/{digest(previous)}", previous)
                checks = await self._checks(rid, commands, work, environment, task)
                saved.update(phase="tested", checks=[c.model_dump() for c in checks], source_hash=tree_hash(work))
                self.store.checkpoint(rid, key, saved)
            self._replan_checks(rid, f"task/{task.origin_id or task.id}", commands, checks, work, environment)
            if not passed(checks):
                failure_record = check_failure(commands, checks, task=task, evidence_ref=key)
                self.store.checkpoint(rid, state_key, {**state, "evidence": "failed"})
                self._task_progress(rid, task, saved, checked=True)
                saved["phase"] = "rejected"
                self.store.checkpoint(rid, key, saved)
                self.recovery_policy.task_outcome(rid, saved, task.id, {"status": "failed",
                    "kind": failure_record.kind, "evidence": key, "observation": digest(failure_record.evidence)})
                raise RecoveryNeeded(failure_record)
            if passed(checks):
                task_context["verification_observation"] = self._require_task_evidence(
                    rid, task, saved, commands, checks, work, environment)
                task_context.update(self._task_approval_context(rid, work, context))
                task_context["source_history"] = SourceHistory(self).collect_task(rid, work, task)
                task_context.update(self._task_design_context(rid, work))
                preview_context = PreviewEvidence(self).context(rid, work, plan, environment, task_id=task.id)
                preview_required = bool(saved.get("preview_permit"))
                if preview_required and not preview_context:
                    raise RecoveryNeeded(self._task_failure(rid, task, saved, kind="evidence", step="preview_stale"))
                task_context.update(preview_context)
                review = await self._invoke("review", {**task_context, "review_kind": "implementation", "implementation": change.model_dump(),
                                                       "project": self.contexts.build(work, scopes=task.allowed_paths),
                                                       "checks": [c.model_dump() for c in checks],
                                                       "source_hash": saved["source_hash"]}, ReviewResult, work, rid, key + "/review", fresh=True)
                saved["review"] = review.model_dump()
                if review.approved:
                    self._task_approval_context(rid, work, context)
                    self._task_design_context(rid, work)
                    self._require_task_evidence(rid, task, saved, commands, checks, work, environment)
                    if preview_required and not PreviewEvidence(self).context(rid, work, plan, environment, task_id=task.id):
                        raise RecoveryNeeded(self._task_failure(rid, task, saved, kind="evidence", step="preview_stale"))
                    self._task_progress(rid, task, saved, checked=True, approved=True)
                    recovery_ref = f"refs/aa2/{rid}/tasks/{task.id}/r{state['revision']}/{base[:8]}"
                    commit = self.workspaces.checkpoint(work, recovery_ref, f"aa2: {task.title}")
                    updates = {key: {**saved, "commit": commit, "workspace": str(work), "recovery_ref": recovery_ref},
                               state_key: {**state, "implementation": "accepted", "evidence": "valid"}}
                    updates.update(self.recovery_policy.task_outcome_updates(rid, saved, task.id,
                        {"status": "verified", "commit": commit, "source_hash": saved["source_hash"], "evidence": key}))
                    ReviewEvidence(self.store).complete(rid, consumer_key(task), [c.model_dump() for c in checks],
                                                        updates=updates)
                    return commit
            self._task_progress(rid, task, saved, checked=True)
            saved["phase"] = "rejected"
            ReviewEvidence(self.store).complete(rid, consumer_key(task), [c.model_dump() for c in checks], updates={key: saved})
            raise RecoveryNeeded(self._task_failure(rid, task, saved, kind="review", step="review"))
    def browser_check_version(self):
        if not self.environments.shared.enabled:
            return PROTOTYPE_BROWSER_VERSION
        from .browser_cache import BrowserCache
        return f"{PROTOTYPE_BROWSER_VERSION}:" + digest(BrowserCache(self.environments).current_identity())

    async def _ensure_browser(self, rid, workspace):
        shared = self.environments.shared.enabled
        if shared:
            from .browser_cache import BrowserCache
            record = BrowserCache(self.environments).prototype_ready()
            if record:
                browser_environment(Path(record["path"]), BrowserCache(self.environments).current_identity())
                return
        cache = self.store.root / "cache/browsers"
        if not shared:
            cache.mkdir(parents=True, exist_ok=True)
            self.resources.register(cache, "cache", rid)
        key = "infrastructure/browser/" + digest([str(workspace), tree_hash(workspace)])
        while shared or not any(cache.glob("chromium-*/chrome-linux*/chrome")):
            previous = self.store.get(rid, key, {"attempts": 0})
            attempt = previous["attempts"] + 1
            permit = None
            if previous["attempts"]:
                if (self.store.get(rid, "delivery/journal") or {}).get("publication_started"):
                    raise HumanIntervention(HumanRequirement(kind="external_input",
                        reason="发布后的浏览器环境准备失败。", action="检查浏览器下载连接与项目内工具链。",
                        resume_condition="浏览器可执行文件在项目缓存中就绪；发布记录和运行预算保持不变。",
                        evidence_refs=[key]), reason_code="browser_install_failed", phase="runtime")
                from .recovery import RecoveryDecision
                failure = FailureRecord.create(kind="environment", component="browser_environment", origin_id="browser_environment",
                    step="browser_install", obligations=[key], evidence_refs=[key], evidence={"installation": previous})
                active = {"attempt": attempt, "failure": failure.model_dump()}
                suggested = RecoveryDecision(failure_id=failure.id, action="recheck", budget_owner=failure.origin_id,
                    reason="浏览器环境准备失败，评估是否重试当前项目内的安装操作", evidence_refs=[key])
                _, permit = await self.recovery_policy.authorize(rid, failure, active, workspace, None, suggested,
                    operation=f"browser-install/{attempt}", force_decision=True, candidates={"recheck": suggested})
                self.recovery_policy.start(rid, permit)
            try:
                await self._install_browser(workspace, cache)
            except RetryableInfrastructureError as exc:
                from .installation_failure import failure_evidence
                result = {"attempts": attempt, "reason_code": "browser_install_failed", "error": str(exc),
                          **failure_evidence(exc)}
                self.store.checkpoint(rid, key, result)
                self.recovery_policy.complete(rid, permit, {"status": "failed", "evidence": key, "attempt": attempt})
                continue
            self.recovery_policy.complete(rid, permit, {"status": "verified", "evidence": key})
            return

    async def _install_browser(self, workspace, cache):
        run = self.store.current(unfinished=True)
        with observe_activity(self.store, run["id"] if run else None, "准备浏览器环境"), self.resources.temporary("browser-install") as scratch:
            if self.environments.shared.enabled:
                from .browser_cache import BrowserCache
                record = await BrowserCache(self.environments).prototype(workspace)
                browser_environment(Path(record["path"]), BrowserCache(self.environments).current_identity())
            else:
                await self.environments.install_browser(workspace, cache, scratch)

    def _agent_instructions(self, work, plan):
        path = work / "AGENTS.md"
        previous = path.read_bytes().decode("utf-8") if path.exists() else "# Project instructions\n"
        start, end = "<!-- aa2:begin -->", "<!-- aa2:end -->"
        if start in previous and end in previous:
            before, rest = previous.split(start, 1)
            _, after = rest.split(end, 1)
            previous = before + after
        sequence = verification_commands([*plan.final_checks, *plan.runtime_probes], work, plan.runtime_prepare, prepare_all=True)
        commands = "\n".join(f"- `{shlex.join(command.argv)}` (cwd: {command.cwd})" for command in sequence)
        block = f"\n{start}\nUse the versioned requirements in docs/requirements and architecture in docs/aa2.\nPreserve accepted behaviour and the DESIGN.md baseline. Run checks relevant to the change;\ncomplete the delivery checks below before shipping. Test expectation changes need independent review.\nUse project-local dependencies. Do not install into system Python, Conda base or global npm.\nStore real secrets only in designated secret files (such as .env or configured sensitive_paths). Source, tests, documentation, examples and generated material must reference environment or configuration variables, never real secret literals. Use unmistakable placeholders in examples. Never copy or link secrets into ordinary files, prompts, logs or diagnostics. Model material admission protects secret paths; it does not inspect ordinary file contents, hashes or inodes for secrets.\n\nDelivery checks:\n{commands}\n{end}\n"
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
            from .check_production import activation_updates, active_receipt
            inherited_production = active_receipt(lambda key: self.store.get(source_run, key))
            if inherited_production:
                # Original admission source is retained, not relabeled as current.
                self.store.checkpoints(rid, {"plan": plan_data, **activation_updates(inherited_production),
                    "evidence_admission/inherited": {"run_id": source_run, "plan_hash": digest(plan_data),
                        "source_binding": inherited_production["context"]["evidence_requirement_source"]["hash"],
                        "current_capture_hash": digest(self.store.get(rid, "capture")),
                        "current_coverage_hash": digest(self.store.get(rid, "clarify"))}})
            for key in ("plan", "design", "prototype", "prototype_approval", "provider_research", "verify/identity", "verify/checks", "verify/review"):
                value = self.store.get(source_run, key)
                if value is not None:
                    self.store.checkpoint(rid, key, value)
            adopted = self.store.get(source_run, "authority/accepted/design")
            if adopted:
                record = self.store.get(source_run, adopted["review_key"])
                if not record:
                    raise StateConflict("Reused design lost its independent adoption receipt")
                self.store.checkpoints(rid, {"authority/accepted/design": adopted, adopted["review_key"]: record})
            # Retain accepted implementations when a reused candidate later needs
            # repair. Current-run budget consumption is never copied or reset.
            if self.store.get(rid, "develop/done") is None and plan_data:
                inherited_tasks = {}
                for task in plan_data["tasks"]:
                    saved = self.store.get(source_run, "develop/task/" + task["id"], {})
                    inherited_tasks["develop/task/" + task["id"]] = {k: v for k, v in saved.items() if k in {"commit", "checks", "review", "source_hash"}}
                    inherited_tasks["task_state/" + task["id"]] = {"version": 1, "revision": 1, "implementation": "accepted", "evidence": "stale"}
                self.store.checkpoints(rid, {**inherited_tasks, "develop/done": [t["id"] for t in plan_data["tasks"]],
                    "task_origins": self.store.get(source_run, "task_origins", {})})
        plan = PlanResult.model_validate(plan_data) if plan_data else None
        approval_context = {}
        if self.store.get(rid, "clarify", {}).get("frontend"):
            if not self.store.get(rid, "prototype") or not plan or not plan.preview:
                raise ApprovalBoundary("现有前端缺少原型审批或应用浏览器验收契约，不能跳过门禁交付")
            try:
                approval_context["prototype_approval"] = await self._ensure_prototype_approval(rid, work)
            except Replan as exc:
                raise ApprovalBoundary(f"复用前端审批未通过，已保留现场：{exc.reason}") from exc
        environment = await self._environment(work, rid, plan)
        # A source-only match cannot establish current environment or evidence identity.
        candidate = self.workspaces.checkpoint(work, f"refs/aa2/{rid}/candidate", "aa2: dependency lock verification")
        policy = self.workspaces.normalize_checkout(work, candidate)
        self.store.checkpoint(rid, "verify/checkout_policy", policy)
        candidate_hash = tree_hash(work)
        checks = self._candidate_checks(plan, work) if plan else (
            IntakeResult.model_validate(self.store.get(rid, "clarify")).verification or self.config.load().verification)
        evidence = await self._checks(rid, checks, work, environment, phase="reuse")
        if not passed(evidence):
            self.store.checkpoint(rid, "verify/checks", [c.model_dump() for c in evidence])
            raise RecoveryNeeded(check_failure(checks, evidence, step="reuse_checks", evidence_ref="verify/checks"))
        self._require_candidate_evidence(rid, checks, evidence, work, environment,
                                         phase="reuse", expected_source=candidate_hash)
        visuals = []
        if approval_context:
            await self._ensure_browser(rid, work)
            try:
                with observe_activity(self.store, rid, "复用实现界面浏览器验收"):
                    visuals = await inspect_application(plan.preview, work, self.store.root / "visuals" / rid / "no-changes",
                                                        environment, self.sandbox, self.store.root)
            except PrototypeBrowserError as exc:
                visuals = self._archive_visuals(exc.results)
                self.store.checkpoint(rid, "verify/visuals", visuals)
                raise RecoveryNeeded(FailureRecord.create(kind="checks", component="browser", origin_id="final",
                    step="reuse_browser", obligations=[r for task in plan.tasks for r in task.requirement_ids],
                    evidence_refs=["verify/visuals"], evidence={"results": visuals, "error": str(exc)})) from exc
            visuals = self._archive_visuals(visuals)
            self.store.checkpoint(rid, "verify/visuals", visuals)
        verification_observation = self._require_candidate_evidence(
            rid, checks, evidence, work, environment, phase="reuse", expected_source=candidate_hash)
        history = SourceHistory(self)
        source_history = (history.collect_plan(rid, work, plan) if plan is not None
                          else history.collect_protected(rid, work))
        review = await self._invoke("verify", {**approval_context, "project": self.contexts.build(work),
                                    "source_history": source_history,
                                    "preview_contracts": PreviewEvidence(self).contracts(source_run or rid, plan),
                                    "verification_contract": self._verification_contract(checks),
                                    "verification_observation": verification_observation,
                                    "specification": self.store.get(rid, "clarify"),
                                    "approved_prototype_visuals": self.store.get(rid, "prototype_approval", {}).get("visuals", []),
                                    "evidence": [c.model_dump() for c in evidence], "implemented_visuals": visuals},
                                    ReviewResult, work, rid, "no-change-review", fresh=True)
        if not review.approved:
            ReviewEvidence(self.store).complete(rid, consumer_key(), [row.model_dump() for row in evidence],
                updates={"verify/review": review.model_dump(), "verify/checks": [row.model_dump() for row in evidence]})
            raise RecoveryNeeded(FailureRecord.create(kind="review", component="reuse_review", origin_id="final",
                step="reuse_review", phase="reuse", obligations=[digest(c.model_dump()) for c in checks],
                evidence_refs=["verify/review", "verify/checks"],
                evidence={"review": review.model_dump(), "checks": [row.model_dump() for row in evidence]}))
        self._task_approval_context(rid, work, {"specification": self.store.get(rid, "clarify", {})})
        if (tree_hash(work) != candidate_hash or git(work, "rev-parse", "HEAD") != candidate
                or git(work, "diff", "--name-only", "HEAD")):
            raise StateConflict("Reused candidate changed during verification")
        self._require_candidate_evidence(rid, checks, evidence, work, environment,
                                         phase="reuse", expected_source=candidate_hash)
        ReviewEvidence(self.store).complete(rid, consumer_key(), [c.model_dump() for c in evidence],
            updates={"verify/checks": [c.model_dump() for c in evidence], "verify/review": review.model_dump()})
        self.store.checkpoint(rid, "verify/identity", digest([tree_hash(work), environment.fingerprint,
                              [c.model_dump() for c in evidence], review.model_dump()]))
        self.store.update(rid, coverage_evidence=evidence_run, external_dependencies=inherited.get("external_dependencies", False))
        return await self.delivery.finish(rid, candidate=candidate, work=work, environment=environment, plan=plan, outcome="NO_CHANGES")

    async def _runtime_checks(self, rid, journal, environment):
        plan = PlanResult.model_validate(journal["plan"]) if journal["plan"] else None
        commands = runtime_commands(plan, self.project, environment)
        attempt = journal.get("runtime_attempt_id") or identifier("runtime-probe")
        checks = []
        for index, command in enumerate(commands):
            identity = digest([journal["candidate"], environment.fingerprint, command.model_dump()])
            key = f"delivery/runtime/{index}"
            # Outputs and runtime caches may change without a source commit. Probe every handoff.
            results = await self._checks(rid, [command], self.project, environment, phase="runtime",
                                         operation=[attempt, index])
            if not passed(results):
                raise RuntimeVerificationError({"checks": [c.model_dump() for c in [*checks, *results]], "visuals": []})
            checks.extend(results)
            self.store.checkpoint(rid, key, {"identity": identity, "checks": [c.model_dump() for c in results]})
        evidence = {"checks": [c.model_dump() for c in checks]}
        failure = self._verification_problem(rid, commands, checks, self.project, environment,
                                             phase="runtime", expected_source=journal.get("runtime_source_hash", journal.get("source_hash")))
        if failure:
            raise RuntimeVerificationError(failure.evidence)
        if plan and plan.preview:
            await self._ensure_browser(rid, self.project)
            evidence["browser_version"] = self.browser_check_version()
            try:
                with observe_activity(self.store, rid, "交付界面运行检查"):
                    evidence["visuals"] = await inspect_application(plan.preview, self.project,
                        self.store.root / "visuals" / rid / "runtime", environment, self.sandbox, self.store.root)
            except PrototypeBrowserError as exc:
                evidence.update(visuals=self._archive_visuals(exc.results), error=str(exc))
                self.store.checkpoint(rid, "delivery/runtime-browser-failure", evidence)
                raise RuntimeVerificationError(evidence) from exc
        if "visuals" in evidence:
            evidence["visuals"] = self._archive_visuals(evidence["visuals"])
            if evidence["browser_version"] != self.browser_check_version():
                self.store.checkpoint(rid, "delivery/runtime-browser-stale", evidence)
                raise StateConflict("Application browser checker changed during runtime verification", phase="runtime")
        failure = self._verification_problem(rid, commands, checks, self.project, environment,
                                             phase="runtime", expected_source=journal.get("runtime_source_hash", journal.get("source_hash")))
        if failure:
            raise RuntimeVerificationError({**evidence, **failure.evidence})
        return evidence

    def _archive_visuals(self, rows):
        for row in rows:
            if row.get("screenshot"):
                path = Path(row["screenshot"])
                ref = self.artifacts.put_file(path, "screenshot")
                row["image_artifact"] = ref
                row["screenshot"] = str(self.artifacts.root / ref["hash"][:2] / ref["hash"])
            self._archive_visuals(row.get("tiles", []))
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
        unfinished = self.store.get(rid, "candidate/delivery-repair", {})
        if unfinished and not unfinished.get("commit") and unfinished.get("phase") != "rejected":
            if not all(key in unfinished for key in ("feedback", "scopes", "trigger", "base_source")):
                raise StateConflict("Legacy unfinished delivery repair lacks its input identity; preserve and reconcile it before continuing")
            await self._delivery_repair(rid, work, unfinished["feedback"], unfinished["scopes"], observation=unfinished["trigger"])
        if journal.get("runtime_failure"):
            failure = digest([journal["candidate"], journal["runtime_failure"], journal.get("runtime_attempt_id")])
            if self.store.get(rid, "delivery/runtime_repaired") != failure:
                await self._delivery_repair(rid, work, {"runtime_failure": journal["runtime_failure"]},
                                            [p for p in git(work, "ls-files", "-z").split("\0") if p], observation="runtime/" + failure)
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
                await self._delivery_repair(rid, work, {"conflicts": unmerged, "incoming": incoming}, unmerged,
                                            observation="merge/" + digest([incoming, journal["candidate"]]))
        plan = PlanResult.model_validate(journal["plan"]) if journal["plan"] else None
        while True:
            environment = await self._environment(work, rid, plan)
            self._export_environment(work, environment)
            candidate = self.workspaces.checkpoint(work, f"refs/aa2/{rid}/candidate", "aa2: reintegrated delivery")
            checkout_policy = self.workspaces.normalize_checkout(work, candidate)
            self.store.checkpoint(rid, "verify/checkout_policy", checkout_policy)
            before = tree_hash(work)
            checks = (self._candidate_checks(plan, work) if plan else
                      IntakeResult.model_validate(self.store.get(rid, "clarify")).verification)
            checks = checks or self.config.load().verification
            verification_operation_id = self.store.get(rid, "recovery/delivery-recheck-active") or [journal["candidate"], incoming, journal["reintegrations"]]
            batch_key = "verification/batches/" + self._verification_operation(rid, checks, work, environment,
                phase="reintegration", operation=verification_operation_id)
            observation = digest(["delivery-verification", rid, candidate, before, verification_operation_id])
            self.store.checkpoint(rid, "delivery/verification-observation", {
                "id": observation, "candidate": candidate, "source_hash": before})
            self.recovery.start_delivery_recheck(rid, work, completed_evidence=batch_key)
            try:
                evidence = await self._checks(rid, checks, work, environment, phase="reintegration",
                    operation=verification_operation_id)
            except RuntimeVerificationError as exc:
                self.recovery.complete_delivery_recheck(rid, exc.evidence)
                await self._delivery_repair(rid, work, exc.evidence,
                    [p for p in git(work, "ls-files", "-z").split("\0") if p], observation=observation)
                continue
            if not passed(evidence):
                self.recovery.complete_delivery_recheck(rid, {"checks": [c.model_dump() for c in evidence]})
                await self._delivery_repair(rid, work, {"checks": [c.model_dump() for c in evidence]},
                                            [p for p in git(work, "ls-files", "-z").split("\0") if p], observation=observation)
                continue
            if await self._recover_delivery_evidence(rid, checks, evidence, work, environment, before, observation):
                continue
            visuals = []
            approval_context = {}
            if plan and plan.preview:
                try:
                    approval_context["prototype_approval"] = await self._ensure_prototype_approval(rid, work)
                except Replan as exc:
                    self.recovery.complete_delivery_recheck(rid, {"prototype_feedback": exc.reason})
                    await self._delivery_repair(rid, work, {"prototype_feedback": exc.reason},
                        [p for p in git(work, "ls-files", "-z", "--", "design/prototypes", "DESIGN.md").split("\0") if p],
                        observation=observation)
                    continue
                await self._ensure_browser(rid, work)
                try:
                    with observe_activity(self.store, rid, "集成后界面浏览器验收"):
                        visuals = await inspect_application(plan.preview, work, self.store.root / "visuals" / rid / "reintegrated",
                                                            environment, self.sandbox, self.store.root)
                except PrototypeBrowserError as exc:
                    visuals = self._archive_visuals(exc.results)
                    self.store.checkpoint(rid, "verify/visuals", visuals)
                    self.recovery.complete_delivery_recheck(rid, {"visuals": visuals, "browser_error": str(exc)})
                    await self._delivery_repair(rid, work, {"visuals": visuals, "browser_error": str(exc)},
                        [p for p in git(work, "ls-files", "-z").split("\0") if p], observation=observation)
                    continue
            if await self._recover_delivery_evidence(rid, checks, evidence, work, environment, before, observation):
                continue
            history = SourceHistory(self)
            source_history = (history.collect_plan(rid, work, plan) if plan is not None
                              else history.collect_protected(rid, work))
            review = await self._invoke("verify", {**approval_context, "project": self.contexts.build(work), "specification": self.store.get(rid, "clarify"),
                "design_review": DesignDocuments(self).context(rid, work),
                "preview_contracts": PreviewEvidence(self).contracts(rid, plan),
                "source_history": source_history,
                "verification_contract": self._verification_contract(checks),
                "approved_prototype_visuals": self.store.get(rid, "prototype_approval", {}).get("visuals", []),
                "evidence": [c.model_dump() for c in evidence], "implemented_visuals": visuals, "incoming": incoming},
                ReviewResult, work, rid, f"reintegration-review/{journal['reintegrations']}", fresh=True)
            if not review.approved:
                self.recovery.complete_delivery_recheck(rid, {"review": review.model_dump()})
                ReviewEvidence(self.store).complete(rid, consumer_key(phase="reintegration"),
                    [c.model_dump() for c in evidence])
                await self._delivery_repair(rid, work, {"review": review.model_dump()},
                                            [p for p in git(work, "ls-files", "-z").split("\0") if p], observation=observation)
                continue
            if (tree_hash(work) != before or git(work, "rev-parse", "HEAD") != candidate
                    or git(work, "diff", "--name-only", "HEAD")):
                raise StateConflict("Reintegrated candidate changed during verification")
            if await self._recover_delivery_evidence(rid, checks, evidence, work, environment, before, observation):
                continue
            self._task_approval_context(rid, work, {"specification": self.store.get(rid, "clarify", {})})
            identity = digest([candidate, before, environment.fingerprint, [c.model_dump() for c in evidence], review.model_dump()])
            self.store.checkpoint(rid, "verify/identity", identity)
            ReviewEvidence(self.store).complete(rid, consumer_key(phase="reintegration"), [c.model_dump() for c in evidence],
                updates={"verify/checks": [c.model_dump() for c in evidence], "verify/review": review.model_dump()})
            visuals = self._archive_visuals(visuals)
            self.store.checkpoint(rid, "verify/visuals", visuals)
            self.recovery.complete_delivery_recheck(rid, {"checks": [c.model_dump() for c in evidence],
                                                         "review": review.model_dump(), "visuals": visuals})
            return {"candidate": candidate, "candidate_tree": git(work, "rev-parse", f"{candidate}^{{tree}}"),
                    "source_hash": before, "validation": identity,
                    "environment": {"prefix": str(environment.prefix), "fingerprint": environment.fingerprint,
                                    "resolution": environment.resolution}}

    async def _recover_delivery_evidence(self, rid, commands, checks, work, environment, source, observation):
        failure = self._verification_problem(rid, commands, checks, work, environment,
                                             phase="reintegration", expected_source=source)
        if failure is None:
            return False
        self.recovery.complete_delivery_recheck(rid, failure.evidence)
        await self._delivery_repair(rid, work, failure.evidence,
            [p for p in git(work, "ls-files", "-z").split("\0") if p], observation=observation)
        return True

    async def _delivery_repair(self, rid, work, feedback, scopes, *, observation=None):
        key = "rounds/delivery"
        pending_key = "candidate/delivery-repair"
        previous = self.store.get(rid, pending_key, {})
        feedback_identity = digest([feedback, scopes])
        resuming = (previous.get("feedback_identity") == feedback_identity
                    and not previous.get("commit") and previous.get("phase") != "rejected")
        if resuming and previous.get("commit_intent"):
            previous["commit"] = self.workspaces.finish_checkpoint(work, previous["commit_intent"])
            previous["result_source"] = tree_hash(work)
            self.store.checkpoint(rid, pending_key, previous)
            self.recovery.complete_delivery_action(rid, previous.get("recovery_permit"),
                {"status": "applied", "commit": previous["commit"], "source_hash": previous["result_source"]})
            return
        if resuming:
            source = prepared_before_hash(work, previous["patch"]) if previous.get("patch") else tree_hash(work)
            if source != previous.get("base_source"):
                raise StateConflict("Delivery repair candidate changed; preserving the pending patch and user changes")
        trigger = observation or (previous.get("trigger") if resuming else digest([tree_hash(work), feedback_identity]))
        identity = digest([trigger, feedback_identity])
        if await self.recovery.delivery_decision(rid, work, feedback, scopes, observation=identity) == "recheck":
            return
        while True:
            pending = self.store.get(rid, pending_key, {})
            rejection = pending.get("rejection", {})
            if pending.get("identity") != identity or pending.get("phase") == "rejected":
                count = self.store.get(rid, key, {"round": 0})["round"]
                published = bool((self.store.get(rid, "delivery/journal", {}) or {}).get("publication_started"))
                if published and count >= self.config.load().execution.max_rounds:
                    raise ResourceBoundary("主分支集成修复预算耗尽，请人工核对规划", action="核对交付故障并明确增加 execution.max_rounds。", condition="发布后修复预算已明确增加，原候选和历史消耗仍可核对。", phase="runtime")
                if pending.get("phase") == "rejected" and not published:
                    self.recovery.complete_delivery_action(rid, pending.get("recovery_permit"),
                        {"status": "rejected", "feedback": rejection})
                    action = await self.recovery.delivery_decision(rid, work,
                        {"prior_failure": feedback, **rejection}, scopes,
                        observation=digest([trigger, count, rejection]))
                    if action == "recheck":
                        return
                if pending:
                    self.artifacts.put(pending, "delivery-repair-history")
                pending = {"identity": identity, "trigger": trigger, "feedback_identity": feedback_identity,
                           "feedback": feedback, "scopes": scopes,
                           "base_source": tree_hash(work), "base_head": git(work, "rev-parse", "HEAD"),
                           "round": count + 1, "rejection": rejection,
                           "recovery_permit": self.store.get(rid, "recovery/delivery-action")}
                self.store.checkpoints(rid, {key: {"round": count + 1}, pending_key: pending})
            if pending.get("commit"):
                if (pending.get("result_source") != tree_hash(work)
                        or git(work, "rev-parse", "HEAD") != pending["commit"]):
                    raise StateConflict("Completed delivery repair changed before resumption; preserving user changes")
                return
            count = pending["round"]
            context = {"task": {"description": "Repair the isolated reintegration without weakening accepted behaviour", "allowed_paths": scopes},
                       "specification": self.store.get(rid, "clarify"), "project": self.contexts.build(work),
                       "feedback": {**feedback, "repair_feedback": rejection}}
            prototype = self.store.get(rid, "prototype", {})
            approval = (self.approvals.current(rid, work, prototype.get("browser_actions", []), self.browser_check_version())
                        if prototype else None)
            if approval:
                context["prototype_approval"] = self.approvals.context(approval)
            context["execution_contract"] = ExecutionContract(
                plan_revision=self.store.get(rid, "plan_revision/current", {}).get("revision", 1),
                task_id="delivery", origin_id="delivery", allowed_paths=scopes,
                approval={"prototype_approval": context["prototype_approval"]} if approval else {}).model_dump()
            proposal = (ImplementationResult.model_validate(pending["proposal"]) if pending.get("proposal") else
                        await self._invoke("implement", context, ImplementationResult, work, rid, f"delivery-repair/{count}"))
            if request_from(proposal):
                self.store.checkpoint(rid, pending_key, {**pending, "proposal": proposal.model_dump()})
                action = await self.recovery.delivery_decision(rid, work, {"proposal": proposal.model_dump(), "prior_failure": feedback}, scopes)
                if action == "recheck":
                    return
                pending.update(phase="rejected", rejection={"controller_recovery": "Repair within the retained delivery scope; original proposal was not applied"})
                self.store.checkpoint(rid, pending_key, pending)
                continue
            if not proposal.changes:
                pending.update(phase="rejected", proposal=proposal.model_dump(),
                    rejection={"candidate_error": "Repair proposal contains no applicable changes"})
                self.store.checkpoint(rid, pending_key, pending)
                continue
            pending["proposal"] = proposal.model_dump()
            self.store.checkpoint(rid, pending_key, pending)
            contracts = "prototype_feedback" in feedback and all(p == "DESIGN.md" or p.startswith("design/prototypes/") for p in scopes)
            try:
                if pending.get("patch") is None:
                    changes, pending["patch"] = await self.patch_repair.prepare(rid, work, proposal.changes, scopes,
                        stage="implement", context=context, operation=f"delivery-repair/{count}", contracts=contracts,
                        artifact_owner="prototype" if contracts else "develop")
                    proposal = proposal.model_copy(update={"changes": changes})
                    pending["proposal"] = proposal.model_dump()
            except (PolicyViolation, PatchRepairExhausted) as exc:
                pending.update(phase="rejected", rejection={"candidate_error": str(exc)})
                self.store.checkpoint(rid, pending_key, pending)
                continue
            self.store.checkpoint(rid, pending_key, pending)
            review = (ReviewResult.model_validate(pending["review"]) if pending.get("review")
                      and self.authority.reusable(rid, pending["review"]) else
                      await self._invoke("review", {**context, "review_kind": "test_changes", "candidate_changes": proposal.model_dump()},
                                         ReviewResult, work, rid, f"delivery-repair-review/{count}", fresh=True))
            if not review.approved:
                pending.update(phase="rejected", rejection={"review": review.model_dump()})
                self.store.checkpoint(rid, pending_key, pending)
                continue
            pending["review"] = review.model_dump()
            self.store.checkpoint(rid, pending_key, pending)
            if pending.get("base_head", git(work, "rev-parse", "HEAD")) != git(work, "rev-parse", "HEAD"):
                raise StateConflict("Delivery repair HEAD changed before publication; preserving user commits")
            validate_stage_writes("prototype" if contracts else "develop", pending["patch"], scopes)
            apply_prepared(work, pending["patch"])
            pending["commit_intent"] = self.workspaces.checkpoint_intent(work, f"refs/aa2/{rid}/candidate")
            self.store.checkpoint(rid, pending_key, pending)
            pending["commit"] = self.workspaces.finish_checkpoint(work, pending["commit_intent"])
            pending["result_source"] = tree_hash(work)
            self.store.checkpoint(rid, pending_key, pending)
            self.recovery.complete_delivery_action(rid, pending.get("recovery_permit"),
                {"status": "applied", "commit": pending["commit"], "source_hash": pending["result_source"]})
            return

    def _candidate_checks(self, plan, work):
        # Whole-source evidence remains conservative: accepted implementation is
        # retained, but every required check is executed on the final candidate.
        checks = [c for task in plan.tasks for c in task.checks]
        commands = [*plan.final_checks, *checks, *plan.runtime_probes]
        try:
            return verification_commands(commands, work, plan.runtime_prepare, prepare_all=True)
        except VerificationPreparationError as exc:
            run = self.store.current(unfinished=True)
            if run and not self.store.get(run["id"], "delivery/journal"):
                raise self._preparation_failure(run["id"], commands, work, exc) from exc
            raise
