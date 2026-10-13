from __future__ import annotations

from pathlib import Path

from .config import ConfigStore
from .environment import Environment
from .errors import AA2Error
from .util import digest, write_json
from .workspace import git, tree_hash


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
            journal = {"phase": "ready", "target": run["data"]["target_branch"],
                       "old": self.store.get(rid, "target_base", ""), "candidate": candidate,
                       "work": str(work), "plan": plan.model_dump() if plan else None,
                       "environment": {"prefix": str(environment.prefix), "fingerprint": environment.fingerprint},
                       "outcome": outcome, "reintegrations": 0,
                       "source_hash": tree_hash(work), "validation": self.store.get(rid, "verify/identity"),
                       "candidate_tree": git(work, "rev-parse", f"{candidate}^{{tree}}")}
            self.save(rid, journal)
        project = self.workspaces.project
        if journal["phase"] == "repairing_runtime":
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
                return await self.finish(rid)
            if (tree_hash(Path(journal["work"])) != journal["source_hash"]
                    or git(Path(journal["work"]), "rev-parse", "HEAD") != journal["candidate"]
                    or git(Path(journal["work"]), "diff", "--name-only", "HEAD")):
                raise AA2Error("已验证候选工作区发生变化，不能发布")
            journal["manifest"] = self.workspaces.publication_manifest(journal["old"], journal["candidate"])
            self.workspaces.check_publication_paths(journal["manifest"])
            journal["index_before"] = git(project, "write-tree")
            journal["publication_started"] = True
            self.save(rid, journal, "publishing")
            try:
                self.workspaces.publish(journal["target"], journal["old"], journal["candidate"])
            except AA2Error:
                # A clean external commit won the race before Git touched our candidate.
                current = self.workspaces.head(journal["target"])
                if current != journal["old"] and not self.workspaces.is_ancestor(journal["candidate"], current):
                    self.workspaces.clean_target(journal["target"])
                    self.save(rid, journal, "ready")
                    return await self.finish(rid)
                raise
            self.save(rid, journal, "published")
        elif journal["phase"] == "publishing":
            self.workspaces.recover_publication(journal)
            self.save(rid, journal, "published")
        if journal["phase"] in {"published", "runtime"}:
            self.workspaces.clean_target(journal["target"])
            if self.workspaces.head(journal["target"]) != journal["candidate"]:
                raise AA2Error("发布后的目标提交发生变化；保留用户提交，需人工核对后继续完成环境交接")
            if tree_hash(project) != journal["source_hash"]:
                raise AA2Error("主目录源码与验证版本不符")
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
                journal.update(runtime_failure=exc.evidence, old=journal["candidate"], incoming=journal["candidate"])
                self.save(rid, journal, "repairing_runtime")
                return await self.finish(rid)
            self.workspaces.clean_target(journal["target"])
            if self.workspaces.head(journal["target"]) != journal["candidate"] or tree_hash(project) != journal["source_hash"]:
                raise AA2Error("运行环境准备期间源码或目标提交发生变化，保留现场")
            resources = [str(env.prefix)] if env.prefix.is_relative_to(self.store.root / "environments") else []
            receipt = {"version": 1, "run_id": rid, "target": journal["target"], "candidate": journal["candidate"],
                       "tree": journal["candidate_tree"], "source_hash": journal["source_hash"],
                       "validation": journal["validation"], "outcome": journal["outcome"],
                       "reintegrations": journal["reintegrations"],
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
