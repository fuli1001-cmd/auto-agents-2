"""Retained controller materials repair a clarification wait without inventing consent."""
import json
import sys

import pytest
from test_clarify_submitted_answers import ReachedCoverage
from test_prototype_approvals import approval_fixture
from test_workflow import CodingAgent

from auto_agents_2.clarification_reentry import ClarificationReentry
from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.diagnostics import references
from auto_agents_2.evidence_refs import controller_context_catalog
from auto_agents_2.faults import StateConflict
from auto_agents_2.models import IntakeResult, ReviewResult
from auto_agents_2.providers import ProviderRouter
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.stage_responsibilities import GATES, prepare_reentry
from auto_agents_2.util import digest, write_json
from auto_agents_2.workspace import git, tree_hash


def retained_wait(tmp_path, monkeypatch, *, turn=0, questions=None, plan=False):
    workflow, rid, document, consent, _, _ = approval_fixture(tmp_path, monkeypatch, legacy=False)
    store = workflow.store
    approval = store.get(rid, "prototype_approval")
    approval["human"] = workflow.approvals.human(
        rid, tmp_path, document.model_dump()["browser_actions"], approval, search_history=False)
    store.checkpoint(rid, "prototype_approval", approval)
    store.checkpoint(rid, "prototype_browser_report", {
        "hash": approval["hash"], "actions": document.model_dump()["browser_actions"],
        "results": approval["visuals"], "version": approval["browser_version"],
    })
    base = git(tmp_path, "rev-parse", "HEAD").strip()
    store.checkpoint(rid, "baseline", base)
    work = workflow.workspaces.allocate(rid, "integration", base)

    # The prototype lives in an earlier reset, not the latest transition.
    pending = store.prepare_replan(rid, "provider_research", "Revisit research")
    store.checkpoint(rid, "replanning/pending", {**pending, "granted": True})
    store.complete_replan(rid, GATES[1:], [])
    if plan:
        store.checkpoint(rid, "plan", {"summary": "Retained plan", "tasks": []})
    store.checkpoint(rid, "rounds/retained", {"round": 3, "stalled": 2})
    store.checkpoint(rid, "proposal-recovery/count/retained", 2)
    pending = store.prepare_replan(rid, "clarify", "New submitted source needs clarification")
    permit = {
        "action": {"action": "clarify"}, "source_hash": tree_hash(work), "plan_hash": digest(store.get(rid, "plan")),
        "task_scopes": {}, "prototype_binding": {}, "preview_binding": {},
        "phase": "completed", "outcome": {"status": "gate_reset", "stage": "clarify"},
    }
    store.checkpoint(rid, "recovery/permits/clarify", permit)
    pending = {**pending, "granted": True, "permit": "recovery/permits/clarify"}
    store.checkpoint(rid, "replanning/pending", pending)
    prepare_reentry(store, rid, pending, work)
    store.complete_replan(rid, GATES, [])

    result = IntakeResult(summary="Needs controller records", document="Preserve the product requirements",
        questions=questions or ["Restore the controller's original approval and browser records"],
        frontend=True, ui_changes=False).model_dump()
    previous = {"identity": "old-clarification", "result": result, "artifact": workflow.artifacts.put(result, "clarify")}
    presentation = {"identity": previous["identity"], "artifact": workflow.artifacts.put(
        {"purpose": "clarify", "payload": {"raw_requirements": "Original product request"}}, "prepared_context")}
    store.checkpoints(rid, {"invocation/clarify": previous,
        "presentation/clarify/" + previous["identity"]: presentation})
    rows = [store.request(rid, f"clarify/{turn}/{i}", prompt, digest(result))
            for i, prompt in enumerate(result["questions"])]
    return workflow, rid, work, rows, consent


def input_context(workflow, rid, work):
    service = ClarificationReentry(workflow)
    context = {"controller_reentry": service.context(rid, work),
               "submitted_answers": workflow.store.submitted_answers(rid)}
    return service, context


@pytest.mark.parametrize("plan", [False, True])
def test_selects_exact_transition_and_earlier_prototype_as_historical_materials(tmp_path, monkeypatch, plan):
    workflow, rid, work, rows, consent = retained_wait(tmp_path, monkeypatch, plan=plan)
    _, context = input_context(workflow, rid, work)
    bridge = context["controller_reentry"]
    assert bridge["status"] == "verified_reentry"
    assert bridge["transition"]["checkpoint"] == "recovery/gate-history/1"
    retained = bridge["retained_prototype"]
    assert retained["candidate"]["checkpoint"] == "recovery/gate-history/0"
    assert retained["human_binding_matches"] and retained["consent"]["question"]["id"] == consent["id"]
    assert retained["status"] == "historical" and retained["current_automatic_gates"] == "not_evaluated"
    assert "status" not in retained["record"]
    assert "prototype" not in workflow.store.get(rid, "recovery/gate-history/1")["checkpoints"]
    catalog = controller_context_catalog(context)
    candidate = "controller_reentry.retained_prototype.candidate.proposal"
    assert candidate in catalog.proposals and candidate not in catalog.controller
    assert retained["candidate"]["proposal"]["hash"] not in catalog.controller
    assert workflow.store.question(rows[0]["id"])["answer"] is None


@pytest.mark.parametrize("damage", ["generation", "history", "binding", "permit", "outcome_hash", "source", "gate_requested", "legacy"])
def test_unverified_transition_does_not_enable_refresh(tmp_path, monkeypatch, damage):
    workflow, rid, work, rows, _ = retained_wait(tmp_path, monkeypatch)
    store = workflow.store
    if damage == "source":
        (work / "changed.txt").write_text("User edit")
    elif damage in {"permit", "gate_requested", "outcome_hash"}:
        permit = store.get(rid, "recovery/permits/clarify")
        if damage == "permit":
            permit["source_hash"] = "unrelated-source"
        elif damage == "gate_requested":
            permit["outcome"] = {"status": "gate_requested", "evidence": "recovery/completed/old"}
        # Fault injection deliberately bypasses completed-permit immutability.
        permit["outcome_hash"] = "corrupt" if damage == "outcome_hash" else digest(permit["outcome"])
        with store.connect() as db:
            db.execute("UPDATE checkpoints SET data=? WHERE run_id=? AND key=?",
                       (json.dumps(permit), rid, "recovery/permits/clarify"))
    elif damage in {"generation", "history"}:
        key = "recovery/gate-history/1"
        value = store.get(rid, key)
        value["pending"]["generation" if damage == "generation" else "reason"] = "unrelated"
        store.checkpoint(rid, key, value)
    elif damage == "binding":
        store.checkpoint(rid, "capture", {"hash": "changed"})
    else:
        store.checkpoint(rid, "replan_feedback", {"stage": "clarify", "reason": "Legacy reentry"})
    service, context = input_context(workflow, rid, work)
    assert context["controller_reentry"].get("status") != "verified_reentry"
    assert service.original is None
    assert workflow.store.question(rows[0]["id"])["answer"] is None


@pytest.mark.parametrize("damage", ["record", "actions", "consent"])
def test_old_human_proof_never_certifies_mismatched_record(tmp_path, monkeypatch, damage):
    workflow, rid, work, _, consent = retained_wait(tmp_path, monkeypatch)
    store = workflow.store
    if damage == "consent":
        store.supersede_questions(rid, "prototype-approval", include_answered=True)
    else:
        # Keep the current transition's binding coherent while changing older material.
        history = store.get(rid, "recovery/gate-history/0")
        history["checkpoints"]["prototype"]["browser_actions"][0]["value"] = "Other action"
        if damage == "record":
            history["checkpoints"]["prototype"]["browser_actions"] = []
        store.checkpoint(rid, "recovery/gate-history/0", history)
    _, context = input_context(workflow, rid, work)
    retained = context["controller_reentry"]["retained_prototype"]
    assert not retained["human_binding_matches"] and "candidate" not in retained
    assert retained["current_automatic_gates"] == "not_evaluated"
    assert store.question(consent["id"])["answer"] == "1"


@pytest.mark.parametrize("damage", ["missing", "identity", "result"])
def test_inconsistent_legacy_receipt_keeps_original_wait(tmp_path, monkeypatch, damage):
    workflow, rid, work, rows, _ = retained_wait(tmp_path, monkeypatch)
    store = workflow.store
    previous = store.get(rid, "invocation/clarify")
    presentation = store.get(rid, "presentation/clarify/old-clarification")
    if damage == "missing":
        previous.pop("artifact")
        store.checkpoint(rid, "invocation/clarify", previous)
    elif damage == "identity":
        presentation["identity"] = "other"
        store.checkpoint(rid, "presentation/clarify/old-clarification", presentation)
    else:
        previous["artifact"] = workflow.artifacts.put({"questions": ["Other question"]}, "clarify")
        store.checkpoint(rid, "invocation/clarify", previous)
    with pytest.raises(StateConflict, match="Retained clarification"):
        input_context(workflow, rid, work)
    assert not store.question(rows[0]["id"])["superseded"]
    assert store.get(rid, "invocation/clarify") == previous


class RefreshAgent(CodingAgent):
    def __init__(self, hook=None, product_question=False):
        super().__init__()
        self.hook, self.product_question = hook, product_question
        self.operations = []

    async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
        if stage == "clarify":
            self.operations.append(operation)
            assert context["controller_reentry"]["retained_prototype"]["human_binding_matches"]
            if self.hook:
                self.hook()
            if self.product_question:
                return IntakeResult(summary="Materials restored", document="Keep genuine user choices",
                                    questions=["Which product flow should be used?"])
        if stage == "coverage_review":
            assert context["controller_reentry"]["retained_prototype"]["current_automatic_gates"] == "not_evaluated"
            raise ReachedCoverage()
        return await super().invoke(stage, context, schema, workspace, run_id, operation, fresh)


async def test_resume_refreshes_internal_wait_and_preserves_audit_consent_and_counts(tmp_path, monkeypatch):
    workflow, rid, _, rows, consent = retained_wait(tmp_path, monkeypatch)
    store = workflow.store
    guarded = {key: store.get(rid, key) for key in (
        "prototype_approval", "prototype_browser_report", "rounds/retained", "proposal-recovery/count/retained")}
    old = store.get(rid, "invocation/clarify")
    agent = workflow.agent = RefreshAgent()
    with pytest.raises(ReachedCoverage):
        await workflow._execute(rid)
    assert len(agent.operations) == 1 and agent.operations[0].startswith("clarify/reentry-refresh/")
    assert store.question(rows[0]["id"])["superseded"] and store.question(rows[0]["id"])["answer"] is None
    assert store.question(consent["id"])["answer"] == "1"
    current = store.get(rid, "invocation/clarify")
    assert current["recovered_from"] == agent.operations[0] and current["identity"] != old["identity"]
    assert all(store.get(rid, key) == value for key, value in guarded.items())
    assert store.run(rid)["data"]["replans"] == 2
    assert any(e["kind"] == "clarify.questions_refreshed" for e in store.events(rid))
    assert store.submitted_answers(rid) == []
    # A restart reuses the same frozen result rather than repeating this round.
    with pytest.raises(ReachedCoverage):
        await workflow._execute(rid)
    assert len(agent.operations) == 1


async def test_genuine_product_question_survives_refreshed_proposal(tmp_path, monkeypatch):
    workflow, rid, _, rows, _ = retained_wait(tmp_path, monkeypatch)
    workflow.agent = RefreshAgent(product_question=True)
    asked = []

    class Waiting(Exception):
        pass

    async def wait(question):
        asked.append(question)
        raise Waiting()

    workflow.question_handler = wait
    with pytest.raises(Waiting):
        await workflow._execute(rid)
    assert workflow.store.question(rows[0]["id"])["superseded"]
    assert asked[-1]["prompt"] == "Which product flow should be used?"
    assert asked[-1]["answer"] is None and not asked[-1]["superseded"]


@pytest.mark.parametrize("when", ["before", "during"])
async def test_real_answer_wins_using_original_nonzero_batch(tmp_path, monkeypatch, when):
    workflow, rid, _, rows, _ = retained_wait(tmp_path, monkeypatch, turn=7,
        questions=["Choose the product flow", "Restore internal evidence"])
    store = workflow.store
    before = store.get(rid, "invocation/clarify")
    def answer():
        store.answer(rows[0]["id"], "Keep the original flow")
    if when == "before":
        answer()
    workflow.agent = RefreshAgent(hook=answer if when == "during" else None)
    used = []

    class ConsumedOriginal(Exception):
        pass

    async def questions(run, key, prompts, fingerprint):
        used.append((key, prompts, fingerprint))
        assert store.question(rows[0]["id"])["answer"] == "Keep the original flow"
        raise ConsumedOriginal()

    workflow._questions = questions
    with pytest.raises(ConsumedOriginal):
        await workflow._execute(rid)
    assert used[0][0] == "clarify/7"
    assert used[0][2] == digest(before["result"])
    assert all(not store.question(row["id"])["superseded"] for row in rows)
    assert store.get(rid, "invocation/clarify") == before
    assert len(workflow.agent.operations) == int(when == "during")



async def test_old_answered_same_result_does_not_hide_current_waiting_batch(tmp_path, monkeypatch):
    workflow, rid, _, rows, _ = retained_wait(tmp_path, monkeypatch, turn=7)
    store = workflow.store
    previous = store.get(rid, "invocation/clarify")
    earlier = store.request(rid, "clarify/3/0", previous["result"]["questions"][0], digest(previous["result"]))
    store.answer(earlier["id"], "Keep the original requirements and recorded approval")
    earlier_record = store.question(earlier["id"])
    # Normal question versioning retains an answered older artifact at the same key.
    older_version = store.request(rid, rows[0]["key"], "Prior product question", "prior-result")
    store.answer(older_version["id"], "Preserve the supplied product choice")
    older_record = store.question(older_version["id"])
    assert store.request(rid, rows[0]["key"], rows[0]["prompt"], rows[0]["artifact_hash"])["id"] == rows[0]["id"]
    active = {q["id"]: (q["artifact_hash"], q["answer"])
              for q in store.decision_questions(rid, [rows[0]["key"]])}
    assert active == {older_version["id"]: ("prior-result", older_record["answer"]),
                      rows[0]["id"]: (digest(previous["result"]), None)}

    class Agent(RefreshAgent):
        async def invoke(self, stage, context, *args, **kwargs):
            if stage == "clarify":
                answers = {row["id"]: row for row in context["submitted_answers"]}
                assert answers[earlier["id"]]["answer"] == earlier_record["answer"]
                assert answers[older_version["id"]]["answer"] == older_record["answer"]
            return await super().invoke(stage, context, *args, **kwargs)

    workflow.agent = Agent()
    with pytest.raises(ReachedCoverage):
        await workflow._execute(rid)
    assert workflow.agent.operations[0].startswith("clarify/reentry-refresh/")
    assert store.question(rows[0]["id"])["superseded"]
    assert store.question(rows[0]["id"])["answer"] is None
    assert store.question(earlier["id"]) == earlier_record
    assert store.question(older_version["id"]) == older_record
    refreshed = [event for event in store.events(rid) if event["kind"] == "clarify.questions_refreshed"]
    assert refreshed[-1]["data"]["questions"] == [rows[0]["id"]]


async def test_multiple_waiting_batches_preserve_old_invocation_and_questions(tmp_path, monkeypatch):
    workflow, rid, _, rows, _ = retained_wait(tmp_path, monkeypatch, turn=7)
    store = workflow.store
    previous = store.get(rid, "invocation/clarify")
    other = store.request(rid, "clarify/3/0", previous["result"]["questions"][0], digest(previous["result"]))
    workflow.agent = RefreshAgent()
    with pytest.raises(StateConflict, match="multiple waiting batches"):
        await workflow._execute(rid)
    assert not workflow.agent.operations
    assert store.get(rid, "invocation/clarify") == previous
    assert all(store.question(q["id"])["answer"] is None and not store.question(q["id"])["superseded"]
               for q in [*rows, other])


@pytest.mark.parametrize("damage", ["question", "presentation", "feedback", "consent", "other_answer", "generation"])
async def test_refresh_cas_preserves_old_wait_on_concurrent_input_change(tmp_path, monkeypatch, damage):
    workflow, rid, work, rows, _ = retained_wait(tmp_path, monkeypatch)
    store = workflow.store
    previous = store.get(rid, "invocation/clarify")
    service, context = input_context(workflow, rid, work)
    def mutate():
        if damage == "question":
            store.supersede_questions(rid, rows[0]["key"])
        elif damage == "presentation":
            store.checkpoint(rid, service.original["presentation_key"], {"identity": "changed"})
        elif damage == "feedback":
            store.checkpoint(rid, "replan_feedback", {"stage": "clarify", "reason": "Other reentry"})
        elif damage == "consent":
            store.supersede_questions(rid, "prototype-approval", include_answered=True)
        elif damage == "generation":
            store.update(rid, replans=store.run(rid)["data"]["replans"] + 1)
        else:
            row = store.request(rid, "prototype-feedback", "Another requirement", "bound")
            store.answer(row["id"], "New substantive request")
    workflow.agent = RefreshAgent(hook=mutate)
    with pytest.raises(StateConflict, match="changed"):
        await service.refresh(rid, work, context)
    assert store.get(rid, "invocation/clarify") == previous
    assert store.get(rid, service.key) is None
    assert store.question(rows[0]["id"])["answer"] is None


async def test_interruption_after_new_call_resumes_without_repeating_or_overwriting_old_receipt(tmp_path, monkeypatch):
    workflow, rid, work, rows, _ = retained_wait(tmp_path, monkeypatch)
    store = workflow.store
    old = store.get(rid, "invocation/clarify")
    service, context = input_context(workflow, rid, work)
    workflow.agent = RefreshAgent()
    commit = store.refresh_clarify_questions
    def interrupted(*args, **kwargs):
        raise InterruptedError("Fixture interruption before CAS")
    monkeypatch.setattr(store, "refresh_clarify_questions", interrupted)
    with pytest.raises(InterruptedError):
        await service.refresh(rid, work, context)
    assert store.get(rid, "invocation/clarify") == old
    assert not store.question(rows[0]["id"])["superseded"]
    monkeypatch.setattr(store, "refresh_clarify_questions", commit)
    resumed, same_context = input_context(workflow, rid, work)
    assert same_context == context
    await resumed.refresh(rid, work, same_context)
    assert len(workflow.agent.operations) == 1
    assert store.question(rows[0]["id"])["superseded"]


@pytest.mark.parametrize("transitive", [False, True])
async def test_prepared_context_mounts_full_retained_records_and_images_read_only(tmp_path, monkeypatch, transitive):
    workflow, rid, work, _, _ = retained_wait(tmp_path / "project", monkeypatch)
    if transitive:
        report = workflow.store.get(rid, "prototype_browser_report")
        workflow.store.checkpoint(rid, "prototype_browser_report", workflow.artifacts.put(report, "prototype-browser-report"))
    _, context = input_context(workflow, rid, work)
    prepared = ContextPlanner(workflow.artifacts).prepare("clarify", context, work, run_id=rid)
    approval = workflow.store.get(rid, "prototype_approval")
    image = approval["visuals"][0]["image_artifact"]
    assert image["hash"] in {ref["hash"] for ref in references(prepared.payload)}
    if transitive:
        assert image["hash"] in {ref["hash"] for ref in prepared.payload["evidence_materials"]}
    else:
        assert prepared.payload["controller_reentry"]["retained_prototype"]["report"]["results"][0]["image_artifact"] == image
    binary = tmp_path / "provider"
    binary.write_text(f"#!{sys.executable}\n" + '''
import hashlib, json, pathlib, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("retained-material-fixture 1")
    sys.exit(0)
schema = json.loads(pathlib.Path(args[args.index("--output-schema") + 1]).read_text())
wire = sys.stdin.read()
context = json.loads(wire.split("<context>\\n", 1)[1].split("\\n</context>", 1)[0])
if "text_token" in schema["properties"]:
    result = {"text_token": pathlib.Path(context["context_files"][0]).read_text(), "image_token": ""}
else:
    retained = context["controller_reentry"]["retained_prototype"]
    refs = [retained["record"], retained["candidate"]["proposal"],
            context["controller_reentry"]["transition"]["report"],
            context["controller_reentry"]["operation"]["report"],
            context["controller_reentry"]["previous_clarification"]["proposal"]]
    def references(value):
        if isinstance(value, dict):
            if {"hash", "kind", "bytes", "location"} <= value.keys():
                yield value
            else:
                for child in value.values():
                    yield from references(child)
        elif isinstance(value, list):
            for child in value:
                yield from references(child)
    refs += list(references(context))
    kinds = set()
    for ref in refs:
        path = pathlib.Path(ref["location"])
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == ref["hash"] and len(raw) == ref["bytes"]
        kinds.add(ref["kind"])
        try:
            path.write_bytes(b"cannot rewrite retained evidence")
        except OSError:
            pass
        else:
            raise AssertionError("Retained artifact is writable")
    assert "screenshot" in kinds
    assert json.loads(pathlib.Path(retained["record"]["location"]).read_text())["human"]["answer"] == "1"
    assert retained["current_automatic_gates"] == "not_evaluated"
    result = {"approved": False, "summary": "Historical materials read; no current acceptance", "findings": []}
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
    result = await router.invoke("coverage_review", context, ReviewResult, work, rid, "retained-materials", fresh=True)
    assert not result.approved
    mounted = workflow.store.get(rid, "materials/retained-materials")["materials"]
    assert image["hash"] in {ref["hash"] for ref in mounted}
