"""Temporary Muyi-only prototype comparison; remove after the comparison.

Shared research is imported with its original receipt, never represented as a
new review. This entry point cannot publish human approval or continue delivery.
Native configuration/authentication still follow the ordinary provider path.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import signal
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace

from .artifacts import ArtifactStore
from .config import Config, ConfigStore
from .diagnostics import references
from .engine import Replan, WorkflowEngine
from .faults import StateConflict
from .models import IntakeResult
from .providers import ProviderRouter
from .research_acquisition import CURRENT
from .review_authority import source_identity
from .state_codec import checkpoint_get_db
from .state_connection import StateConnection, require_format
from .store import Store
from .util import digest, file_lock, write_json
from .visual_design import UIDesign
from .workflow_authority import WorkflowAuthority
from .workspace import WorkspaceManager, git, tree_hash

ARMS = ("codex", "agy")
INPUT_KEYS = ("capture", "clarify", "clarify_context", "provider_research", "ui_design/brief",
              "authority/protocol", "authority/accepted/provider_research")
DIRECTIVE = (
    "This is one isolated prototype comparison arm. Start a new design from the supplied blank baseline; "
    "do not inherit any earlier prototype, visual design or another arm's output. Use the same complete "
    "requirements and accepted research. Keep the interface simple: natural Chinese, minimal necessary "
    "explanations, no internal technical jargon, and no wall of text or buttons. Make supporting actions "
    "available progressively. The user will compare both prototypes; do not select a winner, approve a "
    "prototype or continue architecture/product implementation. Existing research captures are supplied "
    "locally; do not reacquire their sources or bypass any retained access refusal."
)


class SourceView:
    def __init__(self, db):
        self.db = db

    def get(self, rid, key, default=None):
        return checkpoint_get_db(self.db, rid, key, default)

    def submitted_answers(self, rid):
        return Store._submitted_answers(self.db, rid)

    def authority_get(self, rid, key):
        return self.submitted_answers(rid) if key == "authority/submitted-answers" else self.get(rid, key)


def frozen_input(project, rid):
    """Exact read-only source snapshot. This temporary importer supports this blank task only."""
    with sqlite3.connect((project / ".aa2/state.sqlite").as_uri() + "?mode=ro", uri=True,
                         factory=StateConnection) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        require_format(db)
        db.execute("BEGIN")
        source = SourceView(db)
        run = db.execute("SELECT status,stage FROM runs WHERE id=?", (rid,)).fetchone()
        if not run or tuple(run) != ("PAUSED", "provider_research"):
            raise StateConflict("Comparison requires the source run's paused research boundary")
        data = {key: source.get(rid, key) for key in INPUT_KEYS}
        accepted = data["authority/accepted/provider_research"] or {}
        authority = WorkflowAuthority(SimpleNamespace(store=source))
        if not authority.accepted(rid, "provider_research", data["provider_research"]):
            raise StateConflict("Shared research has no valid original acceptance")
        review_key = accepted["review_key"]
        data[review_key] = source.get(rid, review_key)
        if data[review_key]["basis"]["source_identity"] != source_identity(lambda key: source.authority_get(rid, key)):
            raise StateConflict("Shared source changed after its accepted review")
        # This run has no submitted requirement answers and only requirements.md.
        # Do not silently invent a general fork/import protocol for other cases.
        if source.submitted_answers(rid) or data["clarify_context"] is not None:
            raise StateConflict("Temporary blank-task comparison needs an explicit import for additional source answers/context")
        if any(source.get(rid, key) for key in ("prototype", "prototype/pending", "design", "plan", "prototype_approval",
                                               "recovery/active", "replanning/pending", "delivery/journal")):
            raise StateConflict("Comparison source is not the clean research-only boundary")
        baseline = source.get(rid, "baseline")
        entries = WorkspaceManager(project).entries(baseline)
        if set(entries) != {"requirements.md"} or entries["requirements.md"]["mode"] != "100644":
            raise StateConflict("Temporary comparison requires the verified requirements-only baseline")
        text = subprocess.check_output(["git", "-C", str(project), "show", baseline + ":requirements.md"]).decode()
        episode = source.get(rid, source.get(rid, CURRENT)["key"])
        if not episode.get("completed"):
            raise StateConflict("Shared research episode is unfinished")
        body = {"version": 1, "source_project": str(project), "source_run": rid,
                "source_baseline": baseline, "requirements_file": text, "checkpoints": data,
                "research_episode_hash": digest(episode), "original_attempts": len(episode["attempts"]),
                "directive": DIRECTIVE}
        return body


def copy_artifacts(source_project, source_run, target, data):
    """Copy only the owned, hash-verified referenced closure, not native caches or history."""
    source_root = source_project / ".aa2/artifacts"
    pending = {ref["hash"] for ref in references(data)
               if isinstance(ref.get("kind"), str) and type(ref.get("bytes")) is int}
    seen, edges = set(), set()
    with sqlite3.connect((source_project / ".aa2/state.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        while pending:
            sha = pending.pop()
            if sha in seen:
                continue
            if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
                raise StateConflict("Invalid shared artifact identity")
            kinds = [row[0] for row in db.execute("SELECT kind FROM artifact_kinds WHERE hash=?", (sha,))]
            if not kinds or not db.execute("SELECT 1 FROM artifact_owners WHERE hash=? AND run_id=?", (sha, source_run)).fetchone():
                raise StateConflict("Shared artifact lost original ownership")
            from .safe_files import read_regular
            payload = read_regular(source_root / sha[:2] / sha, 128 * 1024 * 1024)
            if digest(payload) != sha:
                raise StateConflict("Shared artifact bytes changed")
            for kind in kinds:
                if target.put(payload, kind)["hash"] != sha:
                    raise StateConflict("Imported artifact identity changed")
            seen.add(sha)
            for (child,) in db.execute("SELECT child FROM artifact_edges WHERE parent=?", (sha,)):
                if db.execute("SELECT 1 FROM artifact_kinds WHERE hash=?", (child,)).fetchone():
                    pending.add(child)
                    edges.add((sha, child))
    with target.store.connect() as db:
        db.executemany("INSERT OR IGNORE INTO artifact_edges(parent,child) VALUES(?,?)", sorted(edges))
    return sorted(seen)


class StageConfig(ConfigStore):
    """Invocation-local selection; ordinary native resolution and permissions remain intact."""
    def __init__(self, project, arm, agy, expected_hash):
        super().__init__(project)
        self.arm, self.agy, self.expected_hash = arm, agy, expected_hash
        self.stage = ContextVar("temporary_prototype_stage_" + arm, default=None)

    @contextmanager
    def at(self, stage):
        token = self.stage.set(stage)
        try:
            yield
        finally:
            self.stage.reset(token)

    def load(self):
        cfg = super().load()
        if digest(cfg.model_dump(mode="json")) != self.expected_hash:
            raise StateConflict("Frozen comparison configuration changed")
        if self.stage.get() != "prototype":
            return cfg
        body = cfg.model_dump(mode="json")
        if self.arm == "codex":
            spec = copy.deepcopy(body["providers"]["codex"])
            spec["extra_args"] = [*spec["extra_args"], "-m", "gpt-6.1-sol"]
        else:
            spec = copy.deepcopy(self.agy)
            spec["extra_args"] = [*spec["extra_args"], "--model", "gemini-3.8-flash-high"]
            level = body["efforts"].get("prototype", "max")
            selected = spec["profile_map"][level]
            if (not isinstance(selected, dict) or selected.get("file")
                    or selected.get("model") != "gemini-3.8-flash-high" or selected.get("effort") != "high"):
                raise StateConflict("AGY comparison requires its verified existing flash/high inline profile")
        body["providers"] = {self.arm: spec}  # No generation fallback to the other arm.
        body["active_provider"] = self.arm
        return Config.model_validate(body)

    def resolve(self, stage, provider=None):
        effective = super().resolve(stage, provider)  # Never bypass selected native input admission.
        if self.stage.get() == "prototype":
            expected = "gpt-6.1-sol" if self.arm == "codex" else "gemini-3.8-flash-high"
            if (effective["provider"] != self.arm or effective["model"] != expected
                    or self.arm == "agy" and effective["effort"] != "high"):
                raise StateConflict("Prototype comparison resolved a different model or effort")
        return effective


class ComparisonPause(asyncio.CancelledError):
    """Use normal controller cancellation cleanup while retaining the exact blocker."""


class ComparisonEngine(WorkflowEngine):
    def __init__(self, project, arm, agy, **kwargs):
        store = Store(project)
        run = store.current()
        source = store.get(run["id"], "comparison/input") if run else None
        validate_frozen(source)
        if digest(agy) != source["agy_configuration_hash"]:
            raise StateConflict("Frozen AGY comparison configuration changed")
        selected = StageConfig(project, arm, agy, source["configuration_hash"])
        selected.load()  # Check before startup can act on changed settings.
        super().__init__(project, **kwargs)
        self.config = selected
        if kwargs.get("question_handler") is None:
            self.question_handler = self._comparison_question
        if kwargs.get("agent") is None:
            self.agent = ProviderRouter(self.config, self.store, self.sandbox)

    async def start(self, *, run_id):
        # Reject retained out-of-scope re-entry before the ordinary loop can run
        # a legacy approval/build/task refresh. The original pending/permit stays.
        with file_lock(self.store.root / "controller.lock", blocking=False):
            pending = self.store.get(run_id, "replanning/pending")
            if pending and pending.get("stage") != "prototype":
                self._pause_boundary(run_id, pending.get("stage"), pending.get("reason"))
        return await super().start(run_id=run_id)

    async def _comparison_question(self, question):
        self._pause_boundary(question["run_id"], "user_input", "New input must be reconciled for both arms", question["id"])

    def _pause_boundary(self, rid, stage, reason, question=None):
        self.store.checkpoint(rid, "comparison/blocked", {"stage": stage, "reason": reason, "question": question})
        self.store.update(rid, status="PAUSED", pause_reason="comparison_shared_input_repair_required")
        raise ComparisonPause(reason)

    async def _resume_replan(self, rid):
        pending = self.store.get(rid, "replanning/pending", {})
        if pending.get("stage") != "prototype":
            self._pause_boundary(rid, pending.get("stage"), pending.get("reason"))
        return await super()._resume_replan(rid)

    async def _invoke(self, stage, *args, **kwargs):
        with self.config.at(stage):
            return await super()._invoke(stage, *args, **kwargs)

    async def _execute(self, rid, *, stop_after=None):
        source = self.store.get(rid, "comparison/input")
        validate_frozen(source)
        for key, value in source["checkpoints"].items():
            if self.store.get(rid, key) != value:
                raise StateConflict("Comparison shared input changed; retain both arms for reconciliation")
        if self.store.submitted_answers(rid):
            raise StateConflict("A new answer must be reconciled into both comparison arms")
        if self.store.get(rid, "prototype_approval") or self.store.get(rid, "prototype"):
            raise StateConflict("Comparison must not inherit or publish prototype approval")
        work = self.workspaces.allocate(rid, "integration", self.store.get(rid, "baseline"))
        self.resources.register(work, "worktree", rid)
        assessment = self.store.get(rid, "clarify")
        specification = IntakeResult.model_validate(assessment)
        raw = self.store.get(rid, "capture")
        original = self.artifacts.read(raw["artifact"])
        from .prototype_contract import prototype_context
        UIDesign(self.store, self.artifacts).brief(rid, assessment,
            prototype_context(self, rid)["prototype_requirement_source"])
        context = {"raw_requirements": original,
            "context_files": [str(self.artifacts.root / raw["artifact"]["hash"][:2] / raw["artifact"]["hash"])],
            "raw_requirements_truncated": False, "history": [], "project": self.contexts.build(work),
            "feedback": self.store.get(rid, "replan_feedback", {}), "specification": assessment,
            "provider_research": self.store.get(rid, "provider_research"),
            "controller_comparison": {"input_hash": source["input_hash"], "instruction": DIRECTIVE}}
        try:
            document, retrieved = await self._prepare_prototype(rid, work, specification, assessment, context, original,
                defer_human=True, frozen_design_candidates=source["design_candidates"])
        except Replan as error:
            if error.stage == "prototype":
                raise
            self.store.checkpoint(rid, "comparison/blocked", {"stage": error.stage, "reason": error.reason})
            self.store.update(rid, status="PAUSED", pause_reason="comparison_shared_input_repair_required")
            return self.store.run(rid)
        pending = self.store.get(rid, "prototype/pending")
        with self.store.connect() as db:
            calls = [json.loads(row[0]) for row in db.execute(
                "SELECT data FROM events WHERE run_id=? AND kind=? ORDER BY seq", (rid, "agent.started"))]
        calls = [{key: row[key] for key in ("attempt", "provider", "model", "effort", "config_hash")}
                 for row in calls if row.get("stage") == "prototype"]
        result = {"version": 1, "generation_calls": calls, "input_hash": source["input_hash"], "arm": self.config.arm,
            "status": "ready_for_user_comparison", "workspace": str(work), "source_hash": tree_hash(work),
            "document": document.model_dump(), "retrieved": retrieved, "review": pending["review"],
            "visuals": pending["visuals"], "browser_version": pending["browser_version"],
            "generation_receipt": self.store.get(rid, "invocation/prototype")}
        self.store.checkpoint(rid, "comparison/result", result)
        self.store.update(rid, status="PAUSED", stage="prototype", pause_reason="awaiting_user_comparison")
        return self.store.run(rid)


def validate_frozen(source):
    if not isinstance(source, dict) or source.get("input_hash") != digest({k: v for k, v in source.items() if k != "input_hash"}):
        raise StateConflict("Frozen comparison input changed")


def initialize_arm(project, arm, source, cfg):
    validate_frozen(source)
    if project.exists():
        store = Store(project)
        run = store.current()
        if (not run or store.get(run["id"], "comparison/input") != source
                or digest(ConfigStore(project).load().model_dump(mode="json")) != source["configuration_hash"]):
            raise StateConflict("Existing comparison arm does not match its frozen input; do not recreate it")
        return run["id"]
    project.mkdir(parents=True)
    write_json(ConfigStore(project).path, cfg.model_dump(mode="json"))
    (project / "requirements.md").write_text(source["requirements_file"], encoding="utf-8")
    workspaces = WorkspaceManager(project)
    workspaces.initialize()
    git(project, "add", "--", "requirements.md")
    git(project, "-c", "user.name=aa2", "-c", "user.email=aa2@localhost", "commit", "-m", "Import shared comparison requirements")
    store = Store(project)
    rid = store.admit(str(project / "requirements.md"), cfg.model_dump(mode="json"))["id"]
    copied = copy_artifacts(Path(source["source_project"]), source["source_run"], ArtifactStore(store.root), source["checkpoints"])
    store.checkpoints(rid, {**source["checkpoints"], "baseline": workspaces.committed_baseline(rid, "main"),
        "comparison/input": source, "comparison/import": {"source_run": source["source_run"],
        "source_project": source["source_project"], "input_hash": source["input_hash"], "artifacts": copied, "arm": arm}})
    authority = WorkflowAuthority(SimpleNamespace(store=store))
    review_key = source["checkpoints"]["authority/accepted/provider_research"]["review_key"]
    if (not authority.accepted(rid, "provider_research", store.get(rid, "provider_research"))
            or authority.identity(rid) != source["checkpoints"][review_key]["basis"]["source_identity"]):
        raise StateConflict("Imported shared acceptance failed verification")
    store.update(rid, status="PAUSED", stage="prototype", pause_reason="comparison_initialized")
    return rid


async def compare(project, rid, output, agy_config):
    project, output = project.resolve(), output.resolve()
    if output == project or project in output.parents or output in project.parents:
        raise StateConflict("Comparison output must be a separate project directory")
    if not (project / ".aa2/controller.lock").is_file():
        raise StateConflict("Source controller lock is missing")
    with file_lock(project / ".aa2/controller.lock", blocking=False):
        source = frozen_input(project, rid)
        cfg = ConfigStore(project).load()
        agy = Config.model_validate(json.loads(agy_config.read_text())).providers["agy"].model_dump(mode="json")
        if agy["kind"] != "antigravity":
            raise StateConflict("Comparison AGY provider is not the existing Antigravity transport")
        source["configuration_hash"] = digest(cfg.model_dump(mode="json"))
        source["agy_configuration_hash"] = digest(agy)
    output.mkdir(parents=True, exist_ok=True)
    with file_lock(output / "comparison.lock", blocking=False):
        manifest = output / "input.json"
        if manifest.exists():
            frozen = json.loads(manifest.read_text())
            validate_frozen(frozen)
            if {k: v for k, v in frozen.items() if k not in {"design_candidates", "input_hash"}} != source:
                raise StateConflict("Shared comparison input/configuration changed; preserve existing arms")
            source = frozen
        else:
            from .design import DesignCatalog
            from .sandbox import SandboxRunner
            # Freeze one ordinary catalog response, not one independently timed response per arm.
            sandbox = SandboxRunner(output / ".aa2", cfg.sandbox_binary)
            original = source["checkpoints"]["capture"]["artifact"]
            from .safe_files import read_regular
            payload = read_regular(project / ".aa2/artifacts" / original["hash"][:2] / original["hash"], 128 * 1024 * 1024)
            if digest(payload) != original["hash"]:
                raise StateConflict("Original captured input changed")
            source["design_candidates"] = await DesignCatalog(output / "catalog", sandbox).candidates(payload.decode())
            source["input_hash"] = digest(source)
            write_json(manifest, source)
        arms = {arm: initialize_arm(output / arm, arm, source, cfg) for arm in ARMS}
        codes = dict(zip(ARMS, await asyncio.gather(
            *(run_arm_process(output / arm, arm, arms[arm], agy_config) for arm in ARMS))))
        results = {arm: arm_result(output / arm, arms[arm], source["input_hash"]) for arm in ARMS}
        for arm, code in codes.items():
            if code:
                results[arm].update(status="incomplete", result=None, controller_exit_code=code)
        with file_lock(project / ".aa2/controller.lock", blocking=False):
            current = frozen_input(project, rid)
        changed = (any(source[key] != value for key, value in current.items())
            or digest(ConfigStore(project).load().model_dump(mode="json")) != source["configuration_hash"]
            or digest(Config.model_validate(json.loads(agy_config.read_text())).providers["agy"].model_dump(mode="json"))
                != source["agy_configuration_hash"])
        status = ("stale_shared_input" if changed else "awaiting_user_comparison"
                  if all(row["status"] == "ready_for_user_comparison" for row in results.values())
                  else "incomplete")
        write_json(output / "results.json", {"input_hash": source["input_hash"], "arms": results, "status": status})
        return results


async def run_arm_process(project, arm, rid, agy_config):
    # Browser runtime and environment are process-global in the ordinary engine.
    # Separate processes preserve that implementation and isolate both providers.
    with (project / ".aa2/comparison-controller.log").open("ab") as log:
        process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
            "auto_agents_2.temporary_prototype_compare", "--arm", arm, "--project", str(project),
            "--run", rid, "--agy-config", str(agy_config.resolve()),
            stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        try:
            return await process.wait()
        except asyncio.CancelledError:
            if process.returncode is None:
                process.send_signal(signal.SIGINT)
            await process.wait()
            raise


def arm_result(project, rid, input_hash):
    store = Store(project)
    run = store.run(rid)
    result = store.get(rid, "comparison/result")
    ready = (run["status"] == "PAUSED" and run["stage"] == "prototype"
             and run["data"].get("pause_reason") == "awaiting_user_comparison"
             and result and result.get("input_hash") == input_hash
             and result.get("source_hash") == tree_hash(Path(result["workspace"]))
             and not store.get(rid, "prototype_approval") and not store.get(rid, "prototype"))
    return {"run_id": rid, "status": "ready_for_user_comparison" if ready else "incomplete",
            "run": {key: run[key] for key in ("status", "stage", "updated")},
            "result": result if ready else None, "blocked": store.get(rid, "comparison/blocked")}


async def run_single_arm(project, arm, rid, agy_config):
    agy = Config.model_validate(json.loads(agy_config.read_text())).providers["agy"].model_dump(mode="json")
    engine = ComparisonEngine(project, arm, agy)
    try:
        await engine.start(run_id=rid)
    except ComparisonPause:
        pass  # Exact question/pending source and all counters remain in this arm.
    return arm_result(project, rid, engine.store.get(rid, "comparison/input")["input_hash"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--agy-config", type=Path, required=True)
    parser.add_argument("--arm", choices=ARMS)
    args = parser.parse_args()
    if args.arm:
        result = asyncio.run(run_single_arm(args.project.resolve(), args.arm, args.run, args.agy_config))
        print(json.dumps({"run_id": result["run_id"], "status": result["status"]}))
    else:
        if args.output is None:
            parser.error("--output is required for the coordinator")
        result = asyncio.run(compare(args.project, args.run, args.output, args.agy_config))
        print(json.dumps({arm: {"run_id": value["run_id"], "status": value["status"]}
                          for arm, value in result.items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
