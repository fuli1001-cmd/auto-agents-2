"""Initial-plan upstream gaps retain review provenance and normal re-entry accounting."""
import copy

import pytest
from authority_review_fixtures import authority_review
from evidence_test_fixtures import evidence_plan, evidence_review
from test_decisions import setup
from test_recovery_contracts import plan as source_plan

from auto_agents_2.context_plan import CONTEXT_VERSION, ContextPlanner
from auto_agents_2.engine import Replan
from auto_agents_2.errors import AA2Error
from auto_agents_2.evidence_admission import admission_context, validate_admission_review
from auto_agents_2.faults import StateConflict
from auto_agents_2.interaction import choice_spec
from auto_agents_2.issues import IssueLedger
from auto_agents_2.models import EvidencePlanReview
from auto_agents_2.plan_review_recovery import PREFIX, PlanReviewRecovery
from auto_agents_2.util import digest


def fixture(tmp_path, owners=("provider_research",), *, presentation_version=CONTEXT_VERSION):
    workflow, rid = setup(tmp_path)
    work = workflow.project
    workflow.store.update(rid, replans=17)
    workflow.store.checkpoints(rid, {
        "authority/protocol": 1, "provider_research": {"document": "Existing source research"},
        "prototype": {"document": "Approved prototype"}, "prototype_approval": {"artifact": "approved-version"}})
    question = workflow.store.request(rid, "prototype-approval", "Review this exact version", "approved-version",
                                     choice_spec(["Approve", "Reject"], ["approve", "reject"]))
    workflow.store.answer(question["id"], "1", "approved-version")
    context = admission_context(lambda key: workflow.store.get(rid, key))
    candidate = evidence_plan(source_plan(), context).model_dump()
    context.update(review_kind="plan", plan=candidate, project=workflow.contexts.build(work),
                   provider_research=workflow.store.get(rid, "provider_research"),
                   prototype=workflow.store.get(rid, "prototype"))
    context, _ = workflow.authority.prepare(rid, "review", context, EvidencePlanReview, work, "plan-review")
    base = authority_review(context, approved=False, summary="Research evidence is required before planning")
    first = base.findings[0]
    base.findings = [first.model_copy(update={"location": f"plan.tasks[{index}]"}) for index in range(len(owners))]
    row = base.authority["findings"][0]
    base.authority["findings"] = [{**row, "finding_index": index, "kind": "evidence_gap", "owner": owner}
                                  for index, owner in enumerate(owners)]
    for assessment in base.authority["coverage"]:
        if assessment["criterion_id"] == row["criterion_id"]:
            assessment["status"] = "unverified"
    review = evidence_review(context, base)
    workflow.test_plan_context = copy.deepcopy(context)
    presentation_identity = digest(context)
    prepared = ContextPlanner(workflow.artifacts).prepare("review", context, work, "plan-review", run_id=rid)
    prepared.version = presentation_version
    workflow.store.checkpoint(rid, "presentation/plan-review/" + presentation_identity, {
        "identity": presentation_identity, "artifact": workflow.artifacts.put(prepared.record(), "prepared_context")})
    workflow.authority.record(rid, review, context, "plan-review")
    IssueLedger(workflow.store, workflow.artifacts).record(rid, "plan-review", review, context)
    artifact = workflow.artifacts.put(candidate, "plan")
    workflow.store.checkpoints(rid, {
        "candidate/plan": {"document": candidate},
        "invocation/plan": {"result": candidate, "artifact": artifact},
        "provider-result/plan": {"artifact": artifact, "attempt": "original-plan"},
        "invocation/plan-review": {"identity": presentation_identity, "result": review.model_dump(),
                                   "artifact": workflow.artifacts.put(review.model_dump(), "review")},
        "infra/plan/codex": {"failures": 1, "grant": 0, "pending": False, "error_kind": "invalid_structured_response"},
        "infra/plan/fallback": {"failures": 2, "grant": 0, "pending": False, "error_kind": "unavailable"},
        "rounds/T1": {"round": 8}})
    return workflow, rid, work, candidate, review, context, question["id"]


async def legacy(tmp_path, owners=("provider_research",), *, presentation_version=CONTEXT_VERSION):
    values = fixture(tmp_path, owners, presentation_version=presentation_version)
    workflow, rid = values[:2]
    workflow._controller_replan(rid, Replan("plan", "Legacy admission rejection incorrectly selected plan"))
    await workflow._resume_replan(rid)
    assert workflow.store.run(rid)["data"]["replans"] == 18
    return values


def routed(workflow, rid, work, candidate, review):
    with pytest.raises(Replan) as caught:
        PlanReviewRecovery(workflow).route(rid, work, candidate, review,
                                           upstream_context=workflow.test_plan_context)
    return caught.value


@pytest.mark.parametrize("owner", ["clarify", "provider_research", "prototype", "design"])
async def test_bound_upstream_review_creates_separate_permit_without_accepting_plan(tmp_path, owner):
    workflow, rid, work, candidate, review, context, qid = fixture(tmp_path, (owner,))
    with pytest.raises(AA2Error, match="Evidence admission rejected"):
        validate_admission_review(source_plan().model_validate(candidate), review, context)
    approval = workflow.store.question(qid)
    exc = routed(workflow, rid, work, candidate, review)
    assert exc.stage == owner and workflow.store.get(rid, "plan") is None
    workflow._controller_replan(rid, exc)
    pending = await workflow._grant_replan(rid, workflow.store.get(rid, "replanning/pending"))
    permit = workflow.store.get(rid, pending["permit"])
    assert permit["action"]["action"] == owner
    assert permit["authority_failure"]["evidence"]["review"] == review.model_dump()
    assert workflow.store.run(rid)["data"]["replans"] == 17
    assert workflow.store.get(rid, "candidate/plan")["document"] == candidate
    assert workflow.store.question(qid) == approval


@pytest.mark.parametrize("presentation_version", ["context-v7", CONTEXT_VERSION])
async def test_latest_legacy_reentry_recovers_once_without_provider_or_budget_reset(tmp_path, presentation_version):
    workflow, rid, work, candidate, review, _, qid = await legacy(tmp_path, presentation_version=presentation_version)
    recovery = PlanReviewRecovery(workflow)
    old_history = copy.deepcopy(workflow.store.get(rid, "recovery/gate-history/17"))
    old_permit_key = old_history["pending"]["permit"]
    old_permit = workflow.store.get(rid, old_permit_key)
    approval = workflow.store.question(qid)
    counters = {k: workflow.store.get(rid, k) for k in ("infra/plan/codex", "infra/plan/fallback", "rounds/T1")}
    calls = list(workflow.agent.calls)
    for _ in range(2):
        with pytest.raises(Replan) as caught:
            recovery.recover(rid, work)
        assert caught.value.stage == "provider_research"
        workflow._controller_replan(rid, caught.value)
    assert workflow.agent.calls == calls  # Recovery consumes the retained independent review.
    assert workflow.store.run(rid)["data"]["replans"] == 18
    pending = await workflow._grant_replan(rid, workflow.store.get(rid, "replanning/pending"))
    assert pending["permit"] != old_permit_key
    assert workflow.store.get(rid, pending["permit"])["action"]["action"] == "provider_research"
    after_grant = list(workflow.agent.calls)
    assert (await workflow._grant_replan(rid, pending))["permit"] == pending["permit"]
    assert workflow.agent.calls == after_grant
    await workflow._resume_replan(rid)
    assert workflow.store.run(rid)["data"]["replans"] == 19
    recovery.recover(rid, work)
    assert workflow.store.run(rid)["data"]["replans"] == 19
    assert workflow.store.get(rid, "recovery/gate-history/17") == old_history
    assert workflow.store.get(rid, old_permit_key) == old_permit
    assert {k: workflow.store.get(rid, k) for k in counters} == counters
    assert workflow.store.question(qid) == approval
    assert workflow.store.get(rid, "plan") is None


@pytest.mark.parametrize("change", ["source", "research", "candidate", "permit", "issue", "latest", "feedback", "presentation"])
@pytest.mark.parametrize("presentation_version", ["context-v7", CONTEXT_VERSION])
async def test_legacy_recovery_rejects_drift_and_incomplete_bindings(tmp_path, monkeypatch, change, presentation_version):
    workflow, rid, work, _, _, _, _ = await legacy(tmp_path, presentation_version=presentation_version)
    if change == "source":
        (work / "user-edit.txt").write_text("preserve user change")
    elif change == "research":
        workflow.store.checkpoint(rid, "provider_research", {"document": "New research"})
    elif change in {"candidate", "latest"}:
        history = workflow.store.get(rid, "recovery/gate-history/17")
        if change == "candidate":
            history["checkpoints"]["candidate/plan"]["document"]["summary"] = "Changed archived candidate"
        else:
            workflow.store.update(rid, replans=19)
        workflow.store.checkpoint(rid, "recovery/gate-history/17", history)
    elif change == "permit":
        key = workflow.store.get(rid, "recovery/gate-history/17")["pending"]["permit"]
        original = workflow.store.get
        def damaged(run, name, default=None):
            value = original(run, name, default)
            return {**value, "phase": "reserved"} if name == key else value
        monkeypatch.setattr(workflow.store, "get", damaged)
    elif change == "presentation":
        identity = workflow.store.get(rid, "invocation/plan-review")["identity"]
        workflow.store.checkpoint(rid, "presentation/plan-review/" + identity, {})
    elif change == "issue":
        issues = workflow.store.get(rid, "issues")
        next(iter(issues.values()))["status"] = "closed"
        workflow.store.checkpoint(rid, "issues", issues)
    else:
        feedback = workflow.store.get(rid, "replan_feedback")
        feedback["reason"] = "Another recovery"
        workflow.store.checkpoint(rid, "replan_feedback", feedback)
    before = workflow.store.run(rid)["data"]["replans"]
    calls = list(workflow.agent.calls)
    with pytest.raises(StateConflict):
        PlanReviewRecovery(workflow).recover(rid, work)
    assert workflow.store.run(rid)["data"]["replans"] == before
    assert workflow.agent.calls == calls
    assert workflow.store.get(rid, "replanning/pending") is None


@pytest.mark.parametrize("owners", [("provider_research", "design"), ("provider_research", "plan")])
async def test_multiple_or_mixed_owners_do_not_dispatch_an_upstream_stage(tmp_path, owners):
    workflow, rid, work, candidate, review, _, _ = fixture(tmp_path, owners)
    with pytest.raises(StateConflict, match="single"):
        PlanReviewRecovery(workflow).route(rid, work, candidate, review,
                                           upstream_context=workflow.test_plan_context)
    assert workflow.store.get(rid, "replanning/pending") is None


async def test_local_plan_findings_and_later_upstream_reentries_are_not_reinterpreted(tmp_path):
    workflow, rid, work, _, _, _, _ = await legacy(tmp_path, ("plan",))
    PlanReviewRecovery(workflow).recover(rid, work)
    assert workflow.store.get(rid, "replanning/pending") is None
    workflow.store.checkpoint(rid, "replan_feedback", {"stage": "provider_research"})
    PlanReviewRecovery(workflow).recover(rid, work)
    assert workflow.store.run(rid)["data"]["replans"] == 18


async def test_new_valid_candidate_prevents_reuse_of_old_rejected_plan(tmp_path):
    workflow, rid, work, candidate, _, _, _ = await legacy(tmp_path)
    workflow.store.checkpoint(rid, "candidate/plan", {"document": candidate})
    PlanReviewRecovery(workflow).recover(rid, work)
    assert workflow.store.get(rid, "replanning/pending") is None


async def test_replan_journal_interrupt_preserves_review_authority_before_second_checkpoint(tmp_path, monkeypatch):
    workflow, rid, work, candidate, review, _, _ = fixture(tmp_path)
    exc = routed(workflow, rid, work, candidate, review)
    assert not PlanReviewRecovery(workflow).requires_research_review(rid)
    original = workflow.store.checkpoint
    def interrupt(run, key, value):
        if key == "replanning/pending" and value.get("controller_owned"):
            raise OSError("fixture interruption after prepare_replan")
        return original(run, key, value)
    monkeypatch.setattr(workflow.store, "checkpoint", interrupt)
    with pytest.raises(OSError, match="fixture interruption"):
        workflow._controller_replan(rid, exc)
    monkeypatch.setattr(workflow.store, "checkpoint", original)
    pending = workflow.store.get(rid, "replanning/pending")
    assert not pending.get("controller_owned") and pending["stage"] == "provider_research"
    proof = PlanReviewRecovery(workflow).evidence(rid, pending, work)
    assert proof["review"] == review.model_dump()
    await workflow._resume_replan(rid)
    history = workflow.store.get(rid, "recovery/gate-history/17")
    permit = workflow.store.get(rid, history["pending"]["permit"])
    assert permit["action"]["action"] == "provider_research"
    assert permit["authority_failure"]["evidence"]["review"] == review.model_dump()
    assert workflow.store.run(rid)["data"]["replans"] == 18
    assert PlanReviewRecovery(workflow).requires_research_review(rid)


@pytest.mark.parametrize("phase", ["granted", "authorized", "active_patch"])
async def test_every_authorization_exit_rechecks_review_and_requires_own_permit(tmp_path, monkeypatch, phase):
    workflow, rid, work, candidate, review, _, _ = fixture(tmp_path)
    workflow._controller_replan(rid, routed(workflow, rid, work, candidate, review))
    pending = workflow.store.get(rid, "replanning/pending")
    if phase == "granted":
        pending = await workflow._grant_replan(rid, pending)
        (work / "user-edit.txt").write_text("after previous grant")
    elif phase == "authorized":
        original = workflow.recovery_policy.authorize
        async def change_after(*args, **kwargs):
            result = await original(*args, **kwargs)
            (work / "user-edit.txt").write_text("during authorization")
            return result
        monkeypatch.setattr(workflow.recovery_policy, "authorize", change_after)
    else:
        workflow.store.checkpoint(rid, "recovery/active", {"permit": "unrelated-plan-patch", "decision": {"action": "plan_patch"}})
    with pytest.raises(StateConflict):
        await workflow._grant_replan(rid, pending)
    assert workflow.store.run(rid)["data"]["replans"] == 17
    assert workflow.store.get(rid, "plan") is None


async def test_missing_persisted_review_route_cannot_fall_back_to_generic_authorization(tmp_path):
    workflow, rid, work, candidate, review, _, _ = fixture(tmp_path)
    exc = routed(workflow, rid, work, candidate, review)
    workflow._controller_replan(rid, exc)
    workflow.store.checkpoint(rid, PREFIX + digest(exc.reason), None)
    with pytest.raises(StateConflict, match="lost its recorded"):
        await workflow._grant_replan(rid, workflow.store.get(rid, "replanning/pending"))


@pytest.mark.parametrize("interrupt_after_commit", [False, True])
@pytest.mark.parametrize("reject_first_research_review", [False, True])
@pytest.mark.parametrize("restore_unreviewed_cache", [False, True])
async def test_engine_routes_research_gap_and_reviews_source_free_research_before_replanning(
        tmp_path, monkeypatch, reject_first_research_review, restore_unreviewed_cache, interrupt_after_commit):
    import asyncio

    from test_decisions import ApprovingAgent
    from test_workflow import engine

    class Agent(ApprovingAgent):
        plans = 0
        research_calls = 0
        research_reviews = 0
        cache_interrupted = False
        resumed_permit = None

        async def invoke(self, stage, context, schema, *args, **kwargs):
            if self.resumed_permit:
                # Receipt reconciliation must precede even the first new call.
                assert workflow.store.get(rid, self.resumed_permit)["phase"] == "completed"
            if stage == "provider_research":
                self.research_calls += 1
            if stage == "plan":
                self.plans += 1
                if self.plans == 2:
                    assert self.research_calls == 2 + int(reject_first_research_review)
                    assert self.research_reviews == 1 + int(reject_first_research_review) + int(restore_unreviewed_cache)
                    raise asyncio.CancelledError()
            if context.get("review_kind") == "research":
                self.research_reviews += 1
                assert context["research"]["sources"] == []
                if restore_unreviewed_cache and not self.cache_interrupted:
                    self.cache_interrupted = True
                    raise asyncio.CancelledError()
                if reject_first_research_review and self.research_reviews == 1 + int(restore_unreviewed_cache):
                    return schema.model_validate(authority_review(context, approved=False,
                        summary="The research gap is still unresolved").model_dump())
            if context.get("review_kind") == "plan":
                base = authority_review(context, approved=False, summary="A required research source is missing")
                base.authority["findings"][0].update(kind="evidence_gap", owner="provider_research")
                criterion = base.authority["findings"][0]["criterion_id"]
                next(row for row in base.authority["coverage"] if row["criterion_id"] == criterion)["status"] = "unverified"
                return schema.model_validate(evidence_review(context, base).model_dump())
            return await super().invoke(stage, context, schema, *args, **kwargs)

    requirements = tmp_path / "requirements.txt"
    requirements.write_text("Implement addition")
    agent = Agent()
    project = tmp_path / "project"
    project.mkdir()
    workflow = engine(project, agent)
    original_complete = workflow.recovery_policy.complete
    committed_history = None
    def interrupt(run, permit_key, outcome):
        if outcome == {"status": "gate_reset", "stage": "provider_research"}:
            assert workflow.store.get(run, "replanning/pending") is None
            assert workflow.store.get(run, "recovery/gate-history/0")["pending"]["permit"] == permit_key
            raise asyncio.CancelledError()
        return original_complete(run, permit_key, outcome)
    if interrupt_after_commit:
        monkeypatch.setattr(workflow.recovery_policy, "complete", interrupt)
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(requirements))
    rid = workflow.store.current(unfinished=True)["id"]
    if interrupt_after_commit:
        committed_history = workflow.store.get(rid, "recovery/gate-history/0")
        agent.resumed_permit = committed_history["pending"]["permit"]
        assert workflow.store.get(rid, agent.resumed_permit)["phase"] == "started"
        assert workflow.store.run(rid)["data"]["replans"] == 1
        assert agent.research_calls == 1 and agent.research_reviews == 0
        monkeypatch.setattr(workflow.recovery_policy, "complete", original_complete)
        with pytest.raises(asyncio.CancelledError):
            await workflow.start(run_id=rid)
    if restore_unreviewed_cache:
        candidate = workflow.store.get(rid, "candidate/provider_research")["document"]
        # Older controllers could cache this exact candidate before its review.
        workflow.store.checkpoint(rid, "provider_research", candidate)
        with pytest.raises(asyncio.CancelledError):
            await workflow.start(run_id=rid)
    assert workflow.store.run(rid)["data"]["replans"] == 1 + int(reject_first_research_review)
    assert workflow.store.get(rid, "plan") is None
    assert workflow.store.get(rid, "authority/accepted/provider_research")
    assert agent.research_calls == 2 + int(reject_first_research_review)
    assert agent.research_reviews == 1 + int(reject_first_research_review) + int(restore_unreviewed_cache)
    assert not PlanReviewRecovery(workflow).requires_research_review(rid)
    if committed_history:
        assert workflow.store.get(rid, "recovery/gate-history/0") == committed_history
        assert workflow.store.get(rid, agent.resumed_permit)["outcome"] == {
            "status": "gate_reset", "stage": "provider_research"}
    if reject_first_research_review:
        issues = [row for row in workflow.store.get(rid, "issues").values()
                  if row["scope"].startswith("research-review/plan-gap/")]
        assert issues and all(row["status"] == "closed" for row in issues)
        accepted = workflow.store.get(rid, "authority/accepted/provider_research")
        acceptance = workflow.store.get(rid, accepted["review_key"])
        for issue in issues:
            assert issue["scope"] == acceptance["operation"].rsplit("/", 1)[0]
            assert issue["closure"]["kind"] == "scoped_independent_review"
            closed = workflow.artifacts.json(issue["closure"]["evidence"])["review"]
            assert closed == acceptance["review"] and closed["approved"]
            observations = [workflow.artifacts.json(ref)["review"] for ref in issue["observations"]]
            assert observations and all(not raw["approved"] for raw in observations)
            rejection = workflow.authority.receipt(rid, observations[0])
            assert rejection["operation"] != acceptance["operation"]
            assert rejection["operation"].rsplit("/", 1)[0] == issue["scope"]
        assert not any(row in IssueLedger(workflow.store, workflow.artifacts).unresolved(rid) for row in issues)


async def test_checkpoint_only_upstream_change_during_review_cannot_acquire_new_binding(tmp_path):
    workflow, rid, work, candidate, review, _, _ = fixture(tmp_path)
    workflow.store.checkpoint(rid, "provider_research", {"document": "Changed during independent review"})
    with pytest.raises(StateConflict, match="inputs changed"):
        routed(workflow, rid, work, candidate, review)
    assert workflow.store.get(rid, "replanning/pending") is None


@pytest.mark.parametrize("already_granted", [False, True])
async def test_old_replan_answer_cannot_authorize_new_owner_without_new_permit(tmp_path, already_granted):
    workflow, rid, work, candidate, review, _, _ = fixture(tmp_path)
    workflow._controller_replan(rid, routed(workflow, rid, work, candidate, review))
    pending = workflow.store.get(rid, "replanning/pending")
    key = f"replan-stalled/{pending['evaluation']}"
    question = workflow.store.request(rid, key, "Legacy plan grant", "legacy-plan")
    workflow.store.answer(question["id"], "yes")
    original = workflow.store.question(question["id"])
    if already_granted:
        with pytest.raises(StateConflict, match="new permit"):
            await workflow._grant_replan(rid, {**pending, "granted": True})
    else:
        granted = await workflow._grant_replan(rid, pending)
        assert granted["permit"]
        assert workflow.store.get(rid, granted["permit"])["action"]["action"] == "provider_research"
    assert workflow.store.question(question["id"]) == original
    assert workflow.store.run(rid)["data"]["replans"] == 17


async def test_route_persistence_before_replan_cannot_activate_old_research_review(tmp_path):
    workflow, rid, work, candidate, review, _, _ = fixture(tmp_path)
    exc = routed(workflow, rid, work, candidate, review)
    recovery = PlanReviewRecovery(workflow)
    assert not recovery.requires_research_review(rid)
    workflow._controller_replan(rid, exc)
    pending = await workflow._grant_replan(rid, workflow.store.get(rid, "replanning/pending"))
    assert pending["granted"] and not recovery.requires_research_review(rid)
    await workflow._resume_replan(rid)
    assert recovery.requires_research_review(rid)
    history = workflow.store.get(rid, "recovery/gate-history/17")
    history["pending"]["reason"] = "Unrelated completed re-entry"
    workflow.store.checkpoint(rid, "recovery/gate-history/17", history)
    with pytest.raises(StateConflict, match="completed authorized"):
        recovery.requires_research_review(rid)


@pytest.mark.parametrize("reason", [None, "Unrelated nonempty legacy grant"])
async def test_review_bound_granted_permit_cannot_drop_reason_to_use_legacy_shortcut(tmp_path, reason):
    workflow, rid, work, candidate, review, _, _ = fixture(tmp_path)
    workflow._controller_replan(rid, routed(workflow, rid, work, candidate, review))
    pending = await workflow._grant_replan(rid, workflow.store.get(rid, "replanning/pending"))
    pending.pop("reason")
    if reason is not None:
        pending["reason"] = reason
    with pytest.raises(StateConflict, match="bound reason"):
        await workflow._grant_replan(rid, pending)
    assert workflow.store.run(rid)["data"]["replans"] == 17


async def committed_without_receipt(tmp_path, monkeypatch, *, legacy_route=False, owner="provider_research"):
    values = await legacy(tmp_path, (owner,)) if legacy_route else fixture(tmp_path, (owner,))
    workflow, rid, work, candidate, review, _, _ = values
    recovery = PlanReviewRecovery(workflow)
    if legacy_route:
        with pytest.raises(Replan) as caught:
            recovery.recover(rid, work)
        exc = caught.value
    else:
        exc = routed(workflow, rid, work, candidate, review)
    workflow._controller_replan(rid, exc)
    before = workflow.store.run(rid)["data"]["replans"]
    def interrupt(*args):
        assert workflow.store.get(rid, "replanning/pending") is None
        assert workflow.store.run(rid)["data"]["replans"] == before + 1
        raise OSError("fixture interruption after real complete_replan commit")
    with monkeypatch.context() as fault:
        fault.setattr(workflow.recovery_policy, "complete", interrupt)
        with pytest.raises(OSError, match="after real complete_replan commit"):
            await workflow._resume_replan(rid)
    return values, workflow.store.get(rid, f"recovery/gate-history/{before}")


@pytest.mark.parametrize("legacy_route", [False, True])
@pytest.mark.parametrize("owner", ["clarify", "provider_research", "prototype", "design"])
async def test_committed_reentry_receipt_reconciles_once_with_original_review(tmp_path, monkeypatch, legacy_route, owner):
    values, history = await committed_without_receipt(tmp_path, monkeypatch, legacy_route=legacy_route, owner=owner)
    workflow, rid, work, _, _, _, qid = values
    generation = history["pending"]["generation"]
    key = history["pending"]["permit"]
    assert workflow.store.get(rid, key)["phase"] == "started"
    counters = {k: workflow.store.get(rid, k) for k in ("infra/plan/codex", "infra/plan/fallback", "rounds/T1")}
    approval = workflow.store.question(qid)
    calls = list(workflow.agent.calls)
    recovery = PlanReviewRecovery(workflow)
    recovery.reconcile_completion(rid)
    permit = workflow.store.get(rid, key)
    assert permit["phase"] == "completed" and permit["outcome"] == {"status": "gate_reset", "stage": owner}
    recovery.reconcile_completion(rid)
    assert workflow.store.get(rid, key) == permit
    assert workflow.store.get(rid, f"recovery/gate-history/{generation}") == history
    assert workflow.store.run(rid)["data"]["replans"] == generation + 1
    assert workflow.store.get(rid, "replanning/pending") is None
    assert workflow.store.question(qid) == approval
    assert {k: workflow.store.get(rid, k) for k in counters} == counters
    assert workflow.agent.calls == calls
    if owner == "provider_research":
        assert recovery.requires_research_review(rid)


@pytest.mark.parametrize("damage", ["source", "history", "feedback", "permit", "review", "issue",
                                    "advanced", "missing_preparation", "missing_history"])
async def test_committed_reentry_mismatch_never_completes_permit(tmp_path, monkeypatch, damage):
    values, history = await committed_without_receipt(tmp_path, monkeypatch)
    workflow, rid, work, _, review, _, _ = values
    pending = history["pending"]
    key = pending["permit"]
    generation = pending["generation"]
    if damage == "source":
        (work / "user-change.txt").write_text("Preserve this concurrent source change")
    elif damage == "history":
        history["pending"]["correction_request"]["permit_hash"] = "different request"
        workflow.store.checkpoint(rid, f"recovery/gate-history/{generation}", history)
    elif damage == "feedback":
        workflow.store.checkpoint(rid, "replan_feedback", {"reason": "Different feedback"})
    elif damage == "permit":
        original = workflow.store.get
        def changed(run, name, default=None):
            value = original(run, name, default)
            return {**value, "source_hash": "different authorization"} if name == key else value
        monkeypatch.setattr(workflow.store, "get", changed)
    elif damage == "review":
        workflow.store.checkpoint(rid, "authority/reviews/" + digest(review.model_dump()), None)
    elif damage == "issue":
        issues = workflow.store.get(rid, "issues")
        next(iter(issues.values()))["status"] = "closed"
        workflow.store.checkpoint(rid, "issues", issues)
    elif damage == "advanced":
        workflow.store.checkpoint(rid, "invocation/provider_research", {"identity": "another call"})
    elif damage == "missing_preparation":
        workflow.store.checkpoint(rid, PREFIX + digest(pending["reason"]) + "/completion", None)
    else:
        workflow.store.checkpoint(rid, f"recovery/gate-history/{generation}", None)
    calls = list(workflow.agent.calls)
    with pytest.raises(StateConflict):
        PlanReviewRecovery(workflow).reconcile_completion(rid)
    assert workflow.store.get(rid, key)["phase"] == "started"
    assert workflow.store.run(rid)["data"]["replans"] == generation + 1
    assert workflow.agent.calls == calls


async def test_precommit_interruption_preserves_started_permit_until_real_reset(tmp_path, monkeypatch):
    import asyncio

    workflow, rid, work, candidate, review, _, _ = fixture(tmp_path)
    workflow._controller_replan(rid, routed(workflow, rid, work, candidate, review))
    def interrupt(*args):
        raise OSError("fixture interruption before gate transaction")
    with monkeypatch.context() as fault:
        fault.setattr(workflow.store, "complete_replan", interrupt)
        with pytest.raises(OSError, match="before gate transaction"):
            await workflow._resume_replan(rid)
    pending = workflow.store.get(rid, "replanning/pending")
    key = pending["permit"]
    assert workflow.store.get(rid, PREFIX + digest(pending["reason"]) + "/completion")
    recovery = PlanReviewRecovery(workflow)
    recovery.reconcile_completion(rid)
    assert workflow.store.get(rid, key)["phase"] == "started"
    assert workflow.store.get(rid, "recovery/gate-history/17") is None
    assert workflow.store.run(rid)["data"]["replans"] == 17
    assert not recovery.requires_research_review(rid)
    async def stopped_after_resume(run):
        assert run == rid
        assert workflow.store.get(rid, key)["phase"] == "completed"
        assert workflow.store.run(rid)["data"]["replans"] == 18
        assert recovery.requires_research_review(rid)
        raise asyncio.CancelledError()
    monkeypatch.setattr(workflow, "_execute", stopped_after_resume)
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(run_id=rid)



@pytest.mark.parametrize("presentation_version", [None, "context-v6", "context-v999"])
async def test_unknown_completed_plan_view_is_not_granted_v7_compatibility(tmp_path, presentation_version):
    workflow, rid, work, _, _, _, _ = await legacy(tmp_path, presentation_version=presentation_version)
    before = workflow.store.run(rid)["data"]["replans"]
    calls = list(workflow.agent.calls)
    with pytest.raises(StateConflict, match="upstream inputs changed or cannot be proven"):
        PlanReviewRecovery(workflow).recover(rid, work)
    assert workflow.store.run(rid)["data"]["replans"] == before
    assert workflow.agent.calls == calls
    assert workflow.store.get(rid, "replanning/pending") is None
