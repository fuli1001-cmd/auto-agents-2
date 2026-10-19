"""Durable application and review of architecture documents, owned by the design gate."""
from __future__ import annotations

from .errors import PolicyViolation
from .faults import StateConflict
from .models import Change, DocumentResult, ReviewResult
from .util import digest
from .workspace import apply_prepared, path_allowed, prepared_before_hash, tree_hash, write_path

DESIGN_SCOPES = ["docs/aa2/design.md", "docs/aa2/design-*.md", "docs/aa2/design"]


class DesignDocuments:
    def __init__(self, engine):
        self.engine = engine

    def verify(self, work, pending):
        for item in pending["prepared"]:
            path, _ = write_path(work, item["path"])
            current = digest(path.read_bytes()) if path.is_file() else None
            if current != item["after"] or path.exists() and path.stat().st_mode & 0o777 != item["mode"]:
                raise StateConflict("Design document changed after review; preserve the candidate and user edits")

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
        if "review" not in pending:
            try:
                review = await engine._invoke("review", {**context, "review_kind": "design",
                    "design": document.model_dump(), "project": engine.contexts.build(work)},
                    ReviewResult, work, rid, "design/review", fresh=True)
            except PolicyViolation as exc:
                if tree_hash(work) != pending["source_hash"]:
                    raise StateConflict("Design inputs changed during review; preserve the unapplied review") from exc
                raise
            if tree_hash(work) != pending["source_hash"]:
                raise StateConflict("Design inputs changed during review; preserve the unapplied review")
            pending["review"] = review.model_dump()
            engine.store.checkpoint(rid, key, pending)
        return document, ReviewResult.model_validate(pending["review"])
