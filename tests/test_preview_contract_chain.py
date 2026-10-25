import copy

import pytest
from authority_review_fixtures import authority_review
from test_preview_evidence_recovery import ready

from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.decisions import DecisionResult
from auto_agents_2.models import BrowserAction, ReviewResult
from auto_agents_2.preview_evidence import ActionMapping, PreviewComparison, PreviewEvidence
from auto_agents_2.util import digest


async def chain_fixture(tmp_path, monkeypatch):
    workflow, rid, work, plan, base, failure, calls, browsers, approval = await ready(tmp_path, monkeypatch)
    baseline = workflow.store.get(rid, "plan_revision/history/5")["plan"]
    intermediate = plan.model_dump()
    workflow.store.checkpoint(rid, "recovery/gate-history/2", {"checkpoints": {"plan": baseline}})
    workflow.store.checkpoint(rid, "plan_revision/history/7", {"plan": intermediate})
    workflow.store.checkpoint(rid, "plan_revision/history/8", {"plan": intermediate})
    plan.preview.actions.append(BrowserAction(action="expect_visible", selector="#empty"))
    workflow.store.checkpoint(rid, "plan", plan.model_dump())
    return workflow, rid, work, plan, browsers, baseline, intermediate


def comparison(contracts):
    return PreviewComparison(review=ReviewResult(approved=True, summary="All archived obligations preserved"),
        mappings=[ActionMapping(source_preview_hash=source["preview_hash"], previous_index=i,
                    current_indexes=[i], reason="Original action preserved")
                  for source in PreviewEvidence.comparison_sources(contracts)
                  for i in range(len(source["preview"]["actions"]))])


async def test_complete_generation_chain_preserves_baseline_and_each_distinct_successor(tmp_path, monkeypatch):
    workflow, rid, work, plan, _, baseline, intermediate = await chain_fixture(tmp_path, monkeypatch)
    contracts = PreviewEvidence(workflow).contracts(rid, plan)
    assert [len(row["preview"]["actions"]) for row in contracts["chain"]] == [3, 4, 5]
    assert contracts["chain"][0]["origin"] == "recovery/gate-history/2"
    assert contracts["chain"][0]["preview"] == baseline["preview"]
    assert contracts["chain"][1]["preview"] == intermediate["preview"]
    assert contracts["chain"][2]["preview"] == plan.preview.model_dump()
    assert contracts["previous"]["preview"] == intermediate["preview"]
    assert len(contracts["history"]) == 5
    for row in contracts["chain"]:
        assert row["preview_hash"] == digest(row["preview"])
        assert row["plan_hash"]
    for stage, extra in [("implement", {}), ("review", {"review_kind": "preview_contract"}),
                         ("recovery_diagnosis", {}), ("verify", {})]:
        view = ContextPlanner(workflow.artifacts).prepare(stage, {**extra, "preview_contracts": contracts}, work)
        assert view.payload["preview_contracts"]["chain"] == contracts["chain"]


@pytest.mark.parametrize("damage", ["oldest_missing", "source_missing", "wrong_source", "duplicate", "bad_target"])
async def test_comparison_cannot_hide_a_missing_version_or_mapping(tmp_path, monkeypatch, damage):
    workflow, rid, _, plan, _, _, _ = await chain_fixture(tmp_path, monkeypatch)
    contracts = PreviewEvidence(workflow).contracts(rid, plan)
    valid = comparison(contracts)
    assert PreviewEvidence.comparison_valid(valid, contracts)
    invalid = copy.deepcopy(valid)
    if damage == "oldest_missing":
        invalid.mappings = [row for row in invalid.mappings if row.source_preview_hash != contracts["chain"][0]["preview_hash"]]
    elif damage == "source_missing":
        for row in invalid.mappings:
            row.source_preview_hash = ""
    elif damage == "wrong_source":
        invalid.mappings[0].source_preview_hash = "unknown"
    elif damage == "duplicate":
        invalid.mappings[0] = copy.deepcopy(invalid.mappings[1])
    else:
        invalid.mappings[0].current_indexes = [len(contracts["current"]["preview"]["actions"])]
    assert not PreviewEvidence.comparison_valid(invalid, contracts)


@pytest.mark.parametrize("complete", [False, True])
async def test_full_chain_comparison_is_required_before_any_browser_actions(tmp_path, monkeypatch, complete):
    workflow, rid, work, plan, browsers, _, _ = await chain_fixture(tmp_path, monkeypatch)

    async def invoke(stage, context, *args, **kwargs):
        if stage == "decision":
            return DecisionResult(action="approve", selected_action="preview_recheck", evidence_refs=["preview_recheck"],
                reason="Compare all versions before execution", expected_result="Full isolated preview")
        assert stage == "review" and context["review_kind"] == "preview_contract"
        contracts = context["preview_contracts"]
        result = comparison(contracts)
        result.review = authority_review(context, result.review)
        if not complete:
            result.mappings = [row for row in result.mappings if row.source_preview_hash == contracts["previous"]["preview_hash"]]
        return result

    workflow.agent.invoke = invoke
    await workflow.recovery.resume(rid)
    assert len(browsers) == int(complete)
    active = workflow.store.get(rid, "recovery/active")
    if complete:
        assert active is None
        saved = workflow.store.get(rid, "develop/task/T1")
        assert saved["phase"] == "tested" and saved["preview_permit"]
    else:
        assert active["failure"]["kind"] == "contract"
        assert not workflow.store.get(rid, "preview-evidence/current/T1")


async def test_without_gate_archive_all_accepted_versions_remain_in_chain(tmp_path, monkeypatch):
    workflow, rid, work, plan, _, _, _, _, _ = await ready(tmp_path, monkeypatch)
    prior = plan.model_dump()
    workflow.store.checkpoint(rid, "plan_revision/history/7", {"plan": prior})
    plan.preview.actions.append(BrowserAction(action="expect_visible", selector="#empty"))
    contracts = PreviewEvidence(workflow).contracts(rid, plan)
    assert [len(row["preview"]["actions"]) for row in contracts["chain"]] == [3, 4, 5]
