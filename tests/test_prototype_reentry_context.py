"""A prototype correction receives complete historical proposals, never replayed patches."""
import asyncio
import copy
import sys

import pytest
from prototype_review_fixtures import prototype_verdict
from test_decisions import ApprovingAgent
from test_prototype_approvals import approval_fixture

from auto_agents_2.approvals import prototype_identity
from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.decisions import DecisionResult
from auto_agents_2.engine import Replan
from auto_agents_2.evidence_refs import controller_context_catalog
from auto_agents_2.faults import StateConflict
from auto_agents_2.interaction import choice_spec
from auto_agents_2.models import (
    BrowserAction,
    Change,
    DesignSelection,
    DocumentResult,
    IntakeResult,
    SourceEvidence,
    UserQuestion,
)
from auto_agents_2.prototype_contract import prototype_contract, prototype_output_contract
from auto_agents_2.prototype_reentry import PrototypeReentry
from auto_agents_2.providers import ProviderRouter
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.util import digest, write_json
from auto_agents_2.workspace import git, tree_hash

URL = "https://docs.example.test/pinned/DESIGN.md"
PAGE = "design/prototypes/index.html"


class AuthorInspected(Exception):
    pass


def proposal(label, count, *, selection=False, files=()):
    return DocumentResult(summary=label, document=label + " complete prototype obligations",
        browser_actions=[BrowserAction(route="/index.html", action="expect_text", selector="h1",
                                       value=f"Check {index}") for index in range(count)],
        sources=[SourceEvidence(url=URL, claim="Retained reference, not acceptance")],
        design_selection=[DesignSelection(url=URL, rationale="Task-focused hierarchy",
                          adaptations=["Keep existing interaction states"])] if selection else [],
        files=list(files))


async def history_fixture(tmp_path, monkeypatch, *, repeat_original=False):
    workflow, rid, _, _, _, _ = approval_fixture(tmp_path, monkeypatch, legacy=False)
    workflow.agent = ApprovingAgent()
    store = workflow.store
    spec = IntakeResult(summary="Unchanged UI", document="Preserve every original interaction",
                        frontend=True, ui_changes=False, prototype_paths=[PAGE])
    store.checkpoints(rid, {"clarify": spec.model_dump(), "baseline": git(tmp_path, "rev-parse", "HEAD"),
                           "rounds/retained": {"round": 8}, "proposal-recovery/count/retained": 3})
    original = proposal("Original approved action baseline", 721, files=[
        Change(path=PAGE, operation="replace", old_text="<h1>Before</h1>", content="<h1>Tasks</h1>")])
    binding = prototype_identity(tmp_path, original.model_dump()["browser_actions"])
    question = store.request(rid, "prototype-approval", "Approve original full action contract",
        binding["identity"], choice_spec(["Approve", "Reject"], ["approve", "reject"]))
    store.answer(question["id"], "1", binding["identity"])
    approval = {**binding, "human": workflow.approvals.human(
        rid, tmp_path, original.model_dump()["browser_actions"], search_history=False),
        "visuals": [], "review": {"approved": True, "summary": "Historical original acceptance"}}
    store.checkpoint(rid, "prototype_approval", approval)
    docs = [original, proposal("Missing actions", 0),
            proposal("Restored actions but selection missing", 793),
            proposal("Selection restored but actions regressed", 0, selection=True)]
    if repeat_original:
        docs[-1] = original
    reviews = {}
    for generation, document in enumerate(docs):
        raw = document.model_dump(mode="json")
        actions = raw["browser_actions"]
        ref = workflow.artifacts.put(raw, "prototype")
        pending = {"identity": digest([generation, raw]), "document": raw, "design_candidates": [],
                   "ui_design_version": 1}
        if generation != 2:
            visuals = [{"route": "/index.html", "viewport": width, "status": 200, "errors": [],
                        "actions": actions, "snapshot_generation": generation} for width in (1440, 390)]
            pending.update(fingerprint=prototype_identity(tmp_path, actions)["hash"], visuals=visuals)
            context = workflow._prototype_review_context(rid, tmp_path, {
                "review_kind": "prototype", "prototype": raw, "browser_evidence": visuals})
            verdict = prototype_verdict(context, approved=generation == 0,
                                        summary=f"Complete review requirements of generation {generation}")
            workflow.authority.record(rid, verdict, context, "prototype-review")
            reviews[generation] = verdict.model_dump()
            if generation == 0:
                pending["review"] = verdict.model_dump()
        store.checkpoints(rid, {"prototype/pending": pending,
            "invocation/prototype": {"identity": digest(["author", generation]), "result": raw, "artifact": ref},
            "provider-result/prototype": {"artifact": ref, "usage": {"input_tokens": 11}}})
        if generation == 0:
            store.checkpoint(rid, "prototype", raw)
        stage = "provider_research" if generation == 0 else "prototype"
        reason = f"Full correction reason {generation}: preserve all original fields; " + document.summary
        workflow._controller_replan(rid, Replan(stage, reason))
        await workflow._resume_replan(rid)
    return workflow, rid, spec, docs, reviews, question


async def test_fresh_author_receives_bounded_complete_candidates_and_exact_reviews(tmp_path, monkeypatch):
    workflow, rid, spec, docs, reviews, question = await history_fixture(tmp_path, monkeypatch)
    saved_approval = workflow.store.get(rid, "prototype_approval")
    starts = []

    class Agent(ApprovingAgent):
        async def invoke(self, stage, context, schema, work, run_id, operation, fresh=False):
            if stage != "prototype":
                return await super().invoke(stage, context, schema, work, run_id, operation, fresh)
            starts.append(operation)
            bridge = context["controller_prototype_reentry"]
            assert bridge["current_source_hash"] == tree_hash(work)
            assert bridge["transition"]["checkpoint"] == "recovery/gate-history/3"
            entries = bridge["candidates"]
            assert len(entries) == 3
            roles = {role: row for row in entries for role in row["roles"]}
            for role, index in [("previous", 3), ("recent_actions", 2), ("human_baseline", 0)]:
                row = roles[role]
                assert row["checkpoint"] == f"recovery/gate-history/{index}"
                assert row["proposal"]["required_read"] is True
                raw = workflow.artifacts.json(row["proposal"]["artifact"])
                assert raw == docs[index].model_dump(mode="json")
                assert row["proposal_hash"] == digest(raw)
                assert row["source"]["hash"]
            assert roles["human_baseline"]["human_binding_matches"] is True
            assert roles["human_baseline"]["consent"]["question"]["id"] == question["id"]
            assert roles["recent_actions"]["review"]["status"] == "not_available"
            record = workflow.artifacts.json(roles["previous"]["review"]["report"])
            assert record["review"] == reviews[3] and record["review"] != reviews[1]
            assert bridge["current_automatic_gates"] == "not_evaluated"
            catalog = controller_context_catalog(context)
            for row in entries:
                assert row["proposal"]["artifact"]["hash"] not in catalog.controller
                assert row["invocation"]["proposal"]["hash"] not in catalog.controller
            view = ContextPlanner(workflow.artifacts).prepare(stage, context, work, run_id=run_id)
            assert view.payload["controller_prototype_reentry"]["candidates"] == entries
            raise AuthorInspected()

    workflow.agent = Agent()
    with pytest.raises(AuthorInspected):
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "Original UI obligations")
    assert starts == ["prototype"]
    assert workflow.store.get(rid, "prototype_approval") == saved_approval
    assert workflow.store.question(question["id"])["answer"] == "1"
    assert workflow.store.run(rid)["data"]["replans"] == 4
    assert workflow.store.get(rid, "rounds/retained") == {"round": 8}


async def test_author_output_contract_keeps_execution_protocol_and_field_completeness(tmp_path, monkeypatch):
    workflow, rid, _, _, _, _ = approval_fixture(tmp_path, monkeypatch, legacy=False)
    original = copy.deepcopy(prototype_contract())

    class Agent(ApprovingAgent):
        async def invoke(self, stage, context, *args, **kwargs):
            if stage == "prototype":
                contract = context["controller_prototype_output"]
                assert contract["complete_fields"] == ["document", "browser_actions", "sources", "design_selection"]
                assert contract["files"] == "current_changes_only"
                assert context["controller_prototype_contract"] == original
                raise AuthorInspected()
            return await super().invoke(stage, context, *args, **kwargs)

    workflow.agent = Agent()
    with pytest.raises(AuthorInspected):
        await workflow._invoke("prototype", {}, DocumentResult, tmp_path, rid, "prototype")


@pytest.mark.parametrize("damage", ["provider_receipt", "author_artifact", "proposal_fields", "missing_selection"])
async def test_inconsistent_selected_candidate_stops_without_touching_approval(tmp_path, monkeypatch, damage):
    workflow, rid, _, _, _, question = await history_fixture(tmp_path, monkeypatch)
    store = workflow.store
    approval = store.get(rid, "prototype_approval")
    key = "recovery/gate-history/3"
    history = store.get(rid, key)
    records = history["checkpoints"]
    if damage == "provider_receipt":
        records["provider-result/prototype"]["artifact"] = workflow.artifacts.put({"different": True}, "prototype")
        store.checkpoint(rid, key, history)
    elif damage == "missing_selection":
        records["invocation/prototype"]["result"].pop("design_selection")
        changed = workflow.artifacts.put(records["invocation/prototype"]["result"], "prototype")
        records["invocation/prototype"]["artifact"] = changed
        records["provider-result/prototype"]["artifact"] = changed
        store.checkpoint(rid, key, history)
    elif damage == "proposal_fields":
        records["prototype/pending"]["document"]["sources"][0]["claim"] = "Unbound replacement"
        store.checkpoint(rid, key, history)
    else:
        ref = records["invocation/prototype"]["artifact"]
        (store.root / "artifacts" / ref["hash"][:2] / ref["hash"]).write_text("corrupt original")
    with pytest.raises((StateConflict, ValueError)):
        PrototypeReentry(workflow).context(rid, tmp_path)
    assert store.get(rid, "prototype_approval") == approval
    assert store.question(question["id"])["answer"] == "1"
    assert store.run(rid)["data"]["replans"] == 4


@pytest.mark.parametrize("damage", ["observations", "normative_source", "ambiguous"])
async def test_same_content_empty_actions_cannot_borrow_another_round_review(tmp_path, monkeypatch, damage):
    workflow, rid, _, _, reviews, _ = await history_fixture(tmp_path, monkeypatch)
    key = "authority/prototype-reviews/" + digest(reviews[3])
    record = workflow.store.get(rid, key)
    if damage == "observations":
        older = workflow.store.get(rid, "authority/prototype-reviews/" + digest(reviews[1]))
        record["basis"]["observations_hash"] = older["basis"]["observations_hash"]
    elif damage == "normative_source":
        record["source_identity"]["capture"] = "different original source"
    else:
        record["review"]["summary"] = "A second independently recorded verdict for identical observations"
        key = "authority/prototype-reviews/" + digest(record["review"])
    workflow.store.checkpoint(rid, key, record)
    bridge = PrototypeReentry(workflow).context(rid, tmp_path)
    previous = next(row for row in bridge["candidates"] if "previous" in row["roles"])
    assert previous["review"]["status"] == ("ambiguous" if damage == "ambiguous" else "not_available")
    assert "report" not in previous["review"]


@pytest.mark.parametrize("damage", ["source", "consent", "feedback"])
async def test_drift_does_not_promote_historical_consent_or_reentry(tmp_path, monkeypatch, damage):
    workflow, rid, _, _, _, question = await history_fixture(tmp_path, monkeypatch)
    store = workflow.store
    approval = store.get(rid, "prototype_approval")
    if damage == "source":
        (tmp_path / PAGE).write_text("<h1>User changed prototype</h1>")
    elif damage == "consent":
        # Ordinary supersede deliberately preserves answered consent. Inject a
        # revoked/corrupt read boundary explicitly in this disposable fixture.
        with store.connect() as db:
            db.execute("UPDATE questions SET superseded=1 WHERE id=?", (question["id"],))
    else:
        feedback = store.get(rid, "replan_feedback")
        store.checkpoint(rid, "replan_feedback", {**feedback, "reason": "An unrelated recovery"})
    bridge = PrototypeReentry(workflow).context(rid, tmp_path)
    if damage == "feedback":
        assert bridge["status"] == "unverified_transition" and not bridge["candidates"]
    elif damage == "consent":
        assert not any("human_baseline" in row["roles"] for row in bridge["candidates"])
    else:
        baseline = next(row for row in bridge["candidates"] if "human_baseline" in row["roles"])
        assert not baseline["human_binding_matches"] and not bridge["transition"]["source_matches_current"]
        assert (tmp_path / PAGE).read_text() == "<h1>User changed prototype</h1>"
    assert store.get(rid, "prototype_approval") == approval
    assert store.question(question["id"])["answer"] == "1"
    assert bridge["current_automatic_gates"] == "not_evaluated"


async def test_duplicate_candidate_roles_share_one_proposal_with_exact_origins(tmp_path, monkeypatch):
    workflow, rid, _, docs, _, _ = await history_fixture(tmp_path, monkeypatch, repeat_original=True)
    bridge = PrototypeReentry(workflow).context(rid, tmp_path)
    assert len(bridge["candidates"]) == 1
    row = bridge["candidates"][0]
    assert row["roles"] == ["previous", "recent_actions", "human_baseline"]
    assert [(origin["role"], origin["checkpoint"]) for origin in row["origins"]] == [
        ("previous", "recovery/gate-history/3"), ("recent_actions", "recovery/gate-history/3"),
        ("human_baseline", "recovery/gate-history/0")]
    assert workflow.artifacts.json(row["proposal"]["artifact"]) == docs[0].model_dump(mode="json")


def test_no_history_is_compatible_and_bridge_is_only_selected_for_prototype(tmp_path, monkeypatch):
    workflow, rid, _, _, _, _ = approval_fixture(tmp_path, monkeypatch, legacy=False)
    assert PrototypeReentry(workflow).context(rid, tmp_path) == {}
    context = {"controller_prototype_reentry": {"version": 1}, "review_feedback": ["Not a global reopening"]}
    planner = ContextPlanner(workflow.artifacts)
    assert "controller_prototype_reentry" not in planner.prepare("design", context, tmp_path).payload
    selected = planner.prepare("prototype", context, tmp_path).payload
    assert selected["controller_prototype_reentry"] == {"version": 1}
    assert "review_feedback" not in selected


@pytest.mark.parametrize("interrupt", ["before_pending", "after_application"])
async def test_resume_reuses_response_and_never_replays_historical_file_changes(tmp_path, monkeypatch, interrupt):
    workflow, rid, spec, docs, _, question = await history_fixture(tmp_path, monkeypatch)
    store = workflow.store
    author_calls = []
    class Agent(ApprovingAgent):
        async def invoke(self, stage, context, *args, **kwargs):
            if stage == "prototype":
                author_calls.append(context)
                return docs[0].model_copy(update={"files": []})
            return await super().invoke(stage, context, *args, **kwargs)
    workflow.agent = Agent()
    approval = store.get(rid, "prototype_approval")
    before = tree_hash(tmp_path)
    checkpoint = store.checkpoint
    async def browser(*args):
        raise AuthorInspected()
    workflow._prototype_browser_report = browser
    if interrupt == "before_pending":
        def stop_before_pending(run_id, key, value):
            if key == "prototype/pending":
                raise asyncio.CancelledError()
            return checkpoint(run_id, key, value)
        monkeypatch.setattr(store, "checkpoint", stop_before_pending)
    with pytest.raises(asyncio.CancelledError if interrupt == "before_pending" else AuthorInspected):
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")
    monkeypatch.setattr(store, "checkpoint", checkpoint)
    with pytest.raises(AuthorInspected):
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")
    assert len(author_calls) == 1
    assert store.get(rid, "prototype/pending")["prepared"] == []
    assert tree_hash(tmp_path) == before and (tmp_path / PAGE).read_text() == "<h1>Tasks</h1>"
    assert store.get(rid, "prototype_approval") == approval
    assert store.question(question["id"])["answer"] == "1"
    assert store.get(rid, "rounds/retained") == {"round": 8}
    assert store.get(rid, "proposal-recovery/count/retained") == 3
    assert store.run(rid)["data"]["replans"] == 4


async def test_native_author_reads_full_large_proposals_and_reviews_read_only(tmp_path, monkeypatch):
    workflow, rid, _, docs, reviews, _ = await history_fixture(tmp_path / "project", monkeypatch)
    work = workflow.project
    bridge = PrototypeReentry(workflow).context(rid, work)
    response = proposal("Complete current proposal", 793, selection=True).model_dump(mode="json")
    binary = tmp_path / "provider"
    binary.write_text(f"#!{sys.executable}\nRESPONSE = {response!r}\n" + r'''
import hashlib, json, pathlib, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("prototype-material-fixture 1")
    sys.exit(0)
schema = json.loads(pathlib.Path(args[args.index("--output-schema") + 1]).read_text())
wire = sys.stdin.read()
context = json.loads(wire.split("<context>\n", 1)[1].split("\n</context>", 1)[0])
if "text_token" in schema["properties"]:
    result = {"text_token": pathlib.Path(context["context_files"][0]).read_text(), "image_token": ""}
else:
    rows = context["controller_prototype_reentry"]["candidates"]
    roles = {role: row for row in rows for role in row["roles"]}
    for role, count in [("previous", 0), ("recent_actions", 793), ("human_baseline", 721)]:
        row = roles[role]
        assert row["proposal"]["required_read"]
        ref = row["proposal"]["artifact"]
        path = pathlib.Path(ref["location"])
        body = path.read_bytes()
        assert hashlib.sha256(body).hexdigest() == ref["hash"] and len(body) == ref["bytes"]
        doc = json.loads(body)
        assert len(doc["browser_actions"]) == count
        assert doc["sources"][0]["url"] == "https://docs.example.test/pinned/DESIGN.md"
        assert len(doc.get("design_selection", [])) == int(role == "previous")
        if role == "human_baseline":
            assert doc["files"][0]["old_text"] == "<h1>Before</h1>"
        try:
            path.write_bytes(b"forbidden rewrite")
        except OSError:
            pass
        else:
            raise AssertionError("Historical proposal is writable")
    ref = roles["previous"]["review"]["report"]
    body = pathlib.Path(ref["location"]).read_bytes()
    assert hashlib.sha256(body).hexdigest() == ref["hash"]
    assert json.loads(body)["review"]["summary"] == "Complete review requirements of generation 3"
    assert "never" in wire.lower() and "current_changes_only" in wire
    result = RESPONSE
pathlib.Path(args[args.index("-o") + 1]).write_text(json.dumps(result))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
''')
    binary.chmod(0o755)
    native = tmp_path / "native"
    native.mkdir()
    config = workflow.config.load().model_dump(mode="json")
    config["providers"] = {"codex": config["providers"]["codex"]}
    config["providers"]["codex"].update(binary=str(binary), environment={"CODEX_HOME": str(native)},
        profile_map={key: {"model": "test"} for key in ("balanced", "deep", "max")})
    config["execution"]["infra_retries"] = 0
    write_json(workflow.config.path, config)
    router = ProviderRouter(workflow.config, workflow.store, SandboxRunner(workflow.store.root))
    context = {"controller_prototype_reentry": bridge, "controller_prototype_output": prototype_output_contract()}
    result = await router.invoke("prototype", context, DocumentResult, work, rid, "retained-prototype", fresh=True)
    assert result.files == [] and len(result.browser_actions) == 793
    materials = workflow.store.get(rid, "materials/retained-prototype")["materials"]
    mounted = {row["hash"] for row in materials if row.get("presentation") == "attachment"}
    for row in bridge["candidates"]:
        assert row["proposal"]["artifact"]["hash"] in mounted
    original = next(row for row in bridge["candidates"] if "human_baseline" in row["roles"])
    assert workflow.artifacts.json(original["proposal"]["artifact"]) == docs[0].model_dump(mode="json")
    assert len(reviews[3]["findings"]) == 1

async def test_accepted_record_fallback_preserves_controller_source_metadata(tmp_path, monkeypatch):
    workflow, rid, _, docs, _, _ = await history_fixture(tmp_path, monkeypatch)
    key = "recovery/gate-history/0"
    history = workflow.store.get(rid, key)
    history["checkpoints"].pop("prototype/pending")
    history["checkpoints"]["prototype"]["verified_sources"] = [{"url": URL, "controller_metadata": "retained"}]
    workflow.store.checkpoint(rid, key, history)
    bridge = PrototypeReentry(workflow).context(rid, tmp_path)
    baseline = next(row for row in bridge["candidates"] if "human_baseline" in row["roles"])
    raw = workflow.artifacts.json(baseline["proposal"]["artifact"])
    assert raw["browser_actions"] == docs[0].model_dump()["browser_actions"]
    assert raw["verified_sources"] == [{"url": URL, "controller_metadata": "retained"}]
    assert baseline["human_binding_matches"]


async def test_real_question_pause_answer_resume_retains_current_and_historical_proposals(tmp_path, monkeypatch):
    workflow, rid, spec, docs, _, consent = await history_fixture(tmp_path, monkeypatch)
    store = workflow.store
    question_text = "Which existing status wording should the prototype retain?"
    answer = "Retain the existing status wording and the original approved interaction coverage."
    current = docs[3].model_copy(update={"summary": "Current unanswered proposal",
        "document": "CURRENT_PENDING_COMPLETE_BODY", "browser_actions": docs[2].browser_actions,
        "questions": [UserQuestion(prompt=question_text)]})
    calls = []
    class Agent(ApprovingAgent):
        async def invoke(self, stage, context, *args, **kwargs):
            if stage == "decision" and context["decision_request"]["kind"] == "technical_clarification":
                return DecisionResult(action="ask_user", reason="The wording choice requires a real product answer")
            if stage == "prototype":
                calls.append(copy.deepcopy(context))
                if len(calls) == 1:
                    return current
                rows = context["controller_prototype_reentry"]["candidates"]
                assert {role for row in rows for role in row["roles"]} == {
                    "previous", "recent_actions", "human_baseline"}
                assert context["previous_prototype"] == current.model_dump(mode="json")
                assert context["prototype_answer"] == [{"question": question_text, "answer": answer}]
                presented = ContextPlanner(workflow.artifacts).prepare("prototype", context, tmp_path, run_id=rid)
                assert presented.payload["previous_prototype"]["document"] == "CURRENT_PENDING_COMPLETE_BODY"
                assert presented.payload["previous_prototype"]["browser_actions"] == current.model_dump()["browser_actions"]
                return current.model_copy(update={"questions": [], "files": []})
            return await super().invoke(stage, context, *args, **kwargs)
    workflow.agent = Agent()
    async def pause(question):
        assert question["key"] == "prototype-clarification/0"
        raise asyncio.CancelledError()
    workflow.question_handler = pause
    before = tree_hash(tmp_path)
    approval = store.get(rid, "prototype_approval")
    with pytest.raises(asyncio.CancelledError):
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")
    pending = store.get(rid, "prototype/pending")
    assert pending["document"] == current.model_dump(mode="json") and "prepared" not in pending
    question = store.pending(rid)[0]
    assert question["artifact_hash"] == digest(current.model_dump())
    store.answer(question["id"], answer, question["artifact_hash"])
    workflow.question_handler = None
    async def browser(*args):
        raise AuthorInspected()
    workflow._prototype_browser_report = browser
    # A fresh Python context emulates normal process restart: no native/context memory.
    with pytest.raises(AuthorInspected):
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")
    assert len(calls) == 2 and store.question(question["id"])["answer"] == answer
    assert store.question(consent["id"])["answer"] == "1"
    assert store.get(rid, "prototype_approval") == approval and tree_hash(tmp_path) == before
    assert store.run(rid)["data"]["replans"] == 4
    assert store.get(rid, "rounds/retained") == {"round": 8}
    assert store.get(rid, "proposal-recovery/count/retained") == 3
    def no_material_refresh(*args):
        pytest.fail("Pure pending reuse must not rebuild historical context or dispatch an author")
    monkeypatch.setattr(PrototypeReentry, "context", no_material_refresh)
    with pytest.raises(AuthorInspected):
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")
    assert len(calls) == 2
