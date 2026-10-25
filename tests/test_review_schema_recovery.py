"""Strict native response parsing shares the bounded review correction policy."""
import asyncio
import copy
import json
import sys

import pytest
from authority_review_fixtures import authority_review, seed_original
from prototype_review_fixtures import prototype_verdict
from test_decisions import ApprovingAgent
from test_runtime import configured

from auto_agents_2.engine import WorkflowEngine
from auto_agents_2.errors import RecoveryPathExhausted
from auto_agents_2.models import DocumentResult, ReviewResult
from auto_agents_2.preview_evidence import PreviewComparison
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.util import write_json
from auto_agents_2.workspace import tree_hash


def native_review(tmp_path, monkeypatch, kind, responses):
    config, store = configured(tmp_path)
    response = store.root / "native-response.txt"
    captured = store.root / "native-fixture-raw"
    captured.mkdir()
    binary = store.root / "native-review-fixture"
    binary.write_text(f"#!{sys.executable}\n" + '''import pathlib,sys,json
args=sys.argv[1:]
if args == ["--version"]:
    print("local-schema-fixture 1")
    sys.exit(0)
wire=sys.stdin.read()
schema=json.loads(pathlib.Path(args[args.index("--output-schema")+1]).read_text())
target=pathlib.Path(args[args.index("-o")+1])
if "text_token" in schema["properties"]:
    context=json.loads(wire.split("<context>\\n",1)[1].split("\\n</context>",1)[0])
    raw=json.dumps({"text_token":pathlib.Path(context["context_files"][0]).read_text(),"image_token":""}).encode()
else:
    raw=pathlib.Path(__RESPONSE__).read_bytes()
    (pathlib.Path(__CAPTURED__)/(target.parent.parent.name+".json")).write_bytes(raw)
    if raw == b"__CLI_TRANSPORT_ERROR__":
        print("fixture provider unavailable",file=sys.stderr)
        sys.exit(9)
target.write_bytes(raw)
print(json.dumps({"type":"thread.started","thread_id":"local-schema-fixture"}))
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":11,"output_tokens":3}}))
'''.replace("__RESPONSE__", repr(str(response))).replace("__CAPTURED__", repr(str(captured))))
    binary.chmod(0o755)
    native = store.root / "native-home"
    native.mkdir()
    body = config.load().model_dump(mode="json")
    spec = body["providers"]["codex"]
    spec.update(binary=str(binary), environment={"CODEX_HOME": str(native)})
    body.update(active_provider="codex", providers={"codex": spec})
    write_json(config.path, body)
    workflow = WorkflowEngine(tmp_path, sandbox=SandboxRunner(store.root, _test_unconfined=True))

    async def unexpected_question(question):
        pytest.fail(f"Unexpected human wait in isolated protocol fixture: {question}")

    workflow.question_handler = unexpected_question
    rid = store.admit("Local response protocol fixture", {})["id"]
    seed_original(workflow, rid)
    store.checkpoint(rid, "rounds/T1", {"round": 8})
    agent = ApprovingAgent()
    call = workflow.agent._call
    observed = []

    async def fixture(effective, stage, context, schema, work, run_id, operation, fresh):
        if stage == "decision":
            return await agent.invoke(stage, context, schema, work, run_id, operation, fresh)
        if stage == "material_probe":
            return await call(effective, stage, context, schema, work, run_id, operation, fresh)
        observed.append(copy.deepcopy(context))
        index = len(observed) - 1
        assert index < len(responses), "Unexpected additional same-input response invocation"
        mode = responses[index]
        if kind == "prototype":
            value = prototype_verdict(context).model_dump()
            review = value
        elif kind == "author":
            value = DocumentResult(summary="author", document="Local design").model_dump()
            review = value
        else:
            review = authority_review(context).model_dump()
            value = {"review": review, "mappings": []} if kind == "preview" else review
        if mode == "missing":
            review.pop("coverage" if kind == "prototype" else "authority", None)
            if kind == "author":
                value.pop("summary")
        elif mode == "invalid_type":
            review["authority"] = []
        elif mode == "stale_basis":
            review["authority"]["basis_hash"] = "not-the-controller-basis"
        text = "{malformed" if mode == "invalid_json" else json.dumps(value)
        if mode == "transport":
            text = "__CLI_TRANSPORT_ERROR__"
        response.write_text(text)
        return await call(effective, stage, context, schema, work, run_id, operation, fresh)

    monkeypatch.setattr(workflow.agent, "_call", fixture)
    if kind == "prototype":
        page = tmp_path / "design/prototypes/index.html"
        page.parent.mkdir(parents=True)
        page.write_text("<h1>Local fixture</h1>")
        document = DocumentResult(summary="prototype", document="Local prototype")
        store.checkpoint(rid, "prototype", document.model_dump())
        stage, context, schema = "review", {"review_kind": "prototype", "prototype": document.model_dump()}, ReviewResult
    elif kind == "preview":
        stage, context, schema = "review", {"review_kind": "preview_contract", "preview_contracts": {}}, PreviewComparison
    elif kind == "verify":
        stage, context, schema = "verify", {"project": workflow.contexts.build(tmp_path)}, ReviewResult
    elif kind == "author":
        stage, context, schema = "design", {}, DocumentResult
    else:
        stage, context, schema = "review", {"review_kind": "design", "design": {"document": "Local design"}}, ReviewResult
    return workflow, rid, stage, context, schema, observed, agent


def requests(workflow, rid):
    with workflow.store.connect() as db:
        return [json.loads(row[0]) for row in db.execute(
            "SELECT data FROM checkpoints WHERE run_id=? AND key LIKE 'proposal-recovery/requests/%'", (rid,))]


@pytest.mark.parametrize("kind,responses", [
    ("design", ["missing", "missing"]),
    ("design", ["invalid_json", "invalid_json"]),
    ("design", ["invalid_type", "invalid_type"]),
    ("prototype", ["missing", "missing"]),
    ("preview", ["missing", "missing"]),
    ("verify", ["missing", "missing"]),
    ("design", ["missing", "stale_basis"]),
    ("design", ["stale_basis", "missing"]),
])
async def test_strict_native_reviews_stop_after_one_correction_and_remain_stopped(tmp_path, monkeypatch, kind, responses):
    workflow, rid, stage, context, schema, observed, agent = native_review(tmp_path, monkeypatch, kind, responses)
    source = tree_hash(tmp_path)
    for _ in range(2):
        with pytest.raises(RecoveryPathExhausted):
            await workflow._invoke(stage, context, schema, tmp_path, rid, "bounded-review", fresh=True)
        assert len(observed) == 2
        state, = requests(workflow, rid)
        assert state["authority_rejections"] == 2 and state["phase"] == "rejected"
        permit = workflow.store.get(rid, state["permit"])
        assert permit["phase"] == "completed" and permit["outcome"]["status"] == "invalid_structured_response"
        assert tree_hash(tmp_path) == source
    assert observed[1]["validation_feedback"]["authority_validation"]["contract"] in {
        "review_authority", "prototype_review_authority"}
    assert [s for s, _ in agent.calls] == ["decision"]
    assert workflow.store.get(rid, "rounds/T1") == {"round": 8}
    assert workflow.store.get(rid, "authority/accepted/design") is None
    assert not workflow.store.get(rid, "issues", {}) and not workflow.store.pending(rid)
    # Two real review response processes. The fixture retains exact output bytes
    # separately from production's temporary-attempt cleanup and material probe.
    outputs = list((workflow.store.root / "native-fixture-raw").glob("*.json"))
    reviews = {e["data"]["attempt"] for e in workflow.store.events(rid)
               if e["kind"] == "agent.started" and e["data"]["stage"] == stage}
    assert len(reviews) == 2 and {p.stem for p in outputs} == reviews
    assert len([e for e in workflow.store.events(rid)
                if e["kind"] == "agent.spawned" and e["data"]["attempt"] in reviews]) == 2


@pytest.mark.parametrize("kind", ["design", "prototype", "preview"])
async def test_strict_native_review_correction_can_succeed_and_reuse_its_receipt(tmp_path, monkeypatch, kind):
    workflow, rid, stage, context, schema, observed, _ = native_review(tmp_path, monkeypatch, kind, ["missing", "valid"])
    result = await workflow._invoke(stage, context, schema, tmp_path, rid, "corrected-review", fresh=True)
    again = await workflow._invoke(stage, context, schema, tmp_path, rid, "corrected-review", fresh=True)
    assert result == again and len(observed) == 2
    state, = requests(workflow, rid)
    assert state["phase"] == "completed" and state["authority_rejections"] == 1
    assert workflow.store.get(rid, "invocation/corrected-review")["result"] == result.model_dump()
    assert workflow.store.get(rid, "rounds/T1") == {"round": 8}


async def test_interruption_after_second_native_failure_preserves_exhausted_review_counter(tmp_path, monkeypatch):
    workflow, rid, stage, context, schema, observed, _ = native_review(tmp_path, monkeypatch, "design", ["missing", "missing"])
    checkpoint = workflow.store.checkpoints

    def interrupt(run_id, values, **kwargs):
        checkpoint(run_id, values, **kwargs)
        if any(key.startswith("proposal-recovery/requests/") and value.get("authority_rejections") == 2
               for key, value in values.items()):
            raise asyncio.CancelledError()

    with monkeypatch.context() as patch:
        patch.setattr(workflow.store, "checkpoints", interrupt)
        with pytest.raises(asyncio.CancelledError):
            await workflow._invoke(stage, context, schema, tmp_path, rid, "interrupted-review", fresh=True)
    with pytest.raises(RecoveryPathExhausted):
        await workflow._invoke(stage, context, schema, tmp_path, rid, "interrupted-review", fresh=True)
    assert len(observed) == 2 and requests(workflow, rid)[0]["authority_rejections"] == 2


async def test_non_review_author_schema_keeps_its_existing_format_recovery_policy(tmp_path, monkeypatch):
    workflow, rid, stage, context, schema, observed, _ = native_review(tmp_path, monkeypatch, "author", ["missing", "missing", "valid"])
    result = await workflow._invoke(stage, context, schema, tmp_path, rid, "author-proposal", fresh=True)
    assert result.document == "Local design" and len(observed) == 3
    state, = requests(workflow, rid)
    assert "authority_rejections" not in state and state["phase"] == "completed"
    assert workflow.store.get(rid, "design") is None


@pytest.mark.parametrize("failure", ["transport", "cancellation"])
async def test_transport_failure_and_cancellation_do_not_spend_review_correction(tmp_path, monkeypatch, failure):
    workflow, rid, stage, context, schema, observed, _ = native_review(tmp_path, monkeypatch, "design", ["transport"])

    async def pause(question):
        assert question["key"] == "provider/transport-review"
        raise asyncio.CancelledError()

    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError()

    workflow.question_handler = pause
    if failure == "cancellation":
        monkeypatch.setattr(workflow.agent, "_call", cancel)
    with pytest.raises(asyncio.CancelledError):
        await workflow._invoke(stage, context, schema, tmp_path, rid, "transport-review", fresh=True)
    assert requests(workflow, rid) == []
    assert workflow.store.get(rid, "rounds/T1") == {"round": 8}
    assert len(observed) == (1 if failure == "transport" else 0)
    assert len(workflow.store.pending(rid)) == (1 if failure == "transport" else 0)
