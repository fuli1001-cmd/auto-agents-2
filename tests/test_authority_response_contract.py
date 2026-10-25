"""Finite response rules reach real review callers without broadening authority."""
import copy
import json
import sys

import pytest
from authority_review_fixtures import seed_original
from test_decisions import ApprovingAgent
from test_review_authority import context, rejected
from test_review_schema_recovery import native_review, requests
from test_runtime import configured
from test_workflow import engine

from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.design_documents import DesignDocuments
from auto_agents_2.engine import Replan, WorkflowEngine
from auto_agents_2.errors import RecoveryPathExhausted
from auto_agents_2.models import DocumentResult, ReviewResult
from auto_agents_2.preview_evidence import PreviewComparison
from auto_agents_2.prompts import PromptCompiler
from auto_agents_2.review_authority import KINDS, InvalidAuthorityReview, validate_review
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.util import digest, write_json
from auto_agents_2.workspace import tree_hash


@pytest.mark.parametrize('kind', [kind for kind in KINDS if kind != 'doctor'])
def test_engine_preparation_and_projection_disclose_exact_response_namespace(tmp_path, kind):
    workflow = engine(tmp_path, ApprovingAgent())
    rid = workflow.store.admit('isolated response contract', {})['id']
    seed_original(workflow, rid)
    workflow.store.checkpoint(rid, 'clarify', {'requirements': [{'id': 'REQ-009', 'description': 'Cancel keeps data.'}]})
    ctx = {'review_kind': kind, 'design': {'document': 'Selected method'}, 'assessment': {},
           'research': {}, 'plan': {}, 'patch': {}, 'candidate_changes': {}, 'preview_contracts': {},
           'project': workflow.contexts.build(tmp_path),
           'feedback': {'blocked_admission': {'unsupported': ['historical-only']}}}
    stage = kind if kind in {'verify', 'coverage_review'} else 'review'
    operation = 'doctor' if kind == 'doctor' else 'response-contract/' + kind
    schema = PreviewComparison if kind == 'preview_contract' else ReviewResult
    prepared, schema = workflow.authority.prepare(rid, stage, ctx, schema, tmp_path, operation)
    basis = prepared['controller_review_basis']
    contract = basis['response_contract']
    assert contract['source_ids'] == sorted(basis['sources'])
    assert contract['observation_ids'] == sorted(basis['observations'])
    assert contract['criterion_ids'] == [c['id'] for c in basis['criteria']]
    assert 'requirement:REQ-009' in contract['goal_ids'] and 'REQ-009' not in contract['goal_ids']
    assert contract['unsupported_obligation_ids'] == []
    assert contract['finding_coverage'] == {'defect': 'violated', 'evidence_gap': 'unverified', 'source_dispute': 'unverified'}
    projected = ContextPlanner(workflow.artifacts).prepare(stage, prepared, tmp_path, operation, run_id=rid)
    prompt, _ = PromptCompiler().compile(stage, projected.payload)
    wire = json.loads(prompt.split('<context>\n', 1)[1].split('\n</context>', 1)[0])
    assert wire['controller_review_basis'] == basis
    assert 'not authority IDs' in prompt and 'source_dispute maps to unverified' in prompt
    schema_json = schema.model_json_schema()
    assert 'material source_ref' in str(schema_json)
    assert 'Historical blocked_admission is background' in str(contract)


@pytest.mark.parametrize('mutation,code', [
    ('material_observation', 'unknown_observation_reference'),
    ('material_source', 'invalid_source_dispute_owner_or_method'),
    ('wrong_status', 'finding_coverage_disagreement'),
    ('unmapped_coverage', 'verdict_requires_grounded_findings'),
    ('bare_goal_id', 'invalid_technical_adoption'),
    ('historical_obligation', 'unmapped_unsupported_obligations'),
    ('wrong_owner', 'invalid_source_dispute_owner_or_method'),
    ('stale_binding', 'stale_review_binding'),
])
def test_disclosed_contract_does_not_normalize_or_accept_bad_review(mutation, code):
    ctx = context()
    basis = ctx['controller_review_basis']
    review = rejected(ctx)
    proof = review.authority['findings'][0]
    assert validate_review(review, basis)
    if mutation == 'material_observation':
        proof['observation_refs'] = ['design.document']
    elif mutation == 'material_source':
        proof['method_source'] = 'design.document'
    elif mutation == 'wrong_status':
        next(c for c in review.authority['coverage'] if c['criterion_id'] == proof['criterion_id'])['status'] = 'violated'
    elif mutation == 'unmapped_coverage':
        review.authority['coverage'][0]['status'] = 'unverified'
    elif mutation == 'bare_goal_id':
        review.authority['technical_choices'] = [{'id': 'choice', 'clause': ctx['design']['document'],
            'goal_ids': ['R1'], 'method_origin': 'derived_choice', 'feasibility': 'not_yet_verified', 'reason': 'unverified method'}]
    elif mutation == 'historical_obligation':
        review.authority['obligations'] = [{'obligation_id': 'historical-only', 'goal_ids': ['input:goals'],
            'method_source': 'candidate', 'method_quote': ctx['design']['document'], 'classification': 'derived_choice',
            'disposition': 'correct_method', 'owner': 'design', 'preserve_ids': ['input:goals'], 'reason': 'old feedback'}]
    elif mutation == 'wrong_owner':
        proof['owner'] = 'develop'
    elif mutation == 'stale_binding':
        review.authority['basis_hash'] = 'previous'
    before = copy.deepcopy(review.model_dump())
    with pytest.raises(InvalidAuthorityReview, match=code):
        validate_review(review, basis)
    assert review.model_dump() == before


def test_old_valid_receipt_remains_readable_without_new_disclosure():
    ctx = context()
    basis = ctx['controller_review_basis']
    basis.pop('response_contract')
    basis['hash'] = digest({k: v for k, v in basis.items() if k != 'hash'})
    review = rejected(ctx)
    assert validate_review(review, basis).requirements_preserved


@pytest.mark.parametrize('mode', ['valid_rejection', 'invalid_twice'])
async def test_native_design_reaudit_reads_contract_and_preserves_gate(tmp_path, monkeypatch, mode):
    config, store = configured(tmp_path)
    captured = store.root / 'native-wire'
    captured.mkdir()
    binary = store.root / 'contract-reader'
    binary.write_text(f'#!{sys.executable}\n' + '''import json,pathlib,sys
args=sys.argv[1:]
if args == ["--version"]:
    print("contract-reader 1");sys.exit(0)
wire=sys.stdin.read()
context=json.loads(wire.split("<context>\\n",1)[1].split("\\n</context>",1)[0])
schema=json.loads(pathlib.Path(args[args.index("--output-schema")+1]).read_text())
target=pathlib.Path(args[args.index("-o")+1])
if "text_token" in schema["properties"]:
    value={"text_token":pathlib.Path(context["context_files"][0]).read_text(),"image_token":""}
else:
    basis=context["controller_review_basis"]
    contract=basis["response_contract"]
    assert "not authority IDs" in wire
    assert "material source_ref" in str(schema)
    assert "design.document" not in contract["source_ids"]
    assert contract["unsupported_obligation_ids"] == []
    stage=next(c for c in basis["criteria"] if c["id"] == "stage:design")
    clause=basis["sources"]["candidate"]["text"].splitlines()[1]
    coverage=[{"criterion_id":key,"status":contract["finding_coverage"]["source_dispute"] if key == stage["id"] else "satisfied","reason":"Isolated scenario assessment"} for key in contract["criterion_ids"]]
    proof={"finding_index":0,"criterion_id":stage["id"],"kind":"source_dispute","owner":"design","source_quote":stage["statement"],"observation_refs":["candidate"],"method_source":"candidate","method_quote":clause,"rationale":"Method is a design choice, not original input; preserve the goal."}
    if __MODE__ == "invalid_twice":
        proof["observation_refs"]=["design.document"]
    value={"approved":False,"summary":"Reconsider the chosen method at design","findings":[{"severity":"major","location":"design","description":"Method not established","evidence":"Candidate and input","resolution":"Return to responsible design owner"}],"authority":{"version":basis["version"],"basis_hash":basis["hash"],"candidate_hash":basis["candidate_hash"],"coverage":coverage,"findings":[proof],"technical_choices":[],"choices_complete":True,"obligations":[],"amendments":[],"requirements_preserved":True,"explanation":"Isolated explicit rejection, not product acceptance"}}
    (pathlib.Path(__CAPTURE__)/(target.parent.parent.name+".json")).write_text(json.dumps({"context":context,"response":value}))
target.write_text(json.dumps(value))
print(json.dumps({"type":"thread.started","thread_id":"isolated-contract-reader"}))
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":11,"output_tokens":3}}))
'''.replace('__MODE__', repr(mode)).replace('__CAPTURE__', repr(str(captured))))
    binary.chmod(0o755)
    native = store.root / 'native-home'
    native.mkdir()
    body = config.load().model_dump(mode='json')
    spec = body['providers']['codex']
    spec.update(binary=str(binary), environment={'CODEX_HOME': str(native)})
    body.update(active_provider='codex', providers={'codex': spec})
    write_json(config.path, body)
    workflow = WorkflowEngine(tmp_path, sandbox=SandboxRunner(store.root, _test_unconfined=True))
    rid = store.admit('isolated design review contract', {})['id']
    store.update(rid, replans=8)
    seed_original(workflow, rid)
    store.checkpoints(rid, {'clarify': {'requirements': [{'id': 'REQ-009', 'description': 'Cancel keeps data.'}]},
        'prototype_approval': {'identity': 'existing-prototype', 'human': {'answer': '1'}}, 'rounds/T1': {'round': 5}})
    protected = [store.get(rid, key) for key in ['prototype_approval', 'rounds/T1']]
    call = workflow.agent._call
    decisions = ApprovingAgent()

    async def invoke(eff, stage, context, schema, work, run_id, operation, fresh):
        if stage == 'decision':
            return await decisions.invoke(stage, context, schema, work, run_id, operation, fresh)
        return await call(eff, stage, context, schema, work, run_id, operation, fresh)

    monkeypatch.setattr(workflow.agent, '_call', invoke)
    document = DocumentResult(summary='Selected method', document='Use an operator-only native dialog proof.')
    source = tree_hash(tmp_path)
    failure = Replan if mode == 'valid_rejection' else RecoveryPathExhausted
    with pytest.raises(failure) as error:
        await DesignDocuments(workflow).reaudit(rid, tmp_path, document.model_dump(), {})
    assert tree_hash(tmp_path) == source
    assert protected == [store.get(rid, key) for key in ['prototype_approval', 'rounds/T1']]
    assert store.run(rid)['data']['replans'] == 8
    assert store.get(rid, 'authority/accepted/design') is None
    outputs = [json.loads(p.read_text()) for p in captured.glob('*.json')]
    if mode == 'valid_rejection':
        assert error.value.stage == 'design' and len(outputs) == 1
        correction = store.get(rid, 'authority/correction')
        assert correction['owner'] == 'design' and correction['previous'] == document.model_dump()
        assert 'requirement:REQ-009' in correction['preserve_ids']
        assert not store.get(rid, 'design') and not store.pending(rid)
    else:
        assert len(outputs) == 2 and not store.get(rid, 'authority/correction')
        state, = requests(workflow, rid)
        assert state['authority_rejections'] == 2
        with pytest.raises(RecoveryPathExhausted):
            await DesignDocuments(workflow).reaudit(rid, tmp_path, document.model_dump(), {})
        assert len(list(captured.glob('*.json'))) == 2


@pytest.mark.parametrize('old_failure,total', [('stale_basis', 2), ('missing', 3)])
async def test_new_disclosed_request_preserves_old_exhaustion_and_aggregate_count(tmp_path, monkeypatch, old_failure, total):
    workflow, rid, stage, ctx, schema, observed, _ = native_review(
        tmp_path, monkeypatch, 'design', [old_failure, old_failure, 'valid'])
    prepare = workflow.authority.prepare

    def old_input(*args, **kwargs):
        context, typed = prepare(*args, **kwargs)
        basis = context.get('controller_review_basis')
        if basis is None:
            return context, typed
        basis.pop('response_contract')
        basis['hash'] = digest({k: v for k, v in basis.items() if k != 'hash'})
        return context, typed

    with monkeypatch.context() as patch:
        patch.setattr(workflow.authority, 'prepare', old_input)
        with pytest.raises(RecoveryPathExhausted):
            await workflow._invoke(stage, ctx, schema, tmp_path, rid, 'bounded-review', fresh=True)
    old, = requests(workflow, rid)
    result = await workflow._invoke(stage, ctx, schema, tmp_path, rid, 'bounded-review', fresh=True)
    assert result.approved and len(observed) == 3
    assert old in requests(workflow, rid)  # The original exhausted request is retained byte for byte.
    if old_failure == 'missing':
        # Strict native parsing also consumed the old operation's infrastructure budget.
        # A fresh contract does not erase it: the existing policy grants the next response retry.
        recovered = [state for state in requests(workflow, rid) if state != old]
        assert len(recovered) == 1 and recovered[0]['phase'] == 'completed'
        assert recovered[0]['authority_rejections'] == 1
    else:
        assert requests(workflow, rid) == [old]
    owner = 'proposal/' + digest([stage, 'bounded-review'])
    assert workflow.store.get(rid, 'proposal-recovery/count/' + owner) == total
    assert old['authority_rejections'] == 2 and old['phase'] == 'rejected'
    assert observed[0]['controller_review_basis']['hash'] != observed[2]['controller_review_basis']['hash']
    assert workflow.store.get(rid, 'rounds/T1') == {'round': 8}
