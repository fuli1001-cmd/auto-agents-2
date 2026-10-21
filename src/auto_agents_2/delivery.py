from __future__ import annotations

from pathlib import Path

from .config import ConfigStore
from .environment import Environment
from .errors import AA2Error
from .faults import ResourceBoundary, StateConflict
from .util import digest, identifier, write_json
from .workspace import git, legacy_tree_hash, tree_hash


class RuntimeVerificationError(AA2Error):
    def __init__(self, evidence):
        self.evidence = {"checks": evidence, "visuals": []} if isinstance(evidence, list) else evidence
        super().__init__("主目录运行检查失败，需要在隔离工作区诊断修复")


class DeliveryCoordinator:
    """Durable publication; no provider transport, dependency installation or GC policy."""

    def __init__(self, store, workspaces, resources, environments, verify, runtime):
        self.store, self.workspaces, self.resources = store, workspaces, resources
        self.environments, self.verify, self.runtime = environments, verify, runtime

    def save(self, rid, journal, phase=None):
        if phase:
            journal["phase"] = phase
        self.store.checkpoint(rid, "delivery/journal", journal)
        self.store.update(rid, stage="deliver", delivery_phase=journal["phase"])

    @staticmethod
    def budget_boundary(reason, setting="delivery.max_runtime_retries"):
        return ResourceBoundary(reason, action=f"处理已记录的运行故障，或明确增加 {setting}。",
            condition="控制器核对新的运行输入或增加后的预算；重启和确认不清零历史消耗。", phase="runtime")

    def published_source(self, rid, journal):
        """Keep candidate identity separate from the actual main-directory execution identity."""
        current = tree_hash(self.workspaces.project)
        binding = journal.get("publication_inputs")
        if binding and binding.get("candidate") == journal["candidate"]:
            current = self.workspaces.published_input_hash(journal, binding)
        elif binding and current == journal["source_hash"]:
            # A newly verified candidate now covers every current execution input.
            # The previous candidate's preservation proof is history, not authority.
            journal.setdefault("publication_input_history", []).append(binding)
            journal.pop("publication_inputs")
            journal["runtime_source_hash"] = current
            self.save(rid, journal)
        elif current != journal["source_hash"]:
            baseline = self.store.get(rid, "baseline")
            snapshot = self.store.get(rid, "baseline/source_snapshot")
            initial = self.store.run(rid)["data"].get("project_source_hash") or (snapshot or {}).get("source_hash")
            if not baseline or not initial:
                raise StateConflict("Verified runtime source changed; 主目录源码与验证版本不符，缺少入场输入证明", phase="delivery")
            new_binding = self.workspaces.bind_published_inputs(journal, baseline, initial, snapshot)
            current = self.workspaces.published_input_hash(journal, new_binding)
            if binding:
                journal.setdefault("publication_input_history", []).append(binding)
            journal["publication_inputs"] = new_binding
            journal["runtime_source_hash"] = current
            self.save(rid, journal)
            self.store.event(rid, "delivery.inputs_preserved", {"candidate": journal["candidate"],
                "retained_inputs": len(new_binding["retained_paths"]), "proof": new_binding["proof"]})
        return current

    async def finish(self, rid, *, candidate=None, work=None, environment=None, plan=None, outcome="DELIVERED"):
        if self.store.run(rid)["status"] == "SUCCEEDED":
            self.resources.finish_delivery(rid)
            return self.store.run(rid)
        journal = self.store.get(rid, "delivery/journal")
        if not journal:
            run = self.store.run(rid)
            journal = {"identity_version": 2, "phase": "ready", "target": run["data"]["target_branch"],
                       "old": self.store.get(rid, "target_base", ""), "candidate": candidate,
                       "work": str(work), "plan": plan.model_dump() if plan else None,
                       "environment": {"prefix": str(environment.prefix), "fingerprint": environment.fingerprint,
                                       "resolution": environment.resolution},
                       "outcome": outcome, "reintegrations": 0, "runtime_budget_used": 0,
                       "source_hash": tree_hash(work), "validation": self.store.get(rid, "verify/identity"),
                       "candidate_tree": git(work, "rev-parse", f"{candidate}^{{tree}}")}
            self.save(rid, journal)
        while True:
            result = await self._advance(rid, journal)
            if result is not None:
                return result
            journal = self.store.get(rid, "delivery/journal")

    async def _advance(self, rid, journal):
        project = self.workspaces.project
        saved_environment = journal["environment"]
        prefix = Path(saved_environment["prefix"])
        if journal["phase"] == "completion_pending":
            return self.complete_pending(rid, journal)
        if journal["phase"] == "runtime_blocked":
            limit = ConfigStore(project).load().delivery.max_runtime_retries + 1
            if (limit <= journal["blocked_limit"]
                    and self.runtime_identity(journal) == journal["blocked_inputs"]):
                raise self.budget_boundary("同一运行输入已重复失败；处理故障或明确增加 delivery.max_runtime_retries 后继续")
            journal.pop("runtime_attempt_id", None)
            self.save(rid, journal, "runtime")
        if (journal.get("identity_version") == 2 and journal["phase"] in {"ready", "published", "runtime"}
                and prefix.is_relative_to(self.store.root / "environments")
                and not self.environments.valid(prefix, saved_environment["fingerprint"])):
            if tree_hash(Path(journal["work"])) != journal["source_hash"]:
                raise StateConflict("Candidate changed while its environment was unavailable; preserving recovery data")
            was_published = journal["phase"] in {"published", "runtime"}
            old_candidate = journal["candidate"]
            if was_published and self.workspaces.head(journal["target"]) != old_candidate:
                raise StateConflict("Target advanced after publication; preserving user commits")
            self.store.checkpoint(rid, "delivery/environment-recovery", dict(journal))
            journal["incoming"] = old_candidate
            journal.update(await self.verify(rid, journal))
            if was_published:
                journal["old"] = old_candidate
                journal["phase"] = "published" if journal["candidate"] == old_candidate else "ready"
            self.save(rid, journal)
        if journal.get("identity_version") != 2 and journal["phase"] != "publishing":
            await self.upgrade_legacy(rid, journal)
        if journal["phase"] == "repairing_runtime" and "runtime_budget_used" not in journal:
            journal["runtime_budget_used"] = ConfigStore(project).load().delivery.max_runtime_retries + 1
            journal["legacy_runtime_budget_unknown"] = True
            self.save(rid, journal)
            raise self.budget_boundary("旧交付运行预算无法核实；已保留现场，明确增加 delivery.max_runtime_retries 后续跑")
        if journal["phase"] == "repairing_runtime":
            if journal["runtime_budget_used"] >= ConfigStore(project).load().delivery.max_runtime_retries + 1:
                raise self.budget_boundary("主目录运行重试预算已耗尽；明确增加 delivery.max_runtime_retries 后继续")
            self.workspaces.clean_target(journal["target"])
            if self.workspaces.head(journal["target"]) != journal["old"]:
                raise StateConflict("运行修复期间目标分支变化，请先人工核对")
            result = await self.verify(rid, journal)
            journal.update(result)
            journal.pop("runtime_failure", None)
            self.save(rid, journal, "ready")
        if journal["phase"] in {"ready", "reintegrating"}:
            self.workspaces.clean_target(journal["target"])
            current = self.workspaces.head(journal["target"])
            if current != journal["old"] or journal["phase"] == "reintegrating":
                if journal["phase"] != "reintegrating":
                    limit = ConfigStore(project).load().delivery.max_reintegrations
                    if journal["reintegrations"] >= limit:
                        raise self.budget_boundary("目标分支再次变化，自动重集成预算已耗尽；请稳定目标分支并明确增加 delivery.max_reintegrations 后继续，重启不会清零计数", "delivery.max_reintegrations")
                    if not current:
                        raise StateConflict("目标分支被删除，请恢复后继续")
                    journal.update(reintegrations=journal["reintegrations"] + 1, incoming=current)
                    self.save(rid, journal, "reintegrating")
                result = await self.verify(rid, journal)
                journal.update(result, old=journal["incoming"])
                self.save(rid, journal, "ready")
                # Check again after potentially slow verification; bounded across restarts.
                return None
            if (tree_hash(Path(journal["work"])) != journal["source_hash"]
                    or git(Path(journal["work"]), "rev-parse", "HEAD") != journal["candidate"]
                    or git(Path(journal["work"]), "diff", "--name-only", "HEAD")):
                raise StateConflict("已验证候选工作区发生变化，不能发布")
            policy = self.workspaces.checkout_policy(Path(journal["work"]), sorted(self.workspaces.entries(journal["candidate"])))
            expected = self.store.get(rid, "verify/checkout_policy")
            if expected is not None and policy != expected:
                raise StateConflict("Publication conversion policy changed after verification")
            journal["checkout_policy"] = policy
            journal["manifest"] = self.workspaces.publication_manifest(journal["old"], journal["candidate"], Path(journal["work"]))
            self.workspaces.check_publication_paths(journal["manifest"])
            journal["index_before"] = git(project, "write-tree")
            journal["publication_started"] = True
            self.save(rid, journal, "publishing")
            try:
                self.workspaces.publish(journal["target"], journal["old"], journal["candidate"])
                # Git may leave canonical-equal files untouched after an attribute change.
                self.workspaces.recover_publication(journal)
            except AA2Error:
                # A clean external commit won the race before Git touched our candidate.
                current = self.workspaces.head(journal["target"])
                if current != journal["old"] and not self.workspaces.is_ancestor(journal["candidate"], current):
                    self.workspaces.clean_target(journal["target"])
                    self.save(rid, journal, "ready")
                    return None
                raise
            self.save(rid, journal, "published")
        elif journal["phase"] == "publishing":
            self.workspaces.recover_publication(journal)
            self.save(rid, journal, "published")
        if journal["phase"] in {"published", "runtime"}:
            if journal.get("identity_version") != 2:
                await self.upgrade_legacy(rid, journal)
                if journal["phase"] == "ready":
                    return None
            self.workspaces.clean_target(journal["target"])
            if self.workspaces.head(journal["target"]) != journal["candidate"]:
                raise StateConflict("发布后的目标提交发生变化；保留用户提交，需人工核对后继续完成环境交接")
            runtime_source = self.published_source(rid, journal)
            limit = ConfigStore(project).load().delivery.max_runtime_retries + 1
            if "runtime_budget_used" not in journal:
                uncertain = journal["phase"] == "runtime" or bool(self.store.get(rid, "delivery/runtime_repaired"))
                journal["runtime_budget_used"] = limit if uncertain else 0
                journal["legacy_runtime_budget_unknown"] = uncertain
                self.save(rid, journal)
            if journal["phase"] != "runtime" or not journal.get("runtime_attempt_id"):
                if journal["runtime_budget_used"] >= limit:
                    raise self.budget_boundary("主目录运行重试预算已耗尽或旧预算无法核实；保留交付现场，明确增加 delivery.max_runtime_retries 可授予有限额外尝试")
                journal["runtime_budget_used"] += 1
                journal["runtime_attempt_id"] = identifier("runtime")
                journal["runtime_source_hash"] = runtime_source
                self.save(rid, journal, "runtime")
            env = Environment(Path(journal["environment"]["prefix"]), journal["environment"]["fingerprint"],
                          journal["environment"].get("resolution", {}))
            plan = journal["plan"] or {}
            commands = plan.get("runtime_prepare", []) + plan.get("runtime_probes", [])
            if plan.get("preview"):
                commands.append(plan["preview"]["command"])
            await self.environments.prepare_delivery(env, [c.get("cwd", ".") for c in commands])
            try:
                evidence = await self.runtime(rid, journal, env)
            except RuntimeVerificationError as exc:
                # Repair source only in the retained isolated checkout, then reverify and FF again.
                # Exclude capture timing, log addresses and changing diagnostic prose.
                rows = exc.evidence.get("checks", [])
                failure = [{k: row.get(k) for k in ("argv", "cwd", "exit_code", "process_exit_code", "failure_reasons",
                    "test_ids", "passed_test_ids", "report_status", "input_hash")} for row in rows]
                visuals = [{k: row.get(k) for k in ("route", "viewport", "status", "actions", "failed_action")}
                           for row in exc.evidence.get("visuals", [])]
                identity = digest([journal["candidate"], self.runtime_identity(journal), failure, visuals])
                repeated = identity in journal.get("runtime_failed_inputs", [])
                journal.setdefault("runtime_failed_inputs", []).append(identity)
                journal.setdefault("runtime_failures", []).append({"attempt": journal["runtime_attempt_id"],
                    "identity": identity, "evidence": exc.evidence})
                journal.update(runtime_failure=exc.evidence, old=journal["candidate"], incoming=journal["candidate"])
                if repeated:
                    journal.update(blocked_inputs=self.runtime_identity(journal), blocked_limit=limit)
                self.save(rid, journal, "runtime_blocked" if repeated else "repairing_runtime")
                if repeated:
                    raise self.budget_boundary("同一候选和运行输入重复失败，停止自动交付并保留证据") from exc
                return None
            self.workspaces.clean_target(journal["target"])
            if (self.workspaces.head(journal["target"]) != journal["candidate"]
                    or self.published_source(rid, journal) != journal.get("runtime_source_hash", journal["source_hash"])):
                raise StateConflict("运行环境准备期间源码或目标提交发生变化，保留现场")
            journal.update(completion_evidence=evidence, completion_inputs=self.runtime_identity(journal))
            self.save(rid, journal, "completion_pending")
            return self.complete_pending(rid, journal)
        self.resources.finish_delivery(rid)
        return self.store.run(rid)

    def runtime_identity(self, journal):
        from .builds import generated_entry, runtime_commands, verification_commands
        from .evidence import input_identity, runtime_configuration
        from .models import PlanResult
        from .util import contained
        project = self.workspaces.project
        env = Environment(Path(journal["environment"]["prefix"]), journal["environment"]["fingerprint"],
                          journal["environment"].get("resolution", {}))
        plan = PlanResult.model_validate(journal["plan"]) if journal.get("plan") else None
        commands = runtime_commands(plan, project, env)
        if plan and plan.preview:
            commands = verification_commands([*commands, plan.preview.command], project)
        integrity = self.environments.integrity(env.prefix) if env.prefix.is_relative_to(self.store.root / "environments") else None
        return digest([tree_hash(project), journal["environment"], integrity, plan.preview.model_dump() if plan and plan.preview else None,
            [(c.model_dump(), input_identity(project, c), runtime_configuration(project, c, env),
              [(p, generated_entry(contained(project, p), project)) for p in c.outputs]) for c in commands]])

    def complete_pending(self, rid, journal):
        from .artifacts import ArtifactStore
        from .builds import runtime_commands
        from .evidence import reusable
        from .models import CheckResult, PlanResult
        self.workspaces.clean_target(journal["target"])
        prefix = Path(journal["environment"]["prefix"])
        if (self.workspaces.head(journal["target"]) != journal["candidate"]
                or self.published_source(rid, journal) != journal.get("runtime_source_hash", journal["source_hash"])):
            raise StateConflict("Verified runtime source changed before completion; pending evidence retained", phase="completion")
        if (self.runtime_identity(journal) != journal["completion_inputs"]
                or prefix.is_relative_to(self.store.root / "environments")
                and not self.environments.valid(prefix, journal["environment"]["fingerprint"])):
            self._invalidate_completion(rid, journal, {"code": "runtime_inputs_changed", "category": "evidence"})
            return None
        evidence = journal["completion_evidence"]
        artifacts = ArtifactStore(self.store.root)
        rows = [CheckResult.model_validate(row) for row in evidence.get("checks", [])]
        env = Environment(prefix, journal["environment"]["fingerprint"], journal["environment"].get("resolution", {}))
        plan = PlanResult.model_validate(journal["plan"]) if journal["plan"] else None
        assessment = reusable(runtime_commands(plan, self.workspaces.project, env), rows,
                              self.workspaces.project, env, artifacts)
        if not assessment.allowed:
            if any(issue.category == "state_conflict" for issue in assessment.issues):
                raise StateConflict("Pending runtime evidence source changed", phase="completion")
            self._invalidate_completion(rid, journal, assessment.record())
            return None
        receipt = journal.get("completion_receipt")
        if receipt is None:
            receipt = {"version": 1, "run_id": rid, "target": journal["target"], "candidate": journal["candidate"],
                       "tree": journal["candidate_tree"], "source_hash": journal["source_hash"],
                       "validation": journal["validation"], "outcome": journal["outcome"],
                       "reintegrations": journal["reintegrations"], "runtime_budget_used": journal["runtime_budget_used"],
                       "runtime": {**journal["environment"], "resources": [str(prefix)]
                           if prefix.is_relative_to(self.store.root / "environments") else [], "evidence": evidence},
                       "tokens": self.store.usage_summary(rid), "evidence": self.store.delivery_evidence(rid)}
            if journal.get("publication_inputs"):
                receipt.update(publication_inputs=journal["publication_inputs"],
                               runtime_source_hash=journal["runtime_source_hash"])
            receipt["owned_refs"] = dict(line.split(" ", 1) for line in git(self.workspaces.project, "for-each-ref",
                "--format=%(refname) %(objectname)", f"refs/aa2/{rid}/").splitlines())
            receipt["digest"] = digest(receipt)
            journal["completion_receipt"] = receipt
            self.save(rid, journal)
        if digest({k: v for k, v in receipt.items() if k != "digest"}) != receipt["digest"]:
            raise StateConflict("Pending delivery receipt integrity mismatch")
        with artifacts.reading(receipt["runtime"]["evidence"], rid):
            write_json(self.store.root / "runtime.json", receipt["runtime"])
        # The pending checkpoint retains evidence after temporary reader holds close.
        self.store.complete_delivery(rid, receipt)
        self.resources.finish_delivery(rid)
        return self.store.run(rid)

    def _invalidate_completion(self, rid, journal, assessment):
        """Retain successful old evidence; a fresh probe consumes the existing runtime budget."""
        previous = {key: journal[key] for key in ("completion_evidence", "completion_inputs", "completion_receipt") if key in journal}
        previous["assessment"] = assessment
        journal.setdefault("completion_history", []).append(previous)
        for key in ("completion_evidence", "completion_inputs", "completion_receipt"):
            journal.pop(key, None)
        journal.pop("runtime_attempt_id", None)
        self.save(rid, journal, "runtime")
        self.store.event(rid, "evidence.invalidated", {"phase": "completion", **assessment})

    async def upgrade_legacy(self, rid, journal):
        """Interpret legacy side effects first; append v2 proof without rewriting history."""
        published = journal["phase"] in {"published", "runtime"}
        source = self.workspaces.project if published else Path(journal["work"])
        if (journal["source_hash"] not in {legacy_tree_hash(source), tree_hash(source)}
                or git(source, "rev-parse", "HEAD") != journal["candidate"]):
            raise StateConflict("Legacy delivery cannot be reconstructed safely; preserving user and recovery data")
        self.store.checkpoint(rid, "delivery/legacy-journal", dict(journal))
        original = journal["candidate"]
        journal.setdefault("incoming", original)
        result = await self.verify(rid, journal)
        journal.update(result, identity_version=2)
        if published:
            journal["old"] = original
            journal["phase"] = "published" if result["candidate"] == original else "ready"
        self.save(rid, journal)
