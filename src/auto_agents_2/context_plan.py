"""Purpose-specific model views of immutable controller facts.

Selection is presentation, not authorization, evidence verification or garbage collection.
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
from dataclasses import dataclass, field
from urllib.parse import urldefrag

from .context import ContextBuilder
from .diagnostics import references
from .errors import InvalidStructuredResponse, ProviderError
from .research_sections import extract_sections, select_section
from .util import digest
from .workspace import git_environment, path_allowed

CONTEXT_VERSION = "context-v1"
TEXT_INLINE = 12000
RESEARCH_INLINE = 16000
LINKED_INLINE = 4000

# Known workflow slots must be explicitly admitted for each purpose. Unknown
# extension fields remain mandatory; a new purpose must always be registered.
SHARED = {"task", "mandatory", "constraints", "global_constraints", "prototype_approval", "feedback",
          "controller_feedback", "evidence_reference_contract", "validation_feedback"}
CONTRACTS = {
    "clarify": {"raw_requirements", "raw_requirements_truncated", "history", "project", "decisions",
                "review_feedback", "additional_evidence", "regression_evidence", "coverage_verification"},
    "coverage_review": {"raw_requirements", "raw_requirements_truncated", "history", "project", "decisions",
                        "assessment", "review_feedback", "coverage_verification"},
    "provider_research": {"specification", "project", "provider_research_answer"},
    "research": {"research", "retrieved_sources"},
    "prototype": {"specification", "project", "design_baseline", "design_candidates", "prototype_answer",
                  "controller_browser_report", "previous_prototype", "prototype", "design_evidence", "browser_evidence"},
    "design": {"specification", "project", "provider_research", "prototype", "design_answer", "design"},
    "plan": {"specification", "project", "design", "provider_research", "prototype", "plan",
             "planning_mode", "coverage_verification", "task_origins", "task_budgets", "task_origin_validation",
             "unresolved_issues", "plan_answer"},
    "plan_patch": {"plan_patch", "base_revision", "plan", "previous_plan", "patch", "failure", "task_origins",
                   "specification", "project", "retained_candidates", "affected", "reopen"},
    "implement": {"specification", "project", "design", "verification_contract", "execution_contract", "unresolved_issues"},
    "implementation": {"specification", "project", "design", "verification_contract", "execution_contract",
                       "unresolved_issues", "implementation", "checks", "source_hash"},
    "test_changes": {"specification", "project", "design", "execution_contract", "verification_contract",
                     "unresolved_issues", "candidate_changes", "purpose"},
    "readme": {"specification", "project", "plan"},
    "verify": {"specification", "project", "plan", "design", "evidence", "verification_contract",
               "implemented_visuals", "approved_prototype_visuals", "incoming", "unresolved_issues"},
    "recovery_diagnosis": {"failure", "plan", "project", "specification", "design", "task_budgets",
                           "diagnosis_feedback", "current_issues", "observations", "delivery_journal", "constraint"},
    "decision": {"decision_request"},
    "patch_repair": {"patch_repair", "project", "execution_contract", "specification"},
    "material_probe": {"context_files", "screenshot"},
    "provider_probe": {"task"},
}
KNOWN = set().union(*CONTRACTS.values(), SHARED, {"context_files", "review_kind", "readme", "selected_design_sources"})


def purpose_for(stage, context, operation=""):
    if stage not in {"clarify", "coverage_review", "provider_research", "prototype", "design", "plan",
                     "implement", "review", "verify", "readme", "recovery_diagnosis", "decision", "material_probe"}:
        raise ProviderError("Unregistered model invocation stage")
    purpose = ("provider_probe" if operation == "doctor" else "patch_repair" if context.get("patch_repair") else
               "plan_patch" if context.get("plan_patch") else context.get("review_kind", "implementation") if stage == "review" else stage)
    if purpose not in CONTRACTS:
        raise ProviderError("Unregistered model invocation purpose")
    return purpose


@dataclass
class PreparedContext:
    payload: dict
    purpose: str
    selections: list[dict] = field(default_factory=list)
    reference_map: dict[str, str] = field(default_factory=dict)
    version: str = CONTEXT_VERSION

    def record(self):
        return {"version": self.version, "purpose": self.purpose, "payload": self.payload,
                "selections": self.selections, "reference_map": self.reference_map}


class ContextPlanner:
    def __init__(self, artifacts, sensitive_paths=()):
        self.artifacts = artifacts
        self.sources = ContextBuilder(sensitive_paths)

    def prepare(self, stage, context, workspace, operation="", *, run_id=None):
        return _Selection(self.artifacts, self.sources, stage, context, workspace, operation, run_id).prepare()


class _Selection:
    def __init__(self, artifacts, sources, stage, context, workspace, operation, run_id):
        self.artifacts, self.sources = artifacts, sources
        self.stage, self.original, self.workspace = stage, context, workspace
        self.purpose = purpose_for(stage, context, operation)
        self.result = PreparedContext({}, self.purpose)
        self.linked_remaining = LINKED_INLINE
        self.task = context.get("task") if isinstance(context.get("task"), dict) else {}
        if not self.task and isinstance(context.get("failure"), dict):
            failure = context["failure"]
            self.task = {"id": failure.get("task_id"), "origin_id": failure.get("origin_id"),
                         "allowed_paths": failure.get("evidence", {}).get("allowed_paths", [])}
        self.scopes = self.task.get("allowed_paths", [])
        self.owners = {v for v in [self.task.get("id"), self.task.get("origin_id"), *self.task.get("depends_on", [])] if v}
        self.requirement_owners = {}
        if run_id and self.task:
            revision = context.get("execution_contract", {}).get("plan_revision")
            current = artifacts.store.get(run_id, "plan_revision/current", {}).get("revision")
            if revision is not None and revision == current:
                for task in (artifacts.store.get(run_id, "plan", {}) or {}).get("tasks", []):
                    for rid in task.get("requirement_ids", []):
                        self.requirement_owners.setdefault(rid, set()).add(task["id"])

    def mark(self, path, presentation, reason, value):
        self.result.selections.append({"source_ref": path, "presentation": presentation, "reason": reason,
            "characters": len(value) if isinstance(value, str) else len(json.dumps(value, ensure_ascii=False)),
            "identity": digest(value)})

    def reference(self, value, path, reason, *, required=False):
        ref = self.artifacts.put(value, "context_material")
        key = "material-" + digest([path, ref["hash"]])[:20]
        self.result.reference_map[key] = path
        previous = self.result.reference_map.get(ref["hash"], path)
        self.result.reference_map[ref["hash"]] = path if previous == path else ""
        self.mark(path, "required_read" if required else "read_on_demand", reason, value)
        return {"material_id": key, "source_ref": path, "artifact": ref, "required_read": required,
                "reason": reason}

    def compact(self, value, path):
        if isinstance(value, str):
            if len(value) > TEXT_INLINE:
                return self.reference(value, path, "Complete necessary text exceeds inline allocation", required=True)
            return value
        if isinstance(value, list):
            return [self.compact(v, f"{path}[{i}]") for i, v in enumerate(value)]
        if not isinstance(value, dict):
            return value
        if {"hash", "kind", "bytes"} <= value.keys():
            return copy.deepcopy(value)
        if {"exit_code", "source_hash", "environment_hash", "argv"} <= value.keys():
            return self.check(value, path)
        out = {}
        for key, child in value.items():
            target = f"{path}.{key}"
            if key in {"test_ids", "passed_test_ids", "observations", "diagnostic_evidence"} and child:
                out[key] = self.reference(child, target, "Detailed evidence history; current observation is supplied")
            elif key == "output" and child and value.get("logs"):
                out[key] = {"availability": "in_logs", "reason": "Read the referenced logs when diagnostic detail is needed"}
            else:
                out[key] = self.compact(child, target)
        return out

    def check(self, value, path):
        detailed = {"output", "test_ids", "passed_test_ids", "attachment_capture"}
        out = {k: self.compact(v, f"{path}.{k}") for k, v in value.items() if k not in detailed}
        all_ids, passed = value.get("test_ids", []), set(value.get("passed_test_ids", []))
        failed = [case for case in all_ids if case not in passed]
        out.update(total_tests=len(all_ids), unpassed_test_ids=failed,
                   source_ref=path, observation=self.reference(value, path, "Complete immutable check observation"))
        if len(json.dumps(failed, ensure_ascii=False)) > TEXT_INLINE:
            out["unpassed_test_ids"] = self.reference(failed, path + ".unpassed_test_ids", "All unpassed cases", required=True)
            # This list is derived; its evidence source is the original observation.
            for alias, original in list(self.result.reference_map.items()):
                if original == path + ".unpassed_test_ids":
                    self.result.reference_map[alias] = path
            out["unpassed_test_ids"]["source_ref"] = path
        if value.get("attachment_capture", {}).get("status") not in {None, "complete"}:
            out["attachment_capture"] = self.compact(value["attachment_capture"], path + ".attachment_capture")
        if value.get("output") and not value.get("logs") and (value.get("exit_code") or value.get("failure_reasons")):
            out["output"] = self.compact(value["output"], path + ".output")
        return out

    def document(self, value, path, *, required=True):
        if not isinstance(value, dict):
            return self.compact(value, path)
        out = {}
        for key, child in value.items():
            if key in {"files", "changes"} and child:
                out[key] = self.reference(child, path + "." + key, "Original proposal; inspect current mounted source and relevant changes")
            elif key == "document" and child:
                out[key] = (child if len(child) <= TEXT_INLINE else
                            self.reference(child, path + ".document", "Accepted narrative and shared constraints", required=required))
            elif key == "verified_sources":
                out[key] = [{k: self.compact(v, f"{path}.{key}[{i}].{k}") for k, v in row.items()
                             if k != "retrieval"} | {"source_ref": f"{path}.{key}[{i}]"}
                            for i, row in enumerate(child)]
            else:
                out[key] = self.compact(child, path + "." + key)
        return out

    def plan(self, value, path):
        if not isinstance(value, dict):
            return self.compact(value, path)
        if self.purpose == "readme":
            return {k: self.compact(v, f"{path}.{k}") for k, v in value.items()
                    if k in {"summary", "environment_file", "requirements_file", "preview", "runtime_prepare", "runtime_probes", "final_checks"}}
        out = {k: self.compact(v, f"{path}.{k}") for k, v in value.items() if k not in {"document", "tasks"}}
        if value.get("document"):
            out["document"] = self.reference(value["document"], path + ".document", "Planning rationale and supplementary constraints")
        tasks = value.get("tasks", [])
        if self.purpose not in {"recovery_diagnosis", "plan_patch"}:
            out["tasks"] = self.compact(tasks, path + ".tasks")
            return out
        selected = set(self.owners) | set(self.original.get("affected", [])) | set(self.original.get("reopen", []))
        patch = self.original.get("patch") or {}
        selected.update(t["id"] for t in patch.get("upsert_tasks", []) if isinstance(t, dict) and "id" in t)
        for task in tasks:
            if task.get("id") in selected:
                selected.update(task.get("depends_on", []))
        out["task_graph"] = [{k: t.get(k) for k in ("id", "origin_id", "title", "requirement_ids", "depends_on", "allowed_paths")}
                             | {"source_ref": f"{path}.tasks[{i}]"} for i, t in enumerate(tasks)]
        out["tasks"] = {str(i): {**self.compact(t, f"{path}.tasks[{i}]"), "source_ref": f"{path}.tasks[{i}]"}
                        for i, t in enumerate(tasks) if t.get("id") in selected}
        out["full_plan"] = self.reference(value, path, "Inspect other task contracts when diagnosing cross-task effects")
        return out

    def specification(self, spec, path="specification"):
        out = self.document(spec, path)
        if not isinstance(spec, dict) or not self.task.get("requirement_ids"):
            return out
        ids = set(self.task["requirement_ids"])
        # Only an accepted task association can exclude another requirement.
        # Unassigned and legacy requirements may be cross-cutting constraints.
        ids.update(r.get("id") for r in spec.get("requirements", [])
                   if not self.requirement_owners.get(r.get("id"))
                   or self.requirement_owners[r["id"]].intersection(self.owners))
        for key, id_key in (("requirements", "id"), ("coverage", "requirement_id")):
            rows = spec.get(key, [])
            if not isinstance(rows, list):
                continue
            # Original indexes are keys, never a re-numbered filtered array.
            out[key] = {str(i): {**self.compact(row, f"{path}.{key}[{i}]"), "source_ref": f"{path}.{key}[{i}]"}
                        for i, row in enumerate(rows) if row.get(id_key) in ids}
        if len(out.get("requirements", {})) < len(spec.get("requirements", [])):
            out["all_requirements"] = self.reference(spec, path, "Other obligations and cross-task constraints")
        return out

    def issues(self, rows, path):
        items = list(rows.items()) if isinstance(rows, dict) else list(enumerate(rows))
        out = {}
        for key, issue in items:
            if not isinstance(issue, dict) or issue.get("status") not in {None, "open", "stale"}:
                continue
            scope = issue.get("scope", "")
            owner = scope.split("/")[1] if scope.startswith(("task/", "checks/")) else issue.get("owner")
            location = issue.get("original", {}).get("location", "").split(":", 1)[0]
            if self.owners and owner and owner not in self.owners and not (location and path_allowed(location, self.scopes)):
                continue
            pointer = f"{path}.{key}" if isinstance(rows, dict) else f"{path}[{key}]"
            out[str(key)] = {**self.compact(issue, pointer), "source_ref": pointer}
        return out

    def history(self, rows, path):
        index, selected = [], {}
        for i, row in enumerate(rows):
            commit = row.get("delivery", "")
            ancestry = "unconfirmed"
            if re.fullmatch(r"[0-9a-f]{40,64}", commit):
                result = subprocess.run(["git", "--literal-pathspecs", "-C", str(self.workspace),
                    "merge-base", "--is-ancestor", commit, "HEAD"], env=git_environment(), capture_output=True, timeout=10)
                if result.returncode == 1:
                    continue
                if result.returncode == 0:
                    ancestry = "ancestor_of_selected_baseline"
            selected[str(i)] = row
            spec = row.get("specification", {})
            index.append({"run": row.get("run"), "delivery": row.get("delivery"),
                          "baseline_relation": ancestry,
                          "requirements": [{"id": r.get("id"), "description": r.get("description"),
                                            "identity": [row.get("run"), r.get("id")]} for r in spec.get("requirements", [])],
                          "source_ref": f"{path}[{i}]"})
        return {"index": index if len(json.dumps(index, ensure_ascii=False)) <= TEXT_INLINE else
                self.reference(index, path, "Search historical requirement identities before selecting relevant versions"),
                "records": self.reference(selected, path, "Historical specifications and provenance by original index; select only relevant versions")}

    def research(self, row, path):
        retrieval = row.get("retrieval", {})
        if not isinstance(retrieval, dict) or not isinstance(retrieval.get("artifact"), dict):
            return self.compact(row, path)
        text = self.artifacts.read(retrieval["artifact"])
        index_ref = retrieval.get("section_index")
        if index_ref:
            index = self.artifacts.json(index_ref)
        elif retrieval.get("raw"):
            extracted, _, index = extract_sections(self.artifacts.bytes(retrieval["raw"]), retrieval.get("mime_type", "text/plain"))
            if extracted != text:
                raise ProviderError("Research section index does not match the retained text")
            index_ref = self.artifacts.put(index, "research_section_index")
        else:
            index = {"headings": [], "anchors": [], "characters": len(text), "text_hash": digest(text)}
            index_ref = self.artifacts.put(index, "research_section_index")
        if index["characters"] != len(text) or index.get("text_hash") != digest(text):
            raise ProviderError("Research section index has a different text identity")
        fragment = urldefrag(row.get("url", retrieval.get("source", "")))[1]
        selection = select_section(index, fragment)
        out = {k: copy.deepcopy(v) for k, v in row.items() if k not in {"retrieval", "excerpt"}}
        out["source_ref"] = path
        out["retrieval"] = {k: retrieval.get(k) for k in
                            ("source", "final_url", "mime_type", "encoding", "captured_at", "extractor_version", "hash")}
        out["selection"] = selection
        out["full_text"] = {"artifact": retrieval["artifact"], "source_ref": path,
                            "reason": "Read additional definitions, inherited constraints or linked sections when needed"}
        # The index and full text are explicit optional mounts; raw HTML/manifests
        # stay archival and cannot drag their dependency graph into the prompt.
        out["section_index"] = {"artifact": index_ref, "source_ref": path}
        selected = None
        if selection["status"] == "selected":
            start, end = selection["start"], selection["end"]
            if not 0 <= start < end <= len(text):
                raise ProviderError("Research section range is invalid")
            selected = text[start:end]
        elif not fragment and len(text) <= RESEARCH_INLINE:
            selected = text
            out["selection"] = {"status": "complete_small_document", "start": 0, "end": len(text)}
            out.pop("full_text")
            out.pop("section_index")
        if selected is not None:
            out["excerpt"] = selected if len(selected) <= RESEARCH_INLINE else self.reference(
                selected, path, "Complete selected research section", required=True)
            self.mark(path, "section", "Exact source range; other sections are available on demand", selected)
            # Small directly linked definitions are cheaper and clearer inline
            # than another model/tool round. Never crop a definition to fit.
            remaining = min(self.linked_remaining, max(0, RESEARCH_INLINE - len(selected)))
            original_range = out["selection"]
            included, pending, seen = [], [], {(original_range.get("start"), original_range.get("end"))}
            for anchor in dict.fromkeys(re.findall(r"\(#([^\s)]+)\)", selected)):
                linked = select_section(index, anchor)
                if linked["status"] != "selected":
                    pending.append(linked)
                    continue
                start, end = linked["start"], linked["end"]
                if not 0 <= start < end <= len(text):
                    raise ProviderError("Linked research section range is invalid")
                if ((start, end) in seen or original_range.get("start", 0) <= start
                        and end <= original_range.get("end", 0)):
                    continue
                seen.add((start, end))
                body = text[start:end]
                entry = {**linked, "excerpt": body}
                cost = len(json.dumps(entry, ensure_ascii=False))
                if cost <= remaining:
                    included.append(entry)
                    remaining -= cost
                    self.linked_remaining -= cost
                else:
                    pending.append({k: linked[k] for k in ("anchor", "start", "end", "status")})
            if included:
                out["linked_sections"] = included
            if pending:
                out["linked_sections_on_demand"] = pending
        else:
            out["selection"]["instruction"] = "Locate the claim in the supplied index/full text; missing context cannot support approval."
        return out

    def prepare(self):
        context = self.original
        allowed = CONTRACTS[self.purpose] | (set() if self.purpose in {"material_probe", "provider_probe"} else SHARED)
        for key, value in context.items():
            if key in {"review_kind", "context_files"}:
                if key == "review_kind" or self.purpose == "material_probe" or not context.get("raw_requirements"):
                    self.result.payload[key] = copy.deepcopy(value)
                continue
            if key in KNOWN and key not in allowed:
                self.mark(key, "not_selected", "Not consumed by this invocation purpose", value)
                continue
            if key == "project" and isinstance(value, dict):
                paths = set()
                def locations(v):
                    if isinstance(v, dict):
                        for k, child in v.items():
                            if k in {"path", "location"} and isinstance(child, str):
                                paths.add(child.split(":", 1)[0])
                            elif k in {"evidence_paths", "prototype_paths"} and isinstance(child, list):
                                paths.update(p for p in child if isinstance(p, str))
                            else:
                                locations(child)
                    elif isinstance(v, list):
                        for child in v:
                            locations(child)
                locations({k: v for k, v in context.items() if k not in {"project", "history"}})
                scopes = self.scopes
                if self.purpose == "patch_repair":
                    paths = {context["patch_repair"]["failure"]["path"]}
                    scopes = list(paths)
                elif self.purpose == "prototype":
                    scopes = (context.get("specification") or {}).get("prototype_paths", []) or ["design/prototypes", "DESIGN.md", "frontend/src"]
                self.result.payload[key] = self.sources.select(self.workspace, scopes=scopes, paths=paths, purpose=self.purpose)
            elif key == "history" and isinstance(value, list):
                self.result.payload[key] = self.history(value, key)
            elif key in {"retrieved_sources", "design_evidence"} and isinstance(value, list):
                self.result.payload[key] = [self.research(row, f"{key}[{i}]") for i, row in enumerate(value)]
            elif key in {"unresolved_issues", "current_issues"} and isinstance(value, (list, dict)):
                self.result.payload[key] = self.issues(value, key)
            elif key in {"specification", "assessment"}:
                self.result.payload[key] = self.specification(value, key)
            elif key in {"design_candidates", "retained_candidates"}:
                self.result.payload[key] = self.reference(value, key, "Searchable supporting record; inspect relevant entries")
            elif key in {"plan", "previous_plan"}:
                self.result.payload[key] = self.plan(value, key)
            elif key == "delivery_journal" and isinstance(value, dict):
                self.result.payload[key] = {k: self.compact(v, f"{key}.{k}") for k, v in value.items()
                    if k in {"phase", "publication_started", "target", "old", "candidate", "work", "source_hash", "runtime_blocked"}}
                self.result.payload[key]["full_record"] = self.reference(value, key, "Full publication recovery record")
            elif key in {"prototype", "design", "provider_research", "previous_prototype"}:
                self.result.payload[key] = self.document(value, key, required=key != "previous_prototype")
            elif key == "patch_repair":
                failure = value["failure"]
                rows = value.get("candidate_changes", [])
                self.result.payload[key] = {**{k: self.compact(v, f"{key}.{k}") for k, v in value.items() if k != "candidate_changes"},
                    "candidate_changes": {str(i): self.compact(c, f"{key}.candidate_changes[{i}]") for i, c in enumerate(rows)
                                          if c.get("path") == failure["path"] and i <= failure["change_index"]}}
            elif key == "decision_request":
                request = copy.deepcopy(value)
                evidence = request.get("evidence", {})
                history = evidence.get("history", {})
                if isinstance(history, dict) and history.get("recent"):
                    recent = history["recent"]
                    outcomes, projected = {}, []
                    for i, row in enumerate(recent):
                        outcome = row.get("outcome", {})
                        identity = digest(outcome)
                        if identity in outcomes:
                            presented = {"source_ref": f"history.recent[{outcomes[identity]}].outcome", "same_content": True}
                        else:
                            outcomes[identity] = i
                            presented = outcome
                        projected.append({"source_ref": f"history.recent[{i}]", "action": row.get("action"),
                                          "status": row.get("status"), "outcome": presented})
                    history["recent"] = projected
                    if any(set(row) - {"action", "status", "outcome"} for row in recent):
                        history["details"] = self.reference(recent, "history.recent", "Complete recovery permit identities and bindings")
                self.result.payload[key] = self.compact(request, key)
            else:
                # Explicit unknown constraints are never silently discarded.
                self.result.payload[key] = self.compact(value, key) if key in KNOWN else copy.deepcopy(value)
            self.mark(key, "inline", "Stage input contract" if key in KNOWN else "Unclassified mandatory extension", value)
        self.evidence_dependencies()
        self.deduplicate_contracts()
        self.result.payload["input_contract"] = {"version": CONTEXT_VERSION, "purpose": self.purpose,
            "references": "Use source_ref/material_id for citations, never the positions of filtered lists. "
                          "Read required_read materials before deciding. Read optional records only when relevant. "
                          "Missing evidence cannot establish success; source and model proposals remain distinct."}
        return self.result

    def deduplicate_contracts(self):
        payload = self.result.payload
        verification = payload.get("verification_contract", {})
        execution = payload.get("execution_contract", {})
        if isinstance(verification, dict) and isinstance(execution, dict):
            commands = verification.get("commands")
            if commands and execution.get("commands") == commands:
                execution["commands"] = {"source_ref": "verification_contract.commands", "same_contract": True}
            if (payload.get("prototype_approval") and execution.get("approval") ==
                    {"prototype_approval": payload["prototype_approval"]}):
                execution["approval"] = {"source_ref": "prototype_approval", "same_contract": True}
        if payload.get("checks") or payload.get("evidence"):
            feedback = payload.get("feedback")
            if isinstance(feedback, dict):
                feedback["observation"] = "prior_attempt; current checks/evidence are authoritative for this candidate"
                if self.original.get("feedback", {}).get("checks"):
                    feedback["checks"] = self.reference(self.original["feedback"]["checks"], "feedback.checks",
                                                       "Prior observation; use current checks for acceptance")
        project = payload.get("project")
        if isinstance(project, dict) and len(json.dumps(project.get("read_on_demand", []), ensure_ascii=False)) > TEXT_INLINE:
            project["read_on_demand"] = self.reference(project["read_on_demand"], "project",
                "Searchable file selection index; inspect relevant files, not the entire directory")

    def evidence_dependencies(self):
        """Expose report/log dependencies of selected records, not their entire archive graph."""
        dependencies, visited = {}, set()
        queue = [(ref, self.result.reference_map.get(ref["hash"], "")) for ref in references(self.result.payload)
                 if {"kind", "bytes"} <= ref.keys()]
        while queue:
            ref, source = queue.pop()
            sha = ref["hash"]
            if sha in visited or ref["kind"].startswith("research_") or self.artifacts.released(ref):
                continue
            visited.add(sha)
            with self.artifacts.store.connect() as db:
                edges = {r[0] for r in db.execute("SELECT child FROM artifact_edges WHERE parent=?", (sha,))}
            if not edges:
                continue
            raw = self.artifacts.bytes(ref)
            try:
                record = json.loads(raw)
            except (ValueError, UnicodeError):
                raise ProviderError("Selected structured evidence cannot be read") from None

            def visit(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        if key in {"logs", "report", "attachments", "image_artifact", "diagnostic_evidence"}:
                            for dependency in references(child):
                                if (dependency["hash"] in edges and {"kind", "bytes"} <= dependency.keys()
                                        and not dependency["kind"].startswith("research_")):
                                    dependencies[dependency["hash"]] = copy.deepcopy(dependency)
                                    queue.append((dependency, source))
                                    if source:
                                        previous = self.result.reference_map.get(dependency["hash"], source)
                                        self.result.reference_map[dependency["hash"]] = source if previous == source else ""
                        elif not (isinstance(child, dict) and {"hash", "kind", "bytes"} <= child.keys()):
                            visit(child)
                elif isinstance(value, list):
                    for child in value:
                        visit(child)
            visit(record)
        if dependencies:
            self.result.payload["evidence_materials"] = list(dependencies.values())


def remap_references(result, mapping):
    """Map presentation aliases to original controller paths before adjudication."""
    def resolve(value):
        target = mapping.get(value, value)
        if value in mapping and not target:
            raise InvalidStructuredResponse("Ambiguous material hash; cite the supplied source_ref or material_id")
        return target
    if isinstance(result, dict):
        return {k: ([resolve(v) for v in value] if k == "evidence_refs" and isinstance(value, list) else
                    resolve(value) if k == "fact_ref" and isinstance(value, str) else remap_references(value, mapping))
                for k, value in result.items()}
    if isinstance(result, list):
        return [remap_references(v, mapping) for v in result]
    return result
