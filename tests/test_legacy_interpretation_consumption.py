"""Ordinary legacy caches stay usable without becoming submitted user methods.

Model responses are scenario fixtures. Intake parsing, persisted checkpoints,
authority validation, Git, checks and downstream acceptance use the real Engine.
Active research corrections are covered in test_research_correction_handoff.py.
"""
import asyncio
import copy

import pytest
from authority_review_fixtures import authority_review
from test_decisions import ApprovingAgent
from test_delivery import fixture
from test_workflow import engine

from auto_agents_2.models import DocumentResult

METHOD = "Use a pure local function for addition."


class LegacyInterpretationAgent(ApprovingAgent):
    def __init__(self, misattribute):
        super().__init__()
        self.misattribute = misattribute
        self.stop_before_design = True
        self.authors = {"clarify": 0, "provider_research": 0}
        self.design_bases = []
        self.research_seen = []

    async def invoke(self, stage, context, schema, work, rid, operation, fresh=False):
        if stage == "clarify":
            self.authors[stage] += 1
            result = await super().invoke(stage, context, schema, work, rid, operation, fresh)
            result.document += "\nSuggested implementation: " + METHOD
            return result
        if stage == "provider_research":
            self.authors[stage] += 1
            return DocumentResult(summary="No external provider", document="Suggestion: " + METHOD)
        if stage == "design":
            if self.stop_before_design:
                raise asyncio.CancelledError()
            self.research_seen.append(copy.deepcopy(context["provider_research"]))
            return DocumentResult(summary="Local addition", document=METHOD)
        if context.get("review_kind") == "design":
            self.design_bases.append(copy.deepcopy(context["controller_review_basis"]))
            result = authority_review(context)
            result.authority["technical_choices"] = [{
                "id": "local-function", "clause": METHOD, "goal_ids": ["requirement:R1"],
                "method_origin": "explicit_input_method" if self.misattribute else "derived_choice",
                "method_source": "specification" if self.misattribute else "candidate",
                "input_quote": METHOD if self.misattribute else "",
                "feasibility": "not_yet_verified", "evidence_refs": ["design_documents"],
                "reason": "Preserve the addition goal; selection still needs actual checks.",
            }]
            return result
        return await super().invoke(stage, context, schema, work, rid, operation, fresh)


@pytest.mark.parametrize("misattribute", [False, True], ids=["derived-choice", "false-user-method"])
async def test_ordinary_legacy_caches_are_consumed_with_current_source_authority(tmp_path, misattribute):
    project, request = fixture(tmp_path)
    request.write_text("Add two numbers.")
    agent = LegacyInterpretationAgent(misattribute)
    workflow = engine(project, agent)
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(request))
    rid = workflow.store.current()["id"]
    capture = workflow.store.get(rid, "capture")

    # A pre-authority cache has these valid IntakeResult/DocumentResult fields.
    # The omitted fields have defaults in both 8ffedaa and the current schema;
    # missing a new acceptance marker alone does not make a legacy cache invalid.
    current_spec = workflow.store.get(rid, "clarify")
    specification = {key: current_spec[key] for key in ("summary", "document", "requirements", "coverage")}
    current_research = workflow.store.get(rid, "provider_research")
    research = {key: current_research[key] for key in ("summary", "document", "sources")}
    assert research["sources"] == []
    workflow.store.checkpoints(rid, {
        "clarify": specification, "provider_research": research,
        "authority/accepted/clarify": None, "authority/accepted/provider_research": None,
    })
    assert workflow.store.get(rid, "authority/correction") is None
    authors = dict(agent.authors)
    agent.stop_before_design = False

    resumed = engine(project, agent)
    result = await resumed.start(run_id=rid)
    assert agent.authors == authors == {"clarify": 1, "provider_research": 1}
    assert agent.research_seen == [research]
    assert resumed.store.get(rid, "clarify") == specification
    assert resumed.store.get(rid, "provider_research") == research
    assert resumed.store.get(rid, "capture") == capture
    assert resumed.store.get(rid, "authority/accepted/clarify") is None
    assert resumed.store.get(rid, "authority/accepted/provider_research") is None
    assert resumed.store.get(rid, "authority/correction") is None
    assert result["data"]["replans"] == 0

    for basis in agent.design_bases:
        sources = basis["sources"]
        assert sources["input"]["kind"] == "submitted_input"
        assert sources["input"]["text"] == request.read_text()
        assert METHOD not in sources["input"]["text"]
        assert sources["specification"]["kind"] == "accepted_interpretation"
        assert METHOD in sources["specification"]["text"]
        assert sources["candidate"]["kind"] == "proposal"
        assert METHOD in sources["candidate"]["text"]
        assert not any(row["kind"] == "submitted_answer" for row in sources.values())

    invalid = [e for e in resumed.store.events(rid) if e["kind"] == "review.authority_invalid"]
    if misattribute:
        assert result["status"] == "PAUSED"
        assert len(agent.design_bases) == len(invalid) == 2
        assert {e["data"]["code"] for e in invalid} == {"method_does_not_quote_submitted_input"}
        assert resumed.store.get(rid, "recovery/path-exhausted")
        assert resumed.store.get(rid, "authority/accepted/design") is None
        assert resumed.store.get(rid, "plan") is None
        assert not any(stage == "implement" for stage, _ in agent.calls)
        assert resumed.store.get(rid, "delivery/receipt") is None
    else:
        assert result["status"] == "SUCCEEDED" and result["data"]["outcome"] == "DELIVERED"
        assert not invalid and len(agent.design_bases) == 1
        accepted = resumed.store.get(rid, "authority/accepted/design")
        review = resumed.store.get(rid, accepted["review_key"])["review"]
        assert review["authority"]["technical_choices"][0]["method_origin"] == "derived_choice"
        assert (project / "calc.py").is_file()
        checks = resumed.store.get(rid, "verify/checks")
        assert checks and all(row["exit_code"] == 0 and not row["failure_reasons"] for row in checks)
        assert resumed.store.get(rid, "rounds/T1")["round"] == 1
