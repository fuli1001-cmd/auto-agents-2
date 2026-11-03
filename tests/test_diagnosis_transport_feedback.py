"""A valid Diagnosis with an ambiguous material hash is a citation error."""
import copy

import pytest
from test_decisions import ApprovingAgent
from test_diagnosis_decisions import proposal_failure
from test_recovery_policy import ready

from auto_agents_2.context_plan import remap_references
from auto_agents_2.errors import InvalidStructuredResponse
from auto_agents_2.recovery import Diagnosis
from auto_agents_2.util import digest

# Actual retained Muya /11 final cited both duplicated check hashes. The consumed
# result file was GCed; native final + frozen reference map supplied this case.
HASHES = [
    "6eb32619efa3d00bcc7fa68763466bc2955c8b8a1f4bd100c0da8aee52264f1b",
    "206fbbf348f983ce7060a096d37850fb4a90ebc39ffbed798ca41bdead3bb0f7",
]

def mapping():
    refs = {h: "" for h in HASHES}
    for h, paths in zip(HASHES, [
        ["failure.evidence.checks[1]", "current_task_results.tasks[8].result.checks[1]"],
        ["previous_failure_observations.recent[2].failure.evidence.checks[1]",
         "failure.evidence.author_input.feedback.checks[1]"],
    ]):
        for path in paths:
            refs["material-" + digest([path, h])[:20]] = path
    return refs

def materials():
    return [{"material_id": alias, "source_ref": source, "artifact": {"hash": h}}
            for alias, source in mapping().items() if source
            for h in HASHES if alias == "material-" + digest([source, h])[:20]]

def bad_output():
    return Diagnosis(action="diagnose", reason="Inspect retained API timeout",
        evidence_refs=["failure.evidence", *HASHES]).model_dump()

def test_every_ambiguous_field_has_exact_safe_candidates_without_selecting_one():
    data = bad_output()
    original = copy.deepcopy(data)
    with pytest.raises(InvalidStructuredResponse) as caught:
        remap_references(data, mapping(), materials=materials())
    message = str(caught.value)
    assert "evidence_refs[1]" in message and "evidence_refs[2]" in message
    assert "failure.evidence.checks[1]" in message
    assert "material-108c32cc7523c3123e23" in message
    assert "previous_failure_observations.recent[2].failure.evidence.checks[1]" in message
    assert "schema" not in message.lower()
    assert data == original

def test_explicit_source_and_material_alias_keep_existing_resolution():
    refs = mapping()
    explicit = "failure.evidence.checks[1]"
    material = "material-" + digest([explicit, HASHES[0]])[:20]
    assert remap_references({"evidence_refs": [explicit, material]}, refs) == {
        "evidence_refs": [explicit, explicit]}

async def test_real_transport_rejection_reaches_next_diagnosis_and_is_durable(tmp_path):
    class Agent(ApprovingAgent):
        contexts = []
        async def invoke(self, stage, context, schema, *args, **kw):
            if stage != "recovery_diagnosis":
                return await super().invoke(stage, context, schema, *args, **kw)
            self.contexts.append(copy.deepcopy(context))
            if len(self.contexts) == 1:
                return remap_references(bad_output(), mapping(), materials=materials())
            feedback = context["diagnosis_feedback"]["validation_error"]
            assert "evidence_refs[1]" in feedback and "evidence_refs[2]" in feedback
            assert "material-108c32cc7523c3123e23" in feedback
            assert "Diagnosis schema" not in feedback
            return Diagnosis(action="repair", task_ids=["T3"], evidence_refs=["failure.evidence"],
                reason="Resolve original source before scoped correction")

    agent = Agent()
    workflow, rid, _, _, record = ready(tmp_path, agent, proposal_failure())
    await workflow.recovery.resume(rid)
    rejection = workflow.store.get(rid, f"recovery/diagnosis-rejections/{record.id}/1")
    assert "evidence_refs[2]" in rejection["validation_error"]
    await workflow.recovery.resume(rid)
    assert len(agent.contexts) == 2
    assert workflow.store.get(rid, f"recovery/diagnosis-rejections/{record.id}/1") == rejection
    assert workflow.store.get(rid, f"recovery/completed/{record.id}/1")["decision"]["action"] == "repair"

def test_nested_fact_ref_is_reported_and_feedback_is_bounded():
    data = {"rows": [{"fact_ref": HASHES[0], "evidence_refs": [HASHES[1]]} for _ in range(100)]}
    with pytest.raises(InvalidStructuredResponse) as caught:
        remap_references(data, mapping(), materials=materials())
    message = str(caught.value)
    assert "rows[0].fact_ref" in message
    assert "rows[0].evidence_refs[0]" in message
    assert len(message) < 18000

def test_untrusted_literal_is_not_echoed_as_a_candidate():
    raw = "UNTRUSTED_DIAGNOSTIC_LITERAL_DO_NOT_ECHO"
    refs = mapping()
    refs[raw] = ""
    with pytest.raises(InvalidStructuredResponse) as caught:
        remap_references({"evidence_refs": [raw]}, refs)
    assert raw not in str(caught.value)
    assert "evidence_refs[0]" in str(caught.value)


def test_candidates_must_be_supplied_wrappers_consistent_with_the_frozen_map():
    rows = materials()
    rows[0]["artifact"]["hash"] = "f" * 64
    rows[1]["source_ref"] = "invented.source"
    with pytest.raises(InvalidStructuredResponse) as caught:
        remap_references({"evidence_refs": [HASHES[0]]}, mapping(), materials=rows)
    assert caught.value.issues == [{"field": "evidence_refs[0]",
        "reason": "ambiguous_material_hash", "candidates": []}]


@pytest.mark.parametrize("bad_schema", [None, "extra", "wrong_type", "missing"])
async def test_managed_provider_keeps_strict_schema_before_ambiguous_reference_validation(tmp_path, bad_schema):
    import json
    from test_provider_terminal_state import fixture_router, START, TURN, COMPLETE
    from auto_agents_2.errors import InvalidEvidenceReference
    text = "retained observation " * 1000
    result = Diagnosis(action="diagnose", reason="Read exact observation",
        evidence_refs=[digest(text)]).model_dump()
    if bad_schema == "extra":
        result["unrecognized"] = "EXAMPLE_PLACEHOLDER_DO_NOT_ECHO"
    elif bad_schema == "wrong_type":
        result["task_ids"] = 42
    elif bad_schema == "missing":
        result.pop("reason")
    router, project, store, rid = fixture_router(
        tmp_path, [START, TURN, COMPLETE], file_response=json.dumps(result))
    # Unlike the small terminal fixture, this response has read-only linked
    # materials. Implement the real capability probe as well as the final call.
    binary = tmp_path / "provider"
    script = binary.read_text()
    script = script.replace("args = sys.argv[1:]", """args = sys.argv[1:]
if args == ['--version']:
    print('fixture-cli 1')
    sys.exit(0)""")
    script = script.replace("sys.stdin.read()", """wire = sys.stdin.read()
schema = json.loads(pathlib.Path(args[args.index('--output-schema') + 1]).read_text())
if 'text_token' in schema['properties']:
    context = json.loads(wire.split('<context>\\n', 1)[1].split('\\n</context>', 1)[0])
    result = {'text_token': pathlib.Path(context['context_files'][0]).read_text(), 'image_token': ''}
    pathlib.Path(args[args.index('-o') + 1]).write_text(json.dumps(result))
    print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}))
    sys.exit(0)""")
    binary.write_text(script)
    context = {"failure": {"evidence": {"first": text, "second": text}}}
    with pytest.raises(InvalidStructuredResponse) as caught:
        await router.invoke("recovery_diagnosis", context, Diagnosis, project, rid, "recovery/diagnose/fixture/1")
    assert isinstance(caught.value, InvalidEvidenceReference) == (bad_schema is None)
    if bad_schema is None:
        assert "failure.evidence.first" in str(caught.value)
        assert "failure.evidence.second" in str(caught.value)
    assert "EXAMPLE_PLACEHOLDER_DO_NOT_ECHO" not in str(caught.value)
    assert store.get(rid, "invocation/recovery/diagnose/fixture/1") is None
    assert store.get(rid, "provider-result/recovery/diagnose/fixture/1") is None
