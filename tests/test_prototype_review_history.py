"""Actual prototype reviewers receive the same controlled historical materials as authors."""
import copy
import sys

import pytest
from browser_fixtures import prototype_rows
from prototype_review_fixtures import prototype_verdict
from test_decisions import ApprovingAgent
from test_prototype_approvals import approval_fixture
from test_prototype_reentry_context import history_fixture

from auto_agents_2.approvals import prototype_identity
from auto_agents_2.engine import Replan
from auto_agents_2.errors import InvalidStructuredResponse
from auto_agents_2.faults import StateConflict
from auto_agents_2.models import DocumentResult, ReviewResult
from auto_agents_2.prototype_recheck import recheck_prototype
from auto_agents_2.prototype_reentry import PrototypeReentry
from auto_agents_2.providers import ProviderRouter
from auto_agents_2.recovery import FailureRecord
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.util import digest, write_json
from auto_agents_2.workspace import tree_hash


def review_context(workflow, document, work):
    return {'review_kind':'prototype', 'prototype':document.model_dump(mode='json'),
            'browser_evidence':prototype_rows(work/'design/prototypes', document.browser_actions, workflow.artifacts)}


async def test_actual_author_and_reviewer_receive_identical_complete_materials(tmp_path, monkeypatch):
    workflow, rid, _, documents, reviews, question = await history_fixture(tmp_path, monkeypatch)
    current = documents[0].model_copy(update={'files':[]})
    received, calls = {}, []
    original_human = workflow.store.question(question['id'])

    class Agent(ApprovingAgent):
        async def invoke(self, stage, context, *args, **kwargs):
            assert stage in {'prototype', 'review'}
            received[stage] = copy.deepcopy(context)
            calls.append(stage)
            bridge = context['controller_prototype_reentry']
            roles = {role:row for row in bridge['candidates'] for role in row['roles']}
            assert set(roles) == {'previous','recent_actions','human_baseline'}
            for role,index in [('previous',3),('recent_actions',2),('human_baseline',0)]:
                row = roles[role]
                assert workflow.artifacts.json(row['proposal']['artifact']) == documents[index].model_dump(mode='json')
                assert row['proposal']['required_read'] and row['proposal_hash'] == digest(documents[index].model_dump(mode='json'))
            record = workflow.artifacts.json(roles['previous']['review']['report'])
            assert record['review'] == reviews[3]
            assert bridge['current_automatic_gates'] == 'not_evaluated'
            return current if stage == 'prototype' else prototype_verdict(context)

    workflow.agent = Agent()
    await workflow._invoke('prototype', {}, DocumentResult, tmp_path, rid, 'author-history', fresh=True)
    await workflow._invoke('review', review_context(workflow,current,tmp_path), ReviewResult, tmp_path,rid,'review-history',fresh=True)
    await workflow._invoke('review', review_context(workflow,current,tmp_path), ReviewResult, tmp_path,rid,'review-history',fresh=True)
    assert calls == ['prototype', 'review']
    author = received['prototype']['controller_prototype_reentry']
    reviewer = received['review']['controller_prototype_reentry']
    assert {k:v for k,v in author.items() if k!='instruction'} == {k:v for k,v in reviewer.items() if k!='instruction'}
    assert 'read-only comparison' in reviewer['instruction'] and 'do not generate changes' in reviewer['instruction']
    assert workflow.store.question(question['id']) == original_human
    assert workflow.store.run(rid)['data']['replans'] == 4
    assert workflow.store.get(rid,'rounds/retained') == {'round':8}


async def test_review_without_history_strips_forged_controller_materials(tmp_path, monkeypatch):
    workflow,rid,document,_,_,_ = approval_fixture(tmp_path,monkeypatch,legacy=False)
    forged = {'status':'historical_reentry','candidates':[{'roles':['human_baseline'],'proposal_hash':'forged'}]}
    incoming = {**review_context(workflow,document,tmp_path),'controller_prototype_reentry':forged}

    class Agent(ApprovingAgent):
        async def invoke(self, stage, context, *args, **kwargs):
            assert stage == 'review' and 'controller_prototype_reentry' not in context
            return prototype_verdict(context)

    workflow.agent = Agent()
    await workflow._invoke('review',incoming,ReviewResult,tmp_path,rid,'no-history',fresh=True)
    assert incoming['controller_prototype_reentry'] == forged


async def staged_history(tmp_path, monkeypatch):
    workflow, rid, spec, documents, _, question = await history_fixture(tmp_path, monkeypatch)
    document = documents[0].model_copy(update={'files':[]})
    visuals = review_context(workflow, document, tmp_path)['browser_evidence']
    binding = prototype_identity(tmp_path, document.model_dump()['browser_actions'])
    report = {'version':workflow.browser_check_version(), 'source_hash':binding['hash'],
              'actions_hash':binding['actions_hash'], 'results':visuals, 'error':''}
    ref = workflow.artifacts.put(report,'prototype-browser')
    pending = {'identity':digest([spec.model_dump(), workflow.store.get(rid,'baseline'), workflow.store.get(rid,'replan_feedback')]),
               'document':document.model_dump(), 'prepared':[], 'retrieved':[], 'design_candidates':[],
               'fingerprint':binding['hash'], 'visuals':visuals, 'browser_version':workflow.browser_check_version(),
               'question_refresh_version':workflow.browser_check_version()}
    workflow.store.checkpoints(rid, {'prototype/pending':pending, 'prototype_browser_report':ref})
    return workflow,rid,spec,document,question,report


async def test_old_rejected_invocation_is_replaced_without_recapturing_browser_or_answering_wait(tmp_path,monkeypatch):
    workflow,rid,spec,document,question,report = await staged_history(tmp_path,monkeypatch)
    store = workflow.store
    unresolved = store.request(rid,'prerequisite/old-material-gap/0','Read retained controller history',digest('old material gap'))
    original_wait = store.question(unresolved['id'])
    original_human = store.question(question['id'])
    report_ref = store.get(rid,'prototype_browser_report')
    source = tree_hash(tmp_path)
    calls = []
    class Agent(ApprovingAgent):
        async def invoke(self,stage,context,*args,**kwargs):
            assert stage == 'review'
            calls.append(copy.deepcopy(context))
            return prototype_verdict(context,approved='controller_prototype_reentry' in context)
    workflow.agent = Agent()
    async def no_browser(*args):
        pytest.fail('Matching successful browser13 evidence must be reused')
    workflow._prototype_browser_report = no_browser
    controlled = workflow._prototype_review_context
    def legacy_context(*args):
        value = controlled(*args)
        value.pop('controller_prototype_reentry',None)
        return value
    with monkeypatch.context() as old:
        old.setattr(workflow,'_prototype_review_context',legacy_context)
        with pytest.raises(Replan):
            await workflow._prepare_prototype(rid,tmp_path,spec,spec.model_dump(),{},'UI')
    previous = store.get(rid,'invocation/prototype-review')
    assert not previous['result']['approved'] and 'review' not in store.get(rid,'prototype/pending')
    await workflow._prepare_prototype(rid,tmp_path,spec,spec.model_dump(),{},'UI')
    current = store.get(rid,'invocation/prototype-review')
    assert len(calls)==2 and current['identity']!=previous['identity'] and current['result']['approved']
    assert 'controller_prototype_reentry' not in calls[0] and len(calls[1]['controller_prototype_reentry']['candidates'])==3
    assert workflow.artifacts.json(previous['artifact'])==previous['result']
    await workflow._prepare_prototype(rid,tmp_path,spec,spec.model_dump(),{},'UI')
    assert len(calls)==2
    assert store.get(rid,'prototype/pending')['visuals']==report['results']
    assert store.get(rid,'prototype_browser_report')==report_ref
    assert store.question(question['id'])==original_human and store.question(unresolved['id'])==original_wait
    assert tree_hash(tmp_path)==source and store.run(rid)['data']['replans']==4
    assert store.get(rid,'rounds/retained')=={'round':8} and store.get(rid,'proposal-recovery/count/retained')==3


async def test_qualified_legacy_review_cache_stays_valid_without_provider_or_browser(tmp_path,monkeypatch):
    workflow,rid,spec,document,question,report = await staged_history(tmp_path,monkeypatch)
    context=workflow._prototype_review_context(rid,tmp_path,review_context(workflow,document,tmp_path))
    context.pop('controller_prototype_reentry')  # Old qualified review did not claim to read new materials.
    review=prototype_verdict(context).model_dump()
    pending=workflow.store.get(rid,'prototype/pending')
    pending['review']=review
    workflow.store.checkpoint(rid,'prototype/pending',pending)
    human=workflow.approvals.human(rid,tmp_path,document.model_dump()['browser_actions'])
    async def forbidden(*args,**kwargs):
        pytest.fail('Qualified cache reuse must not dispatch provider/browser')
    workflow.agent.invoke=forbidden
    workflow._prototype_browser_report=forbidden
    await workflow._prepare_prototype(rid,tmp_path,spec,spec.model_dump(),{},'UI')
    receipt=await workflow._ensure_prototype_approval(rid,tmp_path,document.model_dump())
    assert receipt['human']==human and receipt['independent_review']==review
    assert workflow.store.get(rid,'prototype/pending')['visuals']==report['results']
    assert workflow.store.question(question['id'])['answer']=='1'


@pytest.mark.parametrize('entry',['ensure','recheck'])
async def test_other_actual_review_entrypoints_receive_bound_history(tmp_path,monkeypatch,entry):
    workflow,rid,_,document,question,report=await staged_history(tmp_path,monkeypatch)
    store=workflow.store
    store.checkpoint(rid,'prototype',document.model_dump())
    calls=[]
    class Agent(ApprovingAgent):
        async def invoke(self,stage,context,*args,**kwargs):
            assert stage=='review' and context['controller_prototype_reentry']['status']=='historical_reentry'
            assert len(context['controller_prototype_reentry']['candidates'])==3
            calls.append(context)
            return prototype_verdict(context)
    workflow.agent=Agent()
    browsers=[]
    async def retained_report(*args):
        browsers.append(True)
        return report
    workflow._prototype_browser_report=retained_report
    if entry=='ensure':
        receipt=await workflow._ensure_prototype_approval(rid,tmp_path)
        assert receipt['human']['question_id']==question['id'] and len(browsers)==1
    else:
        # This is the already-authorized callee boundary with its saved report;
        # existing recovery-policy tests exercise normal permit issuance.
        binding=workflow.approvals.recheck_binding(rid,tmp_path)
        permit_key='recovery/permits/retained-review-fixture'
        permit={'prototype_binding':binding,'source_hash':tree_hash(tmp_path),'plan_hash':digest(store.get(rid,'plan'))}
        key='recovery/prototype-rechecks/'+digest(permit_key)
        store.checkpoints(rid,{permit_key:permit,key:{'binding':binding,'source_hash':permit['source_hash'],
            'previous_approval':store.get(rid,'prototype_approval'),'unresolved_issues':[],
            'report':store.get(rid,'prototype_browser_report')}})
        failure=FailureRecord.create(kind='review',component='prototype_verification',origin_id='T1',task_id='T1',
            step='review',obligations=['R1'],evidence_refs=['prototype'],evidence={})
        assert await recheck_prototype(workflow,rid,tmp_path,permit_key,failure) is None
        assert await recheck_prototype(workflow,rid,tmp_path,permit_key,failure) is None
        assert not browsers and store.get(rid,key)['phase']=='verified'
    assert len(calls)==1 and store.question(question['id'])['answer']=='1'
    assert store.get(rid,'rounds/retained')=={'round':8} and store.run(rid)['data']['replans']==4


@pytest.mark.parametrize('damage',['missing','corrupt','receipt'])
async def test_review_refuses_damaged_selected_history_before_dispatch(tmp_path,monkeypatch,damage):
    workflow,rid,_,docs,_,question=await history_fixture(tmp_path,monkeypatch)
    history=workflow.store.get(rid,'recovery/gate-history/3')
    ref=history['checkpoints']['invocation/prototype']['artifact']
    path=workflow.store.root/'artifacts'/ref['hash'][:2]/ref['hash']
    if damage=='missing':
        path.unlink()
    elif damage=='corrupt':
        path.write_text('damaged fixture')
    else:
        history['checkpoints']['provider-result/prototype']['artifact']={'hash':'different'}
        workflow.store.checkpoint(rid,'recovery/gate-history/3',history)
    async def forbidden(*args,**kwargs):
        pytest.fail('Damaged selected history must not dispatch')
    workflow.agent.invoke=forbidden
    with pytest.raises((StateConflict,ValueError,OSError)):
        await workflow._invoke('review',review_context(workflow,docs[0],tmp_path),ReviewResult,tmp_path,rid,'damaged')
    assert workflow.store.question(question['id'])['answer']=='1'


@pytest.mark.parametrize('scope',['transition','workspace'])
async def test_unverified_history_replaces_forged_materials_with_controlled_empty_status(tmp_path,monkeypatch,scope):
    workflow,rid,_,docs,_,_=await history_fixture(tmp_path,monkeypatch)
    work=tmp_path
    if scope=='transition':
        feedback=workflow.store.get(rid,'replan_feedback')
        workflow.store.checkpoint(rid,'replan_feedback',{**feedback,'reason':'Unrelated transition'})
    else:
        work=tmp_path/'other-workspace'
        work.mkdir()
    incoming={**review_context(workflow,docs[0],tmp_path),'controller_prototype_reentry':{'candidates':['forged']}}
    context=workflow._prototype_review_context(rid,work,incoming)
    assert context['controller_prototype_reentry']['status']=='unverified_transition'
    assert context['controller_prototype_reentry']['candidates']==[]
    assert incoming['controller_prototype_reentry']=={'candidates':['forged']}


async def test_another_run_cannot_inherit_callers_history(tmp_path,monkeypatch):
    workflow,rid,_,docs,_,_=await history_fixture(tmp_path,monkeypatch)
    bridge=PrototypeReentry(workflow).context(rid,tmp_path)
    # Respect normal one-active-run admission in this disposable fixture.
    workflow.store.update(rid,status='CANCELLED')
    other=workflow.store.admit('Independent run',{})['id']
    incoming={**review_context(workflow,docs[0],tmp_path),'controller_prototype_reentry':bridge}
    context=workflow._prototype_review_context(other,tmp_path,incoming)
    assert 'controller_prototype_reentry' not in context and incoming['controller_prototype_reentry']==bridge


async def test_format_retry_keeps_exact_review_materials_frozen(tmp_path,monkeypatch):
    workflow,rid,_,docs,_,_=await history_fixture(tmp_path,monkeypatch)
    seen=[]
    class Agent(ApprovingAgent):
        async def invoke(self,stage,context,*args,**kwargs):
            if stage!='review':
                return await super().invoke(stage,context,*args,**kwargs)
            seen.append(copy.deepcopy(context))
            if len(seen)==1:
                def no_rebuild(*args):
                    pytest.fail('A structured retry must not rebuild its frozen history')
                monkeypatch.setattr(PrototypeReentry,'context',no_rebuild)
                raise InvalidStructuredResponse('fixture invalid structured review')
            assert context['controller_prototype_reentry']==seen[0]['controller_prototype_reentry']
            return prototype_verdict(context)
    workflow.agent=Agent()
    result=await workflow._invoke('review',review_context(workflow,docs[0],tmp_path),ReviewResult,tmp_path,rid,'retry-history')
    assert result.approved and len(seen)==2 and seen[1]['validation_feedback']['attempt']==1
    counter='proposal-recovery/count/proposal/'+digest(['review','retry-history'])
    assert workflow.store.get(rid,counter)==1
    assert workflow.store.get(rid,'proposal-recovery/count/retained')==3


@pytest.mark.parametrize('successor_approval', [False, True])
async def test_actual_review_sandbox_reads_complete_historical_arrays_and_bound_reviews(tmp_path,monkeypatch,successor_approval):
    workflow,rid,_,docs,_,question=await history_fixture(tmp_path/'project',monkeypatch)
    work=workflow.project
    if successor_approval:
        publish_successor_approval(workflow, rid, work, docs[0])
    document=docs[0].model_copy(update={'files':[]})
    # This test isolates text-history admission; no image capability probe or
    # product acceptance is claimed. Other entrypoint cases retain browser rows.
    incoming={'review_kind':'prototype','prototype':document.model_dump(),'browser_evidence':[],
              'controller_prototype_reentry':{'proposal':{'hash':'f'*64,'kind':'prototype','bytes':1}}}
    controlled=workflow._prototype_review_context(rid,work,incoming)
    response=prototype_verdict(controlled,approved=False,summary='History read; current candidate still requires review evidence').model_dump()
    binary=tmp_path/'provider'
    binary.write_text(f'#!{sys.executable}\nRESPONSE = {response!r}\n'+r'''
import hashlib,json,pathlib,sys
args=sys.argv[1:]
if args==['--version']:
    print('review-history-fixture 1')
    sys.exit(0)
schema=json.loads(pathlib.Path(args[args.index('--output-schema')+1]).read_text())
wire=sys.stdin.read()
context=json.loads(wire.split('<context>\n',1)[1].split('\n</context>',1)[0])
if 'text_token' in schema['properties']:
    result={'text_token':pathlib.Path(context['context_files'][0]).read_text(),'image_token':''}
else:
    bridge=context['controller_prototype_reentry']
    assert 'read-only comparison' in bridge['instruction']
    assert 'do not generate changes' in bridge['instruction']
    roles={role:row for row in bridge['candidates'] for role in row['roles']}
    for role,count in [('previous',0),('recent_actions',793),('human_baseline',721)]:
        row=roles[role]
        assert row['proposal']['required_read'] and bridge['current_automatic_gates']=='not_evaluated'
        ref=row['proposal']['artifact']
        path=pathlib.Path(ref['location']); raw=path.read_bytes()
        assert hashlib.sha256(raw).hexdigest()==ref['hash'] and len(raw)==ref['bytes']
        document=json.loads(raw)
        assert len(document['browser_actions'])==count
        assert document['sources'][0]['url']=='https://docs.example.test/pinned/DESIGN.md'
        assert len(document.get('design_selection',[]))==int(role=='previous')
        if role=='human_baseline':
            assert document['files'][0]['old_text']=='<h1>Before</h1>'
        try:
            path.write_bytes(b'forbidden history write')
        except OSError:
            pass
        else:
            raise AssertionError('Reviewer can write historical material')
    ref=roles['previous']['review']['report']
    raw=pathlib.Path(ref['location']).read_bytes()
    assert hashlib.sha256(raw).hexdigest()==ref['hash'] and len(raw)==ref['bytes']
    assert json.loads(raw)['review']['summary']=='Complete review requirements of generation 3'
    result=RESPONSE
pathlib.Path(args[args.index('-o')+1]).write_text(json.dumps(result))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))
''')
    binary.chmod(0o755)
    native=tmp_path/'native'
    native.mkdir()
    config=workflow.config.load().model_dump(mode='json')
    config['providers']={'codex':config['providers']['codex']}
    config['providers']['codex'].update(binary=str(binary),environment={'CODEX_HOME':str(native)},
        profile_map={key:{'model':'test'} for key in ('balanced','deep','max')})
    config['execution']['infra_retries']=0
    write_json(workflow.config.path,config)
    workflow.agent=ProviderRouter(workflow.config,workflow.store,SandboxRunner(workflow.store.root))
    result=await workflow._invoke('review',incoming,ReviewResult,work,rid,'sandbox-review-history',fresh=True)
    assert not result.approved
    materials=workflow.store.get(rid,'materials/sandbox-review-history')['materials']
    mounted={row['hash'] for row in materials if row.get('presentation')=='attachment'}
    for row in controlled['controller_prototype_reentry']['candidates']:
        assert row['proposal']['artifact']['hash'] in mounted
        assert workflow.artifacts.json(row['proposal']['artifact'])['document']
    assert workflow.store.question(question['id'])['answer']=='1'


def publish_successor_approval(workflow, rid, work, original):
    """Exercise the normal publication that archives an older exact approval."""
    from auto_agents_2.interaction import choice_spec
    from auto_agents_2.models import BrowserAction

    document = original.model_copy(update={"files": [], "browser_actions": [
        *original.browser_actions,
        BrowserAction(route="/index.html", action="expect_text", selector="h1", value="New assertion"),
    ]})
    raw = document.model_dump(mode="json")
    workflow.store.checkpoint(rid, "prototype", raw)
    context = workflow._prototype_review_context(rid, work, review_context(workflow, document, work))
    review = prototype_verdict(context).model_dump()
    binding = prototype_identity(work, raw["browser_actions"])
    question = workflow.store.request(rid, "prototype-approval", "Approve successor content and actions",
        binding["identity"], choice_spec(["Approve", "Reject"], ["approve", "reject"]))
    workflow.store.answer(question["id"], "1", binding["identity"])
    human = workflow.approvals.human(rid, work, raw["browser_actions"])
    record = workflow.approvals.publish(rid, work, raw["browser_actions"], human, review,
        context["browser_evidence"], workflow.browser_check_version())
    return document, record, workflow.store.question(question["id"])


@pytest.mark.parametrize("stage", ["prototype", "review"])
async def test_successor_approval_preserves_bound_history_without_promoting_old_consent(tmp_path, monkeypatch, stage):
    workflow, rid, _, docs, _, old_question = await history_fixture(tmp_path, monkeypatch)
    old_approval = workflow.store.get(rid, "prototype_approval")
    document, accepted, current_question = publish_successor_approval(workflow, rid, tmp_path, docs[0])
    old_question = workflow.store.question(old_question["id"])
    history_key = "candidate/prototype-approval-history/" + digest(old_approval)
    assert workflow.store.get(rid, history_key) == old_approval
    assert accepted["identity"] != old_approval["identity"]
    calls = []

    class Agent(ApprovingAgent):
        async def invoke(self, actual_stage, context, *args, **kwargs):
            assert actual_stage == stage
            bridge = context["controller_prototype_reentry"]
            assert bridge["status"] == "historical_reentry"
            assert bridge["current_automatic_gates"] == "not_evaluated"
            roles = {role: row for row in bridge["candidates"] for role in row["roles"]}
            assert set(roles) == {"previous", "recent_actions", "human_baseline"}
            for role, index in [("previous", 3), ("recent_actions", 2), ("human_baseline", 0)]:
                assert workflow.artifacts.json(roles[role]["proposal"]["artifact"]) == docs[index].model_dump(mode="json")
            assert roles["human_baseline"]["consent"]["question"]["id"] == old_question["id"]
            assert roles["human_baseline"]["consent"]["approval_hash"] == digest(old_approval)
            calls.append(context)
            return document if stage == "prototype" else prototype_verdict(context)

    workflow.agent = Agent()
    context = {} if stage == "prototype" else review_context(workflow, document, tmp_path)
    schema = DocumentResult if stage == "prototype" else ReviewResult
    await workflow._invoke(stage, context, schema, tmp_path, rid, "successor-history-" + stage, fresh=True)
    assert len(calls) == 1
    assert workflow.store.get(rid, "prototype_approval") == accepted
    assert workflow.store.question(old_question["id"]) == old_question
    assert workflow.store.question(current_question["id"]) == current_question
    assert workflow.store.run(rid)["data"]["replans"] == 4
    assert workflow.store.get(rid, "rounds/retained") == {"round": 8}
    assert workflow.store.get(rid, "proposal-recovery/count/retained") == 3


@pytest.mark.parametrize("damage", ["missing", "changed", "wrong_key"])
async def test_successor_cannot_replace_missing_or_mismatched_transition_approval(tmp_path, monkeypatch, damage):
    workflow, rid, _, docs, _, _ = await history_fixture(tmp_path, monkeypatch)
    old = workflow.store.get(rid, "prototype_approval")
    document, accepted, question = publish_successor_approval(workflow, rid, tmp_path, docs[0])
    key = "candidate/prototype-approval-history/" + digest(old)
    if damage == "changed":
        workflow.store.checkpoint(rid, key, {**old, "review": {"approved": False, "summary": "Unbound body"}})
    else:
        with workflow.store.connect() as db:
            db.execute("DELETE FROM checkpoints WHERE run_id=? AND key=?", (rid, key))
        if damage == "wrong_key":
            workflow.store.checkpoint(rid, "candidate/prototype-approval-history/" + "f" * 64, old)
    context = workflow._prototype_review_context(rid, tmp_path, review_context(workflow, document, tmp_path))
    assert context["controller_prototype_reentry"]["status"] == "unverified_transition"
    assert context["controller_prototype_reentry"]["candidates"] == []
    assert workflow.store.get(rid, "prototype_approval") == accepted
    assert workflow.store.question(question["id"]) == question


async def test_bound_old_approval_does_not_restore_revoked_human_baseline(tmp_path, monkeypatch):
    workflow, rid, _, docs, _, old_question = await history_fixture(tmp_path, monkeypatch)
    document, accepted, question = publish_successor_approval(workflow, rid, tmp_path, docs[0])
    with workflow.store.connect() as db:
        db.execute("UPDATE questions SET superseded=1 WHERE id=?", (old_question["id"],))
    context = workflow._prototype_review_context(rid, tmp_path, review_context(workflow, document, tmp_path))
    bridge = context["controller_prototype_reentry"]
    assert bridge["status"] == "historical_reentry"
    assert not any("human_baseline" in row["roles"] for row in bridge["candidates"])
    assert bridge["current_automatic_gates"] == "not_evaluated"
    assert workflow.store.get(rid, "prototype_approval") == accepted
    assert workflow.store.question(question["id"]) == question
    assert workflow.store.question(old_question["id"])["superseded"] == 1
