"""Durable recovery orchestration at a completed worker-batch boundary."""
from __future__ import annotations

from pathlib import Path

from .builds import verification_commands
from .collection import policy_identity
from .design import PROTOTYPE_BROWSER_VERSION
from .errors import AA2Error
from .evidence import assess_observation
from .interaction import choice_spec
from .models import CheckResult, Command, PlanResult, ReviewResult
from .plan_revision import PlanPatch, apply_patch, descendants, materialize_plan
from .recovery import (
    Diagnosis,
    FailureRecord,
    adjudicate,
    check_failure,
    evidence_references,
    failure_summary,
    implemented_candidate,
)
from .util import digest
from .workspace import git, prepared_before_hash, tree_hash


class RecoveryController:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store

    def submit(self, rid, failure):
        active = self.store.get(rid, "recovery/active")
        if active:
            if active["failure"]["id"] != failure.id:
                raise AA2Error("Another recovery is pending; preserve both candidates")
            return
        history = self.store.get(rid, "recovery/count/" + failure.id, 0)
        self.store.checkpoints(rid, {
            "recovery/active": {"version": 1, "failure": failure.model_dump(), "attempt": history + 1},
            f"recovery/failures/{failure.id}/{history + 1}": failure.model_dump(),
            "recovery/count/" + failure.id: history + 1,
        })
        self.store.event(rid, "recovery.requested", {"failure": failure.id, "owner": failure.origin_id,
                                                     "component": failure.component, "kind": failure.kind})

    async def _capture_retry(self, rid, checks, work):
        failed = [row for row in checks if "evidence_collection_failed" in row.get("failure_reasons", [])]
        if not failed:
            return False
        identity = digest(failed)
        key = "evidence/capture-wait/" + identity
        while (all(row.get("capture_policy_hash") == policy_identity(self.store.root) for row in failed)
               and all(row.get("source_hash") == tree_hash(work) for row in failed)):
            count = self.store.get(rid, key, 0)
            await self.engine._ask(rid, f"evidence-capture/{identity}/{count}",
                "测试附件未能完整归档，验收暂未通过。候选代码、JUnit 和原始附件已保留。"
                "请调整 attachment_capture 上限或测试录制配置后继续；相同配置不会重复执行测试，也不会自动修改业务代码。",
                identity)
            self.store.checkpoint(rid, key, count + 1)
        return True

    async def delivery_decision(self, rid, work, feedback, scopes, *, observation=None):
        """Same fact policy, with delivery journal authority and its existing scope/budget."""
        capture_rows = feedback.get("checks", feedback.get("runtime_failure", {}).get("checks", [])
            if isinstance(feedback.get("runtime_failure"), dict) else [])
        capture_retry = await self._capture_retry(rid, capture_rows, work)
        original_observation = observation
        observation = digest([observation or tree_hash(work), feedback])
        key = "recovery/delivery/" + observation
        saved = self.store.get(rid, key)
        if saved and saved.get("decision", {}).get("action") == "repair":
            # The caller validates a partially applied repair against its saved
            # patch preimage. An already dispatched repair may legitimately edit.
            return saved["decision"]["action"]
        evidence_source = tree_hash(work)
        if saved and saved.get("evidence_source_hash", evidence_source) != evidence_source:
            raise AA2Error("Delivery recovery evidence source changed; preserve the candidate")
        if saved and saved.get("decision", {}).get("action") == "recheck" and saved.get("recheck_key"):
            reservation = self.store.get(rid, saved["recheck_key"], {})
            if reservation.get("phase") == "reserved" and reservation.get("observation") == observation:
                return "recheck"
            if reservation.get("phase") == "started":
                raise AA2Error("Interrupted delivery recheck lacks a completed observation; preserve its occupied reservation")
        evidence_ref = "recovery/delivery-evidence/" + observation
        self.store.checkpoint(rid, evidence_ref, feedback)
        kind = "integration" if feedback.get("conflicts") else "review"
        if feedback.get("checks"):
            results = [CheckResult.model_validate(c) for c in feedback["checks"]]
            commands = [Command(argv=c.argv, cwd=c.cwd) for c in results]
            kind = check_failure(commands, results).kind
        checks = feedback.get("checks", feedback.get("runtime_failure", {}).get("checks", [])
                              if isinstance(feedback.get("runtime_failure"), dict) else [])
        recheck_identity = digest([tree_hash(work), sorted(scopes),
            [{k: row.get(k) for k in ("argv", "cwd", "command_hash", "environment_hash", "input_hash",
                                      "runtime_config_hash", "checker_version", "capture_policy_hash")} for row in checks],
            policy_identity(self.store.root)])
        recheck_key = "recovery/delivery-rechecks/" + recheck_identity
        already_rechecked = bool(self.store.get(rid, recheck_key))
        failure = FailureRecord.create(kind=kind, component="delivery", origin_id="delivery", step="delivery",
            obligations=sorted(scopes), evidence_refs=[evidence_ref], evidence={**feedback, "allowed_paths": scopes})
        count_key = "recovery/budgets/diagnosis/delivery"
        count = self.store.get(rid, count_key, self.store.get(rid, "recovery/diagnosis/" + failure.id, {"round": 0}))
        decision = adjudicate(failure, attempts=max(count["round"], int(bool(saved)), int(already_rechecked)))
        if capture_retry:
            decision = decision.model_copy(update={"action": "recheck", "reason": "Attachment capture configuration changed"})
        if feedback.get("conflicts"):
            decision = decision.model_copy(update={"action": "repair", "reason": "Repair recorded merge conflicts within controller scope"})
        if decision.action == "diagnose":
            project = self.engine.contexts.build(work)
            failure = failure.model_copy(update={"evidence_refs": evidence_references(failure, project)})
            saved = {**(saved or {}), "failure": failure.model_dump(), "evidence_source_hash": evidence_source}
            self.store.checkpoint(rid, key, saved)
            if not saved or not saved.get("round"):
                if count["round"] >= self.engine.config.load().execution.max_diagnosis_rounds:
                    await self._grant(rid, count_key + f"/grant/{count['round']}", "交付故障已达到诊断上限。继续只增加一次独立诊断机会。")
                if tree_hash(work) != evidence_source:
                    raise AA2Error("Delivery recovery evidence source changed during diagnosis approval; preserve the candidate")
                count["round"] += 1
                saved = {**(saved or {}), "failure": failure.model_dump(), "round": count["round"]}
                self.store.checkpoints(rid, {key: saved, count_key: count,
                    "recovery/diagnosis/" + failure.id: {"round": count["round"]}})
            diagnosis = await self.engine._invoke("review", {"review_kind": "recovery_diagnosis",
                **self._approval(rid, work),
                "failure": failure.model_dump(), "project": project,
                "diagnosis_feedback": saved.get("diagnosis_feedback"),
                "specification": self.store.get(rid, "clarify"), "delivery_journal": self.store.get(rid, "delivery/journal"),
                "constraint": "Delivery may only repair within supplied allowed_paths or recheck. Gate/plan changes require user reconciliation; never invalidate a published journal."},
                Diagnosis, work, rid, f"recovery/diagnose/{failure.id}/{saved['round']}", fresh=True)
            if tree_hash(work) != evidence_source:
                raise AA2Error("Delivery recovery evidence source changed during diagnosis; preserve the candidate")
            try:
                decision = adjudicate(failure, diagnosis=diagnosis)
            except AA2Error as exc:
                rejection = {"diagnosis": diagnosis.model_dump(), "validation_error": str(exc)}
                self.store.checkpoints(rid, {key: {"failure": failure.model_dump(), "evidence_source_hash": evidence_source, "diagnosis_feedback": rejection},
                    f"recovery/diagnosis-rejections/{failure.id}/{saved['round']}": rejection})
                return await self.delivery_decision(rid, work, feedback, scopes, observation=original_observation)
        if decision.action not in {"repair", "recheck"}:
            self.store.checkpoint(rid, key, {"failure": failure.model_dump(), "evidence_source_hash": evidence_source, "blocked_decision": decision.model_dump()})
            raise AA2Error("交付恢复需要核对已保留的发布记录：" + decision.reason)
        if tree_hash(work) != evidence_source:
            raise AA2Error("Delivery recovery evidence source changed before dispatch; preserve the candidate")
        updates = {key: {"failure": failure.model_dump(), "decision": decision.model_dump(),
                         "evidence_source_hash": evidence_source}}
        if decision.action == "recheck":
            if already_rechecked:
                self.store.checkpoint(rid, key, {"failure": failure.model_dump(), "evidence_source_hash": evidence_source, "blocked_decision": decision.model_dump()})
                raise AA2Error("同一交付输入已自动复验一次；保留证据并等待新的修复或输入")
            updates[recheck_key] = {"observation": observation, "failure": failure.id, "phase": "reserved",
                                    "workspace": str(work), "source_hash": evidence_source}
            updates[key]["recheck_key"] = recheck_key
            updates["recovery/delivery-recheck-active"] = recheck_key
        self.store.checkpoints(rid, updates)
        self.store.event(rid, "recovery.decided", decision.model_dump())
        return decision.action

    def start_delivery_recheck(self, rid, work):
        key = self.store.get(rid, "recovery/delivery-recheck-active")
        if not key:
            return
        saved = self.store.get(rid, key, {})
        if saved.get("phase") != "reserved":
            raise AA2Error("Interrupted delivery recheck has no durable completion; reservation remains occupied")
        if saved.get("workspace") != str(work) or saved.get("source_hash") != tree_hash(work):
            raise AA2Error("Reserved delivery recheck input changed; preserve recovery evidence")
        self.store.checkpoint(rid, key, {**saved, "phase": "started"})

    def complete_delivery_recheck(self, rid, outcome):
        key = self.store.get(rid, "recovery/delivery-recheck-active")
        if not key:
            return
        saved = self.store.get(rid, key, {})
        if saved.get("phase") == "started":
            self.store.checkpoints(rid, {key: {**saved, "phase": "completed", "outcome": outcome},
                                        "recovery/delivery-recheck-active": None})

    def _approval(self, rid, work):
        prototype = self.store.get(rid, "prototype", {})
        approval = self.engine.approvals.current(rid, work, prototype.get("browser_actions", []), PROTOTYPE_BROWSER_VERSION) if prototype else None
        return {"prototype_approval": self.engine.approvals.context(approval)} if approval else {}

    async def resume(self, rid):
        engine = self.engine
        active = self.store.get(rid, "recovery/active")
        if not active:
            return
        failure = FailureRecord.model_validate(active["failure"])
        plan_data = self.store.get(rid, "plan")
        plan = PlanResult.model_validate(plan_data) if plan_data else None
        integration = self.store.root / "worktrees" / rid / "integration"
        task_state = self.store.get(rid, "develop/task/" + failure.task_id, {})
        work = Path(task_state.get("workspace", integration))
        if not work.is_dir():
            work = integration
        if not work.is_dir():
            raise AA2Error("Recovery workspace missing; restore the recorded worktree before continuing")
        capture_retry = await self._capture_retry(rid, failure.evidence.get("checks", []), work)
        if capture_retry:
            active.pop("decision", None)
        if failure.component == "legacy_adapter" and failure.task_id and not active.get("evidence_reconciled"):
            task = next(t for t in plan.tasks if t.id == failure.task_id)
            commands = verification_commands(task.checks, work, plan.runtime_prepare)
            rows = [CheckResult.model_validate(c) for c in failure.evidence.get("checks", [])]
            source = tree_hash(work)
            if rows and any(c.source_hash != source for c in rows):
                raise AA2Error("Retained candidate changed since its recorded checks; preserve user changes before recovery")
            environment = await engine._environment(work, rid, plan)
            matching = {digest(c.model_dump()): c for c in commands}
            if (set(matching) != {row.command_hash for row in rows} or len(rows) != len(matching)
                    or any(row.command_hash not in matching or not assess_observation(
                        matching[row.command_hash], row, work, environment, engine.artifacts).valid for row in rows)):
                rows = await engine.verifier.run(commands, work, environment)
            engine._replan_checks(rid, "task/" + failure.origin_id, commands, rows, work, environment)
            failure.evidence["checks"] = [c.model_dump() for c in rows]
            active.update(failure=failure.model_dump(), evidence_reconciled=True, evidence_source_hash=source)
            self.store.checkpoints(rid, {"recovery/active": active,
                "develop/task/" + task.id: {**task_state, "workspace": str(work), "source_hash": source}})
        if active.get("evidence_source_hash") and active["evidence_source_hash"] != tree_hash(work):
            journal = self.store.get(rid, f"plan_revision/journal/{failure.id}/{active['attempt']}", {})
            document = work / "docs/aa2/plan.md"
            owned = False
            if (active.get("decision", {}).get("action") == "plan_patch" and journal.get("phase") == "activated"
                    and document.is_file() and not document.is_symlink()
                    and document.read_text() == journal["plan"]["document"]):
                preimages = ([journal["document_preimage"]] if "document_preimage" in journal else
                    [{"path": "docs/aa2/plan.md", "before": before, "mode": 0o644}
                     for before in (digest(journal["previous_plan"]["document"]), None)])
                owned = any(prepared_before_hash(work, [item]) == active["evidence_source_hash"] for item in preimages)
            if not owned:
                raise AA2Error("Recovery evidence source changed; preserve the candidate before reassessing the decision")
        # A legacy plan hold constrains a plan action, not diagnosis or scoped repair.
        # Keep it journalled until the actual action is known.
        pending = self.store.get(rid, "replanning/pending")
        if pending:
            self.store.supersede_questions(rid, f"replan-stalled/{pending['evaluation']}")
        if active.get("decision"):
            from .recovery import RecoveryDecision
            decision = RecoveryDecision.model_validate(active["decision"])
        else:
            decision = adjudicate(failure, attempts=active["attempt"] - 1)
            if capture_retry:
                decision = decision.model_copy(update={"action": "recheck", "reason": "Attachment capture configuration changed"})
            if decision.action == "diagnose":
                project = engine.contexts.build(work)
                failure = failure.model_copy(update={"evidence_refs": evidence_references(failure, project)})
                active["failure"] = failure.model_dump()
                active["evidence_source_hash"] = tree_hash(work)
                self.store.checkpoint(rid, "recovery/active", active)
                budget_key = "recovery/budgets/diagnosis/" + failure.origin_id
                budget = self.store.get(rid, budget_key,
                                        self.store.get(rid, "recovery/diagnosis/" + failure.id, {"round": 0}))
                if not active.get("diagnosis_round"):
                    if budget["round"] >= engine.config.load().execution.max_diagnosis_rounds:
                        await self._grant(rid, "diagnosis/" + failure.id + f"/{budget['round']}",
                                          "同一故障已达到独立诊断上限，缺少可执行的恢复结论。继续将只增加一次诊断机会。\n最新失败："
                                          + failure_summary(failure.evidence.get("checks", [])))
                    if tree_hash(work) != active["evidence_source_hash"]:
                        raise AA2Error("Recovery evidence source changed during diagnosis approval; preserve the candidate")
                    budget["round"] += 1
                    active["diagnosis_round"] = budget["round"]
                    self.store.checkpoints(rid, {budget_key: budget, "recovery/active": active,
                        "recovery/diagnosis/" + failure.id: {"round": budget["round"]}})
                diagnosis = await engine._invoke("review", {
                    "review_kind": "recovery_diagnosis", "failure": failure.model_dump(),
                    "plan": plan_data, "project": project,
                    "specification": self.store.get(rid, "clarify"), "design": self.store.get(rid, "design"),
                    "prototype_approval": self.store.get(rid, "prototype_approval"),
                    "task_budgets": self.store.task_origin_state(rid),
                    "diagnosis_feedback": active.get("diagnosis_feedback"),
                    "current_issues": self.store.get(rid, "issues", {}),
                    "observations": self.store.get(rid, "recovery/observations/" + failure.origin_id, []),
                }, Diagnosis, work, rid, f"recovery/diagnose/{failure.id}/{active['diagnosis_round']}", fresh=True)
                if tree_hash(work) != active["evidence_source_hash"]:
                    raise AA2Error("Recovery evidence source changed during diagnosis; preserve the candidate")
                try:
                    decision = adjudicate(failure, attempts=active["attempt"] - 1, diagnosis=diagnosis, plan=plan)
                except AA2Error as exc:
                    rejection = {"diagnosis": diagnosis.model_dump(), "validation_error": str(exc)}
                    rejected_round = active.pop("diagnosis_round")
                    active["diagnosis_feedback"] = rejection
                    self.store.checkpoints(rid, {"recovery/active": active,
                        f"recovery/diagnosis-rejections/{failure.id}/{rejected_round}": rejection})
                    return
            if decision.action == "recheck":
                identity = digest([failure.origin_id, tree_hash(work), failure.evidence.get("commands"),
                    [{k: c.get(k) for k in ("cwd", "argv", "environment_hash", "input_hash", "runtime_config_hash", "checker_version", "capture_policy_hash")}
                     for c in failure.evidence.get("checks", [])],
                    policy_identity(self.store.root)])
                recheck_key = "recovery/rechecks/" + identity
                reservation = {"failure": failure.id, "attempt": active["attempt"]}
                previous = self.store.get(rid, recheck_key)
                if previous and previous != reservation:
                    decision = decision.model_copy(update={"action": "blocked", "reason":
                        "同一执行输入已自动复验一次；需要新证据或不同修复动作"})
                else:
                    active["recheck_reservation"] = recheck_key
                    self.store.checkpoints(rid, {recheck_key: reservation, "recovery/active": active})
            active["decision"] = decision.model_dump()
            self.store.checkpoint(rid, "recovery/active", active)
            self.store.event(rid, "recovery.decided", decision.model_dump())
        if decision.action == "blocked":
            await self._grant(rid, f"recovery-blocked/{failure.id}/{active['attempt']}",
                              "恢复仍缺少证据：" + decision.reason + "。补充证据后继续将重新诊断。")
            self._finish(rid, active, {}, successor=failure.model_dump())
            return
        if decision.action == "plan_patch":
            if not plan:
                raise AA2Error("No accepted plan exists to amend")
            if await self._patch(rid, active, failure, plan, integration):
                self._finish(rid, active, {"recovery/implementation_pending": True})
            return
        if decision.action in {"clarify", "provider_research", "prototype", "design"}:
            # Only an independently adjudicated genuine gate change reaches this path.
            self._finish(rid, active, {}, replan=(decision.action, decision.reason))
            await engine._resume_replan(rid)
            return
        if decision.action not in {"repair", "recheck"}:
            raise AA2Error("Recovery has no executable action")
        if active.get("evidence_source_hash") and tree_hash(work) != active["evidence_source_hash"]:
            raise AA2Error("Recovery evidence source changed before dispatch; preserve the candidate")
        affected = descendants(plan, decision.task_ids) if plan else set()
        updates = {}
        done = set(self.store.get(rid, "develop/done", []))
        for task_id in affected:
            key = "develop/task/" + task_id
            saved = self.store.get(rid, key, {})
            state_key = "task_state/" + task_id
            state = self.store.get(rid, state_key, {"version": 1, "revision": 1})
            retained_repair = task_id not in decision.task_ids and state.get("implementation") == "needs_repair"
            repair = decision.action == "repair" and task_id in decision.task_ids or retained_repair
            accepted = task_id in done or bool(saved.get("commit"))
            if task_id not in decision.task_ids and not implemented_candidate(saved, accepted):
                continue  # Pending descendants still need their first implementation.
            state.update(implementation="needs_repair" if repair else ("accepted" if accepted else "pending"), evidence="stale")
            if accepted:
                state["revision"] += 1
                retained = {k: saved[k] for k in ("workspace", "source_hash", "recovery_ref", "baseline")
                            if k in saved} if task_id not in done else {}
                saved = {**retained, "previous_evidence": saved}
            feedback = dict(saved.get("feedback", {}))
            if retained_repair:
                feedback["upstream_recovery"] = decision.model_dump()
            else:
                feedback.update(controller_recovery=decision.model_dump(), failure=failure.model_dump())
            detection = feedback.get("failure", {})
            if detection.get("task_id") == task_id and detection.get("evidence", {}).get("checks"):
                feedback["checks"] = detection["evidence"]["checks"]
            updates[key] = {**saved, "phase": "rejected" if repair else "reverify", "revision": state["revision"],
                            "feedback": feedback}
            if detection.get("task_id") == task_id and detection.get("evidence", {}).get("checks"):
                updates[key]["checks"] = detection["evidence"]["checks"]
            # Never replay a proposal that requested recovery, including its unapplied changes.
            updates[key].pop("implementation", None)
            updates[key].pop("patch", None)
            updates[state_key] = state
        updates["develop/done"] = sorted(done - affected)
        updates["verify/identity"] = None
        if affected:
            updates["recovery/implementation_pending"] = True
        self._finish(rid, active, updates)

    async def _grant(self, rid, key, reason):
        interaction = choice_spec(["授权这一次操作", "暂停，稍后续跑"], ["continue", "pause"], yes_no=True)
        interaction["pause_on"] = "n"
        await self.engine._ask(rid, key, reason, digest([key, reason]), interaction)

    def _finish(self, rid, active, updates, *, replan=None, successor=None):
        # State transition and completion receipt share a SQLite transaction.
        pending = self.store.get(rid, "replanning/pending")
        previous_tasks = {key: self.store.get(rid, key) for key in updates if key.startswith("develop/task/")}
        self.store.finish_recovery(rid, {**updates, "recovery/active": None,
            f"recovery/completed/{active['failure']['id']}/{active['attempt']}": {
                **active, "phase": "dispatched", "legacy_pending": pending, "previous_tasks": previous_tasks},
            "replanning/pending": None}, pending, replan=replan, successor=successor)

    async def _patch(self, rid, active, failure, plan, work):
        engine = self.engine
        identity = f"{failure.id}/{active['attempt']}"
        journal_key = "plan_revision/journal/" + identity
        journal = self.store.get(rid, journal_key)
        if journal:
            if journal["phase"] != "activated" and (tree_hash(work) != journal["source_hash"]
                    or git(work, "rev-parse", "HEAD") != journal["base_commit"]):
                raise AA2Error("Integration changed before patch activation; preserved journal requires reconciliation")
            if journal["phase"] != "activated":
                for snapshot in journal.get("snapshots", {}).values():
                    path = Path(snapshot["workspace"])
                    if not path.is_dir() or tree_hash(path) != snapshot["source_hash"]:
                        raise AA2Error("Retained candidate changed before activation; reconcile the saved snapshot first")
            self.store.activate_plan_patch(rid, identity)
            return True
        # Charge only an actual amendment, retaining the historical progress barrier.
        pending = self.store.prepare_replan(rid, "plan", active["decision"]["reason"])
        if pending["reason"] != active["decision"]["reason"]:
            pending = {**pending, "reason": active["decision"]["reason"], "legacy_request": pending,
                       "stage": "plan", "granted": False}
            self.store.checkpoint(rid, "replanning/pending", pending)
        if failure.evidence.get("checks"):
            pending["verification_recheck"] = {"checks": failure.evidence["checks"]}
            self.store.checkpoint(rid, "replanning/pending", pending)
        baseline_key = "recovery/patch-baseline/" + identity
        baseline = self.store.get(rid, baseline_key)
        if baseline is None:
            baseline = {"source_hash": tree_hash(work), "base_commit": git(work, "rev-parse", "HEAD")}
            self.store.checkpoint(rid, baseline_key, baseline)
        # Intent precedes every controller-owned Git mutation. Reconcile it before
        # comparing the baseline, including interruption immediately after commit.
        snapshot_key = "recovery/patch-snapshots/" + identity
        saved_snapshots = self.store.get(rid, snapshot_key, {})
        for snapshot in saved_snapshots.values():
            path = Path(snapshot["workspace"])
            if not snapshot.get("commit"):
                snapshot["commit"] = engine.workspaces.finish_checkpoint(path, snapshot["intent"])
            if path.resolve() == work.resolve():
                baseline["base_commit"] = snapshot["commit"]
        self.store.checkpoints(rid, {snapshot_key: saved_snapshots, baseline_key: baseline})
        await engine._grant_replan(rid, pending)
        revision = self.store.get(rid, "plan_revision/current", {}).get("revision", 1)
        def unchanged(proposal):
            if tree_hash(work) != baseline["source_hash"] or git(work, "rev-parse", "HEAD") != baseline["base_commit"]:
                self.store.checkpoint(rid, "recovery/patch-drift/" + identity, {**baseline, **proposal})
                raise AA2Error("Integration baseline changed during plan amendment; preserved proposal requires reconciliation")
        unchanged({"phase": "approved"})
        patch = await engine._invoke("plan", {"plan_patch": True, "base_revision": revision,
            "plan": plan.model_dump(), "failure": failure.model_dump(), "task_origins": self.store.task_origin_state(rid),
            "specification": self.store.get(rid, "clarify"), "project": engine.contexts.build(work)},
            PlanPatch, work, rid, "recovery/patch/" + identity, fresh=True)
        unchanged({"patch": patch.model_dump()})
        try:
            coverage_verified = False
            if plan.mode == "verification_only":
                from .models import IntakeResult
                from .requirements import validate_coverage
                from .verification import passed
                assessment = IntakeResult.model_validate(self.store.get(rid, "clarify"))
                checks = assessment.verification or engine.config.load().verification
                environment = await engine._environment(work, rid, plan)
                evidence = await engine.verifier.run(checks, work, environment) if checks else []
                engine._replan_checks(rid, "coverage", checks, evidence, work, environment)
                satisfied, _ = validate_coverage(assessment, work)
                coverage_verified = satisfied and bool(evidence) and passed(evidence)
                self.store.checkpoint(rid, "coverage_checks", [r.model_dump() for r in evidence])
            candidate, affected, reopen = apply_patch(plan, patch, revision, {failure.id},
                self.store.get(rid, "task_origins", {}), coverage_verified=coverage_verified)
            for task in candidate.tasks:
                verification_commands(task.checks, work, candidate.runtime_prepare)
            verification_commands([*candidate.final_checks, *candidate.runtime_probes], work, candidate.runtime_prepare, prepare_all=True)
        except AA2Error as exc:
            self._reject_patch(rid, active, failure, identity, {"patch": patch.model_dump(), "validation_error": str(exc)})
            return False
        snapshots, candidates = {}, {}
        for task_id in affected | set(patch.remove_tasks):
            saved = self.store.get(rid, "develop/task/" + task_id, {})
            path = Path(saved["workspace"]) if saved.get("workspace") else None
            if path and path.is_dir():
                if saved.get("source_hash") and tree_hash(path) != saved["source_hash"]:
                    raise AA2Error("Task source changed before plan amendment; preserve user changes and reconcile first")
                ref = f"refs/aa2/{rid}/plan-patches/{revision}/{task_id}"
                snapshot = saved_snapshots.get(task_id)
                if snapshot is None:
                    snapshot = {"workspace": str(path), "ref": ref, "source_hash": tree_hash(path),
                                "intent": engine.workspaces.checkpoint_intent(path, ref)}
                    saved_snapshots[task_id] = snapshot
                    self.store.checkpoint(rid, snapshot_key, saved_snapshots)
                if not snapshot.get("commit"):
                    snapshot["commit"] = engine.workspaces.finish_checkpoint(path, snapshot["intent"])
                snapshots[task_id] = snapshot
                if path.resolve() == work.resolve():
                    baseline["base_commit"] = snapshot["commit"]
                self.store.checkpoints(rid, {snapshot_key: saved_snapshots, baseline_key: baseline})
                candidates[task_id] = {"state": saved, "project": engine.contexts.build(path)}
        review = await engine._invoke("review", {"review_kind": "plan_patch", "previous_plan": plan.model_dump(),
            "patch": patch.model_dump(), "plan": candidate.model_dump(), "failure": failure.model_dump(),
            "retained_candidates": candidates,
            "affected": sorted(affected), "reopen": sorted(reopen), "specification": self.store.get(rid, "clarify"),
            "project": engine.contexts.build(work)}, ReviewResult, work, rid, "recovery/patch-review/" + identity, fresh=True)
        unchanged({"patch": patch.model_dump(), "review": review.model_dump()})
        if not review.approved:
            # Preserve both the rejected proposal and its review. A new bounded
            # diagnosis decides what to do; replaying this cached patch cannot help.
            self._reject_patch(rid, active, failure, identity, {"patch": patch.model_dump(), "patch_review": review.model_dump()})
            return False
        for task_id, snapshot in snapshots.items():
            if tree_hash(Path(snapshot["workspace"])) != snapshot["source_hash"]:
                raise AA2Error("Task source changed during plan review; preserve user changes before activation")
        document = work / "docs/aa2/plan.md"
        journal = {"version": 1, "phase": "prepared", "base_revision": revision, "previous_plan": plan.model_dump(),
            "plan": candidate.model_dump(), "patch": patch.model_dump(), "review": review.model_dump(),
            "affected": sorted(affected), "reopen": sorted(reopen), "reason": patch.reason,
            "snapshots": snapshots, **baseline,
            "document_preimage": {"path": "docs/aa2/plan.md", "before": digest(document.read_bytes()) if document.is_file() else None,
                                  "mode": document.stat().st_mode & 0o7777 if document.is_file() else 0o644}}
        self.store.checkpoint(rid, journal_key, journal)
        self.store.activate_plan_patch(rid, identity)
        # A derived document is written only after activation. Startup materializes
        # it again from the accepted plan; the journal is the recovery authority.
        materialize_plan(self.store, rid, work, candidate)
        return True

    def _reject_patch(self, rid, active, failure, identity, evidence):
        key = "recovery/patch-rejection/" + identity
        self.store.checkpoint(rid, key, evidence)
        successor = failure.model_copy(update={"evidence": {**failure.evidence, **evidence},
            "evidence_refs": [*failure.evidence_refs, key]})
        self._finish(rid, active, {}, successor=successor.model_dump())
