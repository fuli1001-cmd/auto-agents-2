"""UI intent, provenance, recovery and visual-review authority boundaries."""
import copy
import socket
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from prototype_review_fixtures import prototype_verdict
from test_workflow import CodingAgent, engine
from visual_design_fixtures import URL, acquired_source, visual_brief

from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.engine import Replan
from auto_agents_2.errors import InvalidStructuredResponse
from auto_agents_2.faults import StateConflict
from auto_agents_2.models import Change, DocumentResult, IntakeResult
from auto_agents_2.prototype_contract import source_context
from auto_agents_2.prototype_review import (
    InvalidPrototypeReview,
    PrototypeFinding,
    PrototypeReview,
    review_basis,
    review_current,
    validate_review,
)
from auto_agents_2.util import digest
from auto_agents_2.visual_design import UIDesign, method_bundle, validate_brief
from auto_agents_2.workspace import git

PAGE = "design/prototypes/index.html"


def setup(tmp_path):
    workflow = engine(tmp_path, CodingAgent())
    rid = workflow.store.admit("UI design test", {})["id"]
    raw = workflow.artifacts.put("A simple workspace for ordinary users", "requirements")
    workflow.store.checkpoint(rid, "capture", {"artifact": raw})
    spec = IntakeResult(summary="UI", document="A simple workspace", frontend=True,
                        visual_brief=visual_brief(), prototype_paths=[PAGE])
    workflow.store.checkpoint(rid, "clarify", spec.model_dump())
    service = UIDesign(workflow.store, workflow.artifacts)
    source = source_context(workflow.store, workflow.artifacts, rid)["prototype_requirement_source"]
    service.bind_brief(rid, spec.model_dump(), source)
    return workflow, rid, spec, service, source


def test_legacy_models_keep_exact_serialized_shape():
    old = {"summary": "old", "document": "old", "questions": [], "requirements": [], "coverage": [],
           "verification": [], "frontend": True, "ui_changes": True, "prototype_paths": []}
    assert IntakeResult.model_validate(old).model_dump() == old
    document = {"summary": "old", "document": "old", "questions": [], "sources": [], "files": [], "browser_actions": []}
    assert DocumentResult.model_validate(document).model_dump() == document
    old_review = {"approved": True, "summary": "old", "findings": [], "basis_hash": "old", "candidate_hash": "old",
                  "coverage": []}
    assert PrototypeReview.model_validate(old_review).model_dump() == old_review


@pytest.mark.parametrize("field", ["audience", "primary_task", "hierarchy", "density", "language", "components"])
def test_external_material_cannot_mint_brief_sources(field):
    spec = IntakeResult(summary="UI", document="UI", frontend=True, visual_brief=visual_brief()).model_dump()
    spec["visual_brief"][field]["source_refs"] = ["impeccable:quieter"]
    with pytest.raises(InvalidStructuredResponse, match="unknown"):
        validate_brief(spec, "A workspace", [], required=True)


def test_new_ui_requires_brief_but_backend_and_unchanged_ui_do_not():
    legacy = IntakeResult(summary="UI", document="UI", frontend=True)
    with pytest.raises(InvalidStructuredResponse, match="requires"):
        validate_brief(legacy.model_dump(), "UI", [], required=True)
    validate_brief(legacy.model_copy(update={"ui_changes": False}).model_dump(), "UI", [], required=True)
    validate_brief(legacy.model_copy(update={"frontend": False}).model_dump(), "API", [], required=True)


def test_source_bound_brief_is_current_only_for_same_feedback(tmp_path):
    workflow, rid, spec, service, source = setup(tmp_path)
    before = service.brief(rid, spec.model_dump(), source)
    question = workflow.store.request(rid, "prototype-feedback", "Change", "candidate")
    workflow.store.answer(question["id"], "Make secondary buttons quieter")
    current = source_context(workflow.store, workflow.artifacts, rid)["prototype_requirement_source"]
    with pytest.raises(StateConflict, match="source changed"):
        service.context(rid, spec.model_dump(), current)
    assert workflow.store.get(rid, "ui_design/brief") == before
    spec.visual_brief.hierarchy.source_refs = ["answer:" + question["id"]]
    service.bind_brief(rid, spec.model_dump(), current)
    assert service.context(rid, spec.model_dump(), current)["ui_design"]["brief"] != before
    assert workflow.store.get(rid, "ui_design/brief-history/" + before["hash"]) == before


def test_brief_and_acceptance_can_be_published_atomically_after_crash(tmp_path, monkeypatch):
    workflow, rid, spec, service, source = setup(tmp_path)
    workflow.store.invalidate(rid, ["ui_design/brief"])
    updates = service.brief_updates(rid, spec.model_dump(), source)
    assert updates and workflow.store.get(rid, "ui_design/brief") is None
    workflow.store.checkpoints(rid, {"clarify": spec.model_dump(), **updates})
    assert service.brief(rid, spec.model_dump(), source)["visual_brief"] == spec.visual_brief.model_dump()


def test_design_permission_must_be_in_formal_specification():
    spec = IntakeResult(summary="UI", document="UI", frontend=True, prototype_paths=[PAGE],
                        visual_brief=visual_brief(update="required")).model_dump()
    with pytest.raises(InvalidStructuredResponse, match="formal prototype_paths"):
        validate_brief(spec, "UI", [], required=True)
    assert spec["prototype_paths"] == [PAGE]
    spec["prototype_paths"].append("DESIGN.md")
    validate_brief(spec, "UI", [], required=True)


def test_verified_source_migrates_from_archive_and_survives_replan(tmp_path):
    workflow, rid, spec, service, source = setup(tmp_path)
    row = acquired_source(workflow.artifacts)
    workflow.store.checkpoints(rid, {
        "recovery/gate-history/1": {"checkpoints": {"prototype/pending": {"retrieved": [row]}}},
        "rounds/prototype": {"round": 12}, "prototype/pending": {"retrieved": []},
        "replanning/pending": {"generation": 2, "stage": "prototype", "reason": "feedback", "granted": True}})
    retained = service.sources(rid)
    assert retained["origin"]["migration_checkpoint"] == "recovery/gate-history/1"
    assert retained["sources"][0]["body_sha256"] == row["retrieval"]["artifact"]["hash"]
    assert retained["sources"][0]["pinned_revision"] == "a" * 40
    workflow.store.complete_replan(rid, ["prototype", "design", "plan"], [])
    assert service.sources(rid) == retained
    assert workflow.store.get(rid, "rounds/prototype") == {"round": 12}
    assert workflow.store.get(rid, "recovery/gate-history/1")["checkpoints"]["prototype/pending"]["retrieved"] == [row]
    assert workflow.store.get(rid, "ui_design/source-history/" + retained["hash"]) == retained


@pytest.mark.parametrize("damage", ["body", "missing", "manifest", "url", "revision", "record"])
def test_corrupt_or_relabelled_source_is_never_silently_reacquired(tmp_path, damage):
    workflow, rid, _, service, _ = setup(tmp_path)
    row = acquired_source(workflow.artifacts)
    retained = service.retain(rid, [row], origin={"test": True})
    if damage in {"body", "missing"}:
        ref = row["retrieval"]["artifact"]
        path = workflow.artifacts.root / ref["hash"][:2] / ref["hash"]
        if damage == "body":
            path.write_bytes(b"tampered")
        else:
            path.unlink()
    else:
        changed = copy.deepcopy(retained)
        item = changed["sources"][0]
        if damage == "manifest":
            item["retrieval"]["manifest"] = workflow.artifacts.put({"source": URL}, "research_manifest")
        elif damage == "url":
            item["url"] = URL.replace("a" * 40, "main")
        elif damage == "revision":
            item["pinned_revision"] = "b" * 40
        else:
            changed["origin"] = {"forged": True}
        if damage != "record":
            changed["hash"] = digest({k: v for k, v in changed.items() if k != "hash"})
        workflow.store.checkpoint(rid, "ui_design/sources", changed)
    with pytest.raises(StateConflict):
        service.sources(rid)


def test_unverified_legacy_claim_cannot_become_provenance(tmp_path):
    workflow, rid, _, service, _ = setup(tmp_path)
    workflow.store.checkpoint(rid, "recovery/gate-history/0",
        {"checkpoints": {"prototype/pending": {"retrieved": [{"url": URL, "excerpt": "Claimed read"}]}}})
    with pytest.raises(StateConflict):
        service.sources(rid)
    assert workflow.store.get(rid, "ui_design/sources") is None


@pytest.mark.parametrize("stage,kind", [
    ("prototype", None), ("review", "prototype"), ("design", None), ("plan", None),
    ("implement", None), ("review", "implementation"), ("verify", None)])
def test_downstream_presentation_preserves_brief_source_and_method(tmp_path, stage, kind):
    workflow, rid, spec, service, source = setup(tmp_path)
    record = service.retain(rid, [acquired_source(workflow.artifacts)], origin={"test": True},
        selections=[{"url": URL, "rationale": "Task-focused fit", "adaptations": ["Lower secondary emphasis"]}])
    context = {"specification": spec.model_dump(), **service.context(rid, spec.model_dump(), source)}
    if kind:
        context["review_kind"] = kind
    presented = ContextPlanner(workflow.artifacts).prepare(stage, context, tmp_path, run_id=rid).payload
    assert presented["ui_design"]["brief"]["visual_brief"] == spec.visual_brief.model_dump()
    retained = presented["ui_design"]["sources"]
    assert retained["hash"] == record["hash"]
    assert retained["sources"][0]["design_selection"]["adaptations"] == ["Lower secondary emphasis"]
    assert "Restrained controls" in retained["sources"][0]["excerpt"]
    assert presented["ui_design"]["method"]["source"]["commit"] == "414603bf14c7a344edb466bb1f85690c77ce8b7d"


def visual_context(images=True):
    source = {"status": "available", "raw_requirements": "Keep secondary controls visually subordinate."}
    visuals = [{"route": "/", "viewport": 1440, "image_artifact": {
        "hash": "a" * 64, "kind": "screenshot", "bytes": 100}}] if images else []
    basis = review_basis(source, {"document": "Simple workspace"}, {"identity": "candidate"}, visuals,
                         {"method_hash": "fixed"})
    return {"prototype_requirement_source": source, "prototype_review_basis": basis}


@pytest.mark.parametrize("damage,code", [
    ("missing", "incomplete_visual_assessment"), ("duplicate", "incomplete_visual_assessment"),
    ("fake_image", "unknown_visual_observation"), ("no_image", "visual_judgment_requires_images"),
    ("taste_as_rule", "aesthetic_preference_is_not_requirement")])
def test_visual_assessment_must_cover_real_evidence_without_invented_rules(damage, code):
    context = visual_context()
    response = prototype_verdict(context).model_dump()
    rows = response["visual_assessment"]
    if damage == "missing":
        response.pop("visual_assessment")
    elif damage == "duplicate":
        rows[1]["dimension"] = rows[0]["dimension"]
    elif damage == "fake_image":
        rows[0]["image_refs"] = ["screenshot-I-claim-to-have-seen"]
    elif damage == "no_image":
        rows[0]["image_refs"] = []
    else:
        rows[0].update(judgment="requirement_defect", criterion_ids=["stage:prototype"])
    with pytest.raises(InvalidPrototypeReview, match=code):
        validate_review(response, context["prototype_review_basis"], context["prototype_requirement_source"])


def test_aesthetic_advice_does_not_block_and_observed_requirement_defect_does():
    context = visual_context()
    advice = PrototypeFinding(severity="minor", location="home secondary actions", description="Could be quieter",
        evidence="Current home image", resolution="Consider subtler borders", criterion_id="stage:prototype",
        kind="advisory", observation="Borders compete with media", evidence_refs=["browser"],
        owner="prototype", requested_capability="none")
    response = prototype_verdict(context, findings=[advice])
    response.visual_assessment[0].judgment = "advisory"
    assert validate_review(response, context["prototype_review_basis"], context["prototype_requirement_source"]).approved
    defect = advice.model_copy(update={"severity": "major", "criterion_id": "user:original", "kind": "defect"})
    response = prototype_verdict(context, approved=False, findings=[defect])
    response.visual_assessment[0].judgment = "requirement_defect"
    response.visual_assessment[0].criterion_ids = ["user:original"]
    assert not validate_review(response, context["prototype_review_basis"], context["prototype_requirement_source"]).approved


def test_source_method_and_image_changes_invalidate_only_automatic_review():
    context = visual_context()
    response = prototype_verdict(context)
    source = context["prototype_requirement_source"]
    assert review_current(response, context["prototype_review_basis"], source)
    changed = copy.deepcopy(context["prototype_review_basis"])
    changed["visual_design_hash"] = "different-method-or-source"
    changed["hash"] = digest({k: v for k, v in changed.items() if k != "hash"})
    assert not review_current(response, changed, source)
    changed = copy.deepcopy(context["prototype_review_basis"])
    changed["visual_evidence"]["image:0:0"]["artifact"]["hash"] = "b" * 64
    changed["hash"] = digest({k: v for k, v in changed.items() if k != "hash"})
    assert not review_current(response, changed, source)


def test_no_screenshot_cannot_be_called_observed_quality():
    context = visual_context(images=False)
    response = prototype_verdict(context)
    assert all(row.judgment == "not_observable" for row in response.visual_assessment)
    validate_review(response, context["prototype_review_basis"], context["prototype_requirement_source"])
    response.visual_assessment[0].judgment = "supports_goal"
    with pytest.raises(InvalidPrototypeReview, match="requires_images"):
        validate_review(response, context["prototype_review_basis"], context["prototype_requirement_source"])


async def test_retained_source_reaches_next_prototype_without_network(tmp_path, monkeypatch):
    workflow, rid, spec, service, source = setup(tmp_path)
    service.retain(rid, [acquired_source(workflow.artifacts)], origin={"test": True})
    page = tmp_path / PAGE
    page.parent.mkdir(parents=True)
    page.write_text("<h1>Before</h1>")
    git(tmp_path, "add", PAGE)
    git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "baseline")
    baseline = git(tmp_path, "rev-parse", "HEAD")
    document = DocumentResult(summary="Refine", document="Keep baseline", files=[Change(path=PAGE, content="<h1>After</h1>")])
    workflow.store.checkpoints(rid, {"baseline": baseline, "prototype/pending": {
        "identity": digest([spec.model_dump(), baseline, None]), "document": document.model_dump(),
        "design_candidates": [], "ui_design_version": 1, "question_refresh_version": 12}})
    async def no_network(*args):
        pytest.fail("A retained immutable source must not be downloaded again")
    class BrowserReached(Exception):
        pass
    async def browser(*args):
        assert "After" in page.read_text()
        raise BrowserReached()
    monkeypatch.setattr("auto_agents_2.engine.ResearchService.verify", no_network)
    workflow._prototype_browser_report = browser
    with pytest.raises(BrowserReached):
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")
    pending = workflow.store.get(rid, "prototype/pending")
    assert pending["retrieved"][0]["body_sha256"] == acquired_source(workflow.artifacts)["retrieval"]["hash"]


def test_fixed_method_bundle_works_offline_and_detects_tampering(monkeypatch, tmp_path):
    def no_network(*args, **kwargs):
        pytest.fail("Method loading must be offline")
    monkeypatch.setattr(socket, "create_connection", no_network)
    bundle = method_bundle()
    assert bundle["source"]["version"] == "4.8.0"
    assert bundle["source"]["license"] == "Apache-2.0"
    assert len(bundle["upstream_files"]) == 9
    original = Path(__file__).parents[1] / "src/auto_agents_2/design_methods"
    copied = tmp_path / "design_methods"
    copied.mkdir()
    for path in original.iterdir():
        (copied / path.name).write_bytes(path.read_bytes())
    (copied / "adaptation.md").write_text("Unexpected substitution")
    monkeypatch.setattr("auto_agents_2.visual_design.files", lambda package: tmp_path)
    with pytest.raises(StateConflict, match="integrity"):
        method_bundle()


def test_wheel_contains_attributed_offline_methods(tmp_path):
    root = Path(__file__).parents[1]
    result = subprocess.run([sys.executable, "-m", "hatchling", "build", "-t", "wheel", "-d", str(tmp_path)],
                            cwd=root, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    wheel = next(tmp_path.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        for name in ("manifest.json", "adaptation.md", "LICENSE", "UPSTREAM-NOTICE.md"):
            assert "auto_agents_2/design_methods/" + name in archive.namelist()
    code = ("import sys,socket; sys.path.insert(0,sys.argv[1]); "
            "socket.create_connection=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('network forbidden')); "
            "from auto_agents_2.visual_design import method_bundle; "
            "assert method_bundle()['source']['commit']=='414603bf14c7a344edb466bb1f85690c77ce8b7d'")
    result = subprocess.run([sys.executable, "-I", "-c", code, str(wheel)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_sources_survive_terminal_artifact_collection(tmp_path):
    workflow, rid, _, service, _ = setup(tmp_path)
    row = acquired_source(workflow.artifacts)
    record = service.retain(rid, [row], origin={"test": True})
    unrelated = workflow.artifacts.put("discardable diagnostic", "tool_log")
    workflow.store.update(rid, status="SUCCEEDED")
    workflow.resources.collect_artifacts()
    assert service.sources(rid) == record
    assert workflow.artifacts.bytes(row["retrieval"]["raw"])
    assert workflow.artifacts.json(row["retrieval"]["manifest"])
    assert not (workflow.artifacts.root / unrelated["hash"][:2] / unrelated["hash"]).exists()


@pytest.mark.parametrize("stage", ["prototype", "design", "plan", "implement", "verify"])
async def test_engine_injects_controller_design_context_before_provider(tmp_path, stage):
    from auto_agents_2.models import ImplementationResult, PlanResult, ReviewResult

    workflow, rid, spec, service, source = setup(tmp_path)
    service.retain(rid, [acquired_source(workflow.artifacts)], origin={"test": True})
    class ReachedProvider(Exception):
        pass
    async def provider(current_stage, context, *args, **kwargs):
        assert current_stage == stage
        assert context["ui_design"]["brief"]["visual_brief"] == spec.visual_brief.model_dump()
        assert context["ui_design"]["sources"]["sources"][0]["url"] == URL
        assert context["ui_design"]["method"]["adaptation_hash"]
        raise ReachedProvider()
    workflow.agent.invoke = provider
    schema = {"implement": ImplementationResult, "plan": PlanResult, "verify": ReviewResult}.get(stage, DocumentResult)
    with pytest.raises(ReachedProvider):
        await workflow._invoke(stage, {"specification": spec.model_dump()}, schema, tmp_path, rid,
                               "ui-context-test", interactive_retry=False)


async def test_latest_feedback_reenters_clarify_without_resetting_rounds(tmp_path):
    from test_plan_lineage import pending_run

    workflow, rid = pending_run(tmp_path, CodingAgent())
    spec = IntakeResult(summary="UI", document="UI", frontend=True, visual_brief=visual_brief())
    raw = workflow.artifacts.put("Add two numbers", "requirements")
    workflow.store.checkpoints(rid, {"capture": {"artifact": raw, "hash": raw["hash"]},
                                   "clarify": spec.model_dump(), "rounds/prototype": {"round": 12}})
    service = UIDesign(workflow.store, workflow.artifacts)
    service.bind_brief(rid, spec.model_dump(),
        source_context(workflow.store, workflow.artifacts, rid)["prototype_requirement_source"])
    row = workflow.store.request(rid, "prototype-feedback", "Change", "candidate")
    workflow.store.answer(row["id"], "Simplify the details dialog")
    before = workflow.store.get(rid, "ui_design/brief")
    with pytest.raises(Replan, match="source changed") as caught:
        await workflow._execute(rid)
    assert caught.value.stage == "clarify"
    assert workflow.store.get(rid, "rounds/prototype") == {"round": 12}
    assert workflow.store.get(rid, "ui_design/brief") == before
    assert workflow.store.question(row["id"])["answer"] == "Simplify the details dialog"


def test_available_images_cannot_be_skipped_in_an_approved_visual_review():
    context = visual_context()
    response = prototype_verdict(context)
    response.visual_assessment[0].judgment = "not_observable"
    response.visual_assessment[0].image_refs = []
    with pytest.raises(InvalidPrototypeReview, match="available_images_require_visual_assessment"):
        validate_review(response, context["prototype_review_basis"], context["prototype_requirement_source"])


def test_visual_binding_survives_source_presentation(tmp_path):
    from auto_agents_2.prototype_review import context_basis_current

    workflow, rid, spec, service, source = setup(tmp_path)
    service.retain(rid, [acquired_source(workflow.artifacts)], origin={"test": True})
    context = workflow._prototype_review_context(rid, tmp_path, {
        "review_kind": "prototype", "prototype": {}, "browser_evidence": []})
    presented = ContextPlanner(workflow.artifacts).prepare("review", context, tmp_path, run_id=rid).payload
    assert context_basis_current(presented["prototype_review_basis"], presented)


@pytest.mark.parametrize("generations", [1, 3])
def test_verified_reuse_keeps_original_feedback_binding_across_runs(tmp_path, generations):
    workflow, original_run, spec, service, _ = setup(tmp_path)
    question = workflow.store.request(original_run, "prototype-feedback", "Style feedback", "candidate")
    workflow.store.answer(question["id"], "Keep controls subordinate")
    spec.visual_brief.hierarchy.source_refs = ["answer:" + question["id"]]
    workflow.store.checkpoint(original_run, "clarify", spec.model_dump())
    original_source = source_context(workflow.store, workflow.artifacts, original_run)["prototype_requirement_source"]
    original_brief = service.bind_brief(original_run, spec.model_dump(), original_source)
    original_sources = service.retain(original_run, [acquired_source(workflow.artifacts)], origin={"test": True})
    previous = original_run
    for _ in range(generations):
        workflow.store.update(previous, status="SUCCEEDED")
        rid = workflow.store.admit("same verified input", {})["id"]
        workflow.store.checkpoints(rid, {"capture": workflow.store.get(original_run, "capture"),
                                       "clarify": spec.model_dump(), "rounds/prototype": {"round": 5}})
        current = source_context(workflow.store, workflow.artifacts, rid)["prototype_requirement_source"]
        service.inherit(rid, previous, spec.model_dump(), current)
        view = service.context(rid, spec.model_dump(), current)["ui_design"]
        assert view["brief"] == original_brief
        assert view["sources"] == original_sources
        assert view["inherited_from"]["original_answers"][0]["id"] == question["id"]
        assert workflow.store.get(rid, "rounds/prototype") == {"round": 5}
        previous = rid
    row = workflow.store.request(previous, "prototype-feedback", "New feedback", "new-candidate")
    workflow.store.answer(row["id"], "Change the design")
    current = source_context(workflow.store, workflow.artifacts, previous)["prototype_requirement_source"]
    with pytest.raises(StateConflict, match="source changed"):
        service.context(previous, spec.model_dump(), current)
    assert workflow.store.get(original_run, "ui_design/brief") == original_brief
    assert workflow.store.question(question["id"])["answer"] == "Keep controls subordinate"


def test_method_hash_and_delivered_text_use_the_same_read(monkeypatch, tmp_path):
    original = Path(__file__).parents[1] / "src/auto_agents_2/design_methods"
    copied = tmp_path / "design_methods"
    copied.mkdir()
    for path in original.iterdir():
        (copied / path.name).write_bytes(path.read_bytes())
    expected = (copied / "adaptation.md").read_text()

    class SwapAfterRead:
        def __init__(self, path):
            self.path = path
        def joinpath(self, name):
            return SwapAfterRead(self.path / name)
        def read_bytes(self):
            raw = self.path.read_bytes()
            if self.path.name == "adaptation.md":
                self.path.write_text("changed after validated acquisition")
            return raw
        def read_text(self, **kwargs):
            return self.path.read_text(**kwargs)

    monkeypatch.setattr("auto_agents_2.visual_design.files", lambda package: SwapAfterRead(tmp_path))
    assert method_bundle()["methods"] == expected
    with pytest.raises(StateConflict, match="integrity"):
        method_bundle()


@pytest.mark.parametrize("stage,question_key", [
    ("prototype", "prototype-clarification"), ("design", "design"),
])
async def test_answer_during_ui_stage_reenters_clarify_in_same_run(tmp_path, monkeypatch, stage, question_key):
    import asyncio

    workflow, rid, spec, _, _ = setup(tmp_path)
    workflow.store.update(rid, replans=12)
    before = workflow.store.get(rid, "ui_design/brief")
    seen = []

    async def answer(question):
        assert question["key"] == question_key + "/0"
        seen.append(question["id"])
        return "Use concise labels and quieter secondary actions"

    async def execute(run_id):
        assert run_id == rid
        await workflow._questions(rid, question_key, ["How should secondary actions read?"], "candidate")
        # This is the ordinary continuation after the user's answer, in the same start() call.
        await workflow._invoke(stage, {"specification": spec.model_dump()}, DocumentResult,
                               tmp_path, rid, "after-product-answer")

    async def resume(run_id):
        pending = workflow.store.get(run_id, "replanning/pending")
        assert pending["stage"] == "clarify" and pending["controller_owned"]
        assert workflow.store.run(run_id)["data"]["replans"] == 12
        assert workflow.store.get(run_id, "ui_design/brief") == before
        assert workflow.store.submitted_answers(run_id)[0]["id"] == seen[0]
        raise asyncio.CancelledError()

    async def unexpected_wait(*args):
        pytest.fail("A newly submitted product answer must re-enter clarify, not request state repair")

    async def unexpected_provider(*args, **kwargs):
        pytest.fail("Do not continue generation against a stale visual brief")

    workflow.question_handler = answer
    monkeypatch.setattr(workflow, "_execute", execute)
    monkeypatch.setattr(workflow, "_resume_replan", resume)
    monkeypatch.setattr(workflow, "_wait_prerequisite", unexpected_wait)
    monkeypatch.setattr(workflow.agent, "invoke", unexpected_provider)
    with pytest.raises(asyncio.CancelledError):
        await workflow.start()
    assert len(seen) == 1
    assert not workflow.store.pending(rid)
    assert workflow.store.get(rid, "prototype_approval") is None


async def test_corrupt_brief_still_requires_state_repair_not_reclarification(tmp_path, monkeypatch):
    import asyncio

    workflow, rid, spec, _, _ = setup(tmp_path)
    broken = workflow.store.get(rid, "ui_design/brief")
    broken["hash"] = "damaged"
    workflow.store.checkpoint(rid, "ui_design/brief", broken)
    observed = []

    async def execute(run_id):
        await workflow._invoke("prototype", {"specification": spec.model_dump()}, DocumentResult,
                               tmp_path, run_id, "damaged-brief")

    async def repair_required(run_id, error):
        assert run_id == rid and type(error) is StateConflict
        observed.append(error.reason_code)
        raise asyncio.CancelledError()

    monkeypatch.setattr(workflow, "_execute", execute)
    monkeypatch.setattr(workflow, "_wait_prerequisite", repair_required)
    with pytest.raises(asyncio.CancelledError):
        await workflow.start()
    assert observed == ["state_conflict"]
    assert workflow.store.get(rid, "replanning/pending") is None
    assert workflow.store.get(rid, "ui_design/brief") == broken


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("ui_changes", [False, True])
async def test_inheritance_uses_consent_for_the_selected_run(tmp_path, monkeypatch, legacy, ui_changes):
    from test_prototype_approvals import approval_fixture

    from auto_agents_2.approvals import prototype_identity
    from auto_agents_2.interaction import choice_spec
    from auto_agents_2.models import Coverage, Requirement

    workflow, matching, document, _, _, _ = approval_fixture(tmp_path, monkeypatch, legacy=legacy)
    service = UIDesign(workflow.store, workflow.artifacts)
    expected = service.retain(matching, [acquired_source(workflow.artifacts)], origin={"run": matching})
    matching_page = (tmp_path / PAGE).read_bytes()
    workflow.store.update(matching, status="SUCCEEDED")
    unrelated = workflow.store.admit("different historical interface", {})["id"]
    (tmp_path / PAGE).write_text("<h1>Different interface</h1>")
    actions = document.model_dump()["browser_actions"]
    binding = prototype_identity(tmp_path, actions)
    question = workflow.store.request(unrelated, "prototype-approval", "Approve the other interface",
        binding["legacy_hash"] if legacy else binding["identity"],
        choice_spec(["Approve", "Change"], ["approve", "reject"]))
    workflow.store.answer(question["id"], "1", question["artifact_hash"])
    other_approval = copy.deepcopy(workflow.store.get(matching, "prototype_approval"))
    other_approval.update({"hash": binding["legacy_hash"]} if legacy else binding)
    other_document = document.model_copy(update={"summary": "Different historical prototype"})
    workflow.store.checkpoints(unrelated, {"prototype": other_document.model_dump(),
                                          "prototype_approval": other_approval})
    service.retain(unrelated, [acquired_source(workflow.artifacts,
        url=URL.replace("a" * 40, "b" * 40), body="Unrelated reference")], origin={"run": unrelated})
    workflow.store.update(unrelated, status="SUCCEEDED")
    (tmp_path / PAGE).write_bytes(matching_page)
    rid = workflow.store.admit("refine the current interface", {})["id"]
    raw = workflow.artifacts.put("Keep the current interface and refine only the requested details", "requirements")
    spec = IntakeResult(summary="UI", document="Current interface", frontend=True, ui_changes=ui_changes,
        requirements=[Requirement(id="R1", description="Refine requested details", acceptance=["Preserve behavior"])],
        coverage=[Coverage(requirement_id="R1", status="changed", rationale="Requested details need review")],
        visual_brief=visual_brief(), prototype_paths=[PAGE])
    workflow.store.checkpoints(rid, {"capture": {"artifact": raw, "hash": raw["hash"]},
        "baseline": git(tmp_path, "rev-parse", "HEAD"), "clarify": spec.model_dump(),
        "provider_research": DocumentResult(summary="No provider", document="No external provider").model_dump(),
        "rounds/prototype": {"round": 12}})
    workflow.store.update(rid, replans=12)
    service.bind_brief(rid, spec.model_dump(),
        source_context(workflow.store, workflow.artifacts, rid)["prototype_requirement_source"])
    # Newest-first ordering is the trigger: its content differs although actions match.
    assert workflow.store.successful()[0]["id"] == unrelated

    class ReachedGate(Exception):
        pass

    def check_source():
        assert service.sources(rid) == expected
        assert workflow.store.get(rid, "ui_design/source-inheritance")["run_id"] == matching
        assert workflow.store.get(rid, "rounds/prototype") == {"round": 12}
        assert workflow.store.run(rid)["data"]["replans"] == 12

    async def prepare(*args):
        assert ui_changes
        check_source()
        raise ReachedGate()

    async def ensure(run_id, work, prototype):
        assert not ui_changes and prototype == document.model_dump()
        check_source()
        raise ReachedGate()

    monkeypatch.setattr(workflow, "_prepare_prototype", prepare)
    monkeypatch.setattr(workflow, "_ensure_prototype_approval", ensure)
    with pytest.raises(ReachedGate):
        await workflow._execute(rid)
    assert workflow.store.get(rid, "prototype") is None
