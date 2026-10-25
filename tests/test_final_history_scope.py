"""Integrated reviewers must retain the approved task scope for historical bodies."""

import json

import pytest
from authority_review_fixtures import authority_review, seed_original
from test_review_evidence import commit
from test_workflow import CodingAgent, engine

from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.models import Change
from auto_agents_2.util import digest
from auto_agents_2.workspace import git, tree_hash

CONFIGS = {
    "frontend/vite.config.mjs": "export default {base: '/before/'};\n",
    "frontend/vitest.config.mjs": "export default {test: {setupFiles: './tests/setup.ts'}};\n",
    "support/config_helper.py": "value = 'historical helper'\n",
}


@pytest.mark.parametrize("route", ["final", "reuse", "reintegration"])
async def test_integrated_review_keeps_planned_configuration_history(tmp_path, route):
    project = tmp_path / "project"
    project.mkdir()
    requirements = tmp_path / "requirements.md"
    requirements.write_text("Add numbers, preserve historical configuration evidence")
    observed = {}

    class Agent(CodingAgent):
        advanced = False

        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            result = await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)
            if stage == "plan":
                result.tasks[0].allowed_paths.extend(CONFIGS)
            if stage == "implement":
                result.changes.append(Change(path="frontend/vite.config.mjs",
                                             content="export default {base: '/after/'};\n"))
            if stage == "verify":
                history = context["source_history"]
                manifest = workflow.artifacts.json(history["artifact"])
                prepared = ContextPlanner(workflow.artifacts).prepare(stage, context, workspace)
                observed[operation] = (history, manifest, prepared)
                assert history["source_hash"] == tree_hash(workspace)
                if route == "reintegration" and operation == "final-review" and not self.advanced:
                    self.advanced = True
                    (project / "user.txt").write_text("Concurrent unrelated work\n")
                    commit(project, "Concurrent user work")
            return result

    agent = Agent()
    workflow = engine(project, agent)
    for name, text in {**CONFIGS, "frontend/tests/setup.ts": "// retained setup\n",
                       "support/unselected.mjs": "// public but outside the approved task scope\n"}.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    commit(project, "Historical configuration")
    first = await workflow.start(str(requirements))
    assert first["status"] == "SUCCEEDED"
    operation = "final-review"
    if route == "reuse":
        workflow = engine(project, agent)
        reused = await workflow.start(str(requirements))
        assert reused["status"] == "SUCCEEDED" and reused["data"]["outcome"] == "NO_CHANGES"
        operation = "no-change-review"
    elif route == "reintegration":
        operation = "reintegration-review/1"
        assert workflow.store.get(first["id"], "delivery/receipt")["reintegrations"] == 1
        assert (project / "user.txt").read_text() == "Concurrent unrelated work\n"
    _, manifest, prepared = observed[operation]
    mounted = {ref["hash"] for ref in prepared.payload["evidence_materials"]}
    baseline = next(s for s in manifest["snapshots"] if s["anchor"] == "baseline")
    for name in CONFIGS:
        assert baseline["public_tree"][name]["body_status"] == "included", (operation, name)
        ref = baseline["files"][name]["content"]
        raw = workflow.artifacts.bytes(ref)
        assert digest(raw) == ref["hash"] and len(raw) == ref["bytes"]
        assert raw == git(project, "show", baseline["commit"] + ":" + name).encode() + b"\n"
        assert ref["hash"] in mounted
        assert manifest["current"][name]["content"]["hash"] in mounted
    assert baseline["root_tree"] == git(project, "rev-parse", baseline["commit"] + "^{tree}")
    assert baseline["public_tree"]["support/unselected.mjs"]["body_status"] == "outside_scope"
    assert "support/unselected.mjs" not in baseline["files"]
    assert digest((project / "support/unselected.mjs").read_bytes()) not in mounted
    if route != "reuse":
        change = next(c for c in baseline["changes_to_current"] if c["path"] == "frontend/vite.config.mjs")
        assert change["diff"]["hash"] in mounted
        assert "'/before/'" in workflow.artifacts.read(change["diff"])
        assert "'/after/'" in workflow.artifacts.read(change["diff"])
    assert json.dumps(manifest["scope"]) == json.dumps(sorted(["calc.py", "tests", *CONFIGS]))


def plan_for(*scopes):
    from auto_agents_2.models import Command, PlanResult, TaskSpec
    check = Command(argv=["python", "-m", "pytest", "tests"], expected_tests=1)
    return PlanResult(summary="Reviewed fixture", document="# Plan",
        mode="implementation" if scopes else "verification_only", final_checks=[check], tasks=[
        TaskSpec(id=f"T{i}", title="scope", description="Keep configuration evidence", requirement_ids=["R1"],
                 allowed_paths=list(paths), acceptance=["Preserve historical assertions"], checks=[check])
        for i, paths in enumerate(scopes, 1)])


def history_fixture(tmp_path):
    from test_source_history_inventory import recorded_tree
    workflow, rid, _, original = recorded_tree(tmp_path)
    for name, text in CONFIGS.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    commit(tmp_path, "Configuration history")
    workflow.store.checkpoint(rid, "baseline", git(tmp_path, "rev-parse", "HEAD"))
    return workflow, rid, {**original, **CONFIGS}


@pytest.mark.parametrize("no_plan", [True, False])
def test_missing_or_taskless_plan_does_not_infer_history_scope(tmp_path, no_plan):
    from auto_agents_2.source_history import SourceHistory
    workflow, rid, _ = history_fixture(tmp_path)
    history = SourceHistory(workflow)
    plan = None if no_plan else plan_for()
    scoped = history.collect_protected(rid, tmp_path) if no_plan else history.collect_plan(rid, tmp_path, plan)
    assert scoped == history.collect_protected(rid, tmp_path)
    manifest = workflow.artifacts.json(scoped["artifact"])
    assert manifest["scope"] == []
    assert manifest["snapshots"][0]["public_tree"]["frontend/vite.config.mjs"]["body_status"] == "outside_scope"


def test_plan_scope_changes_cache_without_changing_old_frozen_history(tmp_path):
    from auto_agents_2.source_history import SourceHistory
    workflow, rid, _ = history_fixture(tmp_path)
    history = SourceHistory(workflow)
    old = history.collect_protected(rid, tmp_path)
    old_bytes = workflow.artifacts.bytes(old["artifact"])
    workflow.store.checkpoint(rid, "presentation/final-review/frozen", {"source_history": old})
    plan = plan_for(["frontend/vite.config.mjs"], ["support/config_helper.py", "frontend/vite.config.mjs"])
    before_plan = plan.model_dump()
    expanded = history.collect_plan(rid, tmp_path, plan)
    manifest = workflow.artifacts.json(expanded["artifact"])
    assert manifest["scope"] == ["frontend/vite.config.mjs", "support/config_helper.py"]
    assert manifest["snapshots"][0]["public_tree"]["frontend/vite.config.mjs"]["body_status"] == "included"
    assert manifest["snapshots"][0]["public_tree"]["support/config_helper.py"]["body_status"] == "included"
    assert expanded["artifact"] != old["artifact"]
    assert expanded["source_hash"] == old["source_hash"] and expanded["anchors"] == old["anchors"]
    assert history.collect_plan(rid, tmp_path, plan) == expanded
    # A later, narrower approved plan must not inherit previously admitted bodies.
    narrower = history.collect_plan(rid, tmp_path, plan_for(["support/config_helper.py"]))
    current = workflow.artifacts.json(narrower["artifact"])
    assert current["snapshots"][0]["public_tree"]["frontend/vite.config.mjs"]["body_status"] == "outside_scope"
    assert "frontend/vite.config.mjs" not in current["current"]
    assert plan.model_dump() == before_plan
    assert workflow.artifacts.bytes(old["artifact"]) == old_bytes
    assert workflow.store.get(rid, "presentation/final-review/frozen") == {"source_history": old}


def test_plan_scope_preserves_sensitive_type_and_reference_boundaries(tmp_path):
    from auto_agents_2.source_history import SourceHistory
    from auto_agents_2.util import write_json
    workflow, rid, _ = history_fixture(tmp_path)
    private = {".env": "SECRET_PATH_FIXTURE_PLACEHOLDER", "private/helper.mjs": "PRIVATE_PATH_FIXTURE_PLACEHOLDER"}
    for name, text in private.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (tmp_path / "support/binary.mjs").write_bytes(b"\xff\xfe")
    (tmp_path / "support/link.mjs").symlink_to("../.env")
    unrelated = workflow.artifacts.put("UNRELATED_PRIVATE_RECORD_FIXTURE", "private_fixture")
    (tmp_path / "support/reference.mjs").write_text(json.dumps({"report": unrelated}))
    config = workflow.config.load().model_dump(mode="json")
    config["sensitive_paths"] = ["private/**"]
    write_json(workflow.config.path, config)
    git(tmp_path, "add", "--force", *private)
    commit(tmp_path, "Adversarial scope fixture")
    workflow.store.checkpoint(rid, "baseline", git(tmp_path, "rev-parse", "HEAD"))
    plan = plan_for([*CONFIGS, *private, "support/binary.mjs", "support/link.mjs", "support/reference.mjs"])
    before_plan = plan.model_dump()
    history = SourceHistory(workflow).collect_plan(rid, tmp_path, plan)
    manifest = workflow.artifacts.json(history["artifact"])
    snap = manifest["snapshots"][0]
    assert snap["private_paths_omitted"]
    for name in private:
        assert name not in snap["public_tree"] and name not in snap["files"] and name not in manifest["current"]
    assert snap["public_tree"]["support/binary.mjs"]["body_status"] == "non_utf8"
    assert snap["public_tree"]["support/link.mjs"]["body_status"] == "unsupported_type"
    assert "support/binary.mjs" not in snap["files"] and "support/link.mjs" not in snap["files"]
    view = ContextPlanner(workflow.artifacts).prepare("verify", {"source_history": history}, tmp_path)
    mounted = {ref["hash"] for ref in view.payload["evidence_materials"]}
    assert unrelated["hash"] not in mounted
    assert all(digest(text) not in mounted for text in private.values())
    assert snap["files"]["support/reference.mjs"]["content"]["hash"] in mounted
    assert plan.model_dump() == before_plan


def test_expanded_plan_scope_cannot_conceal_a_missing_historical_anchor(tmp_path):
    from auto_agents_2.source_history import SourceHistory
    workflow, rid, _ = history_fixture(tmp_path)
    missing = f"refs/aa2/{rid}/replans/0/unavailable"
    workflow.store.checkpoint(rid, "replan_feedback", {"recovery_refs": [missing]})
    history = SourceHistory(workflow).collect_plan(rid, tmp_path, plan_for(CONFIGS))
    manifest = workflow.artifacts.json(history["artifact"])
    assert history["status"] == "incomplete" and history["missing"] == [{"anchor": missing, "status": "missing"}]
    assert manifest["snapshots"][0]["public_tree"]["frontend/vite.config.mjs"]["body_status"] == "included"


async def test_new_history_scope_invalidates_prior_final_invocation_without_rewriting_artifacts(tmp_path):
    from auto_agents_2.models import ReviewResult
    from auto_agents_2.source_history import SourceHistory
    workflow, rid, _ = history_fixture(tmp_path)
    seed_original(workflow, rid)
    calls = []

    class Reviewer:
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            manifest = workflow.artifacts.json(context["source_history"]["artifact"])
            included = manifest["snapshots"][0]["public_tree"]["frontend/vite.config.mjs"]["body_status"] == "included"
            calls.append(included)
            return schema.model_validate(authority_review(context, approved=included,
                summary="History present" if included else "History unavailable").model_dump())

    workflow.agent = Reviewer()
    histories = SourceHistory(workflow)
    old = histories.collect_protected(rid, tmp_path)
    first = await workflow._invoke("verify", {"source_history": old}, ReviewResult, tmp_path,
                                        rid, "final-review", fresh=True)
    original = workflow.store.get(rid, "invocation/final-review")
    original_bytes = workflow.artifacts.bytes(original["artifact"])
    assert not first.approved
    current = histories.collect_plan(rid, tmp_path, plan_for(CONFIGS))
    second = await workflow._invoke("verify", {"source_history": current}, ReviewResult, tmp_path,
                                         rid, "final-review", fresh=True)
    repeated = await workflow._invoke("verify", {"source_history": current}, ReviewResult, tmp_path,
                                           rid, "final-review", fresh=True)
    assert second.approved and repeated == second and calls == [False, True]
    assert workflow.artifacts.bytes(original["artifact"]) == original_bytes
    assert workflow.store.get(rid, "invocation/final-review")["identity"] != original["identity"]
