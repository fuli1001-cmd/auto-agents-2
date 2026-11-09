"""The authorized correction still traverses real task checks and review gates."""
import asyncio
import json
import sys

import pytest
from authority_review_fixtures import authority_review
from test_workflow import CodingAgent, engine
from test_output_limit_recovery import approve_runtime_messages, double_launch

from auto_agents_2.errors import AA2Error
from auto_agents_2.models import Change, ImplementationResult, PlanResult, TaskSpec, strict_schema
from auto_agents_2.output_limit_recovery import CALL, KEY, code_identity, raw_hash
from auto_agents_2.providers import PROMPT_VERSION, VIEW_VERSION, ProviderRouter, invocation_identity
from auto_agents_2.replanning import check_criteria
from auto_agents_2.state_codec import checkpoint_query_db
from auto_agents_2.util import digest, write_json
from auto_agents_2.workspace import tree_hash


class IncidentAgent(CodingAgent, ProviderRouter):
    """Deterministic transport double, with the same durable receipt boundary."""
    def __init__(self, mode):
        CodingAgent.__init__(self)
        self.mode, self.interrupt = mode, True
        self.incident_roles = []
        self.native = None

    def execution_identity(self, stage):
        return self.runtime_router.execution_identity(stage) if hasattr(self, "runtime_router") else "fixture-native-input-identity"

    async def invoke(self, stage, context, schema, workspace, run_id, operation, fresh=False):
        if stage == "implement" and self.interrupt:
            self.interrupt = False
            self.original = (context, workspace, operation)
            raise asyncio.CancelledError
        scope = CALL.get()
        if scope and scope.role == "correction" and self.native:
            result = await self.native.invoke(stage, context, schema, workspace, run_id, operation, fresh)
            self.incident_roles.append(scope.role)
            return result
        result = await CodingAgent.invoke(self, stage, context, schema, workspace, run_id, operation, fresh)
        if not scope:
            return result
        if scope.role == "correction" and self.mode == "bad-anchor":
            result = ImplementationResult(summary="bad fixture anchor", changes=[
                Change(path="calc.py", operation="replace", old_text="missing original anchor", content="new")])
        elif scope.role == "correction" and self.mode == "failed-check":
            result = result.model_copy(update={"changes": [
                change.model_copy(update={"content": change.content.replace("return a + b", "return a - b")})
                for change in result.changes]})
        elif scope.role == "implementation_review" and self.mode == "rejected-review":
            result = authority_review(context, approved=False, summary="fixture implementation rejection")
        identity = invocation_identity(stage, context, schema, workspace, self.workflow.config.load(),
                                       execution=self.execution_identity(stage))
        attempt = "fixture-" + scope.role
        await scope.ensure_runtime(self.runtime_router, context, schema, workspace)
        launch = double_launch(scope, self.runtime_router, context, schema, workspace, identity)
        scope.claim(attempt, identity, launch)
        self.incident_roles.append(scope.role)
        artifact = self.workflow.artifacts.put(result.model_dump(mode="json"), stage)
        self.workflow.store.checkpoints(run_id, {
            "provider-result/" + operation: {"attempt": attempt, "artifact": artifact},
            "invocation/" + operation: {"attempt": attempt, "identity": identity,
                "result": result.model_dump(mode="json"), "artifact": artifact,
                "incident_launch": launch,
                "cleanup_record": str(self.workflow.store.root / "processes" / (attempt + ".json"))}})
        scope.complete(result)
        return result


def native_correction_fixture(workflow, agent, root):
    """Exercise the real Router, managed process and receipt inside the Engine."""
    native = root / "native"
    native.mkdir()
    binary, counter = root / "fixture-provider", root / "model-invocations"
    proposal = ImplementationResult(summary="implemented", changes=[
        Change(path="calc.py", content="def add(a, b):\n    return a + b\n"),
        Change(path="tests/test_calc.py", content="from calc import add\n\ndef test_sum():\n    assert add(2, 3) == 5\n")])
    binary.write_text(f"#!{sys.executable}\nRESPONSE={json.dumps(proposal.model_dump())!r}\nCOUNTER={str(counter)!r}\n" + '''
import json, pathlib, sys
args = sys.argv[1:]
if args == ['--version']:
    print('codex-cli 0.165.0')
    raise SystemExit(0)
with open(COUNTER, 'a') as stream:
    stream.write('model invocation\\n')
sys.stdin.read()
pathlib.Path(args[args.index('-o') + 1]).write_text(RESPONSE)
for event in [{'type':'thread.started','thread_id':'fixture-native-thread'},
              {'type':'turn.started'}, {'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}]:
    print(json.dumps(event), flush=True)
''')
    binary.chmod(0o755)
    body = workflow.config.load().model_dump(mode="json")
    body["providers"] = {"codex": body["providers"]["codex"]}
    body["active_provider"] = "codex"
    body["providers"]["codex"].update(binary=str(binary), environment={"CODEX_HOME": str(native)})
    body["providers"]["codex"]["profile_map"] = {level: {"model": "test"} for level in ("balanced", "deep", "max")}
    write_json(workflow.config.path, body)
    agent.native = ProviderRouter(workflow.config, workflow.store, workflow.sandbox)
    agent.runtime_router = agent.native
    return binary, counter


def attach_incident(workflow, agent, rid, *, prior_comparable=False):
    """Admission/CAS has separate native-database tests; this seeds runtime facts."""
    context, work, operation = agent.original
    task = context["task"]
    origin = task["origin_id"] or task["id"]
    infra = "infra/" + operation + "/codex"
    logical = invocation_identity("implement", context, ImplementationResult, work, workflow.config.load())
    workflow.store.checkpoints(rid, {infra: {"failures": 2, "grant": 0, "pending": False,
        "error_kind": "unavailable", "error_input": logical}, "rounds/" + origin: {"round": 28, "stalled": 11}})
    if prior_comparable:
        commands = workflow._task_commands(rid, TaskSpec.model_validate(task), work,
            PlanResult.model_validate(workflow.store.get(rid, "plan")))
        previous = workflow.convergence.observe(rid, origin, ["fixture", "prior-unpassed-checks"],
            {"criteria": check_criteria(commands, [], tree_hash(work), "fixture-prior-environment")})
        assert previous["last"]["baseline"] and previous["stalled"] == 11
    keys = [infra, "plan", "plan_revision/current", "rounds/" + origin,
            "recovery/active", "replanning/pending", "control/cancel"]
    guards = {}
    with workflow.store.connect() as db:
        for key in keys:
            row = checkpoint_query_db(db, 'SELECT slot_order AS data FROM checkpoint_records WHERE run_id=? AND key=?', (rid, key), wire_index=0).fetchone()
            guards[key] = raw_hash(row[0]) if row else None
    request = {"run_id": rid, "operation": operation, "task_id": task["id"], "origin_id": origin,
        "task_spec_hash": digest(task), "round": 28, "infra_key": infra, "guards": guards,
        "code_identity": code_identity(), "config_hash": digest(workflow.config.load().model_dump(mode="json")),
        "source_hash": tree_hash(work), "execution_input": invocation_identity("implement", context,
            ImplementationResult, work, workflow.config.load(), execution=agent.execution_identity("implement")),
        "workspace": str(work), "schema_hash": digest(strict_schema(ImplementationResult)),
        "logical_input": logical, "provider": "codex", **{name: workflow.config.resolve("implement", "codex")[name]
                                                         for name in ("model", "effort")}}
    question = workflow.store.request(rid, "provider/" + operation, "fixture original provider gate", "fixture-original")
    with workflow.store.connect() as db:
        row = db.execute("SELECT id,key,artifact_hash,answer,superseded FROM questions WHERE id=?", (question["id"],)).fetchone()
        request.update(question_id=question["id"], question=dict(row), authorization_reference="fixture correction decision")
        db.execute("UPDATE questions SET superseded=1 WHERE id=?", (question["id"],))
    from auto_agents_2.output_limit_runtime import budget_for_request
    workflow.store.checkpoint(rid, KEY, {"state": "active", "request": request, "calls": {},
        "limits": budget_for_request(request), "runtime_binding": {"state": "unbound"}, "runtime_quotes": {}})
    return infra


@pytest.mark.parametrize("mode,roles", [
    ("success", ["correction", "protected_review", "implementation_review"]),
    ("progress-improvement", ["correction", "protected_review", "implementation_review"]),
    ("bad-anchor", ["correction"]),
    ("failed-check", ["correction", "protected_review"]),
    ("integration-failure", ["correction", "protected_review", "implementation_review"]),
    ("restart-after-publication", ["correction", "protected_review", "implementation_review"]),
    ("restart-after-protected-review", ["correction", "protected_review", "implementation_review"]),
    ("restart-after-implementation-review", ["correction", "protected_review", "implementation_review"]),
    ("rejected-review", ["correction", "protected_review", "implementation_review"]),
])
async def test_incident_task_preserves_checks_reviews_rounds_and_stops_failed_candidate(tmp_path, monkeypatch, mode, roles):
    project = tmp_path / "project"
    project.mkdir()
    requirements = tmp_path / "requirements.md"
    requirements.write_text("Add two numbers")
    agent = IncidentAgent(mode)
    workflow = engine(project, agent)
    agent.workflow = workflow
    native = native_correction_fixture(workflow, agent, tmp_path)
    if not mode.startswith("restart-"):
        agent.native, native = None, None
    with pytest.raises(asyncio.CancelledError):
        await workflow.start(str(requirements))
    rid = workflow.store.current(unfinished=True)["id"]
    if native:
        effective = agent.native.effective("implement", "codex")
        capability = digest([effective["fingerprint"], digest(native[0].read_bytes()), digest("codex-cli 0.165.0\n"),
                             VIEW_VERSION, PROMPT_VERSION, workflow.config.load().sensitive_paths, False])
        workflow.store.checkpoint(rid, "material-capability/" + capability, {"status": "observed_read"})
    infra_key = attach_incident(workflow, agent, rid, prior_comparable=mode == "progress-improvement")
    original_start = workflow.start
    async def approved_start(*args, **kwargs):
        async with approve_runtime_messages(workflow.store, rid):
            return await original_start(*args, **kwargs)
    monkeypatch.setattr(workflow, "start", approved_start)
    before = workflow.store.get(rid, infra_key)
    if mode == "integration-failure":
        def fail(*args):
            raise AA2Error("fixture integration conflict")
        monkeypatch.setattr(workflow.workspaces, "integrate", fail)
    if mode.startswith("restart-"):
        original_invoke = workflow._invoke
        interrupt_operation = "develop/task/T1" + {
            "restart-after-publication": "", "restart-after-protected-review": "/test-change-review",
            "restart-after-implementation-review": "/review"}[mode]
        async def interrupt_after_publication(stage, *args, **kwargs):
            result = await original_invoke(stage, *args, **kwargs)
            if args[4] == interrupt_operation:
                raise asyncio.CancelledError
            return result
        monkeypatch.setattr(workflow, "_invoke", interrupt_after_publication)
        with pytest.raises(asyncio.CancelledError):
            await workflow.start()
        receipt = workflow.store.get(rid, "invocation/develop/task/T1")
        assert receipt["provider_receipt"]["attempt"] == workflow.store.get(rid, KEY)["calls"]["correction"]["attempt"]
        assert native[1].read_text().splitlines() == ["model invocation"]
        monkeypatch.setattr(workflow, "_invoke", original_invoke)
    result = await workflow.start()
    assert agent.incident_roles == roles
    if native:
        assert native[1].read_text().splitlines() == ["model invocation"]
        restored = [event for event in workflow.store.events(rid) if event["kind"] == "output_limit.proposal_restored"]
        assert len(restored) == (1 if mode == "restart-after-publication" else 0)
    assert workflow.store.get(rid, infra_key) == before
    assert workflow.store.get(rid, "rounds/T1")["round"] == 28
    incident = workflow.store.get(rid, KEY)
    if mode in {"success", "progress-improvement"} or mode.startswith("restart-"):
        assert result["status"] == "SUCCEEDED" and incident["state"] == "accepted"
        progress = workflow.store.get(rid, "automation/progress/T1")
        if mode == "progress-improvement":
            assert progress["last"]["improved"] and progress["stalled"] == 0
        else:
            # An initial comparable observation is a baseline; original Convergence
            # preserves the legacy diagnostic count instead of granting fresh credit.
            assert progress["last"]["baseline"] and progress["stalled"] == 11
        assert workflow.store.get(rid, "rounds/T1")["stalled"] == progress["stalled"]
        assert "T1" in workflow.store.get(rid, "develop/done")
        assert workflow.store.get(rid, "task_state/T1")["evidence"] == "valid"
        assert "return a + b" in (project / "calc.py").read_text()
    else:
        assert result["status"] == "PAUSED" and incident["state"] == "stopped"
        for _ in range(2):
            assert (await workflow.start())["status"] == "PAUSED"
        assert agent.incident_roles == roles
        assert workflow.store.get(rid, KEY) == incident
        assert "T1" not in workflow.store.get(rid, "develop/done", [])
