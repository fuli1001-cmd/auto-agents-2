"""Capture history at actual engine/recovery calls, never inject collector output."""
import asyncio
import copy
import json

import pytest
from test_decisions import ApprovingAgent
from test_final_history_scope import CONFIGS
from test_plan_patch_evidence_handoff import HandoffAgent, setup_handoff
from test_review_evidence import commit
from test_workflow import CodingAgent, engine

from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.models import Change, Finding, ReviewResult
from auto_agents_2.recovery import Diagnosis
from auto_agents_2.util import digest, write_json
from auto_agents_2.workspace import git, tree_hash

CONFIG = "frontend/vite.config.mjs"
TEST = "tests/test_calc.py"
ASSERTIONS = "from calc import add\n\ndef test_sum():\n    assert add(2, 3) == 5\n"
UNAPPLIED = "export default {base: '/never-applied/'};\n"
FAILED = "export default {base: '/checked-failed/'};\n"
REPAIRED = "export default {base: '/repaired/'};\n"


def capture(workflow, stage, context, workspace):
    history = context["source_history"]
    manifest = workflow.artifacts.json(history["artifact"])
    assert history["source_hash"] == tree_hash(workspace)
    view = ContextPlanner(workflow.artifacts).prepare(stage, context, workspace).payload
    materials = view["evidence_materials"]
    if isinstance(materials, dict):
        materials = workflow.artifacts.json(materials["artifact"])
    mounted = {ref["hash"] for ref in materials}
    refs = [entry["content"] for entry in manifest["current"].values()]
    for snapshot in [*manifest["snapshots"], *manifest["observations"]]:
        refs.extend(entry["content"] for entry in snapshot["files"].values())
        refs.extend(change["diff"] for change in snapshot["changes_to_current"] if change.get("diff"))
    refs.extend(proposal["content"] for proposal in manifest["proposals"])
    for ref in refs:
        raw = workflow.artifacts.bytes(ref)
        assert digest(raw) == ref["hash"] and len(raw) == ref["bytes"]
        assert ref["hash"] in mounted
    return copy.deepcopy(history), manifest, mounted


async def test_rejected_and_failed_candidates_survive_real_repair_pause_resume_and_final_review(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    requirements = tmp_path / "requirements.md"
    requirements.write_text("Add numbers, preserve tests and configuration history")
    observations, calls = [], []

    class Agent(ApprovingAgent):
        implementations = 0
        interrupted = False

        async def invoke(self, stage, context, schema, workspace, rid, operation, fresh=False):
            if "source_history" in context:
                item = capture(workflow, stage, context, workspace)
                observations.append((stage, operation, item))
                _, manifest, mounted = item
                assert manifest["scope"] == sorted([CONFIG, "calc.py", "tests"])
                baseline = next(row for row in manifest["snapshots"] if row["anchor"] == "baseline")
                assert baseline["public_tree"][CONFIG]["body_status"] == "included"
                assert workflow.artifacts.read(baseline["files"][CONFIG]["content"]) == CONFIGS[CONFIG]
                assert baseline["public_tree"]["unrelated.mjs"]["body_status"] == "outside_scope"
                assert ".env" not in baseline["public_tree"] and "private/helper.py" not in manifest["current"]
                assert all(value not in mounted for value in forbidden_hashes)
                source_refs = [row["content"] for row in manifest["current"].values()]
                source_refs += [row["content"] for snapshot in [*manifest["snapshots"], *manifest["observations"]]
                                for row in snapshot["files"].values()]
                assert digest(UNAPPLIED) not in {ref["hash"] for ref in source_refs}
                for proposal in manifest["proposals"]:
                    assert proposal["status"] == "proposal_only"
                    assert proposal["recorded_source_hash"] is None
                    assert proposal["checks"] == proposal["commands"] == []
            calls.append((stage, operation))
            if operation.endswith("/test-change-review") and self.implementations == 1:
                assert (workspace / TEST).read_text() == ASSERTIONS
                assert (workspace / CONFIG).read_text() == CONFIGS[CONFIG]
                return ReviewResult(approved=False, summary="Reject weakened assertion", findings=[Finding(
                    severity="blocking", location=TEST, description="Removes sum assertion",
                    evidence="Proposed assert True", resolution="Retain the assertion and fix addition")])
            if operation.endswith("/review") and context.get("task") and self.implementations == 3:
                if not self.interrupted:
                    self.interrupted = True
                    raise asyncio.CancelledError()
            result = await super().invoke(stage, context, schema, workspace, rid, operation, fresh)
            if stage == "plan":
                result.tasks[0].allowed_paths = sorted([CONFIG, "calc.py", "tests"])
            if stage == "implement":
                self.implementations += 1
                if self.implementations == 1:
                    result.changes[1].content = "def test_sum(): assert True\n"
                elif self.implementations == 2:
                    result.changes[0].content = "def add(a, b):\n    return a - b\n"
                result.changes.append(Change(path=CONFIG, content={1: UNAPPLIED, 2: FAILED, 3: REPAIRED}[
                    self.implementations]))
            return result

    agent = Agent()
    workflow = engine(project, agent)
    private = {".env": "SECRET_FIXTURE", "private/helper.py": "PRIVATE_FIXTURE", "unrelated.mjs": "OUTSIDE_FIXTURE"}
    for name, body in {CONFIG: CONFIGS[CONFIG], TEST: ASSERTIONS, **private}.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    cfg = workflow.config.load().model_dump(mode="json")
    cfg["sensitive_paths"] = ["private/**"]
    write_json(workflow.config.path, cfg)
    forbidden_hashes = {digest(body) for body in private.values()}
    git(project, "add", "--force", ".env")
    commit(project, "Historical assertions and configuration")
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(requirements))
    paused = workflow.store.current()
    rid = paused["id"]
    assert paused["status"] == "PAUSED" and agent.implementations == 3
    assert workflow.store.get(rid, "develop/done") in (None, [])
    rounds = workflow.store.get(rid, "rounds/T1")
    diagnoses = workflow.store.get(rid, "recovery/budgets/diagnosis/T1")
    assert rounds["round"] == 3 and diagnoses["round"] == 2
    failed_sources = [history[0]["source_hash"] for stage, _, history in observations
                      if stage == "recovery_diagnosis" and history[1]["current"].get(CONFIG, {}).get(
                          "content", {}).get("hash") == digest(FAILED)]
    assert failed_sources
    workflow = engine(project, agent)
    result = await workflow.start()
    assert result["status"] == "SUCCEEDED" and result["data"]["outcome"] == "DELIVERED"
    assert agent.implementations == 3 and workflow.store.get(rid, "rounds/T1")["round"] == rounds["round"]
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/T1") == diagnoses
    assert not workflow.store.pending(rid)
    final = next(item for _, op, item in observations if op == "final-review")
    history = final[1]
    proposal = next(p for p in history["proposals"]
                    if UNAPPLIED in json.dumps(workflow.artifacts.json(p["content"])).replace("\\n", "\n"))
    assert proposal["status"] == "proposal_only"
    observed = next(row for row in history["observations"] if row["source_hash"] in failed_sources)
    assert observed["status"] == "observed_only"
    assert workflow.artifacts.read(observed["files"][CONFIG]["content"]) == FAILED
    diff = next(row["diff"] for row in observed["changes_to_current"] if row["path"] == CONFIG)
    assert "checked-failed" in workflow.artifacts.read(diff) and "repaired" in workflow.artifacts.read(diff)
    assert (project / TEST).read_text() == ASSERTIONS and (project / CONFIG).read_text() == REPAIRED
    assert sum(stage == "implement" for stage, _ in calls) == 3


@pytest.mark.parametrize("reintegrate", [False, True])
async def test_real_no_plan_reuse_and_reintegration_choose_only_protected_history(tmp_path, reintegrate):
    project = tmp_path / "project"
    project.mkdir()
    requirements = tmp_path / "requirements.md"
    requirements.write_text("Add numbers")
    observed = []

    class Agent(CodingAgent):
        async def invoke(self, stage, context, schema, workspace, rid, operation, fresh=False):
            if stage == "verify":
                _, manifest, mounted = capture(workflow, stage, context, workspace)
                observed.append(operation)
                assert workflow.store.get(rid, "plan") is None and manifest["scope"] == []
                snap = next(s for s in manifest["snapshots"] if s["anchor"] == "baseline")
                assert snap["public_tree"][CONFIG]["body_status"] == "outside_scope"
                assert snap["files"][TEST]["content"]["hash"] in mounted
                if reintegrate and operation == "no-change-review":
                    (project / "user.txt").write_text("concurrent user work\n")
                    commit(project, "Concurrent user work")
            return await super().invoke(stage, context, schema, workspace, rid, operation, fresh)

    workflow = engine(project, Agent())
    for name, body in {"calc.py": "def add(a, b):\n    return a + b\n", TEST: ASSERTIONS, CONFIG: CONFIGS[CONFIG]}.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    commit(project, "Already implemented external baseline")
    result = await workflow.start(str(requirements))
    assert result["status"] == "SUCCEEDED" and result["data"]["outcome"] == "NO_CHANGES"
    assert observed == ["no-change-review", *(["reintegration-review/1"] if reintegrate else [])]
    if reintegrate:
        assert (project / "user.txt").read_text() == "concurrent user work\n"


async def test_real_diagnosis_and_frozen_patch_review_share_scope_across_restart(tmp_path):
    captured = []

    class Agent(HandoffAgent):
        async def invoke(self, stage, context, schema, workspace, rid, operation, fresh=False):
            if "source_history" in context:
                history, manifest, mounted = capture(self.workflow, stage, context, workspace)
                captured.append((operation, history, manifest))
                assert manifest["scope"] == ["part3.py", "tests/test_3.py"]
                baseline = next(row for row in manifest["snapshots"] if row["anchor"] == "baseline")
                assert baseline["files"]["part3.py"]["content"]["hash"] in mounted
                assert baseline["public_tree"]["part1.py"]["body_status"] == "outside_scope"
                if schema is Diagnosis:
                    progress = self.workflow.recovery_policy.progress.inputs(rid,
                        record, workspace, old)
                    assert progress["source_history"] == history
            return await super().invoke(stage, context, schema, workspace, rid, operation, fresh)

    agent = Agent(assert_stage="both", interrupt=True)
    workflow, rid, work, old, record = setup_handoff(tmp_path, agent)
    for name in ("part3.py", "part1.py"):
        (work / name).write_text(f"# original {name}\n")
    commit(work, "Historical task inputs")
    workflow.store.checkpoint(rid, "baseline", git(work, "rev-parse", "HEAD"))
    with pytest.raises(asyncio.CancelledError):
        await workflow.recovery.resume(rid)
    frozen = next(history for op, history, _ in captured if "/patch/" in op or op.startswith("plan-patch/"))
    frozen_bytes = workflow.artifacts.bytes(frozen["artifact"])
    assert workflow.store.get(rid, "plan") == old.model_dump()
    workflow = engine(tmp_path, agent)
    agent.workflow = workflow
    await workflow.recovery.resume(rid)
    assert len([stage for stage, _ in agent.views if stage == "plan"]) == 1
    assert len([stage for stage, _ in agent.views if stage == "review"]) == 2
    assert workflow.store.get(rid, "plan")["tasks"][2]["checks"][0]["expected_tests"] == 40
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/T3")["round"] == 1
    assert workflow.artifacts.bytes(frozen["artifact"]) == frozen_bytes
    assert all(view["source_history"] == frozen for _, view in agent.views)
    assert not workflow.store.get(rid, "develop/done")
