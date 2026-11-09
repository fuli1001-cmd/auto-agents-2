"""Explicit independent source operations: synthetic state, fake HTTP, real owner gates.

No test contacts the network or demonstrates a native provider no-tools capability.
"""
import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.cli import app
from auto_agents_2.config import ConfigStore
from auto_agents_2.errors import AA2Error, ResearchDestinationRejected, ResearchSourceExcluded
from auto_agents_2.faults import StateConflict, known_exception
from auto_agents_2.models import DocumentResult, ReviewResult, SourceEvidence
from auto_agents_2.providers import ProviderRouter
from auto_agents_2.research import ResearchService
from auto_agents_2.research_acquisition import (
    CURRENT as ACQUISITION,
)
from auto_agents_2.research_acquisition import (
    HARD,
    PREFIX,
    ResearchAcquisitionRecovery,
    ResearchRecoveryDocument,
    claim_rows,
    failure_observation,
)
from auto_agents_2.research_replacement import (
    CURRENT,
    HISTORY,
    current,
    guard_network,
    inspect,
    prepare,
    snapshot,
    submit,
)
from auto_agents_2.state_codec import put_wire_db
from auto_agents_2.store import Store
from auto_agents_2.util import digest

OLD = "https://old.example.test/spec"
NEW = "https://independent.example.test/reference"


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)


def source_case(project, *, workflow=None, shared_hold=False):
    ConfigStore(project).initialize()
    store = workflow.store if workflow else Store(project)
    artifacts = workflow.artifacts if workflow else ArtifactStore(store.root)
    rid = store.admit("Replacement fixture", {})["id"]
    capture = artifacts.put("Preserve the original requirements and every acceptance item.", "original-input")
    clarify = {"summary": "Original", "document": "Complete requirements", "frontend": True,
               "requirements": [{"id": "R1", "description": "Fact needed", "acceptance": ["Same semantics", "Same version"]},
                                {"id": "R2", "description": "Layout", "acceptance": ["Preserve prototype approval"]}]}
    original = DocumentResult(summary="Original proposal", document="Two separate original factual claims.", sources=[
        SourceEvidence(url=OLD + "#first", claim="First original fact", version="1"),
        SourceEvidence(url=OLD + "#second", claim="Second original fact", version="2")]).model_dump()
    store.checkpoints(rid, {"capture": {"artifact": capture, "hash": capture["hash"]}, "clarify": clarify,
                           "candidate/provider_research": {"document": original, "original_invocation": "EXAMPLE"},
                           "prerequisite/fixture-source": 4, "rounds/fixture": {"round": 7},
                           "infra/fixture": {"failures": 5, "grant": 2}})
    failure = failure_observation(ResearchDestinationRejected("old.example.test"))
    if shared_hold:
        failure = {"code": "RESEARCH_RESOLVER_UNAVAILABLE", "classification": "unknown", "automatic": False,
                   "diagnostic": {"operation": "research_resolver", "connection_context_uncertain": True}}
    episode = {"version": 1, "id": digest("fixture-episode"), "original_candidate": artifacts.put(original),
               "binding": {name: digest(store.get(rid, name)) for name in ("capture", "clarify")},
               "claims": claim_rows(original), "attempts": [{"id": "EXAMPLE_LEGACY_ATTEMPT",
                   "sources": original["sources"], "observations": [{"url": OLD, "status": "failed", "failure": failure}]}]}
    fault = {"route": "human", "phase": "provider_research", "reason_code": failure["code"],
             "human_requirement": {"kind": "external_input", "reason": "Original refusal", "action": "Preserve it",
                                   "resume_condition": "Original boundary remains", "evidence": failure["diagnostic"]}}
    store.checkpoints(rid, {PREFIX + episode["id"]: episode,
                           ACQUISITION: {"id": episode["id"], "key": PREFIX + episode["id"]},
                           "recovery/current-fault": fault})
    question = store.request(rid, "prerequisite/fixture-source/4", "Original unanswered refusal", digest(fault))
    store.update(rid, status="WAITING_USER", stage="provider_research", replans=3)
    recovery = ResearchAcquisitionRecovery(workflow or SimpleNamespace(store=store, artifacts=artifacts))
    claims = [{"claim_id": row["id"], "disposition": "supported", "source_indexes": [0],
               "requirement_ids": ["R1"], "reason": "Independent reference must support the exact original fact and version"}
              for row in episode["claims"]]
    proposal = ResearchRecoveryDocument(summary="Independent replacement", document="Full replacement preserving both facts and scope.",
        sources=[SourceEvidence(url=NEW, claim="Both original facts", version="1,2")],
        acquisition_resolution={"episode_id": episode["id"], "status": "ready", "claims": claims,
                                "missing_information": []}).model_dump()
    value = {"proposal": proposal, "requirement_assessment": [
        {"requirement_id": "R1", "claim_ids": [row["id"] for row in episode["claims"]], "reason": "Facts need equivalent independent evidence"},
        {"requirement_id": "R2", "claim_ids": [], "reason": "Layout needs product tests and human prototype approval, not a document claim"}]}
    return SimpleNamespace(project=project, store=store, rid=rid, artifacts=artifacts, original=original,
                           episode=episode, question=question, recovery=recovery, value=value)


def state(case):
    with case.store.connect() as db:
        _, _, basis = snapshot(db, case.rid)
        return basis, [tuple(row) for row in db.execute("SELECT * FROM events ORDER BY seq")]


def request_file(case):
    proposal = case.project / "EXAMPLE-proposal.json"
    proposal.write_text(json.dumps(case.value))
    request = prepare(case.project, run_id=case.rid, proposal=proposal, authorization_reference="EXAMPLE_EXPLICIT_USER_APPROVAL")
    path = case.project / "EXAMPLE-request.json"
    path.write_text(json.dumps(request))
    return request, path, digest(path.read_bytes())


def register(case):
    request, path, sha = request_file(case)
    result = submit(case.project, path, sha, confirm_new_sources=True)
    return request, result


def test_prepare_is_read_only_and_never_calls_old_prepare(tmp_path, monkeypatch):
    case = source_case(tmp_path)
    before = state(case)
    monkeypatch.setattr(ResearchAcquisitionRecovery, "prepare", lambda *a: pytest.fail("Old prepare replayed"))
    request, _, _ = request_file(case)
    assert state(case) == before
    assert request["excluded_hosts"] == ["old.example.test"] and not request["network_holds"]
    assert len(request["requirement_assessment"]) == 2
    assert request["generation"] == 3


def test_registration_preserves_all_old_evidence_questions_and_counters(tmp_path):
    case = source_case(tmp_path)
    old, _ = state(case)
    with pytest.raises(StateConflict, match="Historical research refusal"):
        case.recovery.prepare(case.rid)  # Synthetic state only, never the real stopped project.
    request, result = register(case)
    record = current(case.store, case.rid)
    archive = case.store.get(case.rid, HISTORY + result["request_hash"])
    assert archive["original_candidate"]["document"] == case.original
    assert archive["original_episode"] == case.episode
    assert archive["original_questions"][0]["id"] == case.question["id"]
    assert archive["original_questions"][0]["answer"] is None
    assert archive["original_fault"] == case.store.get(case.rid, "recovery/current-fault")
    assert record["phase"] == "pending_review" and record["replacement_count"] == 1
    assert case.store.get(case.rid, "candidate/provider_research")["document"] == request["proposal"]
    after, _ = state(case)
    for key, sha in old["checkpoints"].items():
        if key != "candidate/provider_research":
            assert after["checkpoints"][key] == sha
    assert after["questions"] == old["questions"]
    assert case.store.run(case.rid)["data"]["replans"] == 3
    assert case.store.run(case.rid)["status"] == "WAITING_USER"
    assert case.store.get(case.rid, "provider_research") is None
    assert case.store.get(case.rid, "prototype_approval") is None
    assert case.recovery.prepare(case.rid) is None


@pytest.mark.parametrize("confirmed,hash_ok", [(False, True), (True, False)])
def test_exact_request_confirmation_and_hash_required(tmp_path, confirmed, hash_ok):
    case = source_case(tmp_path)
    _, path, sha = request_file(case)
    before = state(case)
    with pytest.raises(AA2Error):
        submit(case.project, path, sha if hash_ok else "0" * 64, confirm_new_sources=confirmed)
    assert state(case) == before


def test_duplicate_exact_submission_is_idempotent(tmp_path):
    case = source_case(tmp_path)
    request, path, sha = request_file(case)
    first = submit(case.project, path, sha, confirm_new_sources=True)
    before = state(case)
    replay = submit(case.project, path, sha, confirm_new_sources=True)
    assert replay == {**first, "replayed": True}
    assert replay["request_hash"] == digest(request) and state(case) == before


@pytest.mark.parametrize("change", ["candidate", "capture", "clarify", "generation", "question", "counter", "episode", "hard", "audit", "config"])
def test_preparation_drift_cannot_register(tmp_path, change):
    case = source_case(tmp_path)
    _, path, sha = request_file(case)
    if change in {"candidate", "capture", "clarify"}:
        case.store.checkpoint(case.rid, "candidate/provider_research" if change == "candidate" else change, {"changed": True})
    elif change == "generation":
        case.store.update(case.rid, replans=4)
    elif change == "question":
        case.store.answer(case.question["id"], "EXAMPLE", case.question["artifact_hash"])
    elif change == "counter":
        case.store.checkpoint(case.rid, "prerequisite/fixture-source", 5)
    elif change == "episode":
        case.store.checkpoint(case.rid, PREFIX + case.episode["id"], {**case.episode, "attempts": []})
    elif change == "hard":
        case.store.checkpoint(case.rid, HARD, {"kind": "EXAMPLE_RECORDING_HOLD"})
    elif change == "audit":
        case.store.event(case.rid, "research.acquisition_recording_failed", {"fixture": True})
    else:
        path_config = ConfigStore(case.project).path
        value = json.loads(path_config.read_text())
        value["execution"]["infra_retries"] += 1
        path_config.write_text(json.dumps(value))
    before = state(case)
    with pytest.raises(AA2Error):
        submit(case.project, path, sha, confirm_new_sources=True)
    assert state(case) == before and case.store.get(case.rid, CURRENT) is None


def test_atomic_registration_rolls_back_archive_candidate_and_event(tmp_path, monkeypatch):
    case = source_case(tmp_path)
    _, path, sha = request_file(case)
    before = state(case)
    original = Store._event
    def event(self, db, rid, kind, data):
        if kind == "research.sources_replaced":
            raise RuntimeError("EXAMPLE_COMMIT_INTERRUPTED")
        return original(self, db, rid, kind, data)
    monkeypatch.setattr(Store, "_event", event)
    with pytest.raises(RuntimeError, match="EXAMPLE_COMMIT_INTERRUPTED"):
        submit(case.project, path, sha, confirm_new_sources=True)
    assert state(case) == before and case.store.get(case.rid, CURRENT) is None


@pytest.mark.parametrize("change", ["candidate", "capture", "clarify", "generation", "question", "episode", "hard", "code", "registration"])
def test_registered_operation_rejects_input_or_evidence_drift(tmp_path, monkeypatch, change):
    case = source_case(tmp_path)
    register(case)
    if change in {"candidate", "capture", "clarify"}:
        case.store.checkpoint(case.rid, "candidate/provider_research" if change == "candidate" else change, {"changed": True})
    elif change == "generation":
        case.store.update(case.rid, replans=4)
    elif change == "question":
        case.store.supersede_questions(case.rid, case.question["key"], include_answered=True)
    elif change == "episode":
        changed = copy.deepcopy(case.episode)
        changed["attempts"].append({"id": "EXAMPLE_NEW_FAILURE"})
        case.store.checkpoint(case.rid, PREFIX + case.episode["id"], changed)
    elif change == "hard":
        case.store.checkpoint(case.rid, HARD, {"kind": "EXAMPLE_RECORDING_HOLD"})
    elif change == "code":
        monkeypatch.setattr("auto_agents_2.research_replacement.code_identity", lambda: "EXAMPLE_DIFFERENT_BUILD")
    else:
        record = case.store.get(case.rid, CURRENT)
        # Simulated on-disk corruption, not an authorized state transition.
        with case.store.connect() as db:
            put_wire_db(db, case.rid, CURRENT, json.dumps({**record, "original_attempt_hashes": []}), replace=True)
    with pytest.raises(StateConflict):
        current(case.store, case.rid)


@pytest.mark.parametrize("invalid", ["lost_requirement", "duplicate_requirement", "wrong_claim_mapping", "blank_reason", "lost_claim", "duplicate_claim", "invent_requirement", "invent_source", "verified_sources", "files", "questions", "blocked"])
def test_complete_claim_and_requirement_mapping_cannot_be_silently_weakened(tmp_path, invalid):
    case = source_case(tmp_path)
    value = copy.deepcopy(case.value)
    proposal = value["proposal"]
    rows = proposal["acquisition_resolution"]["claims"]
    if invalid == "lost_requirement":
        value["requirement_assessment"].pop()
    elif invalid == "duplicate_requirement":
        value["requirement_assessment"].append(value["requirement_assessment"][0])
    elif invalid == "wrong_claim_mapping":
        value["requirement_assessment"][0]["claim_ids"] = []
    elif invalid == "blank_reason":
        value["requirement_assessment"][1]["reason"] = " "
    elif invalid == "lost_claim":
        rows.pop()
    elif invalid == "duplicate_claim":
        rows.append(copy.deepcopy(rows[0]))
    elif invalid == "invent_requirement":
        rows[0]["requirement_ids"] = ["EXAMPLE_UNKNOWN_REQUIREMENT"]
    elif invalid == "invent_source":
        rows[0]["source_indexes"] = [100]
    elif invalid == "verified_sources":
        proposal["verified_sources"] = []
    elif invalid == "files":
        proposal["files"] = [{"path": "EXAMPLE.md", "content": "Not applied"}]
    elif invalid == "questions":
        proposal["questions"] = ["Unresolved original claim?"]
    else:
        proposal["acquisition_resolution"]["status"] = "blocked"
    before = state(case)
    with pytest.raises(AA2Error):
        inspect(case.project, case.rid, value, "EXAMPLE_APPROVAL")
    assert state(case) == before


@pytest.mark.parametrize("url", [OLD, OLD + "?new=yes", OLD + "#changed", "http://OLD.EXAMPLE.TEST.:8080/other", "https://sub.old.example.test/ref", "https://old.example.test。/ref", "https://old%2eexample.test/ref", "https://93.184.216.34/ref"])
async def test_excluded_source_is_checked_without_blocking_independent_safe_capture(tmp_path, monkeypatch, url):
    case = source_case(tmp_path)
    register(case)
    calls = []
    async def capture(self, url):
        calls.append(url)
        assert url == NEW, "An excluded target must never reach capture"
        return {"artifact": self.artifacts.put("Independent fixture facts", "research"), "excerpt": "Independent fixture facts",
                "source": url, "final_url": url}
    monkeypatch.setattr(ResearchService, "capture", capture)
    service = ResearchService(case.artifacts, excluded_hosts=guard_network(case.store, case.rid))
    with pytest.raises(AA2Error):
        await service.verify([SourceEvidence(url=NEW, claim="Safe first"), SourceEvidence(url=url, claim="Excluded")])
    assert calls == [NEW]


async def test_redirect_to_excluded_source_stops_before_second_request(tmp_path, monkeypatch):
    from auto_agents_2 import research
    case = source_case(tmp_path)
    register(case)
    calls = []
    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": OLD})
    monkeypatch.setattr(research, "PublicHTTPTransport", lambda **kwargs: httpx.MockTransport(respond))
    service = ResearchService(case.artifacts, excluded_hosts=guard_network(case.store, case.rid))
    with pytest.raises(ResearchSourceExcluded) as observed:
        await service.capture(NEW)
    assert calls == [NEW]
    assert observed.value.evidence["operation"] == "research_source_replacement"
    assert "resolution" not in observed.value.evidence
    assert known_exception(observed.value, "provider_research").requirement.evidence == observed.value.evidence


async def test_independent_new_source_still_uses_normal_controller_capture(tmp_path, monkeypatch):
    from auto_agents_2 import research
    case = source_case(tmp_path)
    register(case)
    calls = []
    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(200, text="Independent original facts", headers={"content-type": "text/plain"})
    async def collect(root, request, **kwargs):
        assert root == case.store.root and request["source"] == NEW
        return {"artifact": case.artifacts.put("Independent original facts", "research"),
                "excerpt": "Independent original facts", "url": NEW, "final_url": request["final_url"]}
    monkeypatch.setattr(research, "PublicHTTPTransport", lambda **kwargs: httpx.MockTransport(respond))
    monkeypatch.setattr("auto_agents_2.collection.run_collection", collect)
    evidence = await ResearchService(case.artifacts, excluded_hosts=guard_network(case.store, case.rid)).verify(
        [SourceEvidence.model_validate(row) for row in case.value["proposal"]["sources"]])
    assert calls == [NEW]
    assert evidence[0]["supported_version"] is None and "require review" in evidence[0]["verification_level"]
    assert case.artifacts.read(evidence[0]["retrieval"]["artifact"]) == "Independent original facts"
    assert case.store.get(case.rid, "provider_research") is None


def test_shared_unknown_prerequisite_is_not_cleared_by_new_source(tmp_path):
    case = source_case(tmp_path, shared_hold=True)
    request, _ = register(case)
    assert request["network_holds"]
    with pytest.raises(StateConflict, match="shared resolver/proxy prerequisite"):
        guard_network(case.store, case.rid)
    assert case.store.get(case.rid, PREFIX + case.episode["id"]) == case.episode
    assert case.store.pending(case.rid)[0]["id"] == case.question["id"]


async def test_native_provider_is_blocked_before_transport_or_configuration(tmp_path, monkeypatch):
    case = source_case(tmp_path)
    register(case)
    router = ProviderRouter(None, case.store, None)
    async def invoke(*args, **kwargs):
        pytest.fail("Native CLI/Web/MCP capability has not been proved")
    monkeypatch.setattr(router, "_invoke_candidates", invoke)
    with pytest.raises(StateConflict, match="no provider call was made"):
        await router.invoke("review", {}, ReviewResult, case.project, case.rid, "EXAMPLE_NEW_REVIEW")


def test_cli_readonly_request_and_confirmation_are_separate(tmp_path):
    case = source_case(tmp_path)
    proposal = case.project / "EXAMPLE-cli-proposal.json"
    proposal.write_text(json.dumps(case.value))
    runner = CliRunner()
    before = state(case)
    result = runner.invoke(app, ["prepare-research-source-replacement", "--project", str(case.project),
        "--run-id", case.rid, "--proposal", str(proposal), "--authorization-reference", "EXAMPLE_APPROVAL"])
    assert result.exit_code == 0, result.output
    request = json.loads(result.output)
    assert state(case) == before
    path = case.project / "EXAMPLE-cli-request.json"
    path.write_text(json.dumps(request))
    result = runner.invoke(app, ["replace-research-sources", "--project", str(case.project), "--request", str(path),
                                 "--expected-sha256", digest(path.read_bytes())])
    assert result.exit_code == 2 and state(case) == before


async def test_synthetic_independent_review_requires_every_original_requirement_and_owned_receipt(tmp_path, monkeypatch):
    from authority_review_fixtures import authority_review
    from test_decisions import ApprovingAgent
    from test_workflow import engine

    class EvidenceOnlyFixture(ApprovingAgent):
        # A deliberately fake no-tool reviewer, not evidence about a native CLI.
        async def invoke(self, stage, context, schema, work, rid, operation, fresh=False):
            assert stage == "review" and context["review_kind"] == "research" and fresh
            replacement = context["controller_research_acquisition"]["source_replacement"]
            assert len(replacement["requirement_assessment"]) == 2
            criteria = [row for row in context["controller_review_basis"]["criteria"]
                        if row["id"].startswith("stage:research-replacement-requirement:")]
            assert len(criteria) == 2 and "Preserve prototype approval" in criteria[1]["statement"]
            return authority_review(context)

    workflow = engine(tmp_path, EvidenceOnlyFixture())
    case = source_case(tmp_path, workflow=workflow)
    request, _ = register(case)
    doc = request["proposal"]
    with pytest.raises(StateConflict, match="controller-acquired"):
        await workflow._review_research(case.rid, tmp_path, doc, [])
    with pytest.raises(StateConflict, match="authority receipt"):
        case.recovery.acceptance(case.rid, doc, ReviewResult(approved=True, summary="Unowned", findings=[]))
    artifact = case.artifacts.put("Independently collected fixture text for both facts", "research")
    retrieved = [{**doc["sources"][0], "retrieval": {"artifact": artifact, "url": NEW, "final_url": NEW},
                  "excerpt": "Independently collected fixture text for both facts"}]
    review = await workflow._review_research(case.rid, tmp_path, doc, retrieved)
    updates = case.recovery.acceptance(case.rid, doc, review, retrieved)
    assert case.store.get(case.rid, CURRENT)["phase"] == "pending_review"
    with pytest.raises(StateConflict, match="atomic transition"):
        case.store.checkpoints(case.rid, updates)
    accepted = {**doc, "verified_sources": [{k: v for k, v in row.items() if k != "excerpt"} for row in retrieved]}
    case.store.checkpoints(case.rid, {"provider_research": accepted, **updates})
    assert current(case.store, case.rid)["phase"] == "accepted"
    case.store.checkpoint(case.rid, "provider_research", accepted)  # Exact accepted replay remains valid.
    assert case.store.run(case.rid)["data"]["replans"] == 3
    assert case.store.get(case.rid, "prototype_approval") is None
    assert case.store.get(case.rid, PREFIX + case.episode["id"])["attempts"] == case.episode["attempts"]
    # Even accepted synthetic evidence cannot grant a real provider new access to old hosts.
    router = ProviderRouter(None, case.store, None)
    with pytest.raises(StateConflict, match="downstream"):
        await router.invoke("design", {}, DocumentResult, tmp_path, case.rid, "EXAMPLE_DOWNSTREAM")
    case.store.checkpoint(case.rid, "provider_research", {**doc, "verified_sources": []})
    with pytest.raises(StateConflict, match="collected research"):
        current(case.store, case.rid)


@pytest.mark.parametrize("phase,blocked", [(None, True), ("unknown", True), ("public_connect", True), ("origin_tls", False)])
@pytest.mark.parametrize("via_fault", [False, True])
def test_missing_connection_phase_preserves_existing_shared_route_rule(tmp_path, phase, blocked, via_fault):
    case = source_case(tmp_path)
    diagnostic = {"operation": "research_document", "category": "network", "error_type": "ConnectError"}
    if phase is not None:
        diagnostic["connection_phase"] = phase
    failure = {"code": "DOCUMENT_ACQUISITION_UNAVAILABLE", "automatic": False,
               "classification": "unknown", "diagnostic": diagnostic}
    if via_fault:
        fault = case.store.get(case.rid, "recovery/current-fault")
        fault["human_requirement"]["evidence"] = diagnostic
        fault["reason_code"] = failure["code"]
        case.store.checkpoint(case.rid, "recovery/current-fault", fault)
    else:
        episode = copy.deepcopy(case.episode)
        episode["attempts"][0]["observations"][0]["failure"] = failure
        case.store.checkpoint(case.rid, PREFIX + episode["id"], episode)
    request, _ = register(case)
    assert bool(request["network_holds"]) == blocked
    if blocked:
        with pytest.raises(StateConflict, match="shared resolver/proxy"):
            guard_network(case.store, case.rid)
    else:
        assert guard_network(case.store, case.rid) == ["old.example.test"]


@pytest.mark.parametrize("location", ["failure", "observed_failures", "fault", "retrieved"])
def test_original_redirect_destination_cannot_be_reintroduced_directly(tmp_path, location):
    case = source_case(tmp_path)
    refused = "https://redirect-refusal.example.test/final"
    diagnostic = {"operation": "research_document", "category": "http_status", "http_status": 403,
                  "source_origin": "https://redirect-refusal.example.test"}
    failure = {"code": "DOCUMENT_ACQUISITION_UNAVAILABLE", "classification": "permission", "automatic": False,
               "diagnostic": diagnostic}
    if location == "fault":
        fault = case.store.get(case.rid, "recovery/current-fault")
        fault["human_requirement"]["evidence"] = diagnostic
        case.store.checkpoint(case.rid, "recovery/current-fault", fault)
    else:
        episode = copy.deepcopy(case.episode)
        row = episode["attempts"][0]["observations"][0]
        if location == "retrieved":
            episode["attempts"][0]["observations"].append(
                {"url": OLD + "/other", "status": "retrieved", "retrieval": {"final_url": refused}})
        elif location == "failure":
            row["failure"] = failure
        else:
            row["observed_failures"] = [failure]
        case.store.checkpoint(case.rid, PREFIX + episode["id"], episode)
    request, _ = register(case)
    assert "redirect-refusal.example.test" in request["excluded_hosts"]
    case.value["proposal"]["sources"][0]["url"] = refused
    with pytest.raises(ResearchSourceExcluded):
        inspect(case.project, case.rid, case.value, "EXAMPLE_APPROVAL")


@pytest.mark.parametrize("field", ["original_candidate", "original_episode", "original_fault", "original_wait", "original_questions"])
def test_archived_original_bodies_must_match_frozen_basis(tmp_path, field):
    case = source_case(tmp_path)
    _, result = register(case)
    key = HISTORY + result["request_hash"]
    archive = case.store.get(case.rid, key)
    if field == "original_wait":
        # This fixture has no modern wait; inventing one also changes the archive.
        archive[field] = {"EXAMPLE": "invented"}
    else:
        archive.pop(field)
    case.store.checkpoint(case.rid, key, archive)
    with pytest.raises(StateConflict, match="original archive"):
        current(case.store, case.rid)


@pytest.mark.parametrize("counter,value", [("prerequisite/fixture-source", 3), ("rounds/fixture", {"round": 6}),
                                          ("infra/fixture", {"failures": 4, "grant": 2})])
def test_original_cumulative_consumption_cannot_decrease_or_disappear(tmp_path, counter, value):
    case = source_case(tmp_path)
    register(case)
    # Explicit simulated disk corruption bypasses Store's existing monotonic round guard.
    with case.store.connect() as db:
        put_wire_db(db, case.rid, counter, json.dumps(value), replace=True)
    with pytest.raises(StateConflict):
        current(case.store, case.rid)


def test_cumulative_floor_allows_increases_and_does_not_freeze_progress_or_grants(tmp_path):
    case = source_case(tmp_path)
    register(case)
    case.store.checkpoints(case.rid, {"prerequisite/fixture-source": 5,
        "rounds/fixture": {"round": 8, "stalled": 0},
        "infra/fixture": {"failures": 6, "grant": 0, "pending": False, "error_input": "EXAMPLE_CHANGED"}})
    assert current(case.store, case.rid)["phase"] == "pending_review"


@pytest.mark.parametrize("value", [[], {}, {"version": "research-source-replacement-v1"}])
def test_malformed_request_cannot_write_state_or_start_work(tmp_path, value):
    case = source_case(tmp_path)
    path = tmp_path / "EXAMPLE-malformed.json"
    path.write_text(json.dumps(value))
    before = state(case)
    with pytest.raises(AA2Error):
        submit(case.project, path, digest(path.read_bytes()), confirm_new_sources=True)
    assert state(case) == before


@pytest.mark.parametrize("damage", ["counter", "archive"])
def test_new_registration_cannot_absorb_invalid_previous_baseline(tmp_path, damage):
    case = source_case(tmp_path)
    _, first = register(case)
    if damage == "counter":
        case.store.checkpoint(case.rid, "prerequisite/fixture-source", 0)
    else:
        key = HISTORY + first["request_hash"]
        archive = case.store.get(case.rid, key)
        archive["original_episode"] = {}
        case.store.checkpoint(case.rid, key, archive)
    case.value["proposal"]["sources"][0]["url"] = "https://third-independent.example.test/ref"
    with pytest.raises(StateConflict):
        request_file(case)


def test_second_explicit_registration_keeps_prior_archive_and_counter_floors(tmp_path):
    case = source_case(tmp_path)
    _, first = register(case)
    prior = case.store.get(case.rid, CURRENT)
    original_archive = case.store.get(case.rid, HISTORY + first["request_hash"])
    case.value["proposal"]["sources"][0]["url"] = "https://third-independent.example.test/ref"
    request, second = register(case)
    record = current(case.store, case.rid)
    archive = case.store.get(case.rid, HISTORY + second["request_hash"])
    assert archive["previous_registration"] == prior and record["replacement_count"] == 2
    assert case.store.get(case.rid, HISTORY + first["request_hash"]) == original_archive
    assert request["counter_floor"] == prior["request"]["counter_floor"]
    assert request["excluded_hosts"] == ["independent.example.test", "old.example.test"]
    case.store.checkpoint(case.rid, HISTORY + first["request_hash"], {})
    with pytest.raises(StateConflict):
        current(case.store, case.rid)



def test_pending_replacement_cannot_publish_stage_without_atomic_receipt(tmp_path):
    case = source_case(tmp_path)
    request, _ = register(case)
    before = state(case)
    with pytest.raises(StateConflict, match="atomic acceptance receipt"):
        case.store.checkpoint(case.rid, "provider_research", request["proposal"])
    assert state(case) == before
    assert case.store.get(case.rid, "provider_research") is None
    # On-disk corruption must also be caught when reading the pending operation.
    with case.store.connect() as db:
        put_wire_db(db, case.rid, "provider_research", json.dumps(request["proposal"]))
    with pytest.raises(StateConflict, match="inputs changed"):
        current(case.store, case.rid)
