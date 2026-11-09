"""Current approved action delivery is a fact, not another diagnosis allowance."""
import asyncio
import copy

import pytest
from evidence_test_fixtures import seed_requirements
from prototype_review_fixtures import prototype_verdict
from test_prototype_approvals import approval_fixture
from test_recovery_contracts import failure, plan

from auto_agents_2.decisions import DecisionResult
from auto_agents_2.errors import RecoveryPathExhausted
from auto_agents_2.faults import StateConflict
from auto_agents_2.recovery import RecoveryDecision
from auto_agents_2.workspace import tree_hash


def ready(tmp_path, monkeypatch):
    workflow, rid, _, question, _, _ = approval_fixture(tmp_path, monkeypatch, legacy=False)
    contract = plan()
    seed_requirements(workflow, rid, contract)
    spec = workflow.store.get(rid, "clarify")
    spec["frontend"] = True
    workflow.store.checkpoint(rid, "clarify", spec)
    approval = workflow.store.get(rid, "prototype_approval")
    actions = workflow.store.get(rid, "prototype")["browser_actions"]
    basis, _ = workflow.approvals.review_inputs(rid, tmp_path, actions, approval["visuals"])
    approval["review"] = prototype_verdict({"prototype_review_basis": basis}).model_dump()
    workflow.store.checkpoint(rid, "prototype_approval", approval)
    workflow.store.checkpoint(rid, "plan", contract.model_dump())
    record = failure()
    workflow.recovery.submit(rid, record)
    workflow.store.update(rid, replans=26)
    return workflow, rid, contract, record, question


async def authorize(workflow, rid, work, contract, record):
    action = RecoveryDecision(failure_id=record.id, action="repair", task_ids=[record.task_id],
        budget_owner=record.origin_id, evidence_refs=["failure"], reason="Repair within current scope")
    return await workflow.recovery_policy.authorize(rid, record,
        workflow.store.get(rid, "recovery/active"), work, contract, action,
        operation="dispatch/2", force_decision=True)


async def test_decision_gets_the_same_exact_material_as_the_author(tmp_path, monkeypatch):
    workflow, rid, contract, record, question = ready(tmp_path, monkeypatch)
    views = []

    async def choose(stage, context, schema, *args, **kwargs):
        assert stage == "decision"
        request = context["decision_request"]
        views.append(copy.deepcopy(request))
        material = request["evidence"]["author_materials"]
        author = workflow._task_approval_context(rid, tmp_path, {"specification": workflow.store.get(rid, "clarify")})
        assert material == author
        receipt = material["prototype_approval"]
        assert workflow.artifacts.json(receipt["browser_actions"]["artifact"]) == workflow.store.get(rid, "prototype")["browser_actions"]
        return DecisionResult(action="approve", selected_action="repair", reason="The exact source is supplied",
            evidence_refs=["author_materials"], expected_result="Author reads original actions; checks/review remain required")

    workflow.agent.invoke = choose
    selected, key = await authorize(workflow, rid, tmp_path, contract, record)
    assert selected.action == "repair" and len(views) == 1
    assert workflow.store.get(rid, key)["phase"] == "reserved"
    assert workflow.store.run(rid)["data"]["replans"] == 26
    assert workflow.store.question(question["id"])["answer"] == "1"


@pytest.mark.parametrize("reject_new", [False, True])
async def test_stopped_selection_sees_material_once_without_reopening_diagnosis(tmp_path, monkeypatch, reject_new):
    workflow, rid, contract, record, question = ready(tmp_path, monkeypatch)
    guard = workflow.recovery_policy.progress
    source = tree_hash(tmp_path)
    original_basis = guard.basis(guard.inputs(rid, record, tmp_path, contract), source, "diagnose")
    for i in range(2):
        guard.record(rid, record.origin_id, "diagnose", original_basis, f"old-diagnosis/{i}", {"status": "inconclusive"})
    workflow.store.checkpoint(rid, "rounds/T3", {"round": 17})
    workflow.store.checkpoint(rid, "recovery/budgets/diagnosis/T3", {"round": 9})
    requests = []

    async def choose(stage, context, *args, **kwargs):
        assert stage == "decision"
        request = copy.deepcopy(context["decision_request"])
        requests.append(request)
        assert "diagnose" not in request["scope"]["candidates"]
        if request["evidence"].get("author_materials") and not reject_new:
            return DecisionResult(action="approve", selected_action="repair", reason="Original actions are now supplied",
                evidence_refs=["author_materials"], expected_result="Read actions, then pass unchanged checks/review")
        return DecisionResult(action="ask_user", reason="No demonstrated executable correction")

    workflow.agent.invoke = choose
    with monkeypatch.context() as old:
        old.setattr(guard, "author_materials", lambda *args: {})
        old.setattr(guard, "author_source", lambda *args: {})
        with pytest.raises(RecoveryPathExhausted):
            await authorize(workflow, rid, tmp_path, contract, record)
        old_stops = {k: v for k, v in workflow.store.delivery_evidence(rid).items()
                     if k.startswith("recovery/stopped-selection/")}
    assert old_stops
    assert len(requests) == 1 and not workflow.store.pending(rid)
    old_requests = copy.deepcopy(workflow.store.decision_requests(rid))
    if reject_new:
        with pytest.raises(RecoveryPathExhausted):
            await authorize(workflow, rid, tmp_path, contract, record)
        with pytest.raises(RecoveryPathExhausted):
            await authorize(workflow, rid, tmp_path, contract, record)
    else:
        selected, key = await authorize(workflow, rid, tmp_path, contract, record)
        assert selected.action == "repair"
        assert (await authorize(workflow, rid, tmp_path, contract, record))[1] == key
    assert len(requests) == 2 and requests[-1]["evidence"]["author_materials"]
    assert set(guard.exclusions(rid, record, tmp_path, contract)) == {"diagnose"}
    assert guard.basis(guard.inputs(rid, record, tmp_path, contract), source, "diagnose") == original_basis
    assert workflow.store.get(rid, "rounds/T3") == {"round": 17}
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/T3") == {"round": 9}
    assert workflow.store.run(rid)["data"]["replans"] == 26
    assert workflow.store.question(question["id"])["answer"] == "1"
    for k, v in old_requests:
        assert workflow.store.get(rid, k) == v
    for k, v in old_stops.items():
        assert workflow.store.get(rid, k) == v


@pytest.mark.parametrize("change", ["no_consent", "rejected", "not_frontend", "outside_plan",
                                    "no_plan", "content", "partial_actions"])
def test_unavailable_or_unscoped_material_does_not_create_a_new_fact(tmp_path, monkeypatch, change):
    workflow, rid, contract, record, question = ready(tmp_path, monkeypatch)
    guard = workflow.recovery_policy.progress
    if change in {"no_consent", "rejected"}:
        with workflow.store.connect() as db:
            db.execute("UPDATE questions SET answer=? WHERE id=?",
                       (None if change == "no_consent" else "2", question["id"]))
    elif change == "not_frontend":
        spec = workflow.store.get(rid, "clarify")
        workflow.store.checkpoint(rid, "clarify", {**spec, "frontend": False})
    elif change == "outside_plan":
        record = record.model_copy(update={"task_id": "outside-plan"})
    elif change == "no_plan":
        contract = None
    elif change == "content":
        (tmp_path / "design/prototypes/index.html").write_text("<h1>Unapproved</h1>")
    else:
        approval = workflow.store.get(rid, "prototype_approval")
        approval["visuals"][0]["actions"] = []
        workflow.store.checkpoint(rid, "prototype_approval", approval)
    assert guard.author_materials(rid, record, tmp_path, contract) == {}
    if change == "outside_plan":
        with pytest.raises(StateConflict, match="task selection"):
            guard._facts(rid, record, tmp_path, contract)
    else:
        facts = guard._facts(rid, record, tmp_path, contract)
        assert set(facts) == {"diagnose", "plan_patch"}


def test_material_fact_excludes_presentation_wording_and_locations(tmp_path, monkeypatch):
    workflow, rid, contract, record, _ = ready(tmp_path, monkeypatch)
    guard = workflow.recovery_policy.progress
    materials = guard.author_materials(rid, record, tmp_path, contract)
    facts = guard._facts(rid, record, tmp_path, contract, author_materials=materials)
    changed = copy.deepcopy(materials)
    changed["prototype_approval"]["browser_actions"]["scope"] = "New explanatory text"
    changed["prototype_approval"]["browser_actions"]["artifact"]["location"] = "/another-presentation-path"
    changed["prototype_approval"]["context_version"] += 1
    assert guard._facts(rid, record, tmp_path, contract, author_materials=changed) == facts


@pytest.mark.parametrize("decision", ["approve", "ask_user"])
@pytest.mark.parametrize("change", ["delete", "corrupt", "human", "actions"])
async def test_decision_return_rechecks_material_and_human_before_permit_or_stop(tmp_path, monkeypatch, decision, change):
    workflow, rid, contract, record, question = ready(tmp_path, monkeypatch)
    guard = workflow.recovery_policy.progress
    basis = guard.basis(guard.inputs(rid, record, tmp_path, contract), tree_hash(tmp_path), "diagnose")
    for i in range(2):
        guard.record(rid, record.origin_id, "diagnose", basis, f"old/{i}", {"status": "inconclusive"})
    removed = []

    async def drift(stage, context, *args, **kwargs):
        assert stage == "decision"
        materials = context["decision_request"]["evidence"]["author_materials"]
        ref = materials["prototype_approval"]["browser_actions"]["artifact"]
        path = workflow.artifacts.root / ref["hash"][:2] / ref["hash"]
        if change == "delete":
            path.unlink()
            removed.append(path)
        elif change == "corrupt":
            path.write_bytes(b"corrupt")
        elif change == "human":
            with workflow.store.connect() as db:
                db.execute("UPDATE questions SET superseded=1 WHERE id=?", (question["id"],))
        else:
            prototype = workflow.store.get(rid, "prototype")
            prototype["browser_actions"][0]["value"] = "another obligation"
            workflow.store.checkpoint(rid, "prototype", prototype)
        return DecisionResult(action=decision, selected_action="repair" if decision == "approve" else None,
            reason="Must not be applied after drift", evidence_refs=["author_materials"],
            expected_result="Execute the existing task checks after an authorized repair")

    workflow.agent.invoke = drift
    with pytest.raises(StateConflict, match="Approved action"):
        await authorize(workflow, rid, tmp_path, contract, record)
    assert all(not path.exists() for path in removed)
    with workflow.store.connect() as db:
        assert not db.execute("SELECT 1 FROM checkpoint_records WHERE run_id=? AND (key LIKE 'recovery/permits/%' OR key LIKE 'recovery/stopped-selection/%')", (rid,)).fetchone()
    assert workflow.store.run(rid)["data"]["replans"] == 26


@pytest.mark.parametrize("state", ["pending", "answered", "issued"])
async def test_old_question_snapshot_and_permit_keep_original_material_absence(tmp_path, monkeypatch, state):
    from test_decisions import configure

    workflow, rid, contract, record, _ = ready(tmp_path, monkeypatch)
    guard = workflow.recovery_policy.progress
    configure(workflow, automation={"enabled": False})

    async def interrupt(question):
        raise asyncio.CancelledError()

    workflow.question_handler = interrupt
    with monkeypatch.context() as old:
        old.setattr(guard, "author_materials", lambda *args: {})
        old.setattr(guard, "author_source", lambda *args: {})
        with pytest.raises(asyncio.CancelledError):
            await authorize(workflow, rid, tmp_path, contract, record)
        question = copy.deepcopy(workflow.store.pending(rid)[0])
        if state in {"answered", "issued"}:
            workflow.store.answer(question["id"], "y")
        key = None
        if state == "issued":
            _, key = await authorize(workflow, rid, tmp_path, contract, record)
            permit = workflow.store.get(rid, key)
    snapshots = {k: copy.deepcopy(v) for k, v in workflow.store.delivery_evidence(rid).items()
                 if k.startswith("recovery/request-snapshots/") and "/history/" not in k}
    assert snapshots
    if state == "pending":
        with pytest.raises(asyncio.CancelledError):
            await authorize(workflow, rid, tmp_path, contract, record)
        assert workflow.store.question(question["id"]) == question
    else:
        _, resumed = await authorize(workflow, rid, tmp_path, contract, record)
        if state == "issued":
            assert resumed == key and workflow.store.get(rid, key) == permit
        assert workflow.store.question(question["id"])["answer"] == "y"
    for k, v in snapshots.items():
        assert "author_materials" not in v["request"]["evidence"]
        assert workflow.store.get(rid, k) == v


@pytest.mark.parametrize("state", ["pending", "stopped"])
@pytest.mark.parametrize("change", ["delete", "human"])
async def test_saved_material_is_checked_before_regeneration_or_new_fact_routing(tmp_path, monkeypatch, state, change):
    from test_decisions import configure

    workflow, rid, contract, record, human = ready(tmp_path, monkeypatch)
    guard = workflow.recovery_policy.progress
    calls = []

    async def decline(stage, context, *args, **kwargs):
        calls.append(context)
        return DecisionResult(action="ask_user", reason="No approved execution yet")

    async def interrupt(question):
        raise asyncio.CancelledError()

    workflow.agent.invoke = decline
    if state == "pending":
        configure(workflow, automation={"enabled": False})
        workflow.question_handler = interrupt
        with pytest.raises(asyncio.CancelledError):
            await authorize(workflow, rid, tmp_path, contract, record)
    else:
        basis = guard.basis(guard.inputs(rid, record, tmp_path, contract), tree_hash(tmp_path), "diagnose")
        for i in range(2):
            guard.record(rid, record.origin_id, "diagnose", basis, f"old/{i}", {"status": "inconclusive"})
        with pytest.raises(RecoveryPathExhausted):
            await authorize(workflow, rid, tmp_path, contract, record)
    snapshots = {k: copy.deepcopy(v) for k, v in workflow.store.delivery_evidence(rid).items()
                 if k.startswith("recovery/request-snapshots/") and "/history/" not in k}
    assert len(snapshots) == 1
    material = next(iter(snapshots.values()))["request"]["evidence"]["author_materials"]
    ref = material["prototype_approval"]["browser_actions"]["artifact"]
    path = workflow.artifacts.root / ref["hash"][:2] / ref["hash"]
    if change == "delete":
        path.unlink()
    else:
        with workflow.store.connect() as db:
            db.execute("UPDATE questions SET superseded=1 WHERE id=?", (human["id"],))
    before = len(calls)
    with pytest.raises(StateConflict, match="Approved action"):
        await authorize(workflow, rid, tmp_path, contract, record)
    assert len(calls) == before
    if change == "delete":
        assert not path.exists()
    for key, value in snapshots.items():
        assert workflow.store.get(rid, key) == value


@pytest.mark.parametrize("change", ["delete", "human"])
async def test_material_is_rechecked_after_the_last_manual_wait(tmp_path, monkeypatch, change):
    from test_decisions import configure

    workflow, rid, contract, record, human = ready(tmp_path, monkeypatch)
    configure(workflow, automation={"enabled": False})
    ref = workflow.recovery_policy.progress.author_materials(rid, record, tmp_path, contract)["prototype_approval"]["browser_actions"]["artifact"]
    path = workflow.artifacts.root / ref["hash"][:2] / ref["hash"]

    async def answer(question):
        if change == "delete":
            path.unlink()
        else:
            with workflow.store.connect() as db:
                db.execute("UPDATE questions SET superseded=1 WHERE id=?", (human["id"],))
        return "y"

    workflow.question_handler = answer
    with pytest.raises(StateConflict, match="Approved action"):
        await authorize(workflow, rid, tmp_path, contract, record)
    with workflow.store.connect() as db:
        assert not db.execute("SELECT 1 FROM checkpoint_records WHERE run_id=? AND key LIKE 'recovery/permits/%'", (rid,)).fetchone()
    if change == "delete":
        assert not path.exists()


async def test_old_issued_permit_does_not_generate_action_materials(tmp_path, monkeypatch):
    workflow, rid, contract, record, _ = ready(tmp_path, monkeypatch)
    guard = workflow.recovery_policy.progress

    async def choose(*args, **kwargs):
        return DecisionResult(action="approve", selected_action="repair", reason="Original scoped repair",
            evidence_refs=["failure"], expected_result="Run current task checks")

    workflow.agent.invoke = choose
    with monkeypatch.context() as old:
        old.setattr(guard, "author_materials", lambda *args: {})
        old.setattr(guard, "author_source", lambda *args: {})
        _, key = await authorize(workflow, rid, tmp_path, contract, record)
    before = workflow.store.get(rid, key)

    def unexpected(*args):
        pytest.fail("Reusing an issued legacy permit must not generate new approval materials")

    monkeypatch.setattr(guard, "author_materials", unexpected)
    assert (await authorize(workflow, rid, tmp_path, contract, record))[1] == key
    assert workflow.store.get(rid, key) == before


@pytest.mark.parametrize("change", ["delete", "human"])
async def test_evidence_child_request_never_republishes_a_lost_frozen_body(tmp_path, monkeypatch, change):
    workflow, rid, contract, record, human = ready(tmp_path, monkeypatch)
    guard = workflow.recovery_policy.progress
    basis = guard.basis(guard.inputs(rid, record, tmp_path, contract), tree_hash(tmp_path), "diagnose")
    for i in range(2):
        guard.record(rid, record.origin_id, "diagnose", basis, f"old/{i}", {"status": "inconclusive"})
    calls = []

    async def choose(stage, context, *args, **kwargs):
        calls.append(copy.deepcopy(context))
        if context["decision_request"]["evidence"].get("author_materials"):
            raise asyncio.CancelledError()
        return DecisionResult(action="ask_user", reason="Old input had no body")

    workflow.agent.invoke = choose
    with monkeypatch.context() as old:
        old.setattr(guard, "author_materials", lambda *args: {})
        old.setattr(guard, "author_source", lambda *args: {})
        with pytest.raises(RecoveryPathExhausted):
            await authorize(workflow, rid, tmp_path, contract, record)
    with pytest.raises(asyncio.CancelledError):
        await authorize(workflow, rid, tmp_path, contract, record)
    assert len(calls) == 2
    material = calls[-1]["decision_request"]["evidence"]["author_materials"]
    ref = material["prototype_approval"]["browser_actions"]["artifact"]
    path = workflow.artifacts.root / ref["hash"][:2] / ref["hash"]
    if change == "delete":
        path.unlink()
        expected = StateConflict
    else:
        with workflow.store.connect() as db:
            db.execute("UPDATE questions SET superseded=1 WHERE id=?", (human["id"],))
        expected = RecoveryPathExhausted  # Return to unchanged original stop, never a fresh choice.
    with pytest.raises(expected):
        await authorize(workflow, rid, tmp_path, contract, record)
    assert len(calls) == 2
    if change == "delete":
        assert not path.exists()


async def test_new_material_publication_failure_never_dispatches_a_decision(tmp_path, monkeypatch):
    workflow, rid, contract, record, _ = ready(tmp_path, monkeypatch)
    original = workflow.artifacts.put
    calls = []

    def publish(value, kind="document"):
        if kind == "approved_prototype_actions":
            raise OSError("fixture: unavailable artifact storage")
        return original(value, kind)

    async def unexpected(*args, **kwargs):
        calls.append(args)
        pytest.fail("No decision may consume an unpublished action body")

    monkeypatch.setattr(workflow.artifacts, "put", publish)
    workflow.agent.invoke = unexpected
    with pytest.raises(OSError, match="unavailable artifact storage"):
        await authorize(workflow, rid, tmp_path, contract, record)
    assert not calls
