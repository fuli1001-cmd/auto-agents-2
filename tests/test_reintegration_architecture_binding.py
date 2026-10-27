"""Real Git target advance plus architecture drift must bind a new final review.

The current contract permits fresh independent verification of the merged state;
it does not require every unverified architecture receipt to reopen design.
"""
import copy
from pathlib import Path

import pytest
from test_decisions import ApprovingAgent
from test_delivery import commit, fixture
from test_workflow import engine

from auto_agents_2.util import digest
from auto_agents_2.workspace import git, tree_hash

ARCHITECTURE = "docs/aa2/design-integration.md"
ORIGINAL = "# Addition integration\nKeep the public add(a, b) interface.\n"
INCOMING = ORIGINAL + "Keep this concurrent integration note when delivering.\n"


class ArchitectureAdvancingAgent(ApprovingAgent):
    def __init__(self, project, stale):
        super().__init__()
        self.project = project
        self.stale = stale
        self.original_review = None
        self.original_context = None
        self.reintegrated_contexts = []
        self.incoming_commit = None

    async def invoke(self, stage, context, schema, work, rid, operation, fresh=False):
        if operation.startswith("reintegration-review/"):
            assert fresh
            assert (work / ARCHITECTURE).read_text() == INCOMING
            self.reintegrated_contexts.append(copy.deepcopy(context))
            basis = context["controller_review_basis"]
            old_basis = self.original_context["controller_review_basis"]
            assert basis["source_hash"] == tree_hash(work) != old_basis["source_hash"]
            assert basis["hash"] != old_basis["hash"]
            assert basis["candidate_hash"] != old_basis["candidate_hash"]
            design = context["design_review"]
            assert design["status"] == "unverified"
            assert design["current_documents"][ARCHITECTURE]["hash"] == digest(INCOMING.encode())
            assert design["records"] == []  # Do not call the old document receipt current approval.
            assert context["incoming"] == self.incoming_commit
            assert context["evidence"]
            assert all(row["source_hash"] == basis["source_hash"] and row["exit_code"] == 0
                       and not row["failure_reasons"] for row in context["evidence"])
            assert {row["command_hash"] for row in context["evidence"]} == {
                row["command_hash"] for row in self.original_context["evidence"]}
            if self.stale:
                # A well-formed, previously approved response, now for the wrong
                # source and evidence. Production authority validation must reject it.
                return schema.model_validate(copy.deepcopy(self.original_review))
        result = await super().invoke(stage, context, schema, work, rid, operation, fresh)
        if operation == "final-review" and self.original_review is None:
            assert context["design_review"]["status"] == "approved"
            assert context["design_review"]["documents"][ARCHITECTURE]["hash"] == digest(ORIGINAL.encode())
            self.original_context = copy.deepcopy(context)
            self.original_review = result.model_dump()
            (self.project / ARCHITECTURE).write_text(INCOMING)
            self.incoming_commit = commit(self.project, "Concurrent architecture note")
        return result


@pytest.mark.parametrize("stale", [False, True], ids=["current-review", "replayed-old-review"])
async def test_architecture_change_and_target_advance_require_current_reintegration_review(tmp_path, stale):
    project, request = fixture(tmp_path)
    agent = ArchitectureAdvancingAgent(project, stale)
    workflow = engine(project, agent)
    (project / ARCHITECTURE).parent.mkdir(parents=True)
    (project / ARCHITECTURE).write_text(ORIGINAL)
    baseline = commit(project, "Original architecture note")

    result = await workflow.start(str(request))
    rid = result["id"]
    assert workflow.workspaces.is_ancestor(baseline, agent.incoming_commit)
    assert (project / ARCHITECTURE).read_text() == INCOMING
    assert sum(stage == "implement" for stage, _ in agent.calls) == 1
    assert workflow.store.get(rid, "rounds/T1")["round"] == 1
    assert result["data"]["replans"] == 0
    assert workflow.store.get(rid, "task_state/T1")["implementation"] == "accepted"
    invalid = [e for e in workflow.store.events(rid) if e["kind"] == "review.authority_invalid"]

    if stale:
        assert result["status"] == "PAUSED"
        assert len(agent.reintegrated_contexts) == len(invalid) == 2
        assert {e["data"]["code"] for e in invalid} == {"stale_review_binding"}
        assert workflow.store.get(rid, "recovery/path-exhausted")
        assert workflow.store.get(rid, "delivery/receipt") is None
        assert git(project, "rev-parse", "HEAD") == agent.incoming_commit
        assert not (project / "calc.py").exists()  # No candidate was published.
        journal = workflow.store.get(rid, "delivery/journal")
        assert journal["phase"] == "reintegrating" and journal["reintegrations"] == 1
        retained_work = Path(journal["work"])
        assert retained_work.is_dir()
        retained_head = git(retained_work, "rev-parse", "HEAD")
        assert git(retained_work, "cat-file", "-t", retained_head) == "commit"
        assert workflow.workspaces.is_ancestor(journal["candidate"], retained_head)
        assert workflow.workspaces.is_ancestor(agent.incoming_commit, retained_head)
        assert (retained_work / "calc.py").is_file()
        assert (retained_work / ARCHITECTURE).read_text() == INCOMING
        assert workflow.store.get(rid, "verify/review") == agent.original_review
    else:
        assert result["status"] == "SUCCEEDED" and result["data"]["outcome"] == "DELIVERED"
        assert len(agent.reintegrated_contexts) == 1 and not invalid
        receipt = workflow.store.get(rid, "delivery/receipt")
        assert receipt["reintegrations"] == 1
        assert workflow.workspaces.is_ancestor(agent.incoming_commit, receipt["candidate"])
        assert git(project, "rev-parse", "HEAD") == receipt["candidate"]
        new_review = workflow.store.get(rid, "verify/review")
        basis = agent.reintegrated_contexts[0]["controller_review_basis"]
        assert new_review["authority"]["basis_hash"] == basis["hash"]
        assert new_review["authority"]["basis_hash"] != agent.original_review["authority"]["basis_hash"]
        assert receipt["evidence"]["verify/review"] == new_review
        assert (project / "calc.py").is_file()
        assert not git(project, "status", "--porcelain")
