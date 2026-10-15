from __future__ import annotations

from pathlib import Path

from .config import ConfigStore
from .environment import Environment
from .errors import AA2Error
from .util import digest, identifier, write_json
from .workspace import git, legacy_tree_hash, tree_hash


class RuntimeVerificationError(AA2Error):
    def __init__(self, evidence):
        self.evidence = evidence
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

    async def finish(self, rid, *, candidate=None, work=None, environment=None, plan=None, outcome="DELIVERED"):
        journal = self.store.get(rid, "delivery/journal")
        if not journal:
            run = self.store.run(rid)
            journal = {"identity_version": 2, "phase": "ready", "target": run["data"]["target_branch"],
                       "old": self.store.get(rid, "target_base", ""), "candidate": candidate,
                       "work": str(work), "plan": plan.model_dump() if plan else None,
                       "environment": {"prefix": str(environment.prefix), "fingerprint": environment.fingerprint},
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
        if (journal.get("identity_version") == 2 and journal["phase"] in {"ready", "published", "runtime"}
                and prefix.is_relative_to(self.store.root / "environments")
                and not self.environments.valid(prefix, saved_environment["fingerprint"])):
            if tree_hash(Path(journal["work"])) != journal["source_hash"]:
                raise AA2Error("Candidate changed while its environment was unavailable; preserving recovery data")
            was_published = journal["phase"] in {"published", "runtime"}
            old_candidate = journal["candidate"]
            if was_published and self.workspaces.head(journal["target"]) != old_candidate:
                raise AA2Error("Target advanced after publication; preserving user commits")
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
            raise AA2Error("旧交付运行预算无法核实；已保留现场，明确增加 delivery.max_runtime_retries 后续跑")
        if journal["phase"] == "repairing_runtime":
            if journal["runtime_budget_used"] >= ConfigStore(project).load().delivery.max_runtime_retries + 1:
                raise AA2Error("主目录运行重试预算已耗尽；明确增加 delivery.max_runtime_retries 后继续")
            self.workspaces.clean_target(journal["target"])
            if self.workspaces.head(journal["target"]) != journal["old"]:
                raise AA2Error("运行修复期间目标分支变化，请先人工核对")
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
                        raise AA2Error("目标分支再次变化，自动重集成预算已耗尽；请稳定目标分支并明确增加 delivery.max_reintegrations 后继续，重启不会清零计数")
                    if not current:
                        raise AA2Error("目标分支被删除，请恢复后继续")
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
                raise AA2Error("已验证候选工作区发生变化，不能发布")
            policy = self.workspaces.checkout_policy(Path(journal["work"]), sorted(self.workspaces.entries(journal["candidate"])))
            expected = self.store.get(rid, "verify/checkout_policy")
            if expected is not None and policy != expected:
                raise AA2Error("Publication conversion policy changed after verification")
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
                raise AA2Error("发布后的目标提交发生变化；保留用户提交，需人工核对后继续完成环境交接")
            if tree_hash(project) != journal["source_hash"]:
                raise AA2Error("主目录源码与验证版本不符")
            limit = ConfigStore(project).load().delivery.max_runtime_retries + 1
            if "runtime_budget_used" not in journal:
                uncertain = journal["phase"] == "runtime" or bool(self.store.get(rid, "delivery/runtime_repaired"))
                journal["runtime_budget_used"] = limit if uncertain else 0
                journal["legacy_runtime_budget_unknown"] = uncertain
                self.save(rid, journal)
            if journal["runtime_budget_used"] >= limit:
                raise AA2Error("主目录运行重试预算已耗尽或旧预算无法核实；保留交付现场，明确增加 delivery.max_runtime_retries 可授予有限额外尝试")
            journal["runtime_budget_used"] += 1
            journal["runtime_attempt_id"] = identifier("runtime")
            self.save(rid, journal, "runtime")
            env = Environment(Path(journal["environment"]["prefix"]), journal["environment"]["fingerprint"])
            plan = journal["plan"] or {}
            commands = plan.get("runtime_prepare", []) + plan.get("runtime_probes", [])
            if plan.get("preview"):
                commands.append(plan["preview"]["command"])
            await self.environments.prepare_delivery(env, [c.get("cwd", ".") for c in commands])
            try:
                evidence = await self.runtime(rid, journal, env)
            except RuntimeVerificationError as exc:
                # Repair source only in the retained isolated checkout, then reverify and FF again.
                from .evidence import runtime_configuration
                from .models import Command
                inputs = [runtime_configuration(project, Command.model_validate(c), env) for c in commands]
                # Exclude capture timing, log addresses and changing diagnostic prose.
                rows = exc.evidence.get("checks", []) if isinstance(exc.evidence, dict) else []
                failure = [{k: row.get(k) for k in ("argv", "cwd", "exit_code", "process_exit_code", "failure_reasons",
                    "test_ids", "passed_test_ids", "report_status", "input_hash")} for row in rows] or exc.evidence
                identity = digest([journal["candidate"], journal["source_hash"], journal["environment"], inputs, failure])
                repeated = identity in journal.get("runtime_failed_inputs", [])
                journal.setdefault("runtime_failed_inputs", []).append(identity)
                journal.setdefault("runtime_failures", []).append({"attempt": journal["runtime_attempt_id"],
                    "identity": identity, "evidence": exc.evidence})
                journal.update(runtime_failure=exc.evidence, old=journal["candidate"], incoming=journal["candidate"])
                self.save(rid, journal, "runtime" if repeated else "repairing_runtime")
                if repeated:
                    raise AA2Error("同一候选和运行输入重复失败，停止自动交付并保留证据") from exc
                return None
            self.workspaces.clean_target(journal["target"])
            if self.workspaces.head(journal["target"]) != journal["candidate"] or tree_hash(project) != journal["source_hash"]:
                raise AA2Error("运行环境准备期间源码或目标提交发生变化，保留现场")
            resources = [str(env.prefix)] if env.prefix.is_relative_to(self.store.root / "environments") else []
            receipt = {"version": 1, "run_id": rid, "target": journal["target"], "candidate": journal["candidate"],
                       "tree": journal["candidate_tree"], "source_hash": journal["source_hash"],
                       "validation": journal["validation"], "outcome": journal["outcome"],
                       "reintegrations": journal["reintegrations"], "runtime_budget_used": journal["runtime_budget_used"],
                       "runtime": {**journal["environment"], "resources": resources, "evidence": evidence},
                       "tokens": self.store.usage_summary(rid), "evidence": self.store.delivery_evidence(rid)}
            receipt["owned_refs"] = dict(line.split(" ", 1) for line in git(project, "for-each-ref",
                "--format=%(refname) %(objectname)", f"refs/aa2/{rid}/").splitlines())
            receipt["digest"] = digest(receipt)
            write_json(self.store.root / "runtime.json", receipt["runtime"])
            # Atomic status and resource pin transfer; interruption retries this tail only.
            self.store.complete_delivery(rid, receipt)
        self.resources.finish_delivery(rid)
        return self.store.run(rid)

    async def upgrade_legacy(self, rid, journal):
        """Interpret legacy side effects first; append v2 proof without rewriting history."""
        published = journal["phase"] in {"published", "runtime"}
        source = self.workspaces.project if published else Path(journal["work"])
        if (journal["source_hash"] not in {legacy_tree_hash(source), tree_hash(source)}
                or git(source, "rev-parse", "HEAD") != journal["candidate"]):
            raise AA2Error("Legacy delivery cannot be reconstructed safely; preserving user and recovery data")
        self.store.checkpoint(rid, "delivery/legacy-journal", dict(journal))
        original = journal["candidate"]
        journal.setdefault("incoming", original)
        result = await self.verify(rid, journal)
        journal.update(result, identity_version=2)
        if published:
            journal["old"] = original
            journal["phase"] = "published" if result["candidate"] == original else "ready"
        self.save(rid, journal)
