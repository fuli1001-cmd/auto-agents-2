"""Controller-owned preview contracts and resumable task preview evidence."""
from __future__ import annotations

import json

from pydantic import Field

from .builds import verification_commands
from .design import APPLICATION_VIEWPORTS, inspect_application
from .errors import PrototypeBrowserError
from .evidence import input_identity, reusable, runtime_configuration
from .faults import StateConflict
from .models import CheckResult, PreviewSpec, Record, ReviewResult
from .recovery import FailureRecord
from .util import digest
from .verification import passed
from .workspace import tree_hash


class ActionMapping(Record):
    source_preview_hash: str = ""
    previous_index: int = Field(ge=0)
    current_indexes: list[int] = Field(min_length=1)
    reason: str = Field(min_length=1)


class PreviewComparison(Record):
    review: ReviewResult
    mappings: list[ActionMapping] = []


class PreviewEvidence:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def contracts(self, rid, plan):
        """Read accepted plans only; a rejected patch cannot become the baseline."""
        if not plan or not plan.preview:
            return None
        current = plan.preview.model_dump()
        records = []
        with self.store.connect() as db:
            rows = db.execute("SELECT key,data FROM checkpoints WHERE run_id=? AND "
                "(key LIKE 'plan_revision/history/%' OR key LIKE 'recovery/gate-history/%') ORDER BY rowid",
                (rid,)).fetchall()
        for key, raw in rows:
            saved = json.loads(raw)
            previous = saved.get("checkpoints", {}).get("plan") if key.startswith("recovery/") else saved.get("plan")
            if not previous or not previous.get("preview"):
                continue
            preview = PreviewSpec.model_validate(previous["preview"]).model_dump()
            records.append({"origin": key, "plan_hash": digest(previous), "preview_hash": digest(preview),
                            "preview": preview})
        # The most recent distinct accepted contract is the predecessor of the
        # current preview, even when subsequent patches changed only task prose.
        previous = next((row for row in reversed(records) if row["preview"] != current), None)
        # A gate re-entry archives the accepted contract at the start of the
        # current planning generation. Preserve it and every later accepted
        # revision, not only the most recent predecessor. Earlier generations
        # remain indexed in history; no rejected candidate enters this chain.
        start = max((i for i, row in enumerate(records) if row["origin"].startswith("recovery/gate-history/")), default=0)
        chain, seen = [], set()
        for row in records[start:]:
            if row["preview_hash"] not in seen and row["preview"] != current:
                chain.append(row)
                seen.add(row["preview_hash"])
        current_record = {"origin": "plan", "plan_hash": digest(plan.model_dump()),
                          "preview_hash": digest(current), "preview": current}
        chain.append(current_record)
        return {"current": current_record, "chain": chain,
                "previous": previous,
                "history": [{k: v for k, v in row.items() if k != "preview"} for row in records],
                "execution_viewports": [{"width": w, "height": h} for w, h in APPLICATION_VIEWPORTS],
                "browser_version": self.engine.browser_check_version(),
                "meaning": "Accepted controller contracts, not execution evidence. Viewports are the controller's "
                           "fixed application execution contract. Compare commands, routes, every operation and "
                           "assertion, including cleanup. The chain contains the archived generation baseline, "
                           "every distinct accepted successor and the current contract. Preserve all chain obligations; "
                           "history indexes earlier generations and duplicate snapshots without replacing the chain."}

    def binding(self, rid, work, plan):
        contracts = self.contracts(rid, plan)
        approval = self.engine.approvals.recheck_binding(rid, work) if contracts else None
        if not approval or (self.store.get(rid, "delivery/journal") or {}).get("publication_started"):
            return None
        prototype = self.store.get(rid, "prototype", {})
        if not self.engine.approvals.current(rid, work, prototype.get("browser_actions", []), self.engine.browser_check_version()):
            return None
        return {"contracts_hash": digest(contracts), "approval": approval}

    @staticmethod
    def comparison_sources(contracts):
        chain = contracts.get("chain")
        if chain:
            return chain[:-1] or [contracts["current"]]
        # Existing single-predecessor evidence remains readable. A new chain
        # with multiple sources requires explicit source-bound mappings.
        return [contracts.get("previous") or contracts["current"]]

    @classmethod
    def comparison_valid(cls, comparison, contracts):
        sources = cls.comparison_sources(contracts)
        expected = {(source["preview_hash"], i) for source in sources
                    for i in range(len(source["preview"]["actions"]))}
        current = contracts["current"]["preview"]["actions"]
        rows = comparison.mappings
        keys = [(row.source_preview_hash or (sources[0]["preview_hash"] if len(sources) == 1 else ""),
                 row.previous_index) for row in rows]
        return (comparison.review.approved and len(keys) == len(expected) and set(keys) == expected
                and all(len(set(row.current_indexes)) == len(row.current_indexes)
                        and all(0 <= i < len(current) for i in row.current_indexes) for row in rows))

    def report_valid(self, report, spec):
        expected = {(route, w, h) for route in spec.routes for w, h in APPLICATION_VIEWPORTS}
        rows = report["results"]
        try:
            for row in rows:
                for image in [row, *row.get("tiles", [])]:
                    ref = image["image_artifact"]
                    raw = self.engine.artifacts.bytes(ref)
                    if ref.get("kind") != "screenshot" or len(raw) != ref["bytes"] or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                        return False
        except (OSError, ValueError, KeyError, TypeError):
            return False
        return (not report.get("error") and len(rows) == len(expected)
                and {(row.get("route"), row.get("viewport"), row.get("viewport_height")) for row in rows} == expected
                and all(not row.get("errors") and 200 <= row.get("status", 0) < 400
                        and row.get("actions") == [a.model_dump() for a in spec.actions if a.route == row["route"]]
                        and row.get("image_artifact") and row.get("cleanup") == {
                            "process_stopped": True, "scratch_removed": True} for row in rows))

    def context(self, rid, work, plan, environment, *, task_id):
        """Only supply a completed preview while its source/build/runtime still match."""
        pointer = self.store.get(rid, "preview-evidence/current/" + task_id)
        if not pointer or not plan.preview or self.store.get(rid, "plan") != plan.model_dump():
            return {}
        saved = self.store.get(rid, pointer["key"], {})
        expected = self.execution_identity(work, plan.preview, environment)
        if (saved.get("phase") != "completed" or saved.get("execution") != expected
                or saved.get("binding") != self.binding(rid, work, plan)):
            return {}
        report = self.engine.artifacts.json(saved["report"])
        comparison = PreviewComparison.model_validate(saved["comparison"])
        if not self.engine.authority.reusable(rid, comparison.review.model_dump()):
            return {}
        if not self.report_valid(report, plan.preview) or not self.comparison_valid(comparison, self.contracts(rid, plan)):
            raise StateConflict("Completed preview evidence no longer matches its contract")
        return {"preview_evidence": {"report": saved["report"], "execution": expected,
                "contracts": self.contracts(rid, plan), "comparison": saved["comparison"]}}

    @staticmethod
    def execution_identity(work, spec, environment):
        return {"source_hash": tree_hash(work), "environment_hash": environment.fingerprint,
                "command_hash": digest(spec.command.model_dump()), "input_hash": input_identity(work, spec.command),
                "runtime_config_hash": runtime_configuration(work, spec.command, environment)}

    def _unstarted_legacy_review_gap(self, rid, work, plan, permit, key, task, failure):
        """Classify a protocol-only prerequisite gap, never grant a different action.

        The preview journal is created before comparison, checks or browser work.
        Once it exists, execution may be occupied and must retain its original gate.
        """
        if (not isinstance(permit, dict) or permit.get("phase") not in {"reserved", "started"}
                or permit.get("action", {}).get("action") != "preview_recheck"
                or permit["action"].get("failure_id") != failure.id
                or permit["action"].get("task_ids") != [task.id]
                or permit.get("task_scopes") != {task.id: task.allowed_paths}
                or permit.get("owner") != failure.origin_id
                or self.store.get(rid, key) is not None
                or self.store.get(rid, "invocation/" + key + "/comparison") is not None
                or (self.store.get(rid, "delivery/journal") or {}).get("publication_started")):
            return False
        contracts = self.contracts(rid, plan)
        human_binding = self.engine.approvals.recheck_binding(rid, work)
        if (not contracts or not human_binding
                or tree_hash(work) != permit.get("source_hash")
                or digest(self.store.get(rid, "plan")) != permit.get("plan_hash")
                or self.store.get(rid, "plan") != plan.model_dump()
                or permit.get("preview_binding") != {"contracts_hash": digest(contracts), "approval": human_binding}):
            return False
        record = self.store.get(rid, "prototype_approval")
        if (not isinstance(record, dict)
                or any(record.get(field) != human_binding[field] for field in ("hash", "identity", "actions_hash"))
                or record.get("browser_version") != self.engine.browser_check_version()):
            return False
        try:
            # A malformed/current rejected review is not an obsolete legacy schema.
            legacy = ReviewResult.model_validate(record.get("review"))
        except (ValueError, TypeError):
            return False
        return legacy.approved and self.binding(rid, work, plan) is None

    async def recheck(self, rid, work, plan, permit_key, failure):
        engine, store = self.engine, self.store
        permit = store.get(rid, permit_key)
        key = "recovery/preview-rechecks/" + digest(permit_key)
        task = next(t for t in plan.tasks if t.id == failure.task_id)
        if self._unstarted_legacy_review_gap(rid, work, plan, permit, key, task, failure):
            # The existing recovery transaction rejects this unexecuted action
            # and records a new fact. Prototype refresh needs its own authorization.
            return FailureRecord.create(kind="review", component="prototype_verification",
                origin_id=failure.origin_id, task_id=failure.task_id, step="prototype_review_protocol",
                obligations=failure.obligations, evidence_refs=[permit_key, "prototype_approval"],
                evidence={"execution_status": "not_started", "preview_permit": permit_key,
                    "prototype_approval": store.get(rid, "prototype_approval"),
                    "previous_failure": failure.model_dump(), "task": task.model_dump(),
                    "validation_error": "The old automatic prototype review does not satisfy the current authority "
                        "protocol. No preview comparison, checks or browser execution started. Retain this permission "
                        "and candidate; independently authorize and perform prototype evidence refresh before preview."})

        def require_binding():
            if (tree_hash(work) != permit["source_hash"] or digest(store.get(rid, "plan")) != permit["plan_hash"]
                    or not permit.get("preview_binding")
                    or self.binding(rid, work, plan) != permit["preview_binding"]):
                raise StateConflict("Preview recheck inputs or human consent changed; preserve the candidate and evidence")

        def failed(reason, *, kind="evidence", extra=None):
            return FailureRecord.create(kind=kind, component="preview_verification", origin_id=failure.origin_id,
                task_id=failure.task_id, step="preview_recheck", obligations=failure.obligations, evidence_refs=[key],
                evidence={"preview_recheck": saved, "validation_error": reason,
                          "task": task.model_dump(), **(extra or {})})

        require_binding()
        contracts = self.contracts(rid, plan)
        saved = store.get(rid, key) or {"binding": permit["preview_binding"], "phase": "comparing"}
        if saved["binding"] != permit["preview_binding"]:
            raise StateConflict("Preview journal no longer matches its permit")
        store.checkpoint(rid, key, saved)
        if saved.get("comparison") and not engine.authority.reusable(rid, saved["comparison"]["review"]):
            store.checkpoint(rid, "authority/preview-history/" + digest(saved), saved)
            saved = {k: v for k, v in saved.items() if k != "comparison"}
            store.checkpoint(rid, key, saved)
        if "comparison" not in saved:
            comparison = await engine._invoke("review", {"review_kind": "preview_contract",
                "preview_contracts": contracts, "task": task.model_dump(),
                "feedback": failure.model_dump(), "project": engine.contexts.build(work),
                "instruction": "Independently compare every complete historical contract in chain against current. "
                    "The final chain entry is current; if it is the only entry, inspect that baseline. "
                    "Reject if the task requires an original not supplied by the controller. "
                    "Return one mapping for EVERY action of EVERY historical chain source (or current for a "
                    "single-entry chain), with its source_preview_hash, zero-based previous_index and current_indexes. "
                    "Cover setup, assertions and cleanup; explain changes and preserve obligations across all versions. "
                    "Check commands, routes, viewport coverage and isolation against the task. Approve only "
                    "when all original obligations are preserved; an execution or delivery is not yet approved."},
                PreviewComparison, work, rid, key + "/comparison", fresh=True)
            require_binding()
            saved["comparison"] = comparison.model_dump()
            store.checkpoint(rid, key, saved)
        comparison = PreviewComparison.model_validate(saved["comparison"])
        if not self.comparison_valid(comparison, contracts):
            return failed("Preview contract comparison rejected or incomplete; retain the original contract and request a plan correction.",
                          kind="contract", extra={"review": comparison.review.model_dump()})
        engine._task_approval_context(rid, work, {})
        environment = await engine._environment(work, rid, plan)
        commands = verification_commands(task.checks, work, plan.runtime_prepare, prepare_all=True)
        state = store.get(rid, "develop/task/" + task.id, {})
        checks = [CheckResult.model_validate(row) for row in saved.get("checks", state.get("checks", []))]
        if "execution" in saved:
            if saved["execution"] != self.execution_identity(work, plan.preview, environment):
                raise StateConflict("Preview execution inputs changed; preserve the completed or occupied observation")
        if not reusable(commands, checks, work, environment, engine.artifacts).allowed:
            if "execution" in saved:
                return failed("Preview prerequisite checks are no longer valid; a new recovery action is required.")
            checks = await engine._checks(rid, commands, work, environment, task, operation=permit_key + "/preview-checks")
        saved["checks"] = [row.model_dump() for row in checks]
        store.checkpoint(rid, key, saved)
        if not passed(checks):
            return failed("Preview prerequisite checks failed", kind="checks", extra={"checks": saved["checks"]})
        require_binding()
        if "report" not in saved:
            if saved["phase"] == "started":
                raise StateConflict("Preview execution has no durable completion; do not replay possibly committed actions")
            await engine._ensure_browser(rid, work)
            saved.update(phase="started", execution=self.execution_identity(work, plan.preview, environment))
            store.checkpoint(rid, key, saved)
            try:
                results = await inspect_application(plan.preview, work, store.root / "visuals" / rid / digest(permit_key),
                                                    environment, engine.sandbox, store.root)
                error = ""
            except PrototypeBrowserError as exc:
                results, error = exc.results, str(exc)
            report = {"results": engine._archive_visuals(results), "error": error,
                      "execution": saved["execution"], "contracts": contracts}
            saved.update(report=engine.artifacts.put(report, "application-preview"), phase="recorded")
            store.checkpoint(rid, key, saved)
        require_binding()
        if saved["execution"] != self.execution_identity(work, plan.preview, environment):
            raise StateConflict("Preview changed its source or execution inputs; retain the report without accepting it")
        report = engine.artifacts.json(saved["report"])
        if not self.report_valid(report, plan.preview):
            return failed("Full preview execution or cleanup is incomplete; retain evidence and restart in a new isolated instance.")
        saved["phase"] = "completed"
        store.checkpoints(rid, {key: saved, "preview-evidence/current/" + task.id: {"key": key}})
        return None
