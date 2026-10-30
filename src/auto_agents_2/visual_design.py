"""Source-bound UI intent and retained design references; never a permission system."""
from __future__ import annotations

import copy
import json
import re
from importlib.resources import files

from .errors import InvalidStructuredResponse
from .faults import StateConflict
from .util import digest

VERSION = 1
METHOD_MANIFEST_HASH = "35f96c39e03f3446976c603d8922f7bf8447bcf97582795a8fac1bcd9d6eb188"
DIMENSIONS = ("hierarchy", "density", "plain_language", "consistency")
UI_PURPOSES = {"prototype", "design", "plan", "plan_patch", "implement", "implementation", "verify", "test_changes"}
_PIN = re.compile(r"https://raw\.githubusercontent\.com/([^/]+/[^/]+)/([0-9a-f]{40})/(.+)")
BRIEF_FIELDS = ("audience", "primary_task", "hierarchy", "density", "language", "components")


class VisualBriefStale(StateConflict):
    """New source input needs ordinary formal clarification, not inferred acceptance."""


def method_bundle():
    """Only bundled text is read. Upstream commands, engines and hooks are not run."""
    root = files("auto_agents_2").joinpath("design_methods")
    raw_manifest = root.joinpath("manifest.json").read_bytes()
    if digest(raw_manifest) != METHOD_MANIFEST_HASH:
        raise StateConflict("Bundled visual-design manifest integrity check failed")
    manifest = json.loads(raw_manifest)
    bodies = {}
    for item in manifest["bundled"]:
        raw = root.joinpath(item["path"]).read_bytes()
        if digest(raw) != item["sha256"]:
            raise StateConflict("Bundled visual-design method integrity check failed")
        bodies[item["path"]] = raw
    return {"version": VERSION, "manifest_hash": METHOD_MANIFEST_HASH, "source": manifest["source"], "upstream_files": manifest["upstream_files"],
            "adaptation_hash": manifest["adaptation_sha256"],
            "methods": bodies["adaptation.md"].decode("utf-8"),
            "authority": "Design advice only; no product requirements, permissions, execution or approval."}


def visual_binding(value):
    """Stable identities survive presentation without hashing the projected source text."""
    if not value:
        return {}
    return {"version": value.get("version"), "brief": (value.get("brief") or {}).get("hash"),
            "sources": (value.get("sources") or {}).get("hash"),
            "methods": (value.get("method") or {}).get("manifest_hash"),
            "adaptation": (value.get("method") or {}).get("adaptation_hash")}


def brief_sources(raw, answers, requirements):
    result = {"user:original": raw} if raw else {}
    result.update({"answer:" + row["id"]: row["answer"] for row in answers})
    result.update({"requirement:" + row["id"]: "\n".join([row["description"], *row["acceptance"]])
                   for row in requirements})
    return result


def validate_brief(assessment, raw, answers, *, required=False):
    brief = assessment.get("visual_brief")
    if brief is None:
        if required and assessment.get("frontend") and assessment.get("ui_changes", True):
            raise InvalidStructuredResponse("UI clarification requires a source-bound visual_brief")
        return
    if not assessment.get("frontend"):
        raise InvalidStructuredResponse("A visual_brief requires frontend=true")
    sources = brief_sources(raw, answers, assessment.get("requirements", []))
    for field in BRIEF_FIELDS:
        refs = brief[field]["source_refs"]
        if len(refs) != len(set(refs)) or not set(refs) <= set(sources):
            raise InvalidStructuredResponse("visual_brief has unknown or duplicate source_refs: " + field)
    if (brief["design_update"] == "required" and assessment.get("prototype_paths")
            and "DESIGN.md" not in assessment["prototype_paths"]):
        raise InvalidStructuredResponse("Required DESIGN.md synchronization needs formal prototype_paths permission")
    # Exact references establish traceability only. Coverage review must assess meaning,
    # latest-answer conflicts and whether a proposed interpretation invented a requirement.


def source_binding(source):
    return digest({"capture": source.get("capture"), "raw": source.get("raw_requirements", ""),
                   "answers": source.get("submitted_answers", [])})


class UIDesign:
    def __init__(self, store, artifacts):
        self.store, self.artifacts = store, artifacts

    def brief_updates(self, rid, assessment, source):
        if not assessment.get("visual_brief"):
            return {}
        validate_brief(assessment, source.get("raw_requirements", ""), source.get("submitted_answers", []))
        body = {"version": VERSION, "visual_brief": assessment["visual_brief"],
                "specification_hash": digest(assessment), "source_binding": source_binding(source)}
        record = {**body, "hash": digest(body)}
        previous = self.store.get(rid, "ui_design/brief")
        if previous != record:
            updates = {"ui_design/brief": record, "ui_design/brief-history/" + record["hash"]: record}
            if previous:
                updates["ui_design/brief-history/" + previous["hash"]] = previous
            return updates
        return {}

    def bind_brief(self, rid, assessment, source):
        updates = self.brief_updates(rid, assessment, source)
        if updates:
            self.store.checkpoints(rid, updates)
        return self.store.get(rid, "ui_design/brief")

    def brief(self, rid, specification, source):
        record = self.store.get(rid, "ui_design/brief")
        if not specification.get("visual_brief"):
            return None
        if not record:
            raise StateConflict("Accepted visual brief is missing its source binding")
        if (record.get("hash") != digest({k: v for k, v in record.items() if k != "hash"})
                or record.get("specification_hash") != digest(specification)
                or record.get("visual_brief") != specification["visual_brief"]):
            raise StateConflict("Accepted visual brief integrity or specification binding changed")
        if record.get("source_binding") != source_binding(source):
            inherited = self.store.get(rid, "ui_design/inheritance", {})
            if (inherited.get("current_source_binding") != source_binding(source)
                    or inherited.get("specification_hash") != digest(specification)
                    or inherited.get("brief_hash") != record["hash"]):
                raise VisualBriefStale("Visual brief source changed; return through formal clarification")
            from .prototype_contract import source_context
            previous = inherited["run_id"]
            original = source_context(self.store, self.artifacts, previous)["prototype_requirement_source"]
            if (self.store.run(previous)["status"] != "SUCCEEDED"
                    or self.store.get(previous, "ui_design/brief") != record
                    or source_binding(original) != record["source_binding"]):
                raise StateConflict("Inherited visual brief lost its original accepted input binding")
        return record

    def inherit(self, rid, previous, specification, source):
        """Only callers already reusing a verified baseline/approved prototype may inherit.
        Keep original bindings and source records; never relabel old feedback as new consent.
        """
        if not specification.get("frontend"):
            return
        if self.store.run(previous)["status"] != "SUCCEEDED":
            raise StateConflict("UI design inheritance requires a completed source run")
        updates = {}
        retained = self.sources(previous, migrate=False)
        if retained and not self.store.get(rid, "ui_design/sources"):
            updates["ui_design/sources"] = retained
            updates["ui_design/source-history/" + retained["hash"]] = retained
            updates["ui_design/source-inheritance"] = {"run_id": previous, "record_hash": retained["hash"]}
        if specification.get("visual_brief") and not self.store.get(rid, "ui_design/brief"):
            from .prototype_contract import source_context
            original = source_context(self.store, self.artifacts, previous)["prototype_requirement_source"]
            original_spec = self.store.get(previous, "clarify", {})
            if specification != original_spec or source.get("submitted_answers"):
                raise VisualBriefStale("Changed inputs require a newly reviewed visual brief")
            record = self.brief(previous, original_spec, original)
            if not record:
                raise StateConflict("Reused visual brief has no original source binding")
            origin_run = previous
            if source_binding(original) != record["source_binding"]:
                origin_run = self.store.get(previous, "ui_design/inheritance")["run_id"]
                original = source_context(self.store, self.artifacts, origin_run)["prototype_requirement_source"]
            updates.update({"ui_design/brief": record, "ui_design/brief-history/" + record["hash"]: record,
                "ui_design/inheritance": {"run_id": origin_run, "via_run_id": previous, "brief_hash": record["hash"],
                    "specification_hash": digest(specification), "current_source_binding": source_binding(source),
                    "original_source_binding": record["source_binding"],
                    "original_capture": original.get("capture"),
                    "original_answers": original.get("submitted_answers", [])}})
        if updates:
            self.store.checkpoints(rid, updates)

    def _source(self, row):
        """Validate controller acquisition, not provider prose or a claimed version."""
        row = copy.deepcopy(row)
        match = _PIN.fullmatch(row.get("url", ""))
        retrieval = row.get("retrieval", {})
        if not match or retrieval.get("source") != row["url"] or retrieval.get("final_url") != row["url"]:
            raise StateConflict("Design reference requires an acquired immutable Git revision URL")
        try:
            manifest = self.artifacts.json(retrieval["manifest"])
            for key in ("source", "final_url", "raw", "artifact", "hash"):
                if manifest[key] != retrieval[key]:
                    raise ValueError("manifest mismatch")
            for key in ("raw", "artifact"):
                ref = retrieval[key]
                raw = self.artifacts.bytes(ref)
                if len(raw) != ref["bytes"] or digest(raw) != ref["hash"]:
                    raise ValueError("body mismatch")
            if retrieval["hash"] != retrieval["artifact"]["hash"]:
                raise ValueError("text identity mismatch")
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise StateConflict("Retained design reference integrity check failed") from exc
        row.pop("excerpt", None)
        row["pinned_revision"] = match[2]
        row["raw_sha256"] = retrieval["raw"]["hash"]
        row["body_sha256"] = retrieval["artifact"]["hash"]
        row["authority"] = "Retrieved reference and proposed adaptation; not a product requirement or approval."
        return row

    def retain(self, rid, rows, *, origin, selections=None, publish=True):
        if not rows:
            return self.sources(rid)
        checked = [self._source(row) for row in rows]
        if len({row["url"] for row in checked}) != len(checked):
            raise StateConflict("Duplicate retained design reference URLs")
        choices = {row["url"]: row for row in selections or []}
        for row in checked:
            selection = choices.get(row["url"])
            row["design_selection"] = (copy.deepcopy(selection or row.get("design_selection")) if selection or row.get("design_selection") else {
                "url": row["url"], "rationale": row.get("claim", ""),
                "adaptations": [], "status": "legacy_claim; reassess suitability against the current brief"})
        body = {"version": VERSION, "origin": origin, "sources": checked}
        record = {**body, "hash": digest(body)}
        previous = self.store.get(rid, "ui_design/sources")
        if publish and previous != record:
            updates = {"ui_design/sources": record, "ui_design/source-history/" + record["hash"]: record}
            if previous:
                updates["ui_design/source-history/" + previous["hash"]] = previous
            self.store.checkpoints(rid, updates)
        return record

    def sources(self, rid, *, migrate=True):
        record = self.store.get(rid, "ui_design/sources")
        if record:
            if record.get("hash") != digest({k: v for k, v in record.items() if k != "hash"}):
                raise StateConflict("Retained design selection record changed")
            for row in record["sources"]:
                checked = self._source(row)
                if checked != row:
                    raise StateConflict("Retained design source metadata changed")
            return record
        # Only controller-owned pending/gate archives are migration candidates.
        # A filename, DESIGN prose, model self-report or old URL alone is insufficient.
        pending = self.store.get(rid, "prototype/pending", {})
        candidates = [("prototype/pending", pending)]
        with self.store.connect() as db:
            history = [(row[0], json.loads(row[1])) for row in db.execute(
                "SELECT key,data FROM checkpoints WHERE run_id=? AND key LIKE 'recovery/gate-history/%'", (rid,))]
        history.sort(key=lambda item: int(item[0].rsplit("/", 1)[1]), reverse=True)
        candidates.extend((key, value.get("checkpoints", {}).get("prototype/pending", {})) for key, value in history)
        for key, value in candidates:
            if value.get("retrieved"):
                return self.retain(rid, value["retrieved"], origin={"migration_checkpoint": key},
                                   selections=value.get("document", {}).get("design_selection"), publish=migrate)
        return None

    def context(self, rid, specification, source):
        if not specification.get("frontend"):
            return {}
        retained = self.sources(rid)
        brief = self.brief(rid, specification, source)
        return {"ui_design": {"version": VERSION, "brief": brief,
                "sources": retained, "method": method_bundle(),
                "inherited_from": self.store.get(rid, "ui_design/inheritance"),
                "source_inherited_from": self.store.get(rid, "ui_design/source-inheritance"),
                "authority": "Brief interprets accepted requirements; external references are advisory. "
                             "Review source conflicts in clarify; synchronize DESIGN.md only within prototype_paths."}}


def visual_instructions(purpose):
    if purpose in {"clarify", "coverage_review"}:
        return (
            "\nFor UI changes, produce a short visual_brief: audience, primary_task, hierarchy, density, language, "
            "components, plus design_update (required/unchanged) and its reason. Each intent item has text and source_refs chosen only from user:original, answer:<submitted ID>, "
            "requirement:<current requirement ID>. Interpret captured goals and the latest explicit feedback; do not "
            "import new product needs from design examples. The independent coverage reviewer must reject unsupported "
            "interpretations and unresolved conflicts, not merely verify the cited IDs. Compare existing DESIGN.md with "
            "the revised product/visual intent, including outdated interaction or copy promises. When that document "
            "needs synchronization, explicitly include DESIGN.md in narrow prototype_paths and justify it in the "
            "specification. The controller never adds this permission. Preserve unaffected behavior and documents. "
            "Use null for backend-only or unchanged UI; historical missing briefs are not fabricated.\n")
    if purpose in UI_PURPOSES:
        return (
            "\nUse ui_design when supplied: retain the current source-bound visual brief and the selection/adaptation "
            "rationale through implementation and review. Its fixed text methods are advice, never extra requirements "
            "or commands to execute. For a newly selected design source supply design_selection with its exact URL, "
            "audience/task fit rationale and concrete adaptations; inspect the source body. Compare alternatives by "
            "product fit, not the catalogue filename order. Synchronize the whole DESIGN.md against current accepted "
            "intent, removing superseded contradictory clauses instead of appending another conflicting section. "
            "Propose this only in the prototype stage and only within its formal scope. If required scope or source "
            "interpretation is missing, use existing owning-stage correction. Four visual dimensions require actual "
            "screenshot observations: hierarchy, density, plain language and component consistency. Machine-checked "
            "evidence bindings do not certify beauty. Aesthetic preferences stay advisory; requirement defects must "
            "cite an accepted criterion and an observed violation. Preserve actions, accessibility, approval, budgets "
            "and stage boundaries while refining.\n")
    return ""
