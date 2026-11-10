"""Synthetic controller replan feedback, without browser or provider execution."""
import copy
import json

import pytest

from test_decisions import ApprovingAgent
from test_prototype_approvals import approval_fixture
from test_prototype_reentry_context import AuthorInspected, proposal

from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.decisions import DecisionResult
from auto_agents_2.engine import Replan
from auto_agents_2.evidence import invocation_identity
from auto_agents_2.models import DocumentResult
from auto_agents_2.prototype_reentry import PrototypeReentry
from auto_agents_2.util import digest

ERROR = "Timeout waiting for #play:enabled; complete original browser error"
REASON = "Previous media-ready wait also failed. Fix lifecycle root cause; preserve all business assertions."


async def completed(tmp_path, monkeypatch):
    workflow, rid, *_ = approval_fixture(tmp_path, monkeypatch, legacy=False)

    class DecisionAgent(ApprovingAgent):
        async def invoke(self, stage, context, *args, **kwargs):
            if stage == "decision":
                return DecisionResult(action="approve", reason=REASON, evidence_refs=["failure"],
                    selected_action="prototype", expected_result="Verify all original interactions")
            return await super().invoke(stage, context, *args, **kwargs)

    workflow.agent = DecisionAgent()
    authorize = workflow.recovery_policy.authorize

    async def forced(*args, **kwargs):
        return await authorize(*args, **{**kwargs, "force_decision": True})

    monkeypatch.setattr(workflow.recovery_policy, "authorize", forced)
    raw = proposal("Retained author", 1).model_dump(mode="json")
    ref = workflow.artifacts.put(raw, "prototype")
    workflow.store.checkpoints(rid, {
        "prototype/pending": {"document": raw},
        "invocation/prototype": {"identity": digest(raw), "result": raw, "artifact": ref},
    })
    workflow._controller_replan(rid, Replan("prototype", ERROR))
    await workflow._grant_replan(rid, workflow.store.get(rid, "replanning/pending"))
    # A reserved/started grant is not a completed transition or feedback.
    assert PrototypeReentry(workflow).context(rid, tmp_path, include_decision=True) == {}
    await workflow._resume_replan(rid)
    return workflow, rid


@pytest.mark.parametrize("large", [False, True])
async def test_bound_reason_reaches_actual_author_and_ordinary_compiler(tmp_path, monkeypatch, large):
    if large:
        monkeypatch.setitem(globals(), "REASON", REASON * 400)
    workflow, rid = await completed(tmp_path, monkeypatch)
    before = copy.deepcopy(workflow.store.get(rid, "replan_feedback"))
    assert before["reason"] == before["correction_request"]["reason"] == ERROR
    history = workflow.store.get(rid, "recovery/gate-history/0")
    bridge = PrototypeReentry(workflow).context(rid, tmp_path, include_decision=True)
    explanation = bridge["decision_feedback"]
    assert explanation["reason"] == REASON
    review = workflow._prototype_reentry_context(rid, tmp_path, {}, review=True)
    assert "decision_feedback" not in review["controller_prototype_reentry"]
    assert explanation["generation"] == history["pending"]["generation"]
    assert set(explanation) == {"reason", "expected_result", "actor", "generation", "scope"}
    private_prefixes = ("recovery/request-snapshots/", "decisions/applied/",
                        "decisions/requests/", "recovery/authorization-inputs/")
    assert not any(prefix in json.dumps(explanation) for prefix in (*private_prefixes, "recovery/permits/"))
    assert workflow.store.get(rid, "replan_feedback") == before

    class Author(ApprovingAgent):
        async def invoke(self, stage, context, *args, **kwargs):
            assert stage == "prototype"
            assert context["feedback"] == before
            assert context["controller_prototype_reentry"]["decision_feedback"] == explanation
            view = ContextPlanner(workflow.artifacts).prepare(stage, context, tmp_path, run_id=rid)
            assert view.payload["feedback"]["reason"] == ERROR
            projection = view.payload["controller_prototype_reentry"]["decision_feedback"]
            assert set(projection) == set(explanation)
            assert not any(prefix in json.dumps(projection)
                           for prefix in (*private_prefixes, "recovery/permits/"))
            # Existing transition/correction permit references are unchanged;
            # the new feedback must not expose private decision-chain pointers.
            assert not any(prefix in json.dumps(view.payload) for prefix in private_prefixes)
            changed = copy.deepcopy(context)
            changed["controller_prototype_reentry"]["decision_feedback"]["reason"] += " Changed reason."
            cfg = workflow.config.load()
            assert invocation_identity(stage, context, DocumentResult, tmp_path, cfg) != invocation_identity(
                stage, changed, DocumentResult, tmp_path, cfg)
            rendered = view.payload["controller_prototype_reentry"]["decision_feedback"]["reason"]
            if large:
                assert rendered["required_read"] is True
                assert workflow.artifacts.read(rendered["artifact"]) == REASON
            else:
                assert rendered == REASON
            raise AuthorInspected()

    workflow.agent = Author()
    with pytest.raises(AuthorInspected):
        await workflow._invoke("prototype", {"feedback": before}, DocumentResult, tmp_path, rid, "prototype")
    assert workflow.store.get(rid, "replan_feedback") == before
    assert workflow.store.run(rid)["data"]["replans"] == 1


@pytest.mark.parametrize("damage", [
    "missing_snapshot", "missing_explanation", "run", "stage", "failure",
    "proposal", "basis", "attempt", "result", "pending_reason",
])
async def test_missing_or_unbound_explanations_are_not_borrowed(tmp_path, monkeypatch, damage):
    workflow, rid = await completed(tmp_path, monkeypatch)
    store = workflow.store
    history = store.get(rid, "recovery/gate-history/0")
    bridge = PrototypeReentry(workflow)
    valid = bridge._decision_feedback(rid, history)
    assert valid["reason"] == REASON
    if damage == "run":
        other = "unrelated-synthetic-run"
        assert bridge._decision_feedback(other, history) is None
        return
    permit_key = history["pending"]["permit"]
    key = "recovery/request-snapshots/" + permit_key.removeprefix("recovery/permits/")
    value = store.get(rid, key)
    applied_key = "decisions/applied/" + digest([permit_key, value["request"]])
    if damage == "missing_snapshot":
        value = {}
    elif damage == "stage":
        history["pending"]["stage"] = "clarify"
    elif damage == "failure":
        value["request"]["evidence"]["failure"]["id"] = "other-failure"
    elif damage == "basis":
        value["basis"] = "other-basis"
    elif damage == "attempt":
        history["pending"]["evaluation"] += 1
    elif damage == "pending_reason":
        history["pending"]["reason"] = "another error"
    elif damage in {"proposal", "result", "missing_explanation"}:
        key = applied_key
        value = store.get(rid, key)
        if damage == "proposal":
            value["result_key"] = "decisions/requests/another-proposal"
        else:
            value["result"]["reason"] = "" if damage == "missing_explanation" else "unbound replacement"
    store.checkpoint(rid, key, value)
    assert bridge._decision_feedback(rid, history) is None
    if damage in {"missing_snapshot", "missing_explanation"}:
        assert "decision_feedback" not in bridge.context(rid, tmp_path, include_decision=True)
    assert store.get(rid, "replan_feedback")["reason"] == ERROR
