"""Declared dependencies and runtime caches retain consistent verification identities."""

import sys

import pytest
from authority_review_fixtures import authority_review, seed_original
from test_build_contracts import fixture, service
from test_workflow import CodingAgent, engine

from auto_agents_2.builds import output_hashes
from auto_agents_2.errors import AA2Error
from auto_agents_2.evidence import CHECKER_VERSION, input_identity, reusable
from auto_agents_2.models import Command, ImplementationResult, PlanResult, TaskSpec
from auto_agents_2.recovery import RecoveryNeeded
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.verification import VerificationService, passed
from auto_agents_2.workspace import git, tree_hash


def dependencies(work):
    package = work / "frontend/node_modules/example"
    package.mkdir(parents=True)
    (package / "index.js").write_text("export const value = 1;\n")
    return package.parent


@pytest.mark.parametrize("declared", [None, "frontend/node_modules", "frontend", "dependency-link"])
@pytest.mark.parametrize("cache", [".cache", ".vite", ".vite-temp", ".tmp"])
def test_runtime_cache_does_not_invalidate_declared_or_discovered_dependencies(tmp_path, declared, cache):
    modules = dependencies(tmp_path)
    (tmp_path / "dependency-link").symlink_to(modules, target_is_directory=True)
    command = Command(argv=["true"], inputs=[declared] if declared else [])
    before = input_identity(tmp_path, command)
    folder = modules / cache
    folder.mkdir()
    (folder / "compiled.js").write_text("generated runtime cache")
    assert input_identity(tmp_path, command) == before
    (modules / "example/index.js").write_text("export const value = 2;\n")
    assert input_identity(tmp_path, command) != before


def test_explicit_cache_file_and_package_source_with_cache_name_are_still_inputs(tmp_path):
    modules = dependencies(tmp_path)
    runtime = modules / ".cache/explicit.txt"
    runtime.parent.mkdir()
    runtime.write_text("explicit input")
    command = Command(argv=["true"], inputs=["frontend/node_modules/.cache/explicit.txt"])
    before = input_identity(tmp_path, command)
    runtime.write_text("changed explicit input")
    assert input_identity(tmp_path, command) != before
    internal = modules / "example/.cache/source.js"
    internal.parent.mkdir()
    internal.write_text("package source")
    command = Command(argv=["true"])
    before = input_identity(tmp_path, command)
    internal.write_text("changed package source")
    assert input_identity(tmp_path, command) != before


def test_cache_name_inside_a_build_is_still_immutable_output(tmp_path):
    output = tmp_path / "frontend/dist/.cache/app.js"
    output.parent.mkdir(parents=True)
    output.write_text("production code")
    command = Command(argv=["build"], cwd="frontend", outputs=["frontend/dist"])
    before = output_hashes(command, tmp_path)
    output.write_text("changed production code")
    assert output_hashes(command, tmp_path) != before


async def test_later_native_check_cache_write_keeps_earlier_pytest_evidence_valid(tmp_path):
    build, check = fixture(tmp_path)
    modules = dependencies(tmp_path)
    check = check.model_copy(update={"inputs": [*check.inputs, "frontend/node_modules"]})
    populate = Command(argv=[sys.executable, "-c", "from pathlib import Path; "
        "Path('node_modules/.cache/generated.txt').write_text('runtime cache')"], cwd="frontend")
    verifier, environment = service(tmp_path)
    results = await verifier.run([build, check, populate], tmp_path, environment)
    assert passed(results) and results[1].tests == 1
    assert (modules / ".cache/generated.txt").is_file()
    assert reusable([build, check, populate], results, tmp_path, environment, verifier.artifacts).allowed
    old = [row.model_copy(update={"checker_version": "verification-v10"}) for row in results]
    assert not reusable([build, check, populate], old, tmp_path, environment, verifier.artifacts).allowed
    assert all(row.checker_version == "verification-v10" for row in old)
    (modules / "example/index.js").write_text("changed dependency")
    invalid = reusable([build, check, populate], results, tmp_path, environment, verifier.artifacts)
    assert not invalid.allowed and "dependencies changed" in invalid.reason


async def task_fixture(tmp_path):
    build, check = fixture(tmp_path)
    workflow = engine(tmp_path, CodingAgent())
    workflow.sandbox = SandboxRunner(workflow.store.root)
    workflow.verifier = VerificationService(workflow.store.root, workflow.sandbox, workflow.resources)
    rid = workflow.store.admit("retained candidate verification", {})["id"]
    seed_original(workflow, rid)
    check = check.model_copy(update={"inputs": [*check.inputs, "frontend/node_modules"]})
    git(tmp_path, "add", "frontend", "backend")
    git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "Retained candidate fixture")
    base = workflow.workspaces.committed_baseline(rid, "main")
    work = workflow.workspaces.allocate(rid, "retained-task", base)
    dependencies(work)
    task = TaskSpec(id="T1", title="verify delivery", description="Verify the retained source",
        requirement_ids=["R1"], allowed_paths=["frontend/source.txt", "backend/test_app.py"],
        acceptance=["current build is consumed by the backend"], checks=[check])
    plan = PlanResult(summary="verification", document="Verify delivery", tasks=[task],
                      runtime_prepare=[build], final_checks=[check])
    workflow.store.checkpoints(rid, {"plan": plan.model_dump(), "rounds/T1": {"round": 6},
        "task_state/T1": {"version": 1, "revision": 1, "implementation": "in_progress", "evidence": "absent"},
        "develop/task/T1": {"phase": "applied", "workspace": str(work), "source_hash": tree_hash(work),
            "implementation": ImplementationResult(summary="Retained implementation").model_dump()}})
    return workflow, rid, work, base, task, plan


@pytest.mark.parametrize("when", ["before_review", "during_review"])
@pytest.mark.parametrize("changed", ["dependency", "output", "source"])
async def test_task_review_drift_preserves_candidate_and_routes_recoverable_inputs_to_recheck(tmp_path, when, changed):
    workflow, rid, work, base, task, plan = await task_fixture(tmp_path)
    target = work / {"dependency": "frontend/node_modules/example/index.js",
                     "output": "frontend/dist/index.html", "source": "frontend/source.txt"}[changed]
    def mutate():
        target.write_text("injected input change")
    reviews = []
    async def review(stage, *args, **kwargs):
        assert stage == "review", "Evidence drift cannot spend another implementation round"
        reviews.append(stage)
        if when == "during_review":
            mutate()
        return authority_review(args[0], approved=True, summary="Review response is a proposal")
    workflow.agent.invoke = review
    if when == "before_review":
        checks = workflow._checks
        async def change_after_checks(*args, **kwargs):
            results = await checks(*args, **kwargs)
            mutate()
            return results
        workflow._checks = change_after_checks
    if changed == "source":
        with pytest.raises(AA2Error) as caught:
            await workflow._task(rid, task, base, plan, {"specification": {}})
        assert not isinstance(caught.value, RecoveryNeeded)
        assert "changed" in str(caught.value)
    else:
        with pytest.raises(RecoveryNeeded) as caught:
            await workflow._task(rid, task, base, plan, {"specification": {}})
        record = caught.value.failure
        assert record.kind == "evidence" and record.step == "verification_inputs"
        assert "changed" in record.evidence["verification_error"]
        assert workflow.store.get(rid, "develop/task/T1")["phase"] == "reverify"
        if when == "during_review":
            assert workflow.store.get(rid, "develop/task/T1")["review"]["approved"]
        workflow.recovery.submit(rid, record)
        await workflow.recovery.resume(rid)
        assert workflow.store.get(rid, "recovery/active") is None
        assert workflow.store.get(rid, "develop/task/T1")["phase"] == "reverify"
    assert target.read_text() == "injected input change"
    assert workflow.store.get(rid, "rounds/T1")["round"] == 6
    assert workflow.store.get(rid, "task_state/T1")["implementation"] != "accepted"
    assert reviews == ([] if when == "before_review" else ["review"])


async def test_old_tested_candidate_rechecks_without_regenerating_implementation(tmp_path):
    workflow, rid, work, base, task, plan = await task_fixture(tmp_path)
    environment = await workflow._environment(work, rid, plan)
    commands = workflow._task_commands(rid, task, work, plan)
    results = await workflow._checks(rid, commands, work, environment, task)
    saved = workflow.store.get(rid, "develop/task/T1")
    workflow.store.checkpoint(rid, "develop/task/T1", {**saved, "phase": "tested",
        "checks": [row.model_copy(update={"checker_version": "verification-v10"}).model_dump() for row in results]})
    calls = []
    checks = workflow._checks
    async def counted_checks(*args, **kwargs):
        calls.append("checks")
        return await checks(*args, **kwargs)
    workflow._checks = counted_checks
    async def review(stage, *args, **kwargs):
        assert stage == "review"
        calls.append(stage)
        return authority_review(args[0], approved=True, summary="Current evidence was inspected")
    workflow.agent.invoke = review
    commit = await workflow._task(rid, task, base, plan, {"specification": {}})
    assert commit and calls == ["checks", "review"]
    assert workflow.store.get(rid, "rounds/T1")["round"] == 6
    assert all(row["checker_version"] == CHECKER_VERSION for row in workflow.store.get(rid, "develop/task/T1")["checks"])
    previous = [value for key, value in workflow.store.delivery_evidence(rid).items()
                if key.startswith("recovery/verification-history/T1/")]
    assert len(previous) == 1 and all(row["checker_version"] == "verification-v10" for row in previous[0]["checks"])
