"""Test authors explicitly translate scenario full contracts to the new proposal shape.

This is fixture construction only. Production has no old-response fallback.
The helper never repairs/deletes a scenario delta; exact old/new rows are compared.
"""
from auto_agents_2.models import PlanResult
from auto_agents_2.plan_proposals import EvidencePlanPatch, materialize
from auto_agents_2.plan_revision import PlanPatch
from auto_agents_2.util import digest


def proposal_response(patch, context, schema):
    if schema is PlanPatch:
        return patch
    assert schema is EvidencePlanPatch
    assert patch.evidence_contract is not None, "Use a frozen legacy fixture to test an omitted legacy contract"
    before = PlanResult.model_validate(context["plan"]).evidence_contract
    after = patch.evidence_contract
    assert after.catalog_hash == context["controller_evidence_capabilities"]["hash"]
    assert after.source_binding == context["evidence_requirement_source"]["hash"]
    old = {row.id: row for row in before.obligations} if before else {}
    new = {row.id: row for row in after.obligations}
    assert len(new) == len(after.obligations)
    changes = []
    for identity, row in old.items():
        if identity not in new:
            changes.append(dict(operation="remove", obligation_id=identity, previous_hash=digest(row.model_dump())))
        elif row != new[identity]:
            changes.append(dict(operation="replace", obligation_id=identity, previous_hash=digest(row.model_dump()),
                                replacement=new[identity].model_dump()))
    changes.extend(dict(operation="add", obligation=row.model_dump()) for identity, row in new.items() if identity not in old)
    update = dict(mode="amend", base_contract_hash=digest(before.model_dump() if before else None), changes=changes)
    if before == after:
        update = dict(mode="preserve", base_contract_hash=digest(before.model_dump()))
    return EvidencePlanPatch(**patch.model_dump(exclude={"evidence_contract"}), proposal_version=1,
                             base_plan_hash=digest(context["plan"]), evidence_update=update)


def full_response(response, context):
    return (materialize(response, PlanResult.model_validate(context["plan"]), context)
            if isinstance(response, EvidencePlanPatch) else response)


async def freeze_legacy_author(workflow, rid, record):
    """Create an explicit pre-upgrade frozen author request in an isolated test."""
    import asyncio
    from auto_agents_2.plan_proposals import PROTOCOL_KEY
    invoke = workflow._invoke
    async def before_author(stage, context, schema, *args, **kwargs):
        if schema is EvidencePlanPatch:
            raise asyncio.CancelledError()
        return await invoke(stage, context, schema, *args, **kwargs)
    workflow._invoke = before_author
    try:
        try:
            await workflow.recovery.resume(rid)
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Author boundary was not reached")
    finally:
        workflow._invoke = invoke
    identity = f"{record.id}/1"
    key = "recovery/patch-context/" + identity
    frozen = workflow.store.get(rid, key)
    assert frozen.pop(PROTOCOL_KEY)
    workflow.store.checkpoint(rid, key, frozen)
    workflow.store.checkpoint(rid, "recovery/patch-progress-basis/" + identity, None)
