"""Opt-in real Conda/npm/main-workdir handoff smoke; no model calls.

Uses disposable repositories only. Source and reports survive test teardown; generated
runtime dependencies are explicitly unpinned at the end to reclaim test disk space.
"""
import asyncio
import os
import tempfile
from pathlib import Path

from manual_frontend import fixture

from auto_agents_2.config import ConfigStore
from auto_agents_2.engine import WorkflowEngine
from auto_agents_2.models import Command, PlanResult, TaskSpec
from auto_agents_2.resources import ResourceManager
from auto_agents_2.util import write_json
from auto_agents_2.verification import passed
from auto_agents_2.workspace import git, tree_hash


async def smoke(root, framework):
    project = root / framework
    project.mkdir()
    cfg = ConfigStore(project)
    cfg.initialize()
    settings = cfg.load().model_dump(mode="json")
    settings["cleanup"].update(cache_max_bytes=0, cache_ttl_seconds=0, min_free_bytes=0)
    write_json(cfg.path, settings)
    workflow = WorkflowEngine(project)
    workflow.workspaces.initialize()
    run = workflow.store.admit("fixture://delivery/" + framework, settings)
    rid = run["id"]
    workflow.store.update(rid, target_branch="main")
    baseline = workflow.workspaces.committed_baseline(rid, "main")
    workflow.store.checkpoint(rid, "target_base", "")
    work = workflow.workspaces.allocate(rid, "integration", baseline)
    workflow.resources.register(work, "worktree", rid)
    # Fixture expects a new directory; move only the fixture's generated source into work.
    staged = root / (framework + "-source")
    if framework == "python":
        staged.mkdir()
        (staged / "app.py").write_text("def add(a, b): return a + b\n")
        (staged / "test_app.py").write_text("from app import add\ndef test_add(): assert add(2,3) == 5\n")
        check = Command(argv=["python", "-m", "pytest", "-q"], expected_tests=1)
        probe = Command(argv=["python", "-c", "from app import add; assert add(2,3)==5"])
        preview = None
    else:
        preview = fixture(staged, framework)
        check = Command(argv=["npm", "run", "build"], timeout=300)
        probe = Command(argv=["node", "-e", "require('react'); console.log('runtime ready')"])
    for path in staged.iterdir():
        path.rename(work / path.name)
    staged.rmdir()
    plan = PlanResult(summary="Runtime handoff fixture", document="Real commands, no model claims",
        tasks=[TaskSpec(id="T1", title="fixture", description="fixture", requirement_ids=["R1"],
                        allowed_paths=["*"], acceptance=["runtime usable after GC"], checks=[check])],
        final_checks=[check], runtime_prepare=[] if framework == "python" else [check], runtime_probes=[probe], preview=preview)
    print(f"Preparing {framework}: isolated dependencies and verification", flush=True)
    try:
        environment = await workflow._environment(work, rid, plan)
        checks = await workflow.verifier.run(plan.final_checks, work, environment)
        assert passed(checks), [c.model_dump() for c in checks]
        workflow.store.checkpoint(rid, "verify/checks", [c.model_dump() for c in checks])
        workflow.store.checkpoint(rid, "verify/identity", tree_hash(work))
        candidate = workflow.workspaces.checkpoint(work, f"refs/aa2/{rid}/candidate", "verified smoke candidate")
        result = await workflow.delivery.finish(rid, candidate=candidate, work=work, environment=environment, plan=plan)
        assert result["status"] == "SUCCEEDED"
        assert not work.exists()
        assert environment.prefix.exists()
        assert not git(project, "for-each-ref", "refs/aa2", "refs/heads/aa2")
        assert not git(project, "status", "--porcelain")
        after_gc = await workflow.verifier.run([probe], project, environment)
        assert passed(after_gc)
        receipt = workflow.store.get(rid, "delivery/receipt")
        write_json(root / f"{framework}-result.json", {"receipt": receipt, "after_gc": [c.model_dump() for c in after_gc]})
        print(f"Passed {framework}: primary source, frozen dependencies, runtime probes, GC pins", flush=True)
    except BaseException:
        workflow.store.update(rid, status="CANCELLED", outcome="SMOKE_FAILED")
        raise
    finally:
        # Explicit teardown of this script's disposable fixture, not production GC behaviour.
        with workflow.store.connect() as db:
            db.execute("DELETE FROM delivery_pins")
        resources = ResourceManager(workflow.store, cfg.load().cleanup)
        resources.release_run(rid)
        resources.collect()
        import shutil
        for name in ("node_modules", ".next", "dist"):
            shutil.rmtree(project / name, ignore_errors=True)


async def main():
    root = Path(tempfile.mkdtemp(prefix="aa2-delivery-smoke-"))
    print(f"Evidence: {root}", flush=True)
    cache = os.environ.get("AA2_BROWSER_CACHE", str(Path(__file__).parents[1] / ".cache/browsers"))
    # WorkflowEngine resets browser_environment, so copy only its browser lookup binding per fixture.
    from auto_agents_2 import engine
    engine.browser_environment = lambda _: os.environ.update(PLAYWRIGHT_BROWSERS_PATH=cache)
    async def existing_browser(*args):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = cache
    engine.WorkflowEngine._ensure_browser = existing_browser
    for framework in ("python", "react", "next"):
        await smoke(root, framework)


if __name__ == "__main__":
    asyncio.run(main())
