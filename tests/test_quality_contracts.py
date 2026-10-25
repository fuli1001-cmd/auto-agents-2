"""Regression and fault-injection tests for the quality-first repair contracts."""

import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from authority_review_fixtures import authority_review, seed_original
from jsonschema import ValidationError, validate

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.config import Cleanup
from auto_agents_2.context import ContextBuilder
from auto_agents_2.dependencies import pip_inputs
from auto_agents_2.environment import EnvironmentManager
from auto_agents_2.errors import PolicyViolation, ProtectedChange
from auto_agents_2.logs import SanitizedLog
from auto_agents_2.models import Change, Command, Finding, ReviewResult, SourceEvidence, strict_schema
from auto_agents_2.readview import create_view, public_files
from auto_agents_2.replanning import assess, check_criteria
from auto_agents_2.research import ResearchService
from auto_agents_2.resources import ResourceManager
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.store import Store
from auto_agents_2.util import digest, write_json
from auto_agents_2.workspace import WorkspaceManager, apply_prepared, git, prepare_changes, tree_hash


def repository(path):
    path.mkdir(exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "fixture")
    git(path, "config", "user.email", "fixture@localhost")


def commit(path):
    git(path, "add", "--all")
    git(path, "commit", "-qm", "fixture")
    return git(path, "rev-parse", "HEAD")


def test_canonical_contracts_and_hard_errors_precede_authorization(tmp_path):
    changes = [Change(path="tests/new.py", content="assert True"), Change(path="docs/./aa2/plan.md", content="bad")]
    with pytest.raises(PolicyViolation) as error:
        prepare_changes(tmp_path, changes, ["tests", "docs"])
    assert not isinstance(error.value, ProtectedChange)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").symlink_to("tests")
    with pytest.raises(PolicyViolation, match="Symlink"):
        prepare_changes(tmp_path, [Change(path="src/x.py", content="bad")], ["src"], protected=False)


def test_exact_text_mode_and_partial_replay(tmp_path):
    original = b"\xef\xbb\xbfalpha\r\nbeta\ngamma\r\n"
    target = tmp_path / "run.py"
    target.write_bytes(original)
    target.chmod(0o755)
    patch = prepare_changes(tmp_path, [Change(path="./run.py", operation="replace", old_text="alpha", content="one"),
                                       Change(path="run.py", operation="replace", old_text="beta", content="two")], ["run.py"])
    apply_prepared(tmp_path, patch)
    apply_prepared(tmp_path, patch)
    assert target.read_bytes() == original.replace(b"alpha", b"one").replace(b"beta", b"two")
    assert target.stat().st_mode & 0o777 == 0o755
    target.chmod(0o644)
    with pytest.raises(PolicyViolation, match="mode"):
        apply_prepared(tmp_path, patch)


def test_execution_identity_tracks_secrets_modes_and_links(tmp_path):
    repository(tmp_path)
    (tmp_path / ".env").write_text("SECRET=initial")
    (tmp_path / "a.py").write_text("good")
    (tmp_path / "b.py").write_text("bad")
    (tmp_path / "current.py").symlink_to("a.py")
    commit(tmp_path)
    first = tree_hash(tmp_path)
    (tmp_path / ".env").write_text("SECRET=changed")
    second = tree_hash(tmp_path)
    assert second != first
    (tmp_path / "a.py").chmod(0o755)
    third = tree_hash(tmp_path)
    assert third != second
    (tmp_path / "current.py").unlink()
    (tmp_path / "current.py").symlink_to("b.py")
    assert tree_hash(tmp_path) != third


def test_context_scope_exact_and_glob(tmp_path):
    (tmp_path / "src").mkdir()
    for name in ("a.py", "a.py.backup", "b.py"):
        (tmp_path / "src" / name).write_text(name)
    assert [p["path"] for p in ContextBuilder().build(tmp_path, scopes=["src/a.py"])["files"]] == ["src/a.py"]
    assert len(ContextBuilder().build(tmp_path, scopes=["src/*.py"])["files"]) == 2


def test_provider_snapshot_protects_secret_paths_and_git_history(tmp_path):
    project = tmp_path / "project"
    repository(project)
    secret = project / ".env"
    secret.write_text("TOKEN=classified-token-value")
    (project / ".npmrc").write_text("//registry.example/:_authToken=registry-private-token")
    (project / "old-secret.txt").write_text("deleted-sensitive-value")
    commit(project)
    (project / "old-secret.txt").unlink()
    os.link(secret, project / "alias.txt")
    (project / "linked.txt").symlink_to(".env")
    (project / "copy.txt").write_bytes(secret.read_bytes())
    (project / "public.txt").write_text("public")
    commit(project)
    names = {p.name for p in public_files(project)}
    assert names == {"public.txt", "alias.txt", "copy.txt"}
    state = project / ".aa2"
    view = create_view(project, state / "attempts/view")
    assert git(view, "ls-files").splitlines() == ["alias.txt", "copy.txt", "public.txt"]
    assert "deleted-sensitive-value" not in git(view, "log", "-p", "--all")
    runner = SandboxRunner(state)
    command = runner.command(["/bin/sh", "-c", f'test "$(cat public.txt)" = public && test ! -e {secret} && test ! -e .env'],
                             view, [], hidden=[project])
    result = subprocess.run(command, capture_output=True)
    assert result.returncode == 0, result.stderr.decode()


def test_recursive_dependency_roots_and_constraint_roles(tmp_path):
    for root, text in (("api", "shared>=1\n-c ../limits.txt\n-e .\n"), ("web", "shared<3\n")):
        (tmp_path / root).mkdir()
        (tmp_path / root / "requirements.txt").write_text(text)
    (tmp_path / "limits.txt").write_text("transitive<2\n")
    automatic = pip_inputs(tmp_path)
    assert automatic.requirements == ["shared>=1", "-e {AA2_SOURCE}/api", "shared<3"]
    assert automatic.constraints == ["transitive<2"] and automatic.local
    (tmp_path / "deps.txt").write_text("-r web/requirements.txt\n-e .\n")
    explicit = pip_inputs(tmp_path, "deps.txt")
    assert explicit.requirements == ["shared<3", "-e {AA2_SOURCE}/."]
    assert not explicit.constraints
    (tmp_path / "deps.txt").write_text("-r deps.txt")
    with pytest.raises(Exception, match="Cyclic"):
        pip_inputs(tmp_path, "deps.txt")


@pytest.mark.asyncio
async def test_corrupt_environment_gets_new_instance_without_destroying_old(tmp_path):
    from test_runtime import configured
    config, store = configured(tmp_path)
    manager = EnvironmentManager(config, SandboxRunner(store.root, _test_unconfined=True))
    async def prepare(prefix, ready, fingerprint, *args):
        (prefix / "bin").mkdir(parents=True)
        (prefix / "bin/python").write_text("immutable fixture")
        from auto_agents_2.installation_evidence import save_installation
        capture_hash = save_installation(manager, prefix, None, [])
        write_json(ready, {"fingerprint": fingerprint, "installation_hash": capture_hash,
                           "integrity": manager.integrity(prefix)})
    manager._prepare = prepare
    first = await manager.ensure(tmp_path, "python")
    assert await manager.ensure(tmp_path, "python") == first
    (first.prefix / "bin/python").write_text("corrupted but retained")
    second = await manager.ensure(tmp_path, "python")
    assert second.prefix != first.prefix
    assert (first.prefix / "bin/python").read_text() == "corrupted but retained"


def test_legacy_obligation_does_not_reset_progress(tmp_path):
    from auto_agents_2.evidence import CHECKER_VERSION
    from auto_agents_2.models import CheckResult
    command = Command(argv=["python", "-c", "pass"])
    legacy = command.model_dump(exclude={"inputs", "outputs"})
    key = "command/" + digest(legacy)
    previous = {"tracked": {"task": {key: False}}, "evaluations": 1, "stalled": 2}
    result = CheckResult(argv=command.argv, cwd=".", exit_code=0, output="", duration=0,
                         checker_version=CHECKER_VERSION,
                         source_hash="source", environment_hash="env", command_hash=digest(command.model_dump()))
    criteria = check_criteria([command], [result], "source", "env")
    updated, decision = assess(previous, {"task": {"criteria": criteria}})
    assert decision["improved"] and updated["tracked"]["task"][key]
    _, decision = assess(updated, {"task": {"criteria": criteria}})
    assert not decision["improved"]
    validate(command.model_dump(), strict_schema(Command))
    with pytest.raises(ValidationError):
        validate(legacy, strict_schema(Command))
    assert Command.model_validate(legacy).inputs == []


def test_streaming_utf8_and_secrets_split_across_chunks(tmp_path):
    path = tmp_path / "sanitized.log"
    log = SanitizedLog(path, ["very-secret-value"])
    data = "中文 Authorization: Bearer abc123\nhttps://user:pass@example.com/?token=hidden\nvery-secret-value\n".encode()
    for byte in data:
        log.feed(bytes([byte]))
    log.finish()
    text = path.read_text()
    assert "中文" in text and log.complete
    assert all(value not in text for value in ("abc123", "user:pass", "hidden", "very-secret-value"))


def test_artifact_gc_traverses_legacy_and_new_nested_dependencies(tmp_path):
    store = Store(tmp_path)
    rid = store.admit("idea", {})["id"]
    artifacts = ArtifactStore(store.root)
    child = artifacts.put(b"binary evidence\x00", "binary")
    parent = artifacts.put({"child": child})
    store.checkpoint(rid, "evidence", parent)
    store.update(rid, status="SUCCEEDED")
    resources = ResourceManager(store, Cleanup(min_free_bytes=0))
    resources.collect_artifacts()
    assert artifacts.bytes(child) == b"binary evidence\x00"
    store.invalidate(rid, ["evidence"])
    resources.collect_artifacts()
    assert not (artifacts.root / child["hash"][:2] / child["hash"]).exists()


@pytest.mark.asyncio
async def test_research_keeps_raw_redirect_links_and_shared_fetch(tmp_path, monkeypatch):
    requests = []
    async def respond(request):
        requests.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/doc"})
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                              content=b'<table><tr><td>v2</td><td>supported</td></tr></table><a href="/limits">limits</a>')
    monkeypatch.setattr("auto_agents_2.research.PublicHTTPTransport", lambda: httpx.MockTransport(respond))
    artifacts = ArtifactStore(Store(tmp_path).root)
    result = await ResearchService(artifacts).verify([SourceEvidence(url="https://docs.example/start#one", claim="first", version="2"),
                                                    SourceEvidence(url="https://docs.example/start#two", claim="second", version="3")])
    assert len(requests) == 2
    assert result[0]["retrieval"]["final_url"] == "https://docs.example/doc"
    assert "/limits" in result[0]["excerpt"] and " | " in result[0]["excerpt"]
    assert b"<table>" in artifacts.bytes(result[0]["retrieval"]["raw"])
    assert result[1]["claimed_version"] == "3" and result[1]["supported_version"] is None


def test_checkout_normalization_and_directory_transition(tmp_path):
    repository(tmp_path)
    (tmp_path / ".gitattributes").write_text("*.txt text eol=crlf\n")
    (tmp_path / "data").mkdir()
    (tmp_path / "data/a.txt").write_bytes(b"old\r\n")
    old = commit(tmp_path)
    manager = WorkspaceManager(tmp_path)
    manager.initialize()
    work = manager.allocate("fixture", "candidate", old)
    (work / "data/a.txt").unlink()
    (work / "data").rmdir()
    (work / "data").write_text("replacement")
    (work / "note.txt").write_bytes(b"new\n")
    new = manager.checkpoint(work, "refs/aa2/fixture/candidate", "candidate")
    manager.normalize_checkout(work, new)
    assert (work / "note.txt").read_bytes() == b"new\r\n"
    manifest = manager.publication_manifest(old, new)
    manager.check_publication_paths(manifest)
    manager.recover_publication({"target": "main", "old": old, "candidate": new, "manifest": manifest})
    assert (tmp_path / "note.txt").read_bytes() == b"new\r\n"
    assert (tmp_path / "data").read_text() == "replacement"
    assert tree_hash(tmp_path) == tree_hash(work)


def test_issue_identity_preserves_findings_and_scoped_closure(tmp_path):
    from auto_agents_2.issues import IssueLedger
    store = Store(tmp_path)
    rid = store.admit("idea", {})["id"]
    ledger = IssueLedger(store, ArtifactStore(store.root))
    finding = Finding(severity="major", location="a.py:3", description="wrong result", evidence="first log", resolution="fix sum")
    ledger.record(rid, "task/1", ReviewResult(approved=False, summary="failed", findings=[finding]), {})
    finding.evidence = "second log"
    ledger.record(rid, "task/1", ReviewResult(approved=False, summary="failed", findings=[finding]), {})
    assert len(ledger.unresolved(rid)) == 1 and ledger.unresolved(rid)[0]["count"] == 2
    ledger.record(rid, "task/2", ReviewResult(approved=True, summary="other task"), {})
    assert ledger.unresolved(rid)
    ledger.record(rid, "task/1", ReviewResult(approved=True, summary="fixed"), {})
    assert not ledger.unresolved(rid)


@pytest.mark.asyncio
async def test_subdirectory_verification_reads_current_workspace_root(tmp_path):
    from auto_agents_2.environment import Environment
    from auto_agents_2.verification import VerificationService, passed
    project = tmp_path / "project"
    project.mkdir()
    (project / "backend").mkdir()
    (project / "shared.py").write_text("VALUE = 42\n")
    store = Store(project)
    command = Command(argv=[sys.executable, "-c", "from shared import VALUE; assert VALUE == 42"], cwd="backend")
    result = await VerificationService(store.root, SandboxRunner(store.root)).run([command], project, Environment(Path(sys.prefix), "fixture"))
    assert passed(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", [None, "durable-attempt"])
async def test_delivery_probes_never_reuse_stale_generated_output(tmp_path, attempt):
    from test_workflow import CodingAgent, engine

    from auto_agents_2.delivery import RuntimeVerificationError
    from auto_agents_2.environment import Environment
    from auto_agents_2.models import PlanResult, TaskSpec
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("idea", {})["id"]
    probe = Command(argv=[sys.executable, "-c", "from pathlib import Path; assert Path('dist/value').read_text() == 'good'"])
    task = TaskSpec(id="T", title="test", description="test", requirement_ids=["R"], allowed_paths=["app.py"], acceptance=["ok"], checks=[probe])
    plan = PlanResult(summary="test", document="test", tasks=[task], final_checks=[probe], runtime_probes=[probe])
    output = tmp_path / "dist/value"
    output.parent.mkdir()
    output.write_text("good")
    journal = {"candidate": "unchanged", "plan": plan.model_dump(), "runtime_attempt_id": attempt}
    environment = Environment(Path(sys.prefix), "fixture")
    await workflow._runtime_checks(rid, journal, environment)
    output.write_text("bad")
    with pytest.raises(RuntimeVerificationError):
        await workflow._runtime_checks(rid, journal, environment)


@pytest.mark.asyncio
async def test_declared_generated_input_invalidates_cached_check(tmp_path):
    from auto_agents_2.environment import Environment
    from auto_agents_2.evidence import reusable
    from auto_agents_2.verification import VerificationService
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist/input").write_text("good")
    store = Store(tmp_path)
    environment = Environment(Path(sys.prefix), "fixture")
    command = Command(argv=[sys.executable, "-c", "pass"], inputs=["dist/input"])
    checks = await VerificationService(store.root, SandboxRunner(store.root)).run([command], tmp_path, environment)
    assert reusable([command], checks, tmp_path, environment, ArtifactStore(store.root)).allowed
    (tmp_path / "dist/input").write_text("changed")
    assert not reusable([command], checks, tmp_path, environment, ArtifactStore(store.root)).allowed


@pytest.mark.asyncio
async def test_delivery_repair_replays_partial_replace_once(tmp_path, monkeypatch):
    import asyncio

    from test_workflow import CodingAgent, engine

    from auto_agents_2.models import ImplementationResult
    class RepairAgent(CodingAgent):
        proposals = 0
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if context.get("review_kind") == "recovery_diagnosis":
                return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)
            if stage == "implement":
                self.proposals += 1
                return ImplementationResult(summary="repair", changes=[Change(path=name, operation="replace", old_text="old", content="new") for name in ("one.py", "two.py")])
            return authority_review(context, approved=True, summary="approved")
    agent = RepairAgent()
    workflow = engine(tmp_path, agent)
    (tmp_path / "one.py").write_text("old")
    (tmp_path / "two.py").write_text("old")
    workflow.workspaces.checkpoint(tmp_path, "refs/aa2/fixture/base", "baseline")
    rid = workflow.store.admit("idea", {})["id"]
    seed_original(workflow, rid)
    def interrupted(work, patch):
        apply_prepared(work, patch[:1])
        raise asyncio.CancelledError()
    monkeypatch.setattr("auto_agents_2.engine.apply_prepared", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await workflow._delivery_repair(rid, tmp_path, {"failure": "same"}, ["one.py", "two.py"])
    monkeypatch.setattr("auto_agents_2.engine.apply_prepared", apply_prepared)
    await workflow._delivery_repair(rid, tmp_path, {"failure": "same"}, ["one.py", "two.py"])
    assert agent.proposals == 1
    assert workflow.store.get(rid, "rounds/delivery")["round"] == 1
    assert (tmp_path / "two.py").read_text() == "new"


@pytest.mark.asyncio
async def test_conda_pip_and_multiple_roots_share_one_resolver_input(tmp_path):
    import yaml
    from test_runtime import configured
    config, store = configured(tmp_path)
    manager = EnvironmentManager(config, SandboxRunner(store.root, _test_unconfined=True))
    (tmp_path / "environment.yml").write_text("dependencies:\n  - python=3.11\n  - pip\n  - pip:\n    - shared>=1\n")
    (tmp_path / "deps.txt").write_text("shared<3\n-c constraints.txt\n-e .\n")
    (tmp_path / "constraints.txt").write_text("transitive<2\n")
    observed = []
    async def prepare(prefix, ready, fingerprint, profile, conda_file, pip_file, workspace, dependencies, local, inputs=None):
        observed.append(pip_file.read_text())
        assert all(isinstance(d, str) for d in yaml.safe_load(conda_file.read_text())["dependencies"])
        constraint_file = Path(pip_file.read_text().splitlines()[0].removeprefix("-c "))
        assert constraint_file.read_text() == "transitive<2\n"
        assert local
        (prefix / "bin").mkdir(parents=True)
        (prefix / "bin/python").write_text("fixture")
        write_json(ready, {"fingerprint": fingerprint, "integrity": manager.integrity(prefix)})
    manager._prepare = prepare
    await manager.ensure(tmp_path, "python", requirements_file="deps.txt")
    assert len(observed) == 1 and "shared<3" in observed[0] and "shared>=1" in observed[0]


def test_all_images_are_attached_and_material_targets_do_not_truncate(tmp_path):
    from auto_agents_2.materials import bundle
    from auto_agents_2.transports import CodexTransport
    images = []
    for index in range(10):
        path = tmp_path / f"{index}.png"
        path.write_bytes(b"fixture")
        images.append(path)
    effective = {"binary": "codex", "native_profile": "", "model": "selected", "effort": "high", "args": []}
    args = CodexTransport().argv(effective, tmp_path, tmp_path / "schema", tmp_path / "out", "text", None, images)
    assert args.count("-i") == 10
    prompt = "mandatory acceptance\n" * 10000
    manifest = bundle(prompt, strict_schema(Command), [], images, {"profile": "fixture"})
    assert manifest["over_target"] and manifest["prompt_hash"] == digest(prompt)
    assert len(manifest["materials"]) == 10 and not manifest["omitted_mandatory_content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["ready", "publishing", "published", "runtime"])
async def test_legacy_delivery_resume_preserves_publication_and_budgets(tmp_path, phase):
    import asyncio

    from test_workflow import CodingAgent, engine

    from auto_agents_2.workspace import legacy_tree_hash
    project = tmp_path / "project"
    project.mkdir()
    requirement = tmp_path / "request.md"
    requirement.write_text("Add numbers")
    agent = CodingAgent()
    workflow = engine(project, agent)
    save = workflow.delivery.save
    def interrupt(rid, journal, next_phase=None):
        save(rid, journal, next_phase)
        if journal["phase"] == phase:
            raise asyncio.CancelledError()
    workflow.delivery.save = interrupt
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(requirement))
    rid = workflow.store.current()["id"]
    journal = workflow.store.get(rid, "delivery/journal")
    journal.pop("identity_version")
    source = project if phase in {"published", "runtime"} else Path(journal["work"])
    journal["source_hash"] = legacy_tree_hash(source)
    workflow.store.checkpoint(rid, "delivery/journal", journal)
    before = workflow.store.get(rid, "rounds/T1")["round"]
    implementations = sum(stage == "implement" for stage, _ in agent.calls)
    resumed = engine(project, agent)
    result = await resumed.start()
    assert result["status"] == "SUCCEEDED"
    assert resumed.store.get(rid, "rounds/T1")["round"] == before
    assert sum(stage == "implement" for stage, _ in agent.calls) == implementations
    assert resumed.store.get(rid, "delivery/legacy-journal")


def test_changed_attributes_materialize_exact_publication_bytes(tmp_path):
    repository(tmp_path)
    (tmp_path / "note.txt").write_bytes(b"same\n")
    old = commit(tmp_path)
    manager = WorkspaceManager(tmp_path)
    manager.initialize()
    work = manager.allocate("fixture", "candidate", old)
    (work / ".gitattributes").write_text("*.txt text eol=crlf\n")
    new = manager.checkpoint(work, "refs/aa2/fixture/candidate", "candidate")
    policy = manager.normalize_checkout(work, new)
    manifest = manager.publication_manifest(old, new, work)
    assert "note.txt" in manifest and manifest["note.txt"]["before"] == manifest["note.txt"]["after"]
    journal = {"target": "main", "old": old, "candidate": new, "manifest": manifest,
               "work": str(work), "checkout_policy": policy}
    manager.publish("main", old, new)
    manager.recover_publication(journal)
    assert (tmp_path / "note.txt").read_bytes() == b"same\r\n"
    assert tree_hash(tmp_path) == tree_hash(work)


def test_file_to_directory_recovery_is_idempotent(tmp_path):
    repository(tmp_path)
    (tmp_path / "node").write_text("old")
    old = commit(tmp_path)
    manager = WorkspaceManager(tmp_path)
    manager.initialize()
    work = manager.allocate("fixture", "candidate", old)
    (work / "node").unlink()
    (work / "node").mkdir()
    (work / "node/child").write_text("new")
    new = manager.checkpoint(work, "refs/aa2/fixture/candidate", "candidate")
    manifest = manager.publication_manifest(old, new, work)
    journal = {"target": "main", "old": old, "candidate": new, "manifest": manifest}
    manager.recover_publication(journal)
    manager.recover_publication(journal)
    assert (tmp_path / "node/child").read_text() == "new"


@pytest.mark.asyncio
async def test_material_references_are_mounted_and_probed_in_cli_sandbox(tmp_path, tmp_path_factory):
    from test_runtime import configured

    from auto_agents_2.providers import ProviderRouter
    from auto_agents_2.transports import CodexTransport
    config, store = configured(tmp_path)
    binary = tmp_path_factory.mktemp("trusted-provider") / "provider-fixture"
    binary.write_text(f"#!{sys.executable}\n" + '''import json, pathlib, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("fixture-cli 1")
    sys.exit(0)
schema = json.loads(pathlib.Path(args[args.index("--output-schema") + 1]).read_text())
prompt = sys.stdin.read()
context = json.loads(prompt.split("<context>\\n", 1)[1].split("\\n</context>", 1)[0])
if "text_token" in schema["properties"]:
    result = {"text_token": pathlib.Path(context["context_files"][0]).read_text(), "image_token": ""}
else:
    assert pathlib.Path(context["report"]["location"]).read_text() == "complete early failure evidence"
    result = {"approved": True, "summary": "read actual evidence", "findings": []}
pathlib.Path(args[args.index("-o") + 1]).write_text(json.dumps(result))
print(json.dumps({"type": "thread.started", "thread_id": "fixture"}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
''')
    binary.chmod(0o755)
    native = tmp_path / "native"
    native.mkdir()
    data = config.load().model_dump(mode="json")
    data["providers"] = {"codex": data["providers"]["codex"]}
    data["providers"]["codex"]["binary"] = str(binary)
    write_json(config.path, data)
    rid = store.admit("fixture", {})["id"]
    report = ArtifactStore(store.root).put("complete early failure evidence", "check_log")
    transport = CodexTransport()
    transport.native_home = lambda _: native
    router = ProviderRouter(config, store, SandboxRunner(store.root), transports={"codex": transport})
    response = await router.invoke("review", {"report": report}, ReviewResult, tmp_path, rid, "review", True)
    assert response.approved
    material = store.get(rid, "materials/review")
    assert material["capability"]["native_read_test"] == "observed_read"
    assert material["materials"][0]["hash"] == report["hash"]
    assert store.usage_summary(rid)["total_tokens"] == 4  # Probe and review both counted.


def test_token_budget_migration_and_config_errors_do_not_echo_values(tmp_path):
    from test_runtime import configured

    from auto_agents_2.config import Execution
    from auto_agents_2.errors import ConfigurationError
    execution = Execution.model_validate({"max_tokens": 1, "max_rounds": 4})
    assert "max_tokens" not in execution.model_dump()
    config, _ = configured(tmp_path)
    data = config.load().model_dump(mode="json")
    data["execution"]["max_rounds"] = "credential-canary-value"
    write_json(config.path, data)
    with pytest.raises(ConfigurationError) as failure:
        config.load()
    assert "credential-canary-value" not in str(failure.value)
    assert "max_rounds" in str(failure.value)


def test_project_rules_are_mandatory_above_context_soft_target(tmp_path):
    rules = "Required acceptance and permission constraint.\r\n" * 3000
    (tmp_path / "AGENTS.md").write_bytes(rules.encode())
    context = ContextBuilder().build(tmp_path, limit=100)
    assert context["files"] == [{"path": "AGENTS.md", "content": rules}]


@pytest.mark.asyncio
async def test_corrupt_delivery_environment_reverified_before_publication(tmp_path):
    import asyncio

    from test_workflow import CodingAgent, engine
    project = tmp_path / "project"
    project.mkdir()
    request = tmp_path / "request.md"
    request.write_text("Add numbers")
    agent = CodingAgent()
    workflow = engine(project, agent)
    save = workflow.delivery.save
    def interrupt(rid, journal, phase=None):
        save(rid, journal, phase)
        if journal["phase"] == "ready":
            raise asyncio.CancelledError()
    workflow.delivery.save = interrupt
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    damaged = workflow.store.root / "environments/damaged"
    (damaged / "bin").mkdir(parents=True)
    (damaged / "bin/python").write_text("retain damaged instance")
    journal = workflow.store.get(rid, "delivery/journal")
    journal["environment"] = {"prefix": str(damaged), "fingerprint": "obsolete"}
    workflow.store.checkpoint(rid, "delivery/journal", journal)
    resumed = engine(project, agent)
    result = await resumed.start()
    assert result["status"] == "SUCCEEDED"
    assert resumed.store.get(rid, "delivery/environment-recovery")
    assert (damaged / "bin/python").read_text() == "retain damaged instance"
    assert resumed.store.get(rid, "delivery/receipt")["runtime"]["prefix"] == sys.prefix


@pytest.mark.asyncio
async def test_constraints_only_are_not_pip_install_targets(tmp_path):
    from test_runtime import configured
    config, store = configured(tmp_path)
    manager = EnvironmentManager(config, SandboxRunner(store.root, _test_unconfined=True))
    (tmp_path / "requirements.txt").write_text("-c constraints.txt\n")
    (tmp_path / "constraints.txt").write_text("not_requested_package<2\n")
    async def prepare(prefix, ready, fingerprint, profile, conda_file, pip_file, *args):
        assert pip_file is None
        (prefix / "bin").mkdir(parents=True)
        (prefix / "bin/python").write_text("fixture")
        write_json(ready, {"fingerprint": fingerprint, "integrity": manager.integrity(prefix)})
    manager._prepare = prepare
    await manager.ensure(tmp_path, "python")


@pytest.mark.asyncio
async def test_stdout_pass_claim_cannot_supply_test_case_evidence(tmp_path):
    from auto_agents_2.environment import Environment
    from auto_agents_2.verification import VerificationService
    store = Store(tmp_path)
    command = Command(argv=[sys.executable, "-c", "print('100 passed')"], expected_tests=1)
    result = (await VerificationService(store.root, SandboxRunner(store.root)).run(
        [command], tmp_path, Environment(Path(sys.prefix), "fixture")))[0]
    assert result.tests == 0 and result.exit_code != 0


def test_secret_storage_contract_uses_paths_not_plaintext_detection(tmp_path):
    (tmp_path / ".env").write_text("HOST=localhost\nPORT=8000\nPASSWORD=private-password-value\n")
    (tmp_path / "server.py").write_text("HOST = 'localhost'\nPORT = 8000\n")
    (tmp_path / "leak.py").write_text("PASSWORD = 'private-password-value'\n")
    # Ordinary paths are admitted even if the project violates the storage rule.
    assert {p.name for p in public_files(tmp_path)} == {"server.py", "leak.py"}
    (tmp_path / ".env").write_text("PASSWORD=private-password-value\n")
    (tmp_path / ".env.example").write_text("HOST=localhost\nPORT=8000\n")
    assert {p.name for p in public_files(tmp_path)} == {"server.py", "leak.py", ".env.example"}


def test_issue_scope_survives_task_renaming(tmp_path):
    from auto_agents_2.issues import IssueLedger
    store = Store(tmp_path)
    rid = store.admit("idea", {})["id"]
    ledger = IssueLedger(store, ArtifactStore(store.root))
    finding = Finding(severity="major", location="a.py", description="wrong result", evidence="failed check", resolution="fix")
    ledger.record(rid, "old/review", ReviewResult(approved=False, summary="failed", findings=[finding]),
                  {"task": {"id": "old", "origin_id": "stable"}})
    identity = ledger.unresolved(rid)[0]["id"]
    ledger.record(rid, "new/review", ReviewResult(approved=True, summary="fixed"),
                  {"task": {"id": "new", "origin_id": "stable"}})
    assert not ledger.unresolved(rid)
    assert store.get(rid, "issues")[identity]["status"] == "closed"


@pytest.mark.asyncio
async def test_delivery_hard_errors_require_new_decision_without_test_review(tmp_path):
    from test_workflow import CodingAgent, engine

    from auto_agents_2.models import ImplementationResult
    class RepairAgent(CodingAgent):
        proposals = 0
        reviews = 0
        decisions = 0
        async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
            if stage == "decision":
                from auto_agents_2.decisions import DecisionResult
                self.decisions += 1
                return DecisionResult(action="approve", selected_action="repair", evidence_refs=["failure"],
                    reason="Correct the rejected path within the existing scope", expected_result="A valid scoped candidate")
            if context.get("review_kind") == "recovery_diagnosis":
                return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)
            if stage == "implement":
                self.proposals += 1
                path = "outside.py" if self.proposals == 1 else "allowed.py"
                return ImplementationResult(summary="repair", changes=[Change(path=path, content="fixed")])
            self.reviews += 1
            return authority_review(context, approved=True, summary="approved")
    agent = RepairAgent()
    workflow = engine(tmp_path, agent)
    (tmp_path / "allowed.py").write_text("old")
    workflow.workspaces.checkpoint(tmp_path, "refs/aa2/fixture/base", "baseline")
    rid = workflow.store.admit("fixture", {})["id"]
    seed_original(workflow, rid)
    await workflow._delivery_repair(rid, tmp_path, {"failure": "repair"}, ["allowed.py"])
    assert agent.proposals == 2 and agent.reviews == 1
    assert agent.decisions == 1
    assert workflow.store.get(rid, "rounds/delivery")["round"] == 2
    assert not (tmp_path / "outside.py").exists()


def test_managed_readme_block_preserves_existing_crlf(tmp_path):
    from test_workflow import CodingAgent, engine
    workflow = engine(tmp_path, CodingAgent())
    original = b"# Project\r\nExisting instructions\r\n"
    (tmp_path / "README.md").write_bytes(original)
    workflow._runtime_instructions(tmp_path)
    assert (tmp_path / "README.md").read_bytes().startswith(original.rstrip(b"\r\n"))


@pytest.mark.asyncio
async def test_changed_execution_variable_invalidates_cached_evidence(tmp_path, monkeypatch):
    from auto_agents_2.environment import Environment
    from auto_agents_2.evidence import reusable
    from auto_agents_2.verification import VerificationService
    store = Store(tmp_path)
    environment = Environment(Path(sys.prefix), "fixture")
    monkeypatch.setenv("AA2_FIXTURE_FEATURE", "good")
    command = Command(argv=[sys.executable, "-c", "import os; assert os.environ['AA2_FIXTURE_FEATURE'] == 'good'"])
    checks = await VerificationService(store.root, SandboxRunner(store.root)).run([command], tmp_path, environment)
    assert reusable([command], checks, tmp_path, environment, ArtifactStore(store.root)).allowed
    monkeypatch.setenv("AA2_FIXTURE_FEATURE", "changed")
    assert not reusable([command], checks, tmp_path, environment, ArtifactStore(store.root)).allowed
