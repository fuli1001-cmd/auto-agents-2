"""Durable application and review of architecture documents, owned by the design gate."""
from __future__ import annotations

import json

from .errors import PolicyViolation
from .faults import StateConflict
from .models import Change, DocumentResult, Record, ReviewResult, UserQuestion
from .readview import public_files
from .source_history import SourceHistory
from .util import digest
from .workspace import apply_prepared, path_allowed, prepared_before_hash, tree_hash, write_path

DESIGN_SCOPES = ["docs/aa2/design.md", "docs/aa2/design-*.md", "docs/aa2/design"]


class DesignQuestions(Record):
    summary: str
    questions: list[str | UserQuestion] = []


class DesignDocuments:
    def __init__(self, engine):
        self.engine = engine

    def verify(self, work, pending):
        for item in pending["prepared"]:
            path, _ = write_path(work, item["path"])
            current = digest(path.read_bytes()) if path.is_file() else None
            if current != item["after"] or path.exists() and path.stat().st_mode & 0o777 != item["mode"]:
                raise StateConflict("Design document changed after review; preserve the candidate and user edits")
        if pending.get("review_documents") is not None and self.manifest(work) != pending["review_documents"]:
            raise StateConflict("Design document set changed after review; preserve the candidate and user edits")

    def manifest(self, work):
        return {path.relative_to(work).as_posix(): {"hash": digest(path.read_bytes())}
            for path in public_files(work, self.engine.config.load().sensitive_paths)
            if path.suffix == ".md" and path_allowed(path.relative_to(work).as_posix(), DESIGN_SCOPES)}

    def evidence(self, rid, work):
        """Expose actual matching decisions, including their original archive and raw artifact."""
        store = self.engine.store
        documents = self.manifest(work)
        candidates = [("candidate/design", {"candidate/design": store.get(rid, "candidate/design", {}),
            "invocation/design/review": store.get(rid, "invocation/design/review", {})}),
            ("design/review-receipt", {"candidate/design": store.get(rid, "design/review-receipt", {})})]
        with store.connect() as db:
            histories = db.execute("SELECT key,data FROM checkpoints WHERE run_id=? AND key LIKE "
                                   "'recovery/gate-history/%' ORDER BY rowid DESC", (rid,)).fetchall()
        candidates.extend((key, json.loads(raw).get("checkpoints", {})) for key, raw in histories)
        records = []
        for origin, checkpoints in candidates:
            pending = checkpoints.get("candidate/design") or checkpoints.get("design/review-receipt", {})
            invocation = checkpoints.get("invocation/design/review", {})
            if not pending.get("review") or not pending.get("source_hash"):
                continue
            binding = pending.get("review_documents")
            if binding is None:
                binding = {p["path"]: {"hash": p["after"]}
                           for p in pending.get("prepared", []) if p.get("after")}
            if not documents or binding != documents:
                continue
            ref = pending.get("review_artifact") or invocation.get("artifact")
            if not ref or ref.get("kind") != "review":
                continue
            try:
                raw = self.engine.artifacts.bytes(ref)
                if len(raw) != ref.get("bytes"):
                    continue
                raw_review = json.loads(raw)
                review = ReviewResult.model_validate(raw_review)
            except (OSError, ValueError):
                continue
            if (raw_review != pending["review"] or not pending.get("review_artifact")
                    and invocation.get("result") != raw_review):
                continue
            records.append({"origin": origin, "operation": "design/review", "documents": binding,
                "reviewed_source_hash": pending["source_hash"], "independent_review": review.model_dump(),
                "review_artifact": ref,
                "specification_hash": pending.get("review_specification"),
                "scope": "Original architecture decision matching these document bytes; not task/test acceptance."})
        return {"current_documents": documents, "records": records,
                "status": "matching_decisions" if records else "no_matching_decision"}

    def context(self, rid, work):
        evidence = self.evidence(rid, work)
        spec = digest(self.engine.store.get(rid, "clarify"))
        pending = self.engine.store.get(rid, "candidate/design", {})
        current = evidence["records"][0] if evidence["records"] else None
        if (not current or current["origin"] not in {"candidate/design", "design/review-receipt"}
                or current["specification_hash"] != spec
                or pending.get("review") and current["origin"] != "candidate/design"):
            return {**evidence, "status": "unverified"}
        return {**current, "status": "approved" if current["independent_review"]["approved"] else "rejected"}

    async def refresh_questions(self, rid, work, document, context):
        """Give the author controller evidence before forwarding genuine product questions."""
        engine, key = self.engine, "candidate/design"
        pending = engine.store.get(rid, key, {})
        if not document.questions or pending.get("questions_refreshed") or "prepared" in pending:
            return document
        fingerprint, source = digest(document.model_dump()), tree_hash(work)
        if pending.get("proposal_source_hash", source) != source:
            raise StateConflict("Design question inputs changed; preserve the original candidate")
        keys = [f"design/{i}" for i in range(len(document.questions))]
        questions = engine.store.decision_questions(rid, keys)
        if any(q["answer"] is not None and q["artifact_hash"] == fingerprint for q in questions):
            return document  # A recorded human answer keeps priority and its original meaning.
        operation = "design/question-evidence/" + digest([fingerprint, source])
        resolution = await engine._invoke("design", {**context,
            "design_review_evidence": self.evidence(rid, work),
            "source_history": SourceHistory(engine).collect_protected(rid, work),
            "controller_feedback": {"previous_proposal": document.model_dump(),
                "instruction": "This request only reconciles questions: return summary and questions under the supplied "
                    "schema. The controller preserves the original document and proposed files byte for byte. "
                    "Reassess every question with the supplied original architecture decisions and public "
                    "source history. Controller review is performed AFTER the design candidate is submitted/applied. "
                    "If no matching decision exists, submit the current design for that review; do not make a future "
                    "controller action a user question or prerequisite for submitting it. Do not claim approval. "
                    "Keep genuine product choices and missing user information as questions. Preserve all obligations "
                    "and unresolved findings; historical comparisons still require independent test-change review. "
                    "Do not ask the user to retrieve internal receipts, Git snapshots, reports or test baselines."}},
            DesignQuestions, work, rid, operation, fresh=True)
        if tree_hash(work) != source:
            raise StateConflict("Design inputs changed during question reconciliation")
        refreshed = document.model_copy(update={"questions": resolution.questions})
        updated = {**pending, "document": refreshed.model_dump(), "questions_refreshed": fingerprint}
        record = {"source_hash": source, "original": document.model_dump(), "refreshed": refreshed.model_dump(),
                  "resolution": resolution.model_dump()}
        engine.store.refresh_design_questions(rid, pending, updated, keys, questions,
            "recovery/design-questions/" + digest([fingerprint, source]), record)
        return refreshed

    async def prepare(self, rid, work, document, context):
        engine = self.engine
        key = "candidate/design"
        pending = engine.store.get(rid, key, {})
        if "prepared" not in pending:
            if pending.get("proposal_source_hash", tree_hash(work)) != tree_hash(work):
                raise StateConflict("Design inputs changed after proposal generation; preserve user edits")
            changes = list(document.files)
            paths = []
            for change in changes:
                _, path = write_path(work, change.path)
                if not path_allowed(path, DESIGN_SCOPES) or not path.endswith(".md"):
                    raise PolicyViolation("Design files must be architecture Markdown under "
                                          "docs/aa2/design.md, design-*.md or design/: " + change.path)
                if path == "docs/aa2/design.md" and change.operation == "delete":
                    raise PolicyViolation("The design entry document cannot be deleted")
                paths.append(path)
            # An explicit entry-file proposal wins. Never overwrite it with the
            # separate document field after applying its replacements.
            if "docs/aa2/design.md" not in paths:
                changes.append(Change(path="docs/aa2/design.md", content=document.document))
            before = tree_hash(work)
            corrected, prepared = await engine.patch_repair.prepare(rid, work, changes, DESIGN_SCOPES,
                stage="design", context=context, operation="design/documents", contracts=True)
            if tree_hash(work) != before:
                raise StateConflict("Design inputs changed during proposal preparation; preserve user edits")
            pending.update(original=document.model_dump(), document=document.model_copy(
                update={"files": corrected[:len(document.files)]}).model_dump(), prepared=prepared, before=before)
            engine.store.checkpoint(rid, key, pending)
        if "source_hash" not in pending:
            if prepared_before_hash(work, pending["prepared"]) != pending["before"]:
                raise StateConflict("Design inputs changed before application; preserve user edits")
            try:
                from .stage_responsibilities import validate_stage_writes
                validate_stage_writes("design", pending["prepared"], DESIGN_SCOPES)
                apply_prepared(work, pending["prepared"])
            except PolicyViolation as exc:
                raise StateConflict("Design candidate changed during application; preserve user edits") from exc
            pending["document"]["document"] = (work / "docs/aa2/design.md").read_text()
            pending["source_hash"] = tree_hash(work)
            engine.store.checkpoint(rid, key, pending)
        if tree_hash(work) != pending["source_hash"]:
            raise StateConflict("Design inputs changed during review; preserve the candidate and user edits")
        self.verify(work, pending)
        document = DocumentResult.model_validate(pending["document"])
        current_specification = engine.store.get(rid, "clarify")
        specification = digest(current_specification)
        if "review" in pending and ("review_documents" not in pending or pending.get("review_specification") != specification):
            engine.store.checkpoint(rid, "recovery/design-review-history/" + digest(pending), pending)
            pending.pop("review")
            pending.pop("review_artifact", None)
        if "review" not in pending:
            pending.update(review_documents=self.manifest(work), review_specification=specification)
            engine.store.checkpoint(rid, key, pending)
        if "review" not in pending:
            try:
                review = await engine._invoke("review", {**context, "review_kind": "design",
                    "specification": current_specification if current_specification is not None else context.get("specification", {}),
                    "design": document.model_dump(), "design_documents": pending["review_documents"],
                    "project": engine.contexts.build(work)},
                    ReviewResult, work, rid, "design/review", fresh=True)
            except PolicyViolation as exc:
                if tree_hash(work) != pending["source_hash"]:
                    raise StateConflict("Design inputs changed during review; preserve the unapplied review") from exc
                raise
            if tree_hash(work) != pending["source_hash"]:
                raise StateConflict("Design inputs changed during review; preserve the unapplied review")
            pending["review"] = review.model_dump()
            pending["review_artifact"] = engine.artifacts.put(review.model_dump(), "review")
            engine.store.checkpoint(rid, key, pending)
        review = ReviewResult.model_validate(pending["review"])
        if self.context(rid, work)["status"] != ("approved" if review.approved else "rejected"):
            raise StateConflict("Design review artifact or document binding is unavailable; preserve the candidate")
        engine.store.checkpoint(rid, "design/review-receipt", {k: pending[k] for k in (
            "source_hash", "review_documents", "review_specification", "review", "review_artifact")})
        return document, review
