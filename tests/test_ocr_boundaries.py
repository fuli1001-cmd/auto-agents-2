"""Behavioural regressions for the independently validated OCR findings."""
import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest
from browser_fixtures import PNG

from auto_agents_2.approvals import PrototypeApprovals, exact_actions
from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.builds import generated_entry, normalized, verification_commands
from auto_agents_2.config import Cleanup
from auto_agents_2.dependencies import copy_execution_source, pip_inputs
from auto_agents_2.environment import Environment
from auto_agents_2.errors import AA2Error, PolicyViolation, ProviderError, VerificationPreparationError
from auto_agents_2.evidence import CHECKER_VERSION, assess_observation, input_identity, runtime_configuration
from auto_agents_2.logs import SanitizedLog
from auto_agents_2.models import Change, CheckResult, Command, TaskSpec
from auto_agents_2.planning import conflict
from auto_agents_2.prompts import PromptCompiler
from auto_agents_2.providers import EventDecoder
from auto_agents_2.readview import create_view, public_files
from auto_agents_2.requirements import TextExtractor, decode_response
from auto_agents_2.research import public_url
from auto_agents_2.resources import ResourceManager
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.store import Store
from auto_agents_2.util import digest, scrub_text
from auto_agents_2.verification import MAX_REPORT_BYTES, VerificationService, report_bytes
from auto_agents_2.workspace import apply_prepared, git, prepare_changes, prepared_before_hash, tree_hash


def setup(tmp_path):
    store = Store(tmp_path)
    rid = store.admit("fixture", {})["id"]
    return store, rid, ArtifactStore(store.root), ResourceManager(store, Cleanup(min_free_bytes=0))


@pytest.mark.parametrize("defect", ["string_false", "missing_image", "malformed_image", "missing_mobile", "missing_page"])
def test_prototype_receipt_requires_validated_review_complete_routes_and_images(tmp_path, defect):
    store, _, artifacts, _ = setup(tmp_path)
    binding = {"hash": "content", "identity": "binding", "actions_hash": "actions"}
    image = artifacts.put(PNG, "screenshot")
    rows = [{"route": route, "viewport": width, "status": 200, "actions": [], "image_artifact": image}
            for route in ("/a.html", "/b.html") for width in (1440, 390)]
    record = {**binding, "browser_version": 3, "review": {"approved": True, "summary": "review"}, "visuals": rows}
    approvals = PrototypeApprovals(store, artifacts)
    def current():
        return approvals.automation_current(record, binding, [], 3, expected_routes=["/a.html", "/b.html"])
    assert current()
    if defect == "string_false":
        record["review"]["approved"] = "false"
    elif defect == "missing_image":
        rows[0].pop("image_artifact")
    elif defect == "malformed_image":
        rows[0]["image_artifact"] = {"invented": True}
    elif defect == "missing_mobile":
        rows[:] = [row for row in rows if row["viewport"] == 1440]
    else:
        rows[:] = [row for row in rows if row["route"] == "/a.html"]
    assert not current()
    assert not exact_actions([], [], expected_routes=[])


@pytest.mark.parametrize("name,content,value", [
    (".credentials/config.json", '{"credentials":{"primary":"private-value-123"}}', "private-value-123"),
    (".npmrc", "//registry.example/:_authToken=registry-private-token", "registry-private-token"),
])
def test_secret_paths_do_not_block_ordinary_matching_text(tmp_path, name, content, value):
    secret = tmp_path / name
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text(content)
    public = tmp_path / "public.txt"
    public.write_text("prefix " + value + " suffix")
    assert public in public_files(tmp_path)


def test_overlapping_and_multiline_secrets_are_redacted_before_spooling(tmp_path):
    assert scrub_text("abcd-sensitive-suffix", ["abcd", "abcd-sensitive-suffix"]) == "<redacted>"
    assert scrub_text("abcdef", ["abcd", "cdef"]) == "<redacted>"
    log = SanitizedLog(tmp_path / "log", ["private-first-line\nprivate-second-line"])
    log.feed(b"private-first-li")
    log.feed(b"ne\nprivate-second-line\n")
    log.finish()
    assert "private-" not in (tmp_path / "log").read_text() + log.tail


@pytest.mark.parametrize("chunks", [[b"12345\n"], [b"1234", b"5\n"], [b"12345"]])
def test_log_size_limit_is_independent_of_chunk_boundaries(tmp_path, chunks):
    log = SanitizedLog(tmp_path / "log", max_record=4)
    try:
        with pytest.raises(AA2Error):
            for chunk in chunks:
                log.feed(chunk)
    finally:
        log.close()
    assert (tmp_path / "log").read_bytes() == b""


def test_public_git_view_ignores_repository_environment(tmp_path, monkeypatch):
    work, other, view = (tmp_path / name for name in ("work", "other", "view"))
    work.mkdir()
    other.mkdir()
    (work / "file").write_text("fixture")
    git(other, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    create_view(work, view)
    assert (view / ".git/HEAD").is_file()
    assert not list((other / ".git/refs/heads").iterdir())


def test_build_identity_includes_routes_named_cache_but_excludes_root_cache(tmp_path):
    output = tmp_path / ".next"
    path = output / "server/app/cache/page.js"
    path.parent.mkdir(parents=True)
    path.write_text("one")
    before = generated_entry(output, tmp_path)
    (output / "cache").mkdir()
    (output / "cache/runtime").write_text("mutable")
    assert generated_entry(output, tmp_path) == before
    path.write_text("two")
    assert generated_entry(output, tmp_path) != before


def test_build_ownership_rejects_git_failure_and_nested_producers(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q")
    with pytest.raises(VerificationPreparationError):
        verification_commands([Command(argv=["a"], outputs=["dist"]),
            Command(argv=["b"], cwd="dist/site", outputs=["dist/site/dist"])], tmp_path)
    def failed(*args, **kwargs):
        raise AA2Error("injected Git failure")
    monkeypatch.setattr("auto_agents_2.builds.git", failed)
    with pytest.raises(VerificationPreparationError):
        normalized(Command(argv=["build"], outputs=["dist"]), tmp_path)


def test_evidence_without_store_or_readable_inputs_cannot_be_reused(tmp_path, monkeypatch):
    _, _, artifacts, _ = setup(tmp_path)
    command = Command(argv=["check"])
    result = CheckResult(argv=command.argv, cwd=".", exit_code=0, process_exit_code=0, output="", duration=0,
        source_hash=tree_hash(tmp_path), environment_hash="host", checker_version=CHECKER_VERSION,
        command_hash=digest(command.model_dump()), input_hash=input_identity(tmp_path, command),
        runtime_config_hash=runtime_configuration(tmp_path, command, None))
    assert assess_observation(command, result, tmp_path, None, artifacts).passed
    assert not assess_observation(command, result, tmp_path, None, None).valid
    def failed(*args):
        raise PermissionError("injected")
    monkeypatch.setattr("auto_agents_2.evidence.input_identity", failed)
    assert not assess_observation(command, result, tmp_path, None, artifacts).valid


@pytest.mark.parametrize("line", [".[dev]", "-e .[dev]", './pkg[dev] ; python_version < "3.12"'])
def test_local_extras_bind_to_owned_source(tmp_path, line):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "requirements.txt").write_text(line)
    inputs = pip_inputs(tmp_path)
    assert inputs.local and "{AA2_SOURCE}" in inputs.requirements[0]
    if ";" in line:
        assert 'python_version < "3.12"' in inputs.requirements[0]


def test_file_url_markers_and_external_metadata(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "pkg").mkdir()
    (work / "requirements.txt").write_text(f'pkg @ {(work / "pkg").as_uri()} ; python_version < "3.12"')
    inputs = pip_inputs(work)
    assert inputs.local and 'python_version < "3.12"' in inputs.requirements[0]
    assert (tmp_path / "owned").as_uri() in inputs.rendered(tmp_path / "owned")[0]
    (work / "requirements.txt").unlink()
    external = tmp_path / "external.toml"
    external.write_text('[project]\ndependencies=["example==1"]')
    (work / "pyproject.toml").symlink_to(external)
    with pytest.raises(AA2Error):
        pip_inputs(work)


def test_internal_directory_alias_respects_selected_execution_inputs(tmp_path):
    work, destination = tmp_path / "work", tmp_path / "copy"
    (work / "pkg").mkdir(parents=True)
    (work / "pkg/safe.py").write_text("safe")
    (work / "pkg/.env").write_text("private")
    (work / "alias").symlink_to("pkg", target_is_directory=True)
    copy_execution_source(work, destination)
    assert (destination / "alias/safe.py").read_text() == "safe"
    assert not (destination / "alias/.env").exists()


def test_source_copy_preserves_empty_internal_directory_and_missing_link_target(tmp_path):
    work, destination = tmp_path / "work", tmp_path / "copy"
    (work / "empty").mkdir(parents=True)
    (work / "alias").symlink_to("empty", target_is_directory=True)
    (work / "future").symlink_to("not-created-yet")
    copy_execution_source(work, destination)
    assert (destination / "alias").is_dir()
    assert (destination / "future").is_symlink() and not (destination / "future").exists()


def test_directory_scopes_conflict_and_patch_plans_keep_shared_rules():
    def task(name, scope):
        return TaskSpec(id=name, title=name, description=name, requirement_ids=["R"],
            allowed_paths=[scope], acceptance=["passes"], checks=[Command(argv=["check"])])
    assert conflict(task("a", "src/"), task("b", "src/main.py"))
    assert conflict(task("a", "src/"), task("b", "src/subdir/*"))
    assert "Do not hand-author" in PromptCompiler().compile("plan", {"plan_patch": True})[0]


@pytest.mark.parametrize("kind,event", [("codex", {"type": "item.completed", "item": None}),
    ("codex", {"type": "turn.completed", "usage": []}), ("antigravity", {"event": "result", "result": []})])
def test_malformed_nested_events_have_controlled_failures(kind, event):
    with pytest.raises(ProviderError):
        EventDecoder(kind).feed(event)


def test_html_empty_links_and_declared_encoding():
    parser = TextExtractor()
    parser.feed("<a href>text</a>")
    assert "text" in "".join(parser.parts)
    assert decode_response(b"caf\xe9", "text/html; charset=iso-8859-1") == "café"
    assert "café" in decode_response(b'<meta charset="windows-1252">caf\xe9', "text/html")
    assert decode_response(b"\xef\xbb\xbfhello", "text/plain; charset=iso-8859-1") == "hello"


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://10.0.0.1/", "http://169.254.169.254/",
    "http://[::1]/", "http://localhost/", "http://[::ffff:127.0.0.1]/"])
def test_nonpublic_research_destinations_are_rejected(url):
    with pytest.raises(AA2Error):
        public_url(url)


@pytest.mark.parametrize("protection", ["reference", "pin"])
def test_gc_preserves_protected_descendants(tmp_path, protection):
    store, rid, _, _ = setup(tmp_path)
    resources = ResourceManager(store, Cleanup(min_free_bytes=0, cache_ttl_seconds=0))
    parent = store.root / "environments/parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (child / "marker").write_text("retain")
    resources.register(parent, "environment", refs=0)
    resources.register(child, "environment", refs=int(protection == "reference"))
    if protection == "pin":
        with store.connect() as db:
            db.execute("INSERT INTO delivery_pins VALUES(?,?)", (str(child), rid))
    resources.collect()
    assert (child / "marker").read_text() == "retain"


def test_cache_budget_counts_retained_resources(tmp_path):
    store, _, _, _ = setup(tmp_path)
    resources = ResourceManager(store, Cleanup(min_free_bytes=0, cache_max_bytes=8192))
    for name, size, refs in (("retained", 8192, 1), ("eligible", 4096, 0)):
        path = store.root / "cache" / name
        path.mkdir(parents=True)
        (path / "data").write_bytes(b"x" * size)
        resources.register(path, "cache", refs=refs)
    resources.collect()
    assert (store.root / "cache/retained").exists()
    assert not (store.root / "cache/eligible").exists()


@pytest.mark.parametrize("suffix", ["?extra", "#extra", "%extra"])
def test_sqlite_uri_preserves_special_path_characters(tmp_path, suffix):
    store = Store(tmp_path / ("project" + suffix))
    rid = store.admit("fixture", {})["id"]
    assert Store(store.root.parent, readonly=True).run(rid)["id"] == rid
    assert not (tmp_path / "project").exists()


def test_git_whitespace_and_preimage_through_links(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / " leading.txt").write_text("old")
    assert git(tmp_path, "ls-files", "--others", "-z") == " leading.txt\0"
    (tmp_path / "alias").symlink_to(" leading.txt")
    before = tree_hash(tmp_path)
    patch = prepare_changes(tmp_path, [Change(path=" leading.txt", content="new")], ["*"])
    apply_prepared(tmp_path, patch)
    assert prepared_before_hash(tmp_path, patch) == before


def test_repository_enumeration_failure_never_falls_back(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q")
    def failed(*args, **kwargs):
        raise AA2Error("injected Git failure")
    monkeypatch.setattr("auto_agents_2.workspace.git", failed)
    with pytest.raises(AA2Error):
        tree_hash(tmp_path)


@pytest.mark.parametrize("ingestion", ["dict", "string", "file"])
def test_reader_retains_nested_diagnostics_for_all_ingestion_paths(tmp_path, ingestion):
    store, rid, artifacts, resources = setup(tmp_path)
    log = artifacts.put("diagnostic", "check_log")
    value = {"logs": [log]}
    if ingestion == "file":
        file = tmp_path / "input.json"
        file.write_text(json.dumps(value))
        parent = artifacts.put_file(file, "document")
    else:
        parent = artifacts.put(value if ingestion == "dict" else json.dumps(value))
    store.checkpoint(rid, "receipt", parent)
    store.update(rid, status="SUCCEEDED")
    with artifacts.reading(parent, rid):
        resources.collect_artifacts()
        assert artifacts.read(log) == "diagnostic"
    resources.collect_artifacts()
    assert artifacts.released(log)


def test_reader_roles_are_order_independent_and_corruption_fails_closed(tmp_path):
    store, rid, artifacts, _ = setup(tmp_path)
    log = artifacts.put("same", "check_log")
    document = artifacts.put("same", "document")
    for refs in ([log, document], [document, log]):
        with artifacts.reading(refs, rid), store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM diagnostic_holds WHERE purpose='reader'").fetchone()[0] == 1
    parent = artifacts.put({"log": log})
    (artifacts.root / parent["hash"][:2] / parent["hash"]).write_text("tampered")
    with pytest.raises(ValueError), artifacts.reading(parent, rid):
        pytest.fail("consumer must not start")


def test_opaque_json_examples_cannot_invent_required_artifact_dependencies(tmp_path):
    _, rid, artifacts, _ = setup(tmp_path)
    example = {"example": {"hash": "f" * 64, "kind": "check_log", "bytes": 42}}
    parent = artifacts.put(json.dumps(example), "research_response")
    with artifacts.reading(parent, rid):
        assert artifacts.json(parent) == example
    with pytest.raises(ValueError), artifacts.reading(example, rid):
        pytest.fail("explicit missing evidence must still fail closed")


def test_report_capture_rejects_links_and_oversized_sparse_files(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("fake secret")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    report = scratch / "junit.xml"
    report.symlink_to(outside)
    with pytest.raises(OSError):
        report_bytes(scratch)
    report.unlink()
    with report.open("wb") as stream:
        stream.truncate(MAX_REPORT_BYTES + 1)
    with pytest.raises(ValueError):
        report_bytes(scratch)
    assert outside.read_text() == "fake secret"


async def test_verifier_rejects_external_executable_alias(tmp_path):
    store = Store(tmp_path)
    (tmp_path / "external").symlink_to("/usr/bin/true")
    with pytest.raises(PolicyViolation):
        await VerificationService(store.root, SandboxRunner(store.root)).run(
            [Command(argv=["./external"])], tmp_path, Environment(Path(sys.prefix), "fixture"))


async def test_sandbox_report_cannot_archive_controller_only_file(tmp_path):
    store = Store(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    secret = store.root / "controller-only.txt"
    secret.write_text("FAKE-SECRET-FOR-REGRESSION")
    (work / "test_probe.py").write_text(
        "import atexit,sys\nfrom pathlib import Path\n"
        f"def test_boundary():\n    assert not Path({str(secret)!r}).exists()\n"
        "@atexit.register\ndef replace_report():\n"
        "    report=Path(next(a.split('=',1)[1] for a in sys.argv if a.startswith('--junitxml=')))\n"
        "    report.unlink(missing_ok=True)\n"
        f"    report.symlink_to({str(secret)!r})\n")
    verifier = VerificationService(store.root, SandboxRunner(store.root))
    result = (await verifier.run([Command(argv=[sys.executable, "-m", "pytest", "-q"], expected_tests=1)],
                                work, Environment(Path(sys.prefix), "fixture")))[0]
    assert result.process_exit_code == 0 and result.exit_code != 0
    assert result.report_status == "invalid" and not result.report
    sha = digest(secret.read_bytes())
    assert not (verifier.artifacts.root / sha[:2] / sha).exists()


async def test_termination_finishes_with_full_pipes_and_ignored_sigterm(tmp_path):
    runner = SandboxRunner(tmp_path / "state", _test_unconfined=True)
    process = await runner.spawn([sys.executable, "-c", "import os,signal,time; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); os.write(1,b'x'*24000000); time.sleep(60)"],
        tmp_path, dict(os.environ), [])
    try:
        await asyncio.sleep(.4)
        await asyncio.wait_for(runner.stop(process), 6)
        assert process.returncode == -signal.SIGKILL
    finally:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await process.communicate()


async def test_registration_failure_prevents_unmanaged_process_launch(tmp_path, monkeypatch):
    processes = []
    spawn = asyncio.create_subprocess_exec
    async def capture(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process
    def failed(*args):
        raise OSError("registration failed")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    monkeypatch.setattr("auto_agents_2.managed_process.write_json", failed)
    runner = SandboxRunner(tmp_path / "state", _test_unconfined=True)
    with pytest.raises(OSError, match="registration failed"):
        await runner.spawn([sys.executable, "-c", "import time; time.sleep(60)"], tmp_path, dict(os.environ), [])
    assert not processes
