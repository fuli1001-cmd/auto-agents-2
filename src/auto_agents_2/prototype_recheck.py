"""Refresh automatic prototype evidence without regenerating an approved design."""
from __future__ import annotations

from .faults import StateConflict
from .issues import IssueLedger
from .models import DocumentResult, ReviewResult
from .recovery import FailureRecord
from .util import digest
from .workspace import tree_hash


async def recheck_prototype(engine, rid, work, permit_key, failure):
    store, approvals = engine.store, engine.approvals
    permit = store.get(rid, permit_key, {})
    binding = permit.get("prototype_binding")
    key = "recovery/prototype-rechecks/" + digest(permit_key)

    def require_binding():
        if (not binding or approvals.recheck_binding(rid, work) != binding
                or tree_hash(work) != permit["source_hash"]
                or digest(store.get(rid, "plan")) != permit["plan_hash"]
                or (store.get(rid, "delivery/journal") or {}).get("publication_started")):
            raise StateConflict("Prototype recheck inputs or human consent changed; preserve the candidate and evidence")

    require_binding()
    prototype = store.get(rid, "prototype")
    document = DocumentResult.model_validate({k: v for k, v in prototype.items() if k in DocumentResult.model_fields})
    actions = [a.model_dump() for a in document.browser_actions]
    saved = store.get(rid, key)
    if saved is None:
        saved = {"binding": binding, "source_hash": permit["source_hash"],
                 "previous_approval": store.get(rid, "prototype_approval", {}),
                 "unresolved_issues": IssueLedger(store, engine.artifacts).unresolved(rid)}
        store.checkpoint(rid, key, saved)
    if saved["binding"] != binding or saved["source_hash"] != permit["source_hash"]:
        raise StateConflict("Prototype recheck journal no longer matches its permit")
    if "report" not in saved:
        report = await engine._prototype_browser_report(rid, work, document)
        require_binding()
        saved["report"] = engine.artifacts.put(report, "prototype-browser")
        store.checkpoint(rid, key, saved)
    report = engine.artifacts.json(saved["report"])
    if (report.get("version") != engine.browser_check_version() or report.get("source_hash") != binding["hash"]
            or report.get("actions_hash") != binding["actions_hash"]):
        raise StateConflict("Prototype recheck report no longer matches the approved contract or checker")
    if saved.get("phase") == "verified":
        current = approvals.current(rid, work, actions, engine.browser_check_version())
        if current and current["review"] == saved["review"] and current["visuals"] == report["results"]:
            return None
        raise StateConflict("Completed prototype evidence changed before task dispatch")
    context = {"review_kind": "prototype", "prototype": prototype,
        "specification": store.get(rid, "clarify", {}), "browser_evidence": report["results"],
        "controller_browser_report": report, "unresolved_issues": saved["unresolved_issues"],
        "feedback": {"failure": failure.model_dump(), "previous_approval": saved["previous_approval"],
            "instruction": "Inspect the fresh images and executed actions against all original prototype findings "
                           "and the task's prototype prerequisite. Human consent is unchanged; production evidence "
                           "cannot resolve prototype findings. Retain every unresolved concern."},
        "project": engine.contexts.build(work)}
    if not report["error"] and "review" not in saved:
        review = await engine._invoke("review", context, ReviewResult, work, rid, key + "/review", fresh=True)
        require_binding()
        saved["review"] = review.model_dump()
        store.checkpoint(rid, key, saved)
    review = saved.get("review", {})
    evidence = {**binding, "review": review, "visuals": report["results"],
                "browser_version": engine.browser_check_version()}
    if report["error"] or not approvals.automation_current(evidence, binding, actions,
            engine.browser_check_version(), expected_routes=approvals.routes(work, actions)):
        return FailureRecord.create(kind="review" if review else "evidence", component="prototype_verification",
            origin_id=failure.origin_id, task_id=failure.task_id, step="prototype_recheck",
            obligations=failure.obligations, evidence_refs=[key], evidence={"prototype_browser_report": saved["report"],
                "review": review, "previous_failure": failure.model_dump()})
    require_binding()
    record = approvals.publish(rid, work, actions, binding["human"], review, report["results"], engine.browser_check_version())
    # Refresh only the prototype's issue scopes. Task acceptance still needs its
    # own independent review; passing production checks cannot close these issues.
    ledger = IssueLedger(store, engine.artifacts)
    for scope in ("prototype-review", "prototype-approval-review/" + binding["identity"]):
        ledger.record(rid, scope, ReviewResult.model_validate(review), context)
    store.checkpoint(rid, key, {**saved, "approval": approvals.context(record), "phase": "verified"})
    return None
