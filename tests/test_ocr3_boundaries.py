import os
import subprocess
import sys
import zipfile

import httpx
import pytest
from pydantic import BaseModel
from test_runtime import configured

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.builds import runtime_commands
from auto_agents_2.context import ContextBuilder
from auto_agents_2.dependencies import copy_execution_source, pip_inputs
from auto_agents_2.environment import Environment, EnvironmentManager
from auto_agents_2.errors import AA2Error, ConfigurationError, PolicyViolation, ProviderError
from auto_agents_2.evidence import input_identity
from auto_agents_2.models import Command, Coverage, IntakeResult, Requirement, ReviewResult, strict_schema
from auto_agents_2.profiles import ProjectProfile
from auto_agents_2.providers import ProviderRouter
from auto_agents_2.readview import public_files, sensitive_values
from auto_agents_2.requirements import extract_document, validate_coverage
from auto_agents_2.research import ResearchService
from auto_agents_2.safe_files import read_regular
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.store import Store
from auto_agents_2.util import digest, scrub_text, write_json
from auto_agents_2.workspace import WorkspaceManager, git, tree_hash


@pytest.mark.parametrize("kind", ["symlink", "parent_link", "fifo", "oversized"])
def test_result_ingress_rejects_unsafe_files(tmp_path, kind):
    original = tmp_path / "private"
    original.write_bytes(b"private")
    directory = tmp_path / "out"
    directory.mkdir()
    result = directory / "result.json"
    if kind == "symlink":
        result.symlink_to(original)
    elif kind == "parent_link":
        result.write_bytes(b"private")
        (tmp_path / "alias").symlink_to(directory)
        result = tmp_path / "alias/result.json"
    elif kind == "fifo":
        os.mkfifo(result)
    else:
        with result.open("wb") as stream:
            stream.truncate(33 * 1024 * 1024)
    with pytest.raises((OSError, ValueError)):
        read_regular(result, 32 * 1024 * 1024)
    assert original.read_bytes() == b"private"


@pytest.mark.parametrize("mode", ["link", "fifo", "large", "missing"])
async def test_provider_result_boundary_in_real_sandbox(tmp_path, mode):
    project = tmp_path / "project"
    project.mkdir()
    config, store = configured(project)
    rid = store.admit("fixture", {})["id"]
    private = store.root / "private.json"
    write_json(private, ReviewResult(approved=True, summary="PRIVATE-FIXTURE").model_dump())
    binary = tmp_path / "provider"
    statement = {"link": f"output.symlink_to({str(private)!r})", "fifo": "os.mkfifo(output)",
                 "large": "output.write_bytes(b' ' * (33 * 1024 * 1024))", "missing": "pass"}[mode]
    binary.write_text(f"#!{sys.executable}\nimport os,sys,json\nfrom pathlib import Path\n"
        f"assert not Path({str(private)!r}).exists()\n"
        "sys.stdin.read()\noutput=Path(sys.argv[sys.argv.index('-o')+1])\n" + statement + "\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':"
        + repr(ReviewResult(approved=True, summary="stdout fixture").model_dump_json()) + "}}))\n"
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))\n")
    binary.chmod(0o755)
    native = tmp_path / "native"
    native.mkdir()
    body = config.load().model_dump(mode="json")
    body["providers"]["codex"].update(binary=str(binary), environment={"CODEX_HOME": str(native)})
    write_json(config.path, body)
    router = ProviderRouter(config, store, SandboxRunner(store.root))
    if mode == "missing":
        result = await router._call(config.resolve("review"), "review", {}, ReviewResult, project, rid, "fixture", True)
        assert result.summary == "stdout fixture"
    else:
        with pytest.raises(ProviderError, match="response file"):
            await router._call(config.resolve("review"), "review", {}, ReviewResult, project, rid, "fixture", True)
        assert store.get(rid, "invocation/fixture") is None
    assert not list((store.root / "processes").glob("*.json"))


@pytest.mark.parametrize("content,value", [
    ("DB_PASS=local-password # comment\n", "local-password"),
    ("export DB_PASS='quoted password' # comment\n", "quoted password"),
    ('DB_PASS="line one\nline two"\n', "line one\nline two"),
    ('DB_PASS="line one\\nline two"\n', "line one\nline two"),
    ("//registry.invalid/:_authToken=private-token\n", "private-token"),
])
def test_protected_values_redact_logs_without_filtering_ordinary_files(tmp_path, content, value):
    (tmp_path / ".env").write_text(content)
    (tmp_path / "public.py").write_text("alias=" + value)
    assert value in sensitive_values(tmp_path)
    assert tmp_path / "public.py" in public_files(tmp_path)


@pytest.mark.parametrize("name,directory", [(".env", False), (".aws", True), ("secrets", True)])
def test_sensitive_links_are_omitted_without_blocking_ordinary_files(tmp_path, name, directory):
    target = tmp_path / "ordinary"
    if directory:
        target.mkdir()
        (target / "text").write_text("fixture")
    else:
        target.write_text("DB_PASS=fixture")
    link = tmp_path / name
    link.symlink_to(target)
    assert link not in public_files(tmp_path, ["secrets"])
    assert link.is_symlink() and target.exists()


@pytest.mark.parametrize("text,key", [("https://x.invalid/?token=private-value", "token"),
    ("Authorization: Bearer private-value", "Bearer"), ("https://user:private-value@x.invalid/", "user")])
def test_explicit_redaction_cannot_hide_a_credential_pattern(text, key):
    result = scrub_text(text, [key])
    assert "private-value" not in result


@pytest.mark.parametrize("payload,content_type,expected", [
    (b"Use x <", "text/html", "Use x <"), (b"Use x &", "Text/HTML", "Use x &"),
    ("<p>中文</p>".encode("utf-16"), "TEXT/HTML", "中文"),
    (b'<meta charset="windows-1252"><p>caf\xe9</p>', "Text/HTML", "café"),
])
async def test_text_entrypoints_share_decoder_and_finalize_html(tmp_path, monkeypatch, payload, content_type, expected):
    text, encoding = extract_document(payload, content_type)
    assert expected in text and "<p>" not in text
    monkeypatch.setattr("auto_agents_2.research.PublicHTTPTransport", lambda: httpx.MockTransport(
        lambda request: httpx.Response(200, content=payload, headers={"content-type": content_type})))
    artifacts = ArtifactStore(Store(tmp_path).root)
    result = await ResearchService(artifacts).capture("https://example.com/document")
    assert artifacts.read(result["artifact"]) == text and result["encoding"] == encoding


@pytest.mark.parametrize("path", ["../outside", "/etc/passwd", "escape"])
def test_invalid_coverage_evidence_is_a_problem(tmp_path, path):
    (tmp_path / "escape").symlink_to("/etc/passwd")
    result = IntakeResult(summary="existing", document="text",
        requirements=[Requirement(id="R1", description="behaviour", acceptance=["exists"])],
        coverage=[Coverage(requirement_id="R1", status="satisfied", rationale="claimed",
                           evidence_paths=[path])])
    valid, problems = validate_coverage(result, tmp_path)
    assert not valid and problems


def test_dictionary_schema_is_rejected_without_changing_normal_schema():
    class Mapping(BaseModel):
        values: dict[str, str]
    original = Mapping.model_json_schema()
    with pytest.raises(ConfigurationError, match="maps"):
        strict_schema(Mapping)
    assert Mapping.model_json_schema() == original
    assert strict_schema(ReviewResult)["additionalProperties"] is False


@pytest.mark.parametrize("body", ["{bad", "null", "[]", '{"dependencies":null}'])
def test_profile_detection_wraps_invalid_manifest_even_when_selected(tmp_path, body):
    (tmp_path / "package.json").write_text(body)
    with pytest.raises(AA2Error, match="package.json"):
        ProjectProfile.detect(tmp_path, "python")


@pytest.mark.parametrize("body", ["invalid private fixture", "[]", "null"])
@pytest.mark.parametrize("action", ["select_target", "activate"])
def test_config_writers_validate_without_mutation_or_disclosure(tmp_path, body, action):
    config, _ = configured(tmp_path)
    config.path.write_text(body)
    with pytest.raises(ConfigurationError) as error:
        getattr(config, action)("main" if action == "select_target" else "codex")
    assert body not in str(error.value) and config.path.read_text() == body


def test_input_walk_errors_cannot_produce_a_reusable_hash(tmp_path):
    child = tmp_path / "frontend"
    child.mkdir()
    child.chmod(0)
    try:
        with pytest.raises(PermissionError):
            input_identity(tmp_path, Command(argv=["true"]))
    finally:
        child.chmod(0o755)


@pytest.mark.parametrize("body", [None, [], 42, {"fingerprint": []}, {"fingerprint": "x", "integrity": {}}])
def test_cache_marker_invalid_shapes_are_cache_misses(tmp_path, body):
    write_json(tmp_path / "aa2-ready.json", body)
    assert not EnvironmentManager.valid(tmp_path, "x")


def test_runtime_identity_probe_survives_python_optimization(tmp_path):
    command = runtime_commands(None, tmp_path, Environment(tmp_path / "other", "fixture"))[0]
    result = subprocess.run([sys.executable, "-O", *command.argv[1:]], capture_output=True)
    assert result.returncode == 1


@pytest.mark.parametrize("name", ["carriage\rreturn.py", os.fsdecode(b"raw-\xff.py")])
def test_git_file_identity_and_private_state_are_lossless(tmp_path, name):
    manager = WorkspaceManager(tmp_path)
    manager.initialize()
    (tmp_path / name).write_text("one")
    manager.checkpoint(tmp_path, "refs/aa2/fixture", "baseline")
    assert name in git(tmp_path, "ls-files", "-z").split("\0")
    before = tree_hash(tmp_path)
    (tmp_path / name).write_text("two")
    assert tree_hash(tmp_path) != before
    store = Store(tmp_path)
    rid = store.admit("fixture", {})["id"]
    store.checkpoint(rid, "files", {"name": name})
    assert store.get(rid, "files") == {"name": name}
    if "\udcff" in name:
        with pytest.raises(PolicyViolation, match="Non-UTF-8"):
            ContextBuilder().build(tmp_path)


@pytest.mark.parametrize("form", ["wheel", "--find-links", "-f", "file_uri"])
def test_explicit_local_wheels_survive_private_copy_and_offline_install(tmp_path, form):
    work = tmp_path / "work"
    wheel = work / "dist/ocr_fixture-1.0-py3-none-any.whl"
    wheel.parent.mkdir(parents=True)
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("ocr_fixture.py", "VALUE=42\n")
        archive.writestr("ocr_fixture-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: ocr-fixture\nVersion: 1.0\n")
        archive.writestr("ocr_fixture-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr("ocr_fixture-1.0.dist-info/RECORD", "")
    requirement = "./dist/" + wheel.name if form == "wheel" else "ocr-fixture==1.0"
    option = [] if form == "wheel" else ["--find-links " + wheel.parent.as_uri() if form == "file_uri" else form + " ./dist"]
    inputs = pip_inputs(work, additional=["--no-index", *option, requirement])
    original_identity = digest(inputs.local_paths)
    destination = tmp_path / "private"
    copy_execution_source(work, destination, inputs)
    wheel.unlink()  # A successful resolution now proves use of the owned copy.
    selected = tmp_path / "selected.txt"
    selected.write_text(inputs.rendered(destination)[0])
    target = tmp_path / "installed"
    result = subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--no-deps",
        "--target", str(target), "-r", str(selected)], capture_output=True, text=True, timeout=30,
        env={**os.environ, "PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
    assert result.returncode == 0, result.stderr
    assert (target / "ocr_fixture.py").read_text() == "VALUE=42\n"
    wheel.write_bytes(b"changed")
    assert digest(pip_inputs(work, additional=option + [requirement]).local_paths) != original_identity


@pytest.mark.parametrize("line", ["--cert ./client.pem", "--find-links ../outside", "./.env", "./.aa2/cache.whl"])
def test_unsupported_or_protected_local_inputs_are_rejected(tmp_path, line):
    (tmp_path / ".env").write_text("secret")
    (tmp_path / ".aa2").mkdir()
    (tmp_path / ".aa2/cache.whl").touch()
    with pytest.raises(AA2Error):
        pip_inputs(tmp_path, additional=[line])
