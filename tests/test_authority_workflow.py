import copy

import pytest
from authority_review_fixtures import authority_review
from test_decisions import ApprovingAgent
from test_workflow import engine

from auto_agents_2.models import DocumentResult, Finding
from auto_agents_2.review_authority import InvalidAuthorityReview, validate_review
from auto_agents_2.util import digest

BAD_METHOD = "Use a human native dialog as the only accepted proof."
GOOD_METHOD = "Use an available real cancellation observation and preserve state and refresh assertions."


class SourceAgent(ApprovingAgent):
    def __init__(self, invalid_review=False):
        super().__init__()
        self.designs = 0
        self.design_reviews = 0
        self.invalid_review = invalid_review
        self.views = []

    async def invoke(self, stage, context, schema, work, rid, operation, fresh=False):
        self.views.append((stage, copy.deepcopy(context)))
        if stage == "design":
            self.designs += 1
            return DocumentResult(summary="design", document=BAD_METHOD if self.designs == 1 else GOOD_METHOD)
        if context.get("review_kind") == "design":
            self.design_reviews += 1
            if self.invalid_review and self.design_reviews == 1:
                result = authority_review(context, approved=False, summary="Author demanded a new collector")
                result.authority["findings"][0]["criterion_id"] = "author:collector"
                return result
            basis = context["controller_review_basis"]
            if self.designs == 1 and not self.invalid_review:
                result = authority_review(context, approved=False, summary="The chosen proof is not a user requirement",
                    findings=[Finding(severity="major", location="design", description="Method source is misattributed",
                                      evidence="candidate and original input", resolution="Return to design")])
                result.authority["findings"][0].update(kind="source_dispute", owner="design",
                    method_source="candidate", method_quote=BAD_METHOD)
                for row in result.authority["coverage"]:
                    if row["criterion_id"] == result.authority["findings"][0]["criterion_id"]:
                        row["status"] = "unverified"
                return result
            result = authority_review(context)
            correction = basis.get("correction")
            if correction:
                result.authority["amendments"] = [{"source": "previous_contract", "old_quote": clause,
                    "new_quote": GOOD_METHOD, "preserve_ids": correction["preserve_ids"],
                    "reason": "Remove the extra method, retain every actual goal"} for clause in correction["clauses"]]
            return result
        return await super().invoke(stage, context, schema, work, rid, operation, fresh)


@pytest.mark.parametrize("kind,key", [("coverage_review", "assessment"), ("research", "research"),
    ("design", "design"), ("plan", "plan"), ("plan_patch", "plan"), ("test_changes", "candidate_changes")])
@pytest.mark.parametrize("invalid", [None, "not a candidate document"])
async def test_missing_owned_candidate_is_rejected_before_provider_call(tmp_path, kind, key, invalid):
    from authority_review_fixtures import seed_original

    from auto_agents_2.models import ReviewResult

    agent = SourceAgent()
    workflow = engine(tmp_path, agent)
    rid = workflow.store.admit("fixture", {})["id"]
    seed_original(workflow, rid)
    stage = "coverage_review" if kind == "coverage_review" else "review"
    context = {"review_kind": kind}
    if invalid is not None:
        context[key] = invalid
    with pytest.raises(InvalidAuthorityReview, match="missing_review_candidate"):
        await workflow._invoke(stage, context, ReviewResult, tmp_path, rid, "missing-owned-candidate", fresh=True)
    assert not agent.views and not agent.calls
    assert workflow.store.get(rid, "authority/accepted/" + kind) is None
    assert not workflow.store.pending(rid)


async def test_test_changes_review_keeps_the_complete_proposal_as_non_owner_evidence(tmp_path):
    from authority_review_fixtures import seed_original

    from auto_agents_2.models import Change, ImplementationResult, ReviewResult
    from auto_agents_2.workspace import tree_hash

    agent = SourceAgent()
    workflow = engine(tmp_path, agent)
    rid = workflow.store.admit("fixture", {})["id"]
    seed_original(workflow, rid)
    proposal = ImplementationResult(summary="Preserve original assertions", changes=[
        Change(path="tests/test_calc.py", content="def test_result(): assert 2 + 3 == 5\n")]).model_dump()
    before = tree_hash(tmp_path)
    result = await workflow._invoke("review", {"review_kind": "test_changes", "candidate_changes": proposal},
                                    ReviewResult, tmp_path, rid, "protected-tests-review", fresh=True)
    receipt = workflow.authority.receipt(rid, result.model_dump())
    assert receipt["reviewed_changes"] == proposal and "owned_candidate" not in receipt
    assert tree_hash(tmp_path) == before and not (tmp_path / "tests/test_calc.py").exists()
    assert workflow.store.get(rid, "authority/accepted/develop") is None


@pytest.mark.asyncio
async def test_source_dispute_returns_to_owner_with_preservation_and_new_permission(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    request = tmp_path / "request.md"
    request.write_text("Add two numbers. Keep all original assertions.")
    agent = SourceAgent()
    workflow = engine(project, agent)
    result = await workflow.start(str(request))
    assert result["status"] == "SUCCEEDED"
    rid = result["id"]
    assert agent.designs == 2 and agent.design_reviews == 2
    assert workflow.store.get(rid, "design")["document"] == GOOD_METHOD
    assert workflow.store.get(rid, "authority/correction")["previous"]["document"] == BAD_METHOD
    assert workflow.store.get(rid, "authority/correction")["completed"]["artifact_hash"] == digest(workflow.store.get(rid, "design"))
    assert workflow.authority.author_scope(rid, "design")["correction"] is None
    assert workflow.store.run(rid)["data"]["replans"] == 1
    second_author = [ctx for stage, ctx in agent.views if stage == "design"][1]
    assert second_author["controller_authority_scope"]["correction"]["clauses"] == [BAD_METHOD]
    history = workflow.store.get(rid, "recovery/gate-history/0")
    assert history["checkpoints"]["candidate/design"]["document"]["document"] == BAD_METHOD
    assert history["pending"]["permit"]
    permit = workflow.store.get(rid, history["pending"]["permit"])
    assert permit["action"]["action"] == "design" and permit["phase"] == "completed"


@pytest.mark.asyncio
async def test_ungrounded_review_corrects_reviewer_without_regenerating_author(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    request = tmp_path / "request.md"
    request.write_text("Add two numbers")
    agent = SourceAgent(invalid_review=True)
    workflow = engine(project, agent)
    result = await workflow.start(str(request))
    assert result["status"] == "SUCCEEDED"
    assert agent.designs == 1 and agent.design_reviews == 2
    assert result["data"].get("replans", 0) == 0
    assert any(e["kind"] == "review.authority_invalid" for e in workflow.store.events(result["id"]))
    assert not any("collector" in str(i["original"]) for i in workflow.store.get(result["id"], "issues", {}).values())


@pytest.mark.asyncio
async def test_nested_preview_comparison_cannot_bypass_authority_validation(tmp_path):
    from auto_agents_2.models import ReviewResult
    from auto_agents_2.preview_evidence import PreviewComparison
    project = tmp_path / "project"
    project.mkdir()
    workflow = engine(project, SourceAgent())
    rid = workflow.store.admit("fixture", {})["id"]
    artifact = workflow.artifacts.put("Preserve original preview operations", "requirements")
    workflow.store.checkpoint(rid, "capture", {"artifact": artifact})
    ctx, schema = workflow.authority.prepare(rid, "review", {"review_kind": "preview_contract",
        "preview_contracts": {"current": {"preview": {"actions": []}}}}, PreviewComparison, project, "comparison")
    assert "authority" in schema.model_fields["review"].annotation.model_fields
    with pytest.raises(InvalidAuthorityReview):
        workflow._validate_stage_response(PreviewComparison(review=ReviewResult(approved=True, summary="ungrounded")),
                                         "review", ctx, rid)


@pytest.mark.asyncio
async def test_actual_author_and_reviewer_projection_keep_source_protocol_without_granting_facts(tmp_path):
    from auto_agents_2.context_plan import ContextPlanner
    from auto_agents_2.evidence_refs import controller_context_catalog
    project = tmp_path / "project"
    project.mkdir()
    workflow = engine(project, SourceAgent())
    rid = workflow.store.admit("fixture", {})["id"]
    workflow.store.checkpoint(rid, "capture", {"artifact": workflow.artifacts.put("Original input", "requirements")})
    from auto_agents_2.models import ReviewResult
    ctx, _ = workflow.authority.prepare(rid, "review", {"review_kind": "design", "design": {"document": "local only"}},
                                        ReviewResult, project, "design/review")
    projected = ContextPlanner(workflow.artifacts).prepare("review", ctx, project, "design/review", run_id=rid)
    assert projected.payload["controller_review_basis"] == ctx["controller_review_basis"]
    catalog = controller_context_catalog(ctx)
    entries = [v for v in catalog.entries.values() if v.path == "controller_review_basis"]
    assert entries and all(v.origin == "controller_protocol" and v.status == "protocol" for v in entries)
    review = authority_review(ctx)
    workflow.authority.record(rid, review, ctx, "design/review")
    assert workflow.authority.reusable(rid, review.model_dump())
    workflow.store.checkpoint(rid, "clarify", {"document": "changed"})
    assert not workflow.authority.reusable(rid, review.model_dump())
    assert validate_review(review, ctx["controller_review_basis"]).basis_hash == ctx["controller_review_basis"]["hash"]
    assert digest(workflow.store.get(rid, "clarify")) != ctx["controller_review_basis"]["source_identity"]["clarify"]


@pytest.mark.asyncio
async def test_pending_delivery_reassesses_legacy_verdict_before_completion(tmp_path, monkeypatch):
    import asyncio

    from auto_agents_2.workspace import git
    project = tmp_path / "project"
    project.mkdir()
    request = tmp_path / "request.md"
    request.write_text("Add two numbers")
    workflow = engine(project, SourceAgent(invalid_review=True))
    def cut(*args):
        raise asyncio.CancelledError()
    with monkeypatch.context() as patch:
        patch.setattr(workflow.store, "complete_delivery", cut)
        with pytest.raises(asyncio.CancelledError):
            await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    journal = workflow.store.get(rid, "delivery/journal")
    assert journal["phase"] == "completion_pending"
    before = git(project, "rev-parse", "HEAD")
    rounds = workflow.store.get(rid, "rounds/T1")
    raw = workflow.store.get(rid, "verify/review")
    raw.pop("authority")  # Reproduce a previously accepted legacy verdict.
    workflow.store.checkpoint(rid, "verify/review", raw)
    calls = len(workflow.agent.views)
    result = await workflow.start(run_id=rid)
    assert result["status"] == "SUCCEEDED"
    assert git(project, "rev-parse", "HEAD") == before
    assert workflow.store.get(rid, "rounds/T1") == rounds
    assert len([s for s, _ in workflow.agent.views[calls:] if s == "verify"]) == 1
    assert workflow.store.get(rid, "delivery/authority-review")["review"]["approved"]


@pytest.mark.parametrize("action,allowed", [("repair", False), ("diagnose", True), ("recheck", True)])
def test_reserved_recovery_cannot_turn_an_old_unfounded_verdict_into_write_permission(tmp_path, action, allowed):
    from test_recovery_contracts import failure

    from auto_agents_2.faults import StateConflict
    from auto_agents_2.recovery_policy import ActionPermit
    project = tmp_path / "project"
    project.mkdir()
    workflow = engine(project, SourceAgent())
    rid = workflow.store.admit("fixture", {})["id"]
    record = failure(evidence={"review": {"approved": False, "summary": "Author wants extra telemetry", "findings": []}})
    permit = ActionPermit(cycle_id="cycle", owner="T3", action={"action": action, "failure_id": record.id},
                          actor="human", source_hash="source", plan_hash="plan", authority_failure=record.model_dump())
    workflow.store.checkpoints(rid, {"authority/protocol": 1, "permit": permit.model_dump()})
    if allowed:
        workflow.recovery_policy.start(rid, "permit")
        assert workflow.store.get(rid, "permit")["phase"] == "started"
    else:
        with pytest.raises(StateConflict, match="reassessment"):
            workflow.recovery_policy.start(rid, "permit")
        assert workflow.store.get(rid, "permit")["phase"] == "reserved"


@pytest.mark.asyncio
async def test_repeated_ungrounded_verdict_stops_after_one_correction_without_author_rewrite(tmp_path):
    from auto_agents_2.errors import RecoveryPathExhausted
    from auto_agents_2.models import ReviewResult
    class InvalidAgent(SourceAgent):
        reviews = 0
        async def invoke(self, stage, context, schema, *args, **kwargs):
            if context.get("review_kind") == "design":
                self.reviews += 1
                return ReviewResult(approved=False, summary="Invented requirement without grounding")
            return await super().invoke(stage, context, schema, *args, **kwargs)
    project = tmp_path / "project"
    project.mkdir()
    agent = InvalidAgent()
    workflow = engine(project, agent)
    rid = workflow.store.admit("fixture", {})["id"]
    workflow.store.checkpoint(rid, "capture", {"artifact": workflow.artifacts.put("Preserve data", "requirements")})
    with pytest.raises(RecoveryPathExhausted):
        await workflow._invoke("review", {"review_kind": "design", "design": {"document": "Local app"}},
                               ReviewResult, project, rid, "design/review")
    assert agent.reviews == 2 and agent.designs == 0
    assert len([e for e in workflow.store.events(rid) if e["kind"] == "review.authority_invalid"]) == 2
    assert not workflow.store.get(rid, "authority/accepted/design")


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["reintegrating", "repairing_runtime"])
async def test_delivery_work_in_progress_resumes_before_stable_candidate_authority_check(tmp_path, monkeypatch, phase):
    import asyncio
    from pathlib import Path

    from test_delivery import commit

    from auto_agents_2.delivery import RuntimeVerificationError
    from auto_agents_2.models import Change, ImplementationResult
    from auto_agents_2.workspace import git, tree_hash
    project = tmp_path / 'project'
    project.mkdir()
    request = tmp_path / 'request.md'
    request.write_text('Add two numbers')
    class Agent(ApprovingAgent):
        advanced = False
        async def invoke(self, stage, context, schema, work, rid, operation, fresh=False):
            if stage == 'implement' and operation.startswith('delivery-repair/'):
                self.calls.append((stage, fresh))
                return ImplementationResult(summary='Repair retained runtime candidate', changes=[
                    Change(path='calc.py', content='# Runtime repair\ndef add(a, b):\n    return a + b\n')])
            result = await super().invoke(stage, context, schema, work, rid, operation, fresh)
            if phase == 'reintegrating' and operation == 'final-review' and not self.advanced:
                self.advanced = True
                (project / 'user.txt').write_text('Retain this concurrent user change')
                commit(project)
            return result
    agent = Agent()
    workflow = engine(project, agent)
    (project / 'initial.txt').write_text('baseline')
    commit(project)
    runtime = workflow.delivery.runtime
    runtime_calls = 0
    async def first_runtime_failure(*args):
        nonlocal runtime_calls
        result = await runtime(*args)
        runtime_calls += 1
        if phase == 'repairing_runtime' and runtime_calls == 1:
            row = copy.deepcopy(result['checks'][0])
            row.update(exit_code=1, process_exit_code=1, passed=False, failure_reasons=['Injected runtime failure'])
            raise RuntimeVerificationError({'checks': [row]})
        return result
    workflow.delivery.runtime = first_runtime_failure
    environment = workflow._environment
    async def interrupt_after_owned_write(work, rid, *args, **kwargs):
        journal = workflow.store.get(rid, 'delivery/journal', {})
        if journal.get('phase') == phase and git(work, 'rev-parse', 'HEAD') != journal['candidate']:
            raise asyncio.CancelledError()
        return await environment(work, rid, *args, **kwargs)
    with monkeypatch.context() as stopped:
        stopped.setattr(workflow, '_environment', interrupt_after_owned_write)
        with pytest.raises(asyncio.CancelledError):
            await workflow.start(str(request))
    rid = workflow.store.current()['id']
    journal = workflow.store.get(rid, 'delivery/journal')
    assert journal['phase'] == phase
    assert tree_hash(Path(journal['work'])) != journal['source_hash']
    calls = len(agent.calls)
    counters = workflow.store.get(rid, 'rounds/T1')
    result = await workflow.start(run_id=rid)
    assert result['status'] == 'SUCCEEDED'
    assert workflow.store.get(rid, 'rounds/T1') == counters
    assert not any(stage == 'implement' for stage, _ in agent.calls[calls:])
    assert any(stage == 'verify' for stage, _ in agent.calls[calls:])
    if phase == 'reintegrating':
        assert (project / 'user.txt').read_text() == 'Retain this concurrent user change'
    else:
        assert workflow.store.get(rid, 'rounds/delivery')['round'] == 1
