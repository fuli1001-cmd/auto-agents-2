"""Approved actions must reach production consumers without enlarging authority."""
import asyncio
import copy
import json
import sys

import pytest
from test_prototype_approvals import approval_fixture
from test_provider_prompt_delivery import fixture_router

from auto_agents_2.approvals import PrototypeApprovals
from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.errors import ProviderError
from auto_agents_2.faults import StateConflict
from auto_agents_2.models import ImplementationResult, ReviewResult
from auto_agents_2.util import digest


async def test_task_approval_delivers_original_actions_without_changing_consent(tmp_path, monkeypatch):
    workflow, rid, document, question, _, _ = approval_fixture(tmp_path, monkeypatch, legacy=False)
    before = copy.deepcopy(workflow.store.get(rid, "prototype_approval"))
    context = workflow._task_approval_context(rid, tmp_path, {"specification": {"frontend": True}})
    receipt = context["prototype_approval"]
    assert "browser_actions" in receipt
    action_input = receipt["browser_actions"]
    expected = [action.model_dump() for action in document.browser_actions]
    assert action_input["required_read"] is True
    assert action_input["source_ref"] == "prototype_approval.browser_actions"
    assert workflow.artifacts.json(action_input["artifact"]) == expected
    assert digest(expected) == receipt["actions_hash"] == receipt["human"]["actions_hash"]
    for stage, extra in [
        ("implement", {}), ("review", {"review_kind": "implementation"}),
        ("review", {"review_kind": "test_changes"}), ("verify", {}),
    ]:
        prepared = ContextPlanner(workflow.artifacts).prepare(stage, {**context, **extra}, tmp_path, run_id=rid)
        supplied = prepared.payload["prototype_approval"]["browser_actions"]
        assert supplied == action_input
        assert workflow.artifacts.json(supplied["artifact"]) == expected
    assert workflow.store.get(rid, "prototype_approval") == before
    current_question = workflow.store.question(question["id"])
    assert current_question["answer"] == "1" and not current_question["superseded"]


def action_fixture():
    # Interleaved routes and exact Unicode/whitespace make regrouping lossy.
    return [{"route": f"/index.html?scene={i % 3}", "action": "expect_text",
             "selector": f"#field-{i}", "value": f"原值\n{i}"} for i in range(915)]


def transport_receipt(store, actions):
    # Only a transport fixture. Real consent admission is exercised above and
    # in test_prototype_approvals; this receipt is never used by a workflow gate.
    record = {"hash": "fixture-content", "identity": "fixture-approval",
              "actions_hash": digest(actions), "human": {"question_id": "fixture-question",
              "decision": "approve", "actions_hash": digest(actions)},
              "browser_version": 13, "review": {"approved": True}}
    return PrototypeApprovals(store, ArtifactStore(store.root)).context(record, actions=actions)


@pytest.mark.parametrize("damage", ["omit", "reorder", "change", "wrong_type"])
def test_action_body_must_match_the_exact_approval(tmp_path, damage):
    _, _, store, _ = fixture_router(tmp_path)
    actions = action_fixture()
    record = {"actions_hash": digest(actions)}
    changed = copy.deepcopy(actions)
    if damage == "omit":
        changed.pop()
    elif damage == "reorder":
        changed.reverse()
    elif damage == "change":
        changed[-1]["value"] = "different obligation"
    else:
        changed = tuple(changed)
    before = set((store.root / "artifacts").glob("*/*"))
    with pytest.raises(StateConflict, match="action source changed"):
        PrototypeApprovals(store, ArtifactStore(store.root)).context(record, actions=changed)
    assert set((store.root / "artifacts").glob("*/*")) == before


@pytest.mark.parametrize(("stage", "kind"), [
    ("implement", None), ("review", "implementation"), ("review", "test_changes"), ("verify", None),
])
async def test_exact_action_body_reaches_readonly_provider_and_frozen_resume(tmp_path, stage, kind):
    router, project, store, rid = fixture_router(tmp_path)
    binary = tmp_path / "provider"
    binary.write_text(f"#!{sys.executable}\n" + r"""
import hashlib, json, pathlib, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("fixture-cli 1")
    sys.exit(0)
schema = json.loads(pathlib.Path(args[args.index("--output-schema") + 1]).read_text())
wire = sys.stdin.read()
context = json.loads(wire.split("<context>\n", 1)[1].split("\n</context>", 1)[0])
if "text_token" in schema["properties"]:
    result = {"text_token": pathlib.Path(context["context_files"][0]).read_text(), "image_token": ""}
else:
    receipt = context["prototype_approval"]
    reference = receipt["browser_actions"]
    assert reference["required_read"] is True
    path = pathlib.Path(reference["artifact"]["location"])
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == reference["artifact"]["hash"]
    actions = json.loads(raw)
    expected = [{"route": f"/index.html?scene={i % 3}", "action": "expect_text",
                 "selector": f"#field-{i}", "value": f"原值\n{i}"} for i in range(915)]
    assert actions == expected
    logical = hashlib.sha256(json.dumps(actions, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":")).encode()).hexdigest()
    assert logical == receipt["actions_hash"] == receipt["human"]["actions_hash"]
    try:
        path.write_text("must remain read-only")
    except OSError:
        pass
    else:
        raise AssertionError("Approved action source became writable")
    result = ({"summary": logical, "changes": [], "replan_stage": "", "replan_reason": "", "recovery_request": None}
              if "changes" in schema["properties"] else
              {"approved": False, "summary": logical, "findings": []})
pathlib.Path(args[args.index("-o") + 1]).write_text(json.dumps(result))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
""")
    actions = action_fixture()
    receipt = transport_receipt(store, actions)
    context = {"prototype_approval": receipt, "execution_contract": {"approval": {"prototype_approval": receipt}}}
    if kind:
        context["review_kind"] = kind
    original = copy.deepcopy(context)
    schema = ImplementationResult if stage == "implement" else ReviewResult
    for _ in range(2):
        result = await router.invoke(stage, context, schema, project, rid, "approved-actions", fresh=True)
        assert result.summary == digest(actions)
    manifest = store.get(rid, "materials/approved-actions")
    assert receipt["browser_actions"]["artifact"]["hash"] in {r["hash"] for r in manifest["materials"]}
    assert manifest["omitted_mandatory_content"] == []
    with store.connect() as db:
        frozen = db.execute("SELECT data FROM checkpoints WHERE run_id=? AND key LIKE 'presentation/approved-actions/%'",
                            (rid,)).fetchall()
    assert len(frozen) == 1
    stored = ArtifactStore(store.root).json(json.loads(frozen[0][0])["artifact"])
    assert stored["payload"]["prototype_approval"]["browser_actions"] == receipt["browser_actions"]
    assert context == original
    assert not list((store.root / "processes").glob("*.json"))


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
async def test_missing_or_corrupt_actions_do_not_dispatch_or_create_approval(tmp_path, damage):
    router, project, store, rid = fixture_router(tmp_path)
    receipt = transport_receipt(store, action_fixture())
    ref = receipt["browser_actions"]["artifact"]
    original_prepare = router._prepare_context

    def damage_after_freeze(*args, **kwargs):
        prepared = original_prepare(*args, **kwargs)
        path = store.root / "artifacts" / ref["hash"][:2] / ref["hash"]
        if damage == "missing":
            path.unlink()
        else:
            path.write_bytes(b"wrong approved actions")
        return prepared

    router._prepare_context = damage_after_freeze
    with pytest.raises(ProviderError, match="unavailable|invalid"):
        await router.invoke("implement", {"prototype_approval": receipt}, ImplementationResult,
                            project, rid, "bad-actions", fresh=True)
    assert not [e for e in store.events(rid) if e["kind"] == "agent.started"]
    assert store.get(rid, "prototype_approval") is None
    assert store.get(rid, "provider-result/bad-actions") is None


async def test_legacy_consent_preserves_binding_and_attaches_verified_actions(tmp_path, monkeypatch):
    workflow, rid, document, question, _, _ = approval_fixture(tmp_path, monkeypatch, legacy=True)
    receipt = await workflow._ensure_prototype_approval(rid, tmp_path)
    assert receipt["human"]["question_id"] == question["id"]
    assert receipt["human"]["binding"] == "legacy_content"
    assert workflow.artifacts.json(receipt["browser_actions"]["artifact"]) == [
        action.model_dump() for action in document.browser_actions]
    assert workflow.store.question(question["id"])["answer"] == "1"


@pytest.mark.parametrize("stage", ["recovery_diagnosis", "decision"])
@pytest.mark.parametrize("interrupted", [False, True])
async def test_legacy_frozen_recovery_keeps_old_input_and_new_call_gets_actions(tmp_path, stage, interrupted):
    from test_decisions import ApprovingAgent, setup
    from test_recovery_contracts import failure

    from auto_agents_2.decisions import DecisionResult
    from auto_agents_2.recovery import Diagnosis

    class Agent(ApprovingAgent):
        def __init__(self):
            super().__init__()
            self.inputs = []

        async def invoke(self, stage, context, *args, **kwargs):
            self.inputs.append(copy.deepcopy(context))
            if interrupted and len(self.inputs) == 1:
                raise asyncio.CancelledError()
            return await super().invoke(stage, context, *args, **kwargs)

    agent = Agent()
    workflow, rid = setup(tmp_path, agent)
    receipt = transport_receipt(workflow.store, action_fixture())
    old_receipt = {k: v for k, v in receipt.items() if k != "browser_actions"}
    context = ({"review_kind": "recovery_diagnosis", "failure": failure().model_dump()}
               if stage == "recovery_diagnosis" else {"decision_request": {"evidence": [], "scope": {}}})
    context["prototype_approval"] = old_receipt
    schema = Diagnosis if stage == "recovery_diagnosis" else DecisionResult
    key = "recovery/approved-actions/legacy"
    if interrupted:
        with pytest.raises(asyncio.CancelledError):
            await workflow._invoke(stage, context, schema, tmp_path, rid, key, fresh=True)
    else:
        await workflow._invoke(stage, context, schema, tmp_path, rid, key, fresh=True)
    frozen_key = "recovery/invocation-inputs/" + digest([stage, key])
    frozen = workflow.store.get(rid, frozen_key)
    current = {**context, "prototype_approval": receipt}
    for _ in range(2):
        await workflow._invoke(stage, current, schema, tmp_path, rid, key, fresh=True)
    assert workflow.store.get(rid, frozen_key) == frozen
    assert len(agent.inputs) == (2 if interrupted else 1)
    assert all(row["prototype_approval"] == old_receipt for row in agent.inputs)
    assert current["prototype_approval"] == receipt
    await workflow._invoke(stage, current, schema, tmp_path, rid, key + "/new", fresh=True)
    assert agent.inputs[-1]["prototype_approval"] == receipt


@pytest.mark.parametrize("change", ["actions_hash", "human", "identity", "existing_body", "new_approval"])
async def test_recovery_action_compatibility_never_hides_approval_or_body_drift(tmp_path, change):
    from test_decisions import setup
    from test_recovery_contracts import failure

    from auto_agents_2.recovery import Diagnosis

    workflow, rid = setup(tmp_path)
    receipt = transport_receipt(workflow.store, action_fixture())
    old = {k: v for k, v in receipt.items() if k != "browser_actions" or change == "existing_body"}
    context = {"review_kind": "recovery_diagnosis", "failure": failure().model_dump()}
    if change != "new_approval":
        context["prototype_approval"] = old
    key = "recovery/approved-actions/drift"
    await workflow._invoke("recovery_diagnosis", context, Diagnosis, tmp_path, rid, key, fresh=True)
    saved = workflow.store.get(rid, "invocation/" + key)
    changed = copy.deepcopy(receipt)
    if change in {"actions_hash", "identity"}:
        changed[change] = "different-binding"
    elif change == "human":
        changed["human"]["question_id"] = "different-approval"
    elif change == "existing_body":
        changed["browser_actions"]["artifact"]["hash"] = "f" * 64
    with pytest.raises(StateConflict, match="source changed"):
        await workflow._invoke("recovery_diagnosis", {**context, "prototype_approval": changed},
                               Diagnosis, tmp_path, rid, key, fresh=True)
    assert workflow.store.get(rid, "invocation/" + key) == saved
    assert len(workflow.agent.calls) == 1
