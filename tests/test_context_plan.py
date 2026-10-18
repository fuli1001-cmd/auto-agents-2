import copy
import json

import pytest

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.collection_worker import execute
from auto_agents_2.context import ContextBuilder
from auto_agents_2.context_plan import CONTRACTS, ContextPlanner, purpose_for, remap_references
from auto_agents_2.errors import ProviderError
from auto_agents_2.models import ReviewResult
from auto_agents_2.prompts import ROLES, PromptCompiler
from auto_agents_2.requirements import extract_document
from auto_agents_2.research_sections import extract_sections, select_section
from auto_agents_2.store import Store


@pytest.fixture
def planner(tmp_path):
    store = Store(tmp_path)
    store.admit("fixture", {})
    return ContextPlanner(ArtifactStore(store.root))


def text_of(planner, pointer):
    return planner.artifacts.read(pointer["artifact"])


def research_row(planner, raw, url="https://docs.example/spec#target", **claim):
    import base64
    result = execute(planner.artifacts.store.root, {"action": "research", "source": url.split("#")[0],
        "final_url": url.split("#")[0], "mime": "text/html", "raw": base64.b64encode(raw).decode()})
    return {"url": url, "claim": "The fixture requires approval", "claimed_version": "1", **claim,
            "retrieval": {k: v for k, v in result.items() if k != "excerpt"}, "excerpt": result["excerpt"]}


@pytest.mark.parametrize("stage", list(ROLES))
def test_every_stage_has_a_registered_contract_and_preserves_unknown_constraints(planner, tmp_path, stage):
    context = {"mandatory": ["preserve tests"], "extension_constraint": "must remain exact"}
    result = planner.prepare(stage, context, tmp_path)
    assert result.purpose in CONTRACTS
    assert result.payload["extension_constraint"] == "must remain exact"
    assert context == {"mandatory": ["preserve tests"], "extension_constraint": "must remain exact"}


def test_unknown_stage_or_review_purpose_fails_before_dispatch():
    for stage, context in [("typo", {}), ("review", {"review_kind": "typo"})]:
        with pytest.raises(ProviderError, match="Unregistered"):
            purpose_for(stage, context)


def test_scoped_requirements_keep_original_indexes_and_shared_constraints(planner, tmp_path):
    rid = planner.artifacts.store.current()["id"]
    planner.artifacts.store.checkpoint(rid, "plan_revision/current", {"revision": 1})
    planner.artifacts.store.checkpoint(rid, "plan", {"tasks": [
        {"id": "T1", "requirement_ids": ["R1"]}, {"id": "T2", "requirement_ids": ["R2"]}]})
    context = {"task": {"id": "T2", "requirement_ids": ["R2"], "allowed_paths": ["b.py"]},
        "execution_contract": {"plan_revision": 1},
        "specification": {"document": "GLOBAL: preserve transactional updates across both tasks.",
                          "requirements": [{"id": "R1", "description": "unrelated"},
                                           {"id": "R2", "acceptance": ["keep keyboard support"]}]},
        "history": [{"document": "UNRELATED_HISTORY" * 10000}], "prototype": {"files": ["old UI"]}}
    original = copy.deepcopy(context)
    result = planner.prepare("implement", context, tmp_path, run_id=rid)
    assert context == original and "history" not in result.payload and "prototype" not in result.payload
    spec = result.payload["specification"]
    assert list(spec["requirements"]) == ["1"]
    assert spec["requirements"]["1"]["source_ref"] == "specification.requirements[1]"
    assert spec["document"] == context["specification"]["document"]
    alias = spec["all_requirements"]["material_id"]
    assert remap_references({"evidence_refs": [alias]}, result.reference_map) == {"evidence_refs": ["specification"]}


def test_issues_filter_only_unrelated_owners_and_preserve_unknown_shared_and_dependency_issues(planner, tmp_path):
    issues = [{"id": "own", "scope": "task/T2/implementation", "status": "open"},
              {"id": "dependency", "scope": "checks/T1", "status": "stale"},
              {"id": "other", "scope": "task/T3/implementation", "status": "open"},
              {"id": "shared", "scope": "task/T3/implementation", "status": "open",
               "original": {"location": "shared.py:5"}},
              {"id": "unknown", "status": "open"}, {"id": "closed", "status": "closed"}]
    result = planner.prepare("implement", {"task": {"id": "T2", "depends_on": ["T1"], "allowed_paths": ["shared.py"]},
        "unresolved_issues": issues}, tmp_path)
    assert set(result.payload["unresolved_issues"]) == {"0", "1", "3", "4"}
    assert result.payload["unresolved_issues"]["3"]["source_ref"] == "unresolved_issues[3]"


def test_patch_correction_only_supplies_same_file_predecessors_and_failed_edit(planner, tmp_path):
    changes = [{"path": "a.py", "old_text": "one", "content": "ONE"},
               {"path": "other.py", "content": "UNRELATED_CONTENT"},
               {"path": "a.py", "old_text": "two", "content": "TWO"},
               {"path": "a.py", "old_text": "three", "content": "THREE"}]
    context = {"patch_repair": {"failure": {"path": "a.py", "change_index": 2}, "candidate_changes": changes},
               "history": ["unrelated"], "design_candidates": ["unrelated"]}
    result = planner.prepare("implement", context, tmp_path)
    assert result.purpose == "patch_repair"
    assert set(result.payload["patch_repair"]["candidate_changes"]) == {"0", "2"}
    assert "UNRELATED_CONTENT" not in json.dumps(result.payload)
    assert "history" not in result.payload and "design_candidates" not in result.payload


def test_checks_keep_failure_state_and_full_evidence_without_repeating_passed_ids(planner, tmp_path):
    ids = [f"case-{i}" for i in range(2000)]
    check = {"argv": ["pytest"], "source_hash": "source", "environment_hash": "env", "process_exit_code": 0,
             "exit_code": 1, "report_status": "invalid", "failure_reasons": ["report_mismatch"],
             "tests": 1999, "skipped": 0, "test_ids": ids, "passed_test_ids": ids[:-1], "output": "failed case"}
    result = planner.prepare("verify", {"evidence": [check]}, tmp_path)
    projected = result.payload["evidence"][0]
    assert projected["unpassed_test_ids"] == ["case-1999"]
    assert projected["exit_code"] == 1 and projected["process_exit_code"] == 0
    assert projected["failure_reasons"] == ["report_mismatch"] and projected["report_status"] == "invalid"
    assert json.loads(text_of(planner, projected["observation"])) == check
    assert "test_ids" not in projected and "passed_test_ids" not in projected


def test_source_selection_prefers_code_and_direct_imports_to_large_readme(tmp_path):
    (tmp_path / "README.md").write_text("documentation " * 2000)
    (tmp_path / "AGENTS.md").write_text("Preserve public contracts")
    (tmp_path / "app.py").write_text("from shared import helper\nVALUE = helper()\n")
    (tmp_path / "shared.py").write_text("def helper(): return 1\n")
    (tmp_path / "unrelated.py").write_text("unrelated")
    (tmp_path / ".env").write_text("SECRET=EXAMPLE_PLACEHOLDER")
    builder = ContextBuilder()
    general = builder.select(tmp_path, limit=1000)
    assert "app.py" in [r["path"] for r in general["files"]]
    scoped = builder.select(tmp_path, scopes=["app.py"], limit=1000)
    assert {r["path"] for r in scoped["files"]} == {"AGENTS.md", "app.py", "shared.py"}
    assert ".env" not in json.dumps(scoped)


@pytest.mark.parametrize("container", [False, True])
def test_section_ranges_preserve_exact_text_tables_links_and_following_constraints(container):
    target = '<section id="target"><h2>Target</h2>' if container else '<h2 id="target">Target</h2>'
    ending = "</section>" if container else ""
    raw = ('<h1>Version 1</h1><p>Global rule applies.</p>' + target +
           '<p>Requires permission.</p><table><tr><td>A</td><td>B</td></tr></table>'
           '<p>Exception: see <a href="#constraint">inherited rule</a>.</p>' + ending +
           '<h2 id="constraint">Other</h2><p>Inherited rule must also hold.</p>').encode()
    text, _, index = extract_sections(raw, "text/html")
    assert text == extract_document(raw, "text/html")[0]
    selected = select_section(index, "target")
    section = text[selected["start"]:selected["end"]]
    assert "Requires permission" in section and " | A | B" in section and "(#constraint)" in section
    assert "Inherited rule must also hold" not in section
    assert selected["headings"]


def test_research_reads_selected_section_and_keeps_other_constraints_accessible(planner, tmp_path):
    raw = ('<h1>Version 1</h1><h2>Unrelated</h2><p>' + "IRRELEVANT " * 120000 +
           '</p><h2 id="target">Target</h2><p>Requires approval; see <a href="#other">global rule</a>.</p>'
           '<h2 id="other">Global rule</h2><p>Requires authentication.</p>').encode()
    row = research_row(planner, raw)
    context = {"review_kind": "research", "retrieved_sources": [row]}
    result = planner.prepare("review", context, tmp_path)
    selected = result.payload["retrieved_sources"][0]
    assert "IRRELEVANT" not in json.dumps(result.payload)
    assert "Requires approval" in selected["excerpt"] and "(#other)" in selected["excerpt"]
    assert "Requires authentication" in text_of(planner, selected["full_text"])
    assert "raw" not in selected["retrieval"]
    prompt, _ = PromptCompiler().compile("review", result.payload)
    assert len(prompt) < 64000
    assert context["retrieved_sources"][0]["excerpt"] == planner.artifacts.read(row["retrieval"]["artifact"])


@pytest.mark.parametrize("fragment,expected", [("absent", "missing_anchor"), ("target", "ambiguous_anchor"), ("", "no_anchor")])
def test_unresolved_research_anchor_supplies_index_without_fabricating_an_excerpt(planner, tmp_path, fragment, expected):
    raw = ('<h2 id="target">One</h2><p>' + "CONTENT " * 5000 +
           '</p><h2 id="target">Two</h2><p>restriction</p>').encode()
    row = research_row(planner, raw, "https://docs.example/spec#" + fragment)
    selected = planner.prepare("review", {"review_kind": "research", "retrieved_sources": [row]}, tmp_path).payload["retrieved_sources"][0]
    assert selected["selection"]["status"] == expected
    assert "excerpt" not in selected and selected["section_index"]


def test_probe_and_readme_do_not_inherit_business_workflow_rules(planner, tmp_path):
    probe = planner.prepare("material_probe", {"context_files": ["fixture"]}, tmp_path)
    prompt, _ = PromptCompiler().compile("material_probe", probe.payload)
    assert len(prompt) < 2000
    assert "runtime_prepare" not in prompt and "task_origins" not in prompt and "design catalogue" not in prompt
    docs = planner.prepare("readme", {"history": ["old"], "prototype": {"files": ["old"]},
                                     "specification": {"requirements": ["current behaviour"]}}, tmp_path)
    assert "history" not in docs.payload and "prototype" not in docs.payload


def test_frozen_presentation_is_reused_and_corruption_stops_dispatch(tmp_path):
    from test_runtime import configured

    from auto_agents_2.providers import ProviderRouter
    from auto_agents_2.sandbox import SandboxRunner
    config, store = configured(tmp_path)
    rid = store.admit("fixture", {})["id"]
    router = ProviderRouter(config, store, SandboxRunner(store.root))
    first = router._prepare_context("review", {"mandatory": ["original"]}, tmp_path, rid, "review", "identity")
    second = router._prepare_context("review", {"mandatory": ["changed"]}, tmp_path, rid, "review", "identity")
    assert second.record() == first.record()
    record = store.get(rid, "presentation/review/identity")
    sha = record["artifact"]["hash"]
    (store.root / "artifacts" / sha[:2] / sha).write_text("corrupt")
    with pytest.raises(ValueError, match="integrity"):
        router._prepare_context("review", {}, tmp_path, rid, "review", "identity")


def test_presentation_version_does_not_change_logical_call_identity(tmp_path, monkeypatch):
    from test_runtime import configured

    from auto_agents_2.evidence import invocation_identity
    config, _ = configured(tmp_path)
    before = invocation_identity("review", {}, ReviewResult, tmp_path, config.load(), source_hash="fixture")
    monkeypatch.setattr("auto_agents_2.prompts.PROMPT_VERSION", "new-rendering-only")
    assert invocation_identity("review", {}, ReviewResult, tmp_path, config.load(), source_hash="fixture") == before


def test_equal_text_in_different_records_never_ambiguously_rebinds_a_hash(planner, tmp_path):
    context = {"specification": {"document": "shared text" * 2000}, "design": {"document": "shared text" * 2000}}
    result = planner.prepare("implement", context, tmp_path)
    spec = result.payload["specification"]["document"]
    design = result.payload["design"]["document"]
    assert remap_references({"evidence_refs": [spec["material_id"], design["material_id"]]}, result.reference_map) == {
        "evidence_refs": ["specification.document", "design.document"]}
    with pytest.raises(ProviderError, match="Ambiguous"):
        remap_references({"evidence_refs": [spec["artifact"]["hash"]]}, result.reference_map)


def test_diagnosis_plan_keeps_global_graph_and_original_task_indexes(planner, tmp_path):
    tasks = [{"id": "T1", "acceptance": ["upstream"], "depends_on": []},
             {"id": "T2", "acceptance": ["current"], "depends_on": ["T1"]},
             {"id": "T3", "acceptance": ["unrelated"], "depends_on": []}]
    plan = {"tasks": tasks, "document": "Global plan constraints"}
    result = planner.prepare("recovery_diagnosis", {"failure": {"task_id": "T2", "origin_id": "T2"}, "plan": plan}, tmp_path)
    view = result.payload["plan"]
    assert list(view["tasks"]) == ["0", "1"]
    assert view["tasks"]["1"]["source_ref"] == "plan.tasks[1]"
    assert [t["id"] for t in view["task_graph"]] == ["T1", "T2", "T3"]
    assert json.loads(text_of(planner, view["full_plan"])) == plan


def test_unassigned_global_requirement_survives_without_task_id(planner, tmp_path):
    context = {"task": {"id": "T1", "requirement_ids": ["R1"]}, "specification": {
        "requirements": [{"id": "R1", "acceptance": ["feature"]},
                         {"id": "GLOBAL", "acceptance": ["never log private records"]}]}}
    projected = planner.prepare("implement", context, tmp_path).payload["specification"]
    assert projected["requirements"]["1"]["acceptance"] == ["never log private records"]


def test_section_index_cannot_be_substituted_from_another_equal_length_document(planner, tmp_path):
    first = research_row(planner, b'<h2 id="target">A</h2><p>Requires X.</p>')
    second = research_row(planner, b'<h2 id="target">B</h2><p>Requires Y.</p>')
    second["retrieval"]["section_index"] = first["retrieval"]["section_index"]
    with pytest.raises(ProviderError, match="different text identity"):
        planner.prepare("review", {"review_kind": "research", "retrieved_sources": [second]}, tmp_path)


def test_short_linked_definition_is_inlined_whole_to_avoid_unnecessary_read_round(planner, tmp_path):
    row = research_row(planner, b'<h2 id="target">Export</h2><p>Requires X and <a href="#other">membership</a>.</p>'
                       b'<h2 id="other">Membership</h2><p>Permission Y is also mandatory.</p>')
    view = planner.prepare("review", {"review_kind": "research", "retrieved_sources": [row]}, tmp_path).payload
    assert "Permission Y is also mandatory" in view["retrieved_sources"][0]["linked_sections"][0]["excerpt"]


def test_history_from_other_branch_is_excluded_and_unknown_ancestry_stays_visible(planner, tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr("auto_agents_2.context_plan.subprocess.run", lambda *a, **k: SimpleNamespace(returncode=1))
    rows = [{"run": "other", "delivery": "a" * 40, "specification": {"document": "OTHER_BRANCH_ONLY"}},
            {"run": "legacy", "delivery": "", "specification": {"document": "KEEP_UNKNOWN"}}]
    view = planner.prepare("clarify", {"history": rows}, tmp_path).payload["history"]
    assert [r["run"] for r in view["index"]] == ["legacy"]
    assert view["index"][0]["baseline_relation"] == "unconfirmed"
    assert "OTHER_BRANCH_ONLY" not in text_of(planner, view["records"])


def test_small_complete_specification_does_not_require_an_extra_material_read(planner, tmp_path):
    context = {"task": {"id": "T1", "requirement_ids": ["R1"]},
               "specification": {"document": "Add numbers.", "requirements": [{"id": "R1", "acceptance": ["2+3=5"]}]}}
    view = planner.prepare("implement", context, tmp_path).payload
    assert view["specification"]["document"] == "Add numbers."
    assert "all_requirements" not in view["specification"]


def test_project_package_imports_side_effect_styles_and_nested_rules_are_selected(tmp_path):
    for name, body in {
        "backend/app/main.py": "from app.repository import load\n",
        "backend/app/repository.py": "def load(): return []\n",
        "backend/AGENTS.md": "Preserve backend transactions.",
        "frontend/src/App.tsx": 'import "./style.css";\nexport const App = () => null;\n',
        "frontend/src/style.css": ".button {color: blue}",
        "unrelated/AGENTS.md": "UNRELATED_RULE",
        "unrelated/package.json": '{"name":"unrelated"}',
    }.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    builder = ContextBuilder()
    python = builder.select(tmp_path, scopes=["backend/app/main.py"])
    assert {r["path"] for r in python["files"]} == {
        "backend/app/main.py", "backend/app/repository.py", "backend/AGENTS.md"}
    javascript = builder.select(tmp_path, scopes=["frontend/src/App.tsx"])
    assert {r["path"] for r in javascript["files"]} == {"frontend/src/App.tsx", "frontend/src/style.css"}
    general = builder.select(tmp_path)
    assert "backend/AGENTS.md" in [r["path"] for r in general["files"]]


def test_identical_recovery_outcomes_are_inline_once_without_losing_original_positions(planner, tmp_path):
    outcome = {"status": "validated", "diagnosis": {"reason": "Earlier unrelated environment problem"}}
    rows = [{"action": {"action": "diagnose"}, "status": "completed", "outcome": outcome} for _ in range(16)]
    context = {"decision_request": {"evidence": {"history": {"recent": rows, "total_actions": 16}},
                                    "scope": {"candidates": {"recheck": {}}}}}
    original = copy.deepcopy(context)
    view = planner.prepare("decision", context, tmp_path).payload["decision_request"]["evidence"]["history"]
    assert context == original and view["recent"][0]["outcome"] == outcome
    assert view["recent"][15]["source_ref"] == "history.recent[15]"
    assert view["recent"][15]["outcome"] == {"source_ref": "history.recent[0].outcome", "same_content": True}
    assert "details" not in view and view["total_actions"] == 16


def test_live_doctor_uses_only_the_provider_contract_role(planner, tmp_path):
    view = planner.prepare("review", {"task": "Return approved=true, summary='verified', findings=[]"}, tmp_path, "doctor")
    prompt, _ = PromptCompiler().compile("review", view.payload)
    assert "completed implementation gate" not in prompt
    assert "actual code and independently executed task evidence" not in prompt
    assert "Do not execute tools" in prompt and len(prompt) < 2000
