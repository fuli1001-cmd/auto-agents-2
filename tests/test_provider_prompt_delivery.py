import copy
import json
import sys

import pytest
from test_runtime import configured

from auto_agents_2.errors import ProviderError
from auto_agents_2.models import ReviewResult
from auto_agents_2.prompts import PromptCompiler
from auto_agents_2.providers import ProviderRouter, diagnostic_summary
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.transports import CodexTransport
from auto_agents_2.util import atomic_write, digest, write_json

LIMIT = 1048576  # Observed turn/start boundary in Codex CLI 0.163.0.


def fixture_router(tmp_path, *, reject_probe=False):
    project = tmp_path / "project"
    project.mkdir()
    config, store = configured(project)
    native = tmp_path / "native"
    native.mkdir()
    binary = tmp_path / "provider"
    binary.write_text(f"#!{sys.executable}\n" + f"REJECT_PROBE = {reject_probe!r}\n" + '''
import hashlib, json, pathlib, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("fixture-cli 1")
    sys.exit(0)
assert args[args.index("-s") + 1] == "read-only"
schema_path = pathlib.Path(args[args.index("--output-schema") + 1])
schema = json.loads(schema_path.read_text())
wire = sys.stdin.read()
if len(wire) > 1048576:
    print('Error: turn/start failed: Input exceeds the maximum length of 1048576 characters. '
          'data: {"input_error_code":"input_too_large"}', file=sys.stderr)
    sys.exit(1)
if "text_token" in schema["properties"]:
    context = json.loads(wire.split("<context>\\n", 1)[1].split("\\n</context>", 1)[0])
    result = {"text_token": "wrong" if REJECT_PROBE else pathlib.Path(context["context_files"][0]).read_text(),
              "image_token": ""}
else:
    descriptor = json.loads(wire.split("<request_file>\\n", 1)[1].split("\\n</request_file>", 1)[0])
    path = pathlib.Path(descriptor["path"])
    assert path == schema_path.parent / "prompt.txt"
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == descriptor["sha256"]
    text = raw.decode("utf-8")
    assert len(text) == descriptor["characters"] > 1048576
    try:
        path.write_text("must remain read-only")
    except OSError:
        pass
    else:
        raise AssertionError("Prompt file is writable")
    context = json.loads(text.split("<context>\\n", 1)[1].split("\\n</context>", 1)[0])
    assert context["mandatory"] == ["FIRST_OBLIGATION", "LAST_OBLIGATION"]
    assert context["document"] == "FIRST_FACT\\r\\n" + "研究资料🙂" * 220000 + "\\nLAST_FACT"
    result = {"approved": True, "summary": descriptor["sha256"], "findings": []}
pathlib.Path(args[args.index("-o") + 1]).write_text(json.dumps(result))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
''')
    binary.chmod(0o755)
    body = config.load().model_dump(mode="json")
    body["providers"] = {"codex": body["providers"]["codex"]}
    body["providers"]["codex"].update(binary=str(binary), environment={"CODEX_HOME": str(native)})
    write_json(config.path, body)
    rid = store.admit("fixture", {})["id"]
    return ProviderRouter(config, store, SandboxRunner(store.root)), project, store, rid


def large_context():
    return {"mandatory": ["FIRST_OBLIGATION", "LAST_OBLIGATION"],
            "document": "FIRST_FACT\r\n" + "研究资料🙂" * 220000 + "\nLAST_FACT"}


async def test_large_prompt_is_complete_read_only_and_probed_before_invocation(tmp_path):
    router, project, store, rid = fixture_router(tmp_path)
    context = large_context()
    original = copy.deepcopy(context)
    result = await router.invoke("review", context, ReviewResult, project, rid, "large", fresh=True)
    assert result.approved and context == original
    manifest = store.get(rid, "materials/large")
    with store.connect() as db:
        frozen = json.loads(db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key LIKE 'presentation/large/%'", (rid,)).fetchone()[0])
    from auto_agents_2.artifacts import ArtifactStore
    payload = ArtifactStore(store.root).json(frozen["artifact"])["payload"]
    assert payload["mandatory"] == context["mandatory"] and payload["document"] == context["document"]
    full_prompt, _ = PromptCompiler().compile("review", {**payload, "context_files": []}, "test")
    assert result.summary == manifest["prompt_hash"] == digest(full_prompt)
    assert manifest["prompt_characters"] == len(full_prompt) > LIMIT
    assert manifest["input"]["characters"] < LIMIT
    assert manifest["input"]["max_characters"] == LIMIT
    assert manifest["presentation"] == "referenced_full_prompt"
    assert manifest["omitted_mandatory_content"] == []
    assert manifest["capability"]["native_read_test"] == "observed_read"
    assert manifest["materials"][0]["purpose"] == "prompt"
    assert manifest["materials"][0]["hash"] == digest(full_prompt)
    assert store.usage_summary(rid)["total_tokens"] == 4
    assert not list((store.root / "processes").glob("*.json"))
    assert not list((store.root / "attempts" / rid).iterdir())


async def test_failed_read_probe_never_dispatches_large_request_or_saves_acceptance(tmp_path):
    router, project, store, rid = fixture_router(tmp_path, reject_probe=True)
    with pytest.raises(ProviderError, match="read challenge failed"):
        await router.invoke("review", large_context(), ReviewResult, project, rid, "large", fresh=True)
    starts = [e["data"] for e in store.events(rid) if e["kind"] == "agent.started"]
    assert [s["stage"] for s in starts] == ["material_probe"]
    assert store.get(rid, "invocation/large") is None
    assert store.get(rid, "provider-result/large") is None
    assert not list((store.root / "processes").glob("*.json"))
    assert not list((store.root / "attempts" / rid).iterdir())


@pytest.mark.parametrize("message", [
    'Error: Input exceeds the maximum length of 1048576 characters.',
    '{"input_error_code":"input_too_large","max_chars":1048576,"actual_chars":1562609}',
])
def test_native_input_limit_is_diagnosed_without_disclosing_arbitrary_output(message):
    assert diagnostic_summary(message + "\nretry connection API_KEY=EXAMPLE_PLACEHOLDER") == "provider 输入超过 CLI 长度上限"


@pytest.mark.parametrize("length", [LIMIT - 1, LIMIT, LIMIT + 1])
def test_prompt_delivery_uses_native_character_limit_without_changing_smaller_inputs(tmp_path, length):
    from auto_agents_2.materials import prepare_prompt
    prompt = "界" * length
    path = tmp_path / "prompt.txt"
    delivery = prepare_prompt(prompt, path, CodexTransport.max_prompt_characters)
    if length <= LIMIT:
        assert delivery.text == prompt and delivery.file is None
    else:
        assert len(delivery.text) < LIMIT and delivery.file == path
        descriptor = json.loads(delivery.text.split("<request_file>\n", 1)[1].split("\n</request_file>", 1)[0])
        assert descriptor == {"path": str(path), "sha256": digest(prompt), "characters": length}


def test_provider_without_native_limit_keeps_complete_inline_prompt(tmp_path):
    from auto_agents_2.materials import prepare_prompt
    from auto_agents_2.transports import AntigravityTransport
    prompt = "x" * (LIMIT + 1)
    delivery = prepare_prompt(prompt, tmp_path / "prompt.txt", AntigravityTransport.max_prompt_characters)
    assert delivery.text == prompt and delivery.file is None


@pytest.mark.parametrize("fault", ["missing", "changed", "symlink"])
def test_request_file_integrity_is_required_before_delivery(tmp_path, fault):
    from auto_agents_2.materials import bundle, prepare_prompt
    prompt = "x" * (LIMIT + 1)
    path = tmp_path / "prompt.txt"
    delivery = prepare_prompt(prompt, path, LIMIT)
    if fault == "changed":
        atomic_write(path, "truncated request")
    elif fault == "symlink":
        atomic_write(tmp_path / "other.txt", prompt)
        path.symlink_to(tmp_path / "other.txt")
    with pytest.raises(ProviderError):
        bundle(prompt, {}, [], [], {}, delivery=delivery)


@pytest.mark.parametrize("structured", [False, True])
async def test_input_limit_failure_is_actionable_and_does_not_retry_connection_noise(tmp_path, structured):
    router, project, store, rid = fixture_router(tmp_path)
    binary = tmp_path / "provider"
    binary.write_text(f"#!{sys.executable}\n" + f"STRUCTURED = {structured!r}\n" + '''
import json, sys, time
sys.stdin.read()
message = 'Input exceeds the maximum length of 1048576 characters. API_KEY=EXAMPLE_PLACEHOLDER'
if STRUCTURED:
    print(json.dumps({"type": "turn.failed", "error": {"message": message}}), flush=True)
else:
    print(message, file=sys.stderr, flush=True)
    time.sleep(0.1)
for _ in range(3):
    print("connection timeout " + "x" * 1900, file=sys.stderr, flush=True)
    time.sleep(0.1)
sys.exit(1)
''')
    body = router.config.load().model_dump(mode="json")
    body["execution"]["infra_retries"] = 1
    write_json(router.config.path, body)
    with pytest.raises(ProviderError, match="provider 输入超过 CLI 长度上限") as failure:
        await router.invoke("review", {}, ReviewResult, project, rid, "failure", fresh=True)
    events = store.events(rid)
    assert len([e for e in events if e["kind"] == "agent.started"]) == 1
    assert not any(e["kind"] == "agent.retrying" for e in events)
    finished = next(e["data"] for e in events if e["kind"] == "agent.finished")
    assert finished["diagnostic"] == "provider 输入超过 CLI 长度上限"
    assert store.get(rid, "provider-result/failure") is None
    assert "EXAMPLE_PLACEHOLDER" not in str(failure.value) + json.dumps(events)
