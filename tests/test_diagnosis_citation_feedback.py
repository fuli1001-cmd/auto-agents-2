"""Recovery feedback names every invalid citation without changing admission."""
import copy

import pytest
from test_decisions import ApprovingAgent
from test_recovery_contracts import failure, plan
from test_recovery_policy import ready

from auto_agents_2.errors import AA2Error
from auto_agents_2.recovery import Diagnosis, RepairTarget, adjudicate


def scoped_case():
    contract = plan()
    record = failure(evidence_refs=["part1.py", "part2.py", "part3.py"],
                     evidence={"task": contract.tasks[2].model_dump()})
    context = {"failure": record.model_dump(), "plan": contract.model_dump()}
    proposal = Diagnosis(action="repair", task_ids=["T1", "T2"],
        evidence_refs=["failure.evidence"], reason="Repair the existing owned files",
        repair_targets=[
            RepairTarget(task_id="T1", paths=["part1.py"], evidence_refs=["part1.py"],
                         reason="Current existing source"),
            RepairTarget(task_id="T2", paths=["part2.py"], evidence_refs=["part2.py"],
                         reason="Current existing source"),
        ])
    return contract, record, context, proposal


@pytest.mark.parametrize("bad_top", [False, True])
def test_reports_all_invalid_target_citations_even_when_factual_refs_exist(bad_top):
    contract, record, context, proposal = scoped_case()
    proposal.repair_targets[0].evidence_refs += ["plan.task_graph[0]"]
    proposal.repair_targets[1].evidence_refs += ["plan.task_graph[1]"]
    if bad_top:
        proposal.evidence_refs += ["project.read_on_demand"]
    before = copy.deepcopy((record.model_dump(), context, proposal.model_dump()))
    with pytest.raises(AA2Error) as caught:
        adjudicate(record, diagnosis=proposal, plan=contract, evidence_context=context)
    message = str(caught.value)
    assert "repair_targets[0].evidence_refs[1]" in message
    assert "repair_targets[1].evidence_refs[1]" in message
    if bad_top:
        assert "invalid evidence_refs indexes: 1" in message
    assert "source_ref" in message and "material_id" in message
    assert "proposals or protocols alone are insufficient" not in message
    assert (record.model_dump(), context, proposal.model_dump()) == before


def test_invalid_reference_diagnostic_does_not_echo_model_supplied_literal():
    contract, record, context, proposal = scoped_case()
    marker = "UNTRUSTED_DIAGNOSTIC_LITERAL_DO_NOT_ECHO"
    proposal.repair_targets[0].evidence_refs += [marker]
    with pytest.raises(AA2Error) as caught:
        adjudicate(record, diagnosis=proposal, plan=contract, evidence_context=context)
    assert "repair_targets[0].evidence_refs[1]" in str(caught.value)
    assert marker not in str(caught.value)


def test_resolvable_proposals_still_cannot_establish_repair_facts():
    contract, record, context, proposal = scoped_case()
    context["failure"]["proposal"] = {"suggestion": "unverified"}
    proposal.repair_targets[0].evidence_refs = ["failure.proposal.suggestion"]
    with pytest.raises(AA2Error, match="requires supplied factual evidence_refs"):
        adjudicate(record, diagnosis=proposal, plan=contract, evidence_context=context)


def test_valid_citations_leave_scope_checks_and_accepted_decision_unchanged():
    contract, record, context, proposal = scoped_case()
    decision = adjudicate(record, diagnosis=proposal, plan=contract, evidence_context=context)
    assert decision.action == "repair"
    assert decision.task_ids == ["T1", "T2"]
    assert decision.repair_targets == proposal.repair_targets
    proposal.repair_targets[0].paths = ["outside.py"]
    with pytest.raises(AA2Error, match="not a public literal path"):
        adjudicate(record, diagnosis=proposal, plan=contract, evidence_context=context)


async def test_complete_rejection_feedback_survives_until_normal_scoped_recovery(tmp_path):
    class CorrectingAgent(ApprovingAgent):
        def __init__(self):
            super().__init__()
            self.diagnoses = []
            self.first = Diagnosis(action="repair", task_ids=["T3"],
                evidence_refs=["failure.evidence", "plan.full_plan"], reason="Read current contract",
                repair_targets=[RepairTarget(task_id="T3", paths=["part3.py"],
                    evidence_refs=["part3.py", "plan.task_graph[2]"], reason="Current owned source")])

        async def invoke(self, stage, context, schema, *args, **kwargs):
            if stage != "recovery_diagnosis":
                return await super().invoke(stage, context, schema, *args, **kwargs)
            self.diagnoses.append(copy.deepcopy(context))
            if len(self.diagnoses) == 1:
                return self.first
            feedback = context["diagnosis_feedback"]
            assert feedback["diagnosis"] == self.first.model_dump()
            assert "invalid evidence_refs indexes: 1" in feedback["validation_error"]
            assert "repair_targets[0].evidence_refs[1]" in feedback["validation_error"]
            return Diagnosis(action="repair", task_ids=["T3"], evidence_refs=["failure.evidence"],
                             reason="Corrected citation under the same task scope")

    agent = CorrectingAgent()
    contract = plan()
    record = failure(evidence_refs=["part3.py"], evidence={"task": contract.tasks[2].model_dump()})
    workflow, rid, _, _, record = ready(tmp_path, agent, record)
    workflow.store.checkpoint(rid, "rounds/T3", {"round": 20})
    await workflow.recovery.resume(rid)
    rejection_key = f"recovery/diagnosis-rejections/{record.id}/1"
    rejection = workflow.store.get(rid, rejection_key)
    assert rejection["diagnosis"] == agent.first.model_dump()
    assert workflow.store.get(rid, "recovery/active")["diagnosis_errors"] == 1
    assert workflow.store.get(rid, "develop/task/T3") is None
    assert not workflow.store.pending(rid)
    await workflow.recovery.resume(rid)
    assert len(agent.diagnoses) == 2
    assert workflow.store.get(rid, rejection_key) == rejection
    assert workflow.store.get(rid, "recovery/budgets/diagnosis/T3")["round"] == 2
    assert workflow.store.get(rid, "rounds/T3")["round"] == 20
    assert workflow.store.get(rid, "recovery/active") is None
    completed = workflow.store.get(rid, f"recovery/completed/{record.id}/1")
    assert completed["decision"]["action"] == "repair"
    assert workflow.store.get(rid, "develop/task/T3")["phase"] == "rejected"
    assert "T3" not in workflow.store.get(rid, "develop/done", [])
