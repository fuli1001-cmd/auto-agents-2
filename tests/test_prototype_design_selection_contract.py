"""Pinpoint prototype source coverage without forcing comparison-only adoption."""
import copy
import json
from types import SimpleNamespace

import pytest
from test_workflow import engine

from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.engine import Replan
from auto_agents_2.models import Change, DesignSelection, DocumentResult, IntakeResult, SourceEvidence
from auto_agents_2.prompts import PromptCompiler
from auto_agents_2.prototype_contract import (
    PROTOTYPE_DESIGN_SELECTION_RULE,
    design_selection_feedback,
    prototype_contract,
)
from auto_agents_2.util import digest

BASE = "https://raw.githubusercontent.com/VoltAgent/awesome-design-md/" + "a" * 40 + "/"
ELEVENLABS, RUNWAY, NOTION, OTHER = [BASE + name + "/DESIGN.md"
                                  for name in ("ElevenLabs", "Runway", "Notion", "Other")]
OLD_REASON = "Explain each selected design reference's product fit and adaptations in design_selection"


def proposal(urls):
    return DocumentResult(summary="Complete six-file prototype", document="All current prototype obligations",
        files=[Change(path=f"design/prototypes/page-{index}.html", content=f"<h1>Page {index}</h1>")
               for index in range(6)],
        sources=[SourceEvidence(url=url, claim="Comparison only; not adopted" if url == NOTION else "Product fit")
                 for url in (ELEVENLABS, RUNWAY, NOTION)],
        design_selection=[DesignSelection(url=url,
            rationale="Comparison only: workspace density is not adopted." if url == NOTION else "Task-focused fit",
            adaptations=["Not adopted; no adaptations apply."] if url == NOTION else ["Preserve current task flow"])
            for url in urls])


def pending_fixture(tmp_path, document):
    workflow = engine(tmp_path, None)
    rid = workflow.store.admit("reference contract fixture", {})["id"]
    spec = IntakeResult(summary="UI", document="UI", frontend=True)
    feedback = {"stage": "prototype", "reason": OLD_REASON}
    pending = {"identity": digest([spec.model_dump(), None, feedback]), "document": document.model_dump(),
               "design_candidates": [{"url": url} for url in (ELEVENLABS, RUNWAY, NOTION, OTHER)],
               "ui_design_version": 1}
    workflow.store.checkpoints(rid, {"clarify": spec.model_dump(), "replan_feedback": feedback,
                                    "prototype/pending": pending,
                                    "infra/prototype/codex": {"pending": True, "failures": 2, "grant": 3}})
    return workflow, rid, spec, copy.deepcopy(pending)


def differences(reason):
    prefix = "design_selection URL coverage mismatch: "
    assert reason.startswith(prefix)
    return json.loads(reason[len(prefix):].split("\n", 1)[0])


@pytest.mark.parametrize("urls,missing,unexpected,duplicates", [
    ([ELEVENLABS, RUNWAY], [NOTION], [], []),
    ([ELEVENLABS, RUNWAY, NOTION, NOTION], [], [], [NOTION]),
    ([ELEVENLABS, RUNWAY, NOTION, OTHER], [], [OTHER], []),
])
async def test_existing_gate_pinpoints_only_missing_unexpected_or_duplicate_reference(
        tmp_path, urls, missing, unexpected, duplicates):
    workflow, rid, spec, pending = pending_fixture(tmp_path, proposal(urls))
    budget = workflow.store.get(rid, "infra/prototype/codex")
    with pytest.raises(Replan) as error:
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")
    assert error.value.stage == "prototype"
    delta = differences(error.value.reason)
    assert delta["missing"] == missing
    assert [row["reference"] for row in delta["unexpected"]] == unexpected
    assert [row["reference"] for row in delta["duplicates"]] == duplicates
    if missing:
        assert "ElevenLabs" not in error.value.reason and "Runway" not in error.value.reason
    assert workflow.store.get(rid, "prototype/pending") == pending
    assert len(pending["document"]["files"]) == 6
    assert workflow.store.get(rid, "infra/prototype/codex") == budget
    assert workflow.store.get(rid, "prototype_approval") is None


@pytest.mark.parametrize("unknown", [
    "https://EXAMPLE_USER:EXAMPLE_PASSWORD@private.invalid/EXAMPLE_PATH?x=EXAMPLE_QUERY",
    "/EXAMPLE_PRIVATE_PATH/EXAMPLE_SECRET",
])
async def test_unexpected_and_duplicate_arbitrary_url_values_remain_opaque(tmp_path, unknown):
    workflow, rid, spec, _ = pending_fixture(tmp_path, proposal([ELEVENLABS, RUNWAY, NOTION, unknown, unknown]))
    with pytest.raises(Replan) as error:
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")
    delta = differences(error.value.reason)
    for key in ("unexpected", "duplicates"):
        assert delta[key] == [{"reference": {"sha256": digest(unknown), "characters": len(unknown)},
                              "design_selection_indexes": [3, 4]}]
    assert "EXAMPLE_" not in error.value.reason and "private.invalid" not in error.value.reason


async def test_comparison_only_nonadoption_entry_reaches_original_source_verification(tmp_path, monkeypatch):
    document = proposal([ELEVENLABS, RUNWAY, NOTION])
    workflow, rid, spec, pending = pending_fixture(tmp_path, document)
    class VerificationReached(Exception):
        pass
    async def verify(sources):
        assert [row.url for row in sources] == [ELEVENLABS, RUNWAY, NOTION]
        raise VerificationReached()
    monkeypatch.setattr(workflow, "_research_service", lambda _: SimpleNamespace(verify=verify))
    with pytest.raises(VerificationReached):
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, "UI")
    assert workflow.store.get(rid, "prototype/pending") == pending
    assert document.design_selection[-1].rationale.startswith("Comparison only")
    assert document.design_selection[-1].adaptations == ["Not adopted; no adaptations apply."]
    assert workflow.store.get(rid, "prototype_approval") is None


async def test_each_author_call_refreshes_reference_guidance_despite_saved_generic_feedback(tmp_path):
    received = []
    class AuthorInspected(Exception):
        pass
    class Agent:
        async def invoke(self, stage, context, schema, work, rid, key, fresh=False):
            assert stage == "prototype"
            contract = context["controller_prototype_output"]
            assert contract["version"] == 2
            assert contract["design_selection_coverage"] == PROTOTYPE_DESIGN_SELECTION_RULE
            view = ContextPlanner(workflow.artifacts).prepare(stage, context, work, run_id=rid)
            assert view.payload["controller_prototype_output"] == contract
            prompt, _ = PromptCompiler().compile(stage, view.payload)
            assert PROTOTYPE_DESIGN_SELECTION_RULE in prompt
            assert "comparison-only and non-adopted" in prompt and "never claim adoption" in prompt
            received.append(context)
            raise AuthorInspected()
    workflow = engine(tmp_path, Agent())
    rid = workflow.store.admit("saved generic prototype feedback", {})["id"]
    feedback = {"stage": "prototype", "reason": OLD_REASON}
    workflow.store.checkpoint(rid, "replan_feedback", feedback)
    original_protocol = copy.deepcopy(prototype_contract())
    for _ in range(2):
        with pytest.raises(AuthorInspected):
            await workflow._invoke("prototype",
                {"feedback": feedback, "controller_prototype_output": {"version": 1}},
                DocumentResult, tmp_path, rid, "prototype")
    assert len(received) == 2 and workflow.store.get(rid, "replan_feedback") == feedback
    assert all(row["controller_prototype_contract"] == original_protocol for row in received)


@pytest.mark.parametrize("url,candidates", [
    (BASE + "EXAMPLE_RETAINED_PATH/DESIGN.md", []),
    (BASE + "Notion/DESIGN.md?token=EXAMPLE_QUERY", [BASE + "Notion/DESIGN.md?token=EXAMPLE_QUERY"]),
])
def test_retained_or_query_bearing_references_are_not_public_diagnostic_whitelist(url, candidates):
    delta = differences(design_selection_feedback([url], [], candidates))
    assert delta["missing"] == [{"sha256": digest(url), "characters": len(url)}]
    assert "EXAMPLE_" not in json.dumps(delta)
