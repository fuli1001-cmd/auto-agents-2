"""Boundary regressions validated against the second OCR report."""
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import httpcore
import httpx
import pytest
from test_runtime import configured
from typer.testing import CliRunner

from auto_agents_2 import builds, dependencies, evidence, requirements
from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.cli import app
from auto_agents_2.config import validate_extra_args
from auto_agents_2.design import PreviewServer
from auto_agents_2.environment import Environment, EnvironmentManager
from auto_agents_2.errors import AA2Error, ConfigurationError, PolicyViolation, VerificationPreparationError
from auto_agents_2.interaction import question_text
from auto_agents_2.models import BrowserAction, Command, PreviewSpec
from auto_agents_2.network import PreviewProxy, PublicHTTPTransport, ResponseStream
from auto_agents_2.readview import public_files
from auto_agents_2.resources import ResourceManager
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.store import Store
from auto_agents_2.util import file_lock, scrub_text, write_json
from auto_agents_2.verification import VerificationService


@pytest.mark.parametrize("mode", ["json", "copy", "hardlink", "embedded"])
def test_ordinary_aliases_are_admitted_by_path_policy(tmp_path, mode):
    secret = tmp_path / (".credentials/config.json" if mode == "json" else "build/.env")
    secret.parent.mkdir(parents=True)
    value = "FIXTURE-PRIVATE-VALUE"
    secret.write_text(json.dumps({"primary": value}) if mode == "json" else "TOKEN=" + value)
    public = tmp_path / "public.txt"
    if mode == "hardlink":
        os.link(secret, public)
    else:
        public.write_text(secret.read_text() if mode == "copy" else "prefix " + value + " suffix")
    safe = tmp_path / "safe.txt"
    safe.write_text("ordinary source")
    assert public in public_files(tmp_path) and safe in public_files(tmp_path)


@pytest.mark.parametrize("secret", ["x", "ab", "abc"])
def test_short_explicit_credentials_are_scrubbed(secret):
    assert secret not in scrub_text("credential is " + secret, [secret])


@pytest.mark.parametrize("link", ["file", "directory", "index"])
def test_static_server_never_follows_links(tmp_path, link):
    private = tmp_path / "private"
    private.mkdir()
    (private / "secret.txt").write_text("FAKE-PRIVATE")
    public = tmp_path / "public"
    public.mkdir()
    (public / "safe.txt").write_text("public")
    if link == "file":
        (public / "secret.txt").symlink_to(private / "secret.txt")
        route = "secret.txt"
    elif link == "directory":
        (public / "linked").symlink_to(private)
        route = "linked/secret.txt"
    else:
        (public / "index.html").symlink_to(private / "secret.txt")
        route = ""
    with PreviewServer(public) as server, httpx.Client(trust_env=False) as client:
        assert client.get(server.url + route).status_code == 404
        assert client.get(server.url + "safe.txt").text == "public"


def test_gc_lock_and_intermediate_resource_link(tmp_path):
    cfg, store = configured(tmp_path)
    resources = ResourceManager(store, cfg.load().cleanup)
    candidate = store.root / "tmp/a/item"
    candidate.mkdir(parents=True)
    resources.register(candidate, "temporary", refs=0)
    with file_lock(store.root / "controller.lock"):
        result = CliRunner().invoke(app, ["gc", "--project", str(tmp_path)])
    assert result.exit_code != 0 and candidate.exists()
    protected = store.root / "environments/live/item"
    protected.mkdir(parents=True)
    resources.register(protected, "temporary", refs=1)
    candidate.rmdir()
    candidate.parent.rmdir()
    candidate.parent.symlink_to(protected.parent)
    result = resources.collect()
    assert result["errors"] and protected.is_dir() and result["removed"] == 0


@pytest.mark.parametrize("pinned", [False, True])
def test_gc_dry_run_uses_effective_references_and_descendant_pins(tmp_path, pinned):
    cfg, store = configured(tmp_path)
    resources = ResourceManager(store, cfg.load().cleanup)
    rid = store.admit("fixture", {})["id"]
    path = store.root / "tmp/item"
    path.mkdir(parents=True)
    resources.register(path, "temporary", rid, refs=0 if pinned else 1)
    if pinned:
        (path / "pin").mkdir()
        with store.connect() as db:
            db.execute("INSERT INTO delivery_pins VALUES(?,?)", (str(path / "pin"), rid))
    else:
        store.update(rid, status="CANCELLED")
    before = store.path.read_bytes()
    dry = resources.collect(True)
    assert store.path.read_bytes() == before
    actual = resources.collect()
    assert dry["candidates"] == actual["candidates"]
    assert actual["removed"] == int(not pinned)


def test_generated_input_alias_orders_and_blocks_consumer(tmp_path):
    (tmp_path / "frontend").mkdir()
    (tmp_path / "artifacts").symlink_to("frontend/dist")
    build = Command(argv=["build"], cwd="frontend", outputs=["frontend/dist"])
    check = Command(argv=["check"], inputs=["artifacts/index.html"])
    assert builds.verification_commands([check], tmp_path, [build]) == [build, check]
    assert builds.overlaps(tmp_path, "artifacts/index.html", "frontend/dist")


def test_mutable_only_build_is_not_immutable_output(tmp_path):
    (tmp_path / ".next/cache").mkdir(parents=True)
    (tmp_path / ".next/cache/item").write_text("cache")
    command = Command(argv=["build"], outputs=[".next"])
    with pytest.raises(VerificationPreparationError):
        builds.output_hashes(command, tmp_path)
    (tmp_path / ".next/server").mkdir()
    (tmp_path / ".next/server/app.js").write_text("code")
    assert builds.output_hashes(command, tmp_path)


@pytest.mark.parametrize("body", ["{", "[]", '{"dependencies":null}'])
def test_build_manifest_errors_are_recoverable(tmp_path, body):
    (tmp_path / "package.json").write_text(body)
    with pytest.raises(VerificationPreparationError):
        builds.normalized(Command(argv=["npm", "run", "build"]), tmp_path)


def test_nested_dependencies_and_prefix_assets_change_identity(tmp_path):
    item = tmp_path / "packages/app/node_modules/pkg/code.js"
    item.parent.mkdir(parents=True)
    item.write_text("v1")
    command = Command(argv=["node", "check.js"], cwd="packages/app")
    before = evidence.input_identity(tmp_path, command)
    item.write_text("v2")
    assert evidence.input_identity(tmp_path, command) != before
    prefix = tmp_path / "prefix"
    asset = prefix / "share/jupyter/template"
    asset.parent.mkdir(parents=True)
    asset.write_text("v1")
    before = EnvironmentManager.integrity(prefix)
    asset.unlink()
    assert EnvironmentManager.integrity(prefix) != before


@pytest.mark.parametrize("args", [["-c", "model"], ["-c", "model="], ["--model="], ["--print-timeout=",], ["--model", ""]])
def test_empty_provider_options_rejected(args):
    with pytest.raises(ConfigurationError):
        validate_extra_args(args)


@pytest.mark.parametrize("mode", ["binary", "provider_env", "profile_env", "native_base"])
def test_execution_inputs_invalidate_provider_session_identity(tmp_path, mode):
    config, _ = configured(tmp_path)
    body = config.load().model_dump(mode="json")
    native = tmp_path / "native"
    native.mkdir()
    base = native / "config.toml"
    base.write_text('model_provider="first"\n')
    body["providers"]["codex"]["environment"] = {"CODEX_HOME": str(native)}
    write_json(config.path, body)
    first = config.resolve("review")["fingerprint"]
    if mode == "binary":
        executable = tmp_path / "provider-fixture"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        body["providers"]["codex"]["binary"] = str(executable)
    elif mode == "provider_env":
        body["providers"]["codex"]["environment"]["FIXTURE_ENDPOINT"] = "second"
    elif mode == "profile_env":
        body["providers"]["codex"]["profile_map"]["balanced"]["environment"] = {"FIXTURE_ENDPOINT": "second"}
    else:
        base.write_text('model_provider="second"\n')
    write_json(config.path, body)
    assert config.resolve("review")["fingerprint"] != first


def test_empty_pip_entries_and_local_wheel_options(tmp_path):
    assert not dependencies.pip_inputs(tmp_path, additional=["", "  "]).local
    wheel = tmp_path / "with space/pkg-1.0-py3-none-any.whl"
    wheel.parent.mkdir()
    wheel.touch()
    value = dependencies.pip_inputs(tmp_path, additional=[f'"./with space/{wheel.name}" --hash=sha256:abc'])
    rendered, _ = value.rendered(tmp_path)
    assert rendered.strip() == wheel.as_uri() + " --hash=sha256:abc"


@pytest.mark.parametrize("additional", [False, True])
def test_include_quote_failures_are_domain_errors(tmp_path, additional):
    line = '-r "unterminated'
    if not additional:
        (tmp_path / "requirements.txt").write_text(line)
    with pytest.raises(AA2Error):
        dependencies.pip_inputs(tmp_path, additional=[line] if additional else [])


def test_question_prefix_keeps_paragraph_and_indentation():
    assert question_text("Heading\nR008:\n\n    indented text") == "Heading\n\n\n    indented text"


def test_hidden_html_elements_do_not_make_a_document():
    parser = requirements.TextExtractor()
    parser.feed('<script id="bootstrap">hidden()</script><style id="style">body{}</style>')
    assert not "".join(parser.parts).strip()


async def test_html_media_type_case_is_normalized(tmp_path, monkeypatch):
    client = httpx.AsyncClient
    monkeypatch.setattr(requirements.httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, headers={"content-type": "Text/HTML; charset=utf-8"},
                                  content=b"<h1>Hello</h1><script>hidden()</script>")), **kw))
    art = ArtifactStore(Store(tmp_path).root)
    captured = await requirements.capture("https://fixture.invalid", art)
    text = art.read(captured["artifact"])
    assert "Hello" in text and "hidden" not in text and "<h1>" not in text


async def test_local_requirements_read_is_bounded_despite_stale_stat(tmp_path, monkeypatch):
    source = tmp_path / "input.md"
    source.write_bytes(b"x" * (requirements.MAX_REQUIREMENTS_BYTES + 1))
    original = Path.stat
    monkeypatch.setattr(Path, "stat", lambda path, *a, **kw: SimpleNamespace(st_size=1) if path == source else original(path, *a, **kw))
    with pytest.raises(AA2Error, match="4 MiB"):
        await requirements.capture(str(source), ArtifactStore(Store(tmp_path).root))


@pytest.mark.parametrize("kind", ["ReadTimeout", "WriteError", "PoolTimeout", "RemoteProtocolError"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_http_exception_types_preserved(kind, streaming):
    error = getattr(httpcore, kind)
    class Stream:
        async def __aiter__(self):
            yield b"partial"
            raise error("fixture")
    if streaming:
        with pytest.raises(getattr(httpx, kind)):
            async for _ in ResponseStream(Stream()):
                pass
    else:
        transport = PublicHTTPTransport()
        async def fail(*a):
            raise error("fixture")
        transport.pool.handle_async_request = fail
        try:
            with pytest.raises(getattr(httpx, kind)):
                await transport.handle_async_request(httpx.Request("GET", "https://fixture.invalid"))
        finally:
            await transport.aclose()


@pytest.mark.parametrize("connect", [False, True])
async def test_proxy_half_close_keeps_remaining_response(connect):
    done = asyncio.Event()
    async def serve(reader, writer):
        try:
            await reader.read()
            writer.write(b"complete-response")
            await writer.drain()
        finally:
            writer.close()
            done.set()
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    proxy = await PreviewProxy(f"http://127.0.0.1:{port}", []).start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.server.sockets[0].getsockname()[1])
        target = f"127.0.0.1:{port}" if connect else f"http://127.0.0.1:{port}/"
        writer.write(f"{'CONNECT' if connect else 'GET'} {target} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        if connect:
            assert b"200" in await reader.readuntil(b"\r\n\r\n")
        writer.write_eof()
        assert await asyncio.wait_for(reader.read(), 2) == b"complete-response"
        writer.close()
        await done.wait()
    finally:
        await proxy.close()
        server.close()
        await server.wait_closed()


async def test_sandbox_attachment_root_cannot_read_controller_file(tmp_path, monkeypatch):
    store = Store(tmp_path)
    secret = store.root / "controller-only/private.txt"
    secret.parent.mkdir()
    secret.write_text("FAKE-PRIVATE-FIXTURE")
    work = tmp_path / "work"
    work.mkdir()
    (work / "probe.py").write_text("import os\nfrom pathlib import Path\n"
        f"assert not Path({str(secret)!r}).exists()\n"
        f"(Path(os.environ['TMPDIR'])/'playwright-results').symlink_to({str(secret.parent)!r})\n")
    monkeypatch.setattr("auto_agents_2.verification.test_runner", lambda *a: "playwright")
    monkeypatch.setattr("auto_agents_2.verification.report_command", lambda argv, *a, **kw: (argv, {}))
    service = VerificationService(store.root, SandboxRunner(store.root))
    row = (await service.run([Command(argv=[sys.executable, "probe.py"])], work, Environment(Path(sys.prefix), "fixture")))[0]
    assert row.process_exit_code == 0 and not row.attachments
    assert "test_attachments_invalid" in row.failure_reasons


def test_preview_actions_require_declared_route():
    with pytest.raises(ValueError, match="declared route"):
        PreviewSpec(command=Command(argv=["server", "{port}"]), actions=[BrowserAction(
            route="/checkout", action="expect_visible", selector="#button")])


def test_hidden_project_executable_cannot_be_implicitly_mounted(tmp_path):
    hidden = tmp_path / "private"
    hidden.mkdir()
    binary = hidden / "tool"
    binary.write_text("#!/bin/sh\nexit 0")
    binary.chmod(0o755)
    public = tmp_path / "public"
    public.mkdir()
    with pytest.raises(PolicyViolation, match="protected project"):
        SandboxRunner(tmp_path / "state").command([str(binary)], public, [], hidden=[hidden])


@pytest.mark.parametrize("valid", [True, False])
def test_local_wheel_hash_is_enforced_by_real_offline_pip(tmp_path, valid):
    import hashlib
    import subprocess
    import zipfile
    wheel = tmp_path / "wheel store/ocr_fixture-1.0-py3-none-any.whl"
    wheel.parent.mkdir()
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("ocr_fixture.py", "VALUE = 1\n")
        archive.writestr("ocr_fixture-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: ocr-fixture\nVersion: 1.0\n")
        archive.writestr("ocr_fixture-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr("ocr_fixture-1.0.dist-info/RECORD", "")
    sha = hashlib.sha256(wheel.read_bytes()).hexdigest() if valid else "0" * 64
    inputs = dependencies.pip_inputs(tmp_path, additional=[f'"./wheel store/{wheel.name}" --hash=sha256:{sha}'])
    rendered, _ = inputs.rendered(tmp_path)
    requirements_file = tmp_path / "selected.txt"
    requirements_file.write_text(rendered)
    result = subprocess.run([sys.executable, "-m", "pip", "install", "--dry-run", "--no-index", "--no-deps",
        "--require-hashes", "-r", str(requirements_file)], capture_output=True, text=True, timeout=30,
        env={**os.environ, "PIP_CONFIG_FILE": os.devnull, "PIP_CACHE_DIR": str(tmp_path / "cache"), "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
    assert (result.returncode == 0) == valid, result.stdout + result.stderr
    assert "Would install" in result.stdout if valid else "DO NOT MATCH" in result.stderr
