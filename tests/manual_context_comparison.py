"""Ten isolated live calls against frozen fixtures and the user's existing profiles.

No provider configuration is copied to the report or evaluation source tree.
The baseline is test-only; production has one context preparation path.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import json
import sys
import time
from pathlib import Path

from fixtures.input_baseline_prompts import PromptCompiler as BaselineCompiler

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.collection_worker import execute
from auto_agents_2.config import ConfigStore
from auto_agents_2.context import ContextBuilder
from auto_agents_2.context_plan import PreparedContext, purpose_for
from auto_agents_2.decisions import (
    DecisionRequest,
    DecisionResult,
    DecisionService,
    DecisionValidationError,
    decision_reference_catalog,
)
from auto_agents_2.models import Command, ImplementationResult, ReviewResult
from auto_agents_2.providers import ProviderRouter
from auto_agents_2.resources import ResourceManager
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.store import Store
from auto_agents_2.util import digest, write_json
from auto_agents_2.verification import VerificationService
from auto_agents_2.workspace import apply_prepared, prepare_changes


class BaselineRouter(ProviderRouter):
    def _prepare_context(self, stage, context, workspace, run_id, operation, identity):
        return PreparedContext(copy.deepcopy(context), purpose_for(stage, context), version="baseline-inline")


async def cases(project, store):
    artifacts = ArtifactStore(store.root)
    (project / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (project / "AGENTS.md").write_text("This is an isolated fixture. Preserve the assigned scope and never install packages.\n")
    history = [{"run": f"old-{i}", "specification": {"document": "An unrelated archived feature. " * 180,
               "requirements": [{"id": "R1", "description": f"Unrelated module {i}"}]}}
               for i in range(16)]
    spec = {"document": "Implement addition without changing public function names.",
            "requirements": [{"id": "R1", "description": "add(a,b) returns the sum", "acceptance": ["positive and negative integers"]}]}
    task = {"id": "T1", "origin_id": "T1", "title": "Addition", "description": "Fix add(a,b)",
            "requirement_ids": ["R1"], "depends_on": [], "allowed_paths": ["calc.py"],
            "acceptance": ["add(2,3)==5 and add(-2,2)==0"], "checks": []}
    source = ContextBuilder().build(project)
    raw = ('<h1>Fixture API version 1</h1><h2>Unrelated endpoints</h2><p>' +
           "Unrelated endpoints use JSON. " * 4000 + '</p><h2 id="exports">Exports</h2>'
           '<p>Exports require export:read. Additional authorization is defined in '
           '<a href="#membership">Membership</a>.</p><h2 id="membership">Membership</h2>'
           '<p>Every export also requires project:member. Neither permission alone is sufficient.</p>').encode()
    captured = execute(store.root, {"action": "research", "source": "https://docs.example/fixture",
        "final_url": "https://docs.example/fixture", "mime": "text/html", "raw": base64.b64encode(raw).decode()})
    research = {"review_kind": "research", "task": "Review the claim using only the frozen owner-supplied fixture documents; do not browse.",
        "retrieved_sources": [{"url": "https://docs.example/fixture#exports", "claim": "export:read alone authorizes an export.",
            "claimed_version": "1", "retrieval": {k: v for k, v in captured.items() if k != "excerpt"}, "excerpt": captured["excerpt"]}]}
    checks = {"argv": ["pytest"], "cwd": ".", "source_hash": "fixture-source", "environment_hash": "fixture-env",
        "process_exit_code": 1, "exit_code": 1, "report_status": "valid", "failure_reasons": ["test_failed"],
        "tests": 1999, "skipped": 0, "output": "case-1999: add(2,3) expected 5; actual -1",
        "test_ids": [f"case-{i}" for i in range(2000)], "passed_test_ids": [f"case-{i}" for i in range(1999)]}
    request = DecisionRequest(kind="recovery_action", owner="T1", allowed_choices=["approve", "ask_user"],
        scope={"one_action": True, "allowed_paths": ["calc.py"], "candidates": {
            "recheck": {"action": "recheck", "reason": "Collect the missing machine report with the existing command"}}},
        evidence={"failure": {"kind": "evidence", "reason_code": "test_report_missing",
            "evidence": {"process_exit_code": 0, "report_status": "missing", "product_failure_observed": False}},
            "history": {"total_actions": 16, "by_action": {"diagnose": 16}, "older_actions": 0,
                "recent": [{"action": {"action": "diagnose", "task_ids": ["T1"]}, "status": "completed",
                            "outcome": {"status": "validated", "diagnosis": {"notes": "Earlier unrelated environment investigation. " * 100}}}
                           for _ in range(16)]},
            "consumption": {"implementation": {"round": 1}, "diagnosis": {"round": 16}, "tokens": {"total_tokens": 1000}}},
        state_identity="frozen-fixture")
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 800, "height": 400})
            await page.set_content('<body style="background:white;font:24px sans-serif;padding:40px"><h1>Draft editor</h1>'
                                   '<button style="background:#d11;color:white;padding:16px">Delete all</button></body>')
            screenshot = store.root / "fixture.png"
            await page.screenshot(path=str(screenshot))
        finally:
            await browser.close()
    image_ref = artifacts.put_file(screenshot, "screenshot")
    visual = {"review_kind": "prototype", "specification": {"requirements": [
        {"id": "UI1", "description": "The primary action is a blue button labelled Save draft.",
         "acceptance": ["No destructive primary action"]}]},
        "prototype": {"summary": "Fixture candidate"},
        "browser_evidence": [{"route": "/", "screenshot": str(artifacts.root / image_ref["hash"][:2] / image_ref["hash"])}],
        "task": "Review the actual supplied screenshot against UI1; report visible mismatches. Do not browse."}
    return [
        ("research", "review", research, ReviewResult, None),
        ("implementation", "implement", {"task": task, "specification": spec, "project": source, "history": history}, ImplementationResult, None),
        ("task_review", "review", {"task": task, "specification": spec, "project": source, "checks": [checks], "history": history}, ReviewResult, None),
        ("decision", "decision", {"decision_request": request.model_dump()}, DecisionResult, request),
        ("visual_review", "review", visual, ReviewResult, None),
    ]


async def oracle(name, response, request, project, store, sandbox, operation):
    if name == "implementation":
        work = store.root / "validation" / operation.replace("/", "_")
        work.mkdir(parents=True)
        (work / "calc.py").write_bytes((project / "calc.py").read_bytes())
        apply_prepared(work, prepare_changes(work, response.changes, ["calc.py"]))
        checks = await VerificationService(store.root, sandbox).run([Command(argv=[sys.executable, "-c",
            "from calc import add; assert add(2,3)==5; assert add(-2,2)==0"])], work)
        return bool(response.changes) and all(c.exit_code == 0 for c in checks)
    if name == "decision":
        DecisionService.validate(request, response)
        return response.action == "approve" and response.selected_action == "recheck"
    findings = not response.approved and any(f.severity in {"blocking", "major"} for f in response.findings)
    text = response.model_dump_json().lower()
    return findings and ("project:member" in text if name == "research" else
                         "delete all" in text if name == "visual_review" else True)


async def evaluate(provider_project, project, output, rerun=(), revision="initial"):
    from auto_agents_2.design import browser_environment
    browser_environment(provider_project / ".aa2/cache/browsers")
    config = ConfigStore(provider_project)
    config.load()  # Read only; do not activate or copy account configuration.
    project.mkdir(parents=True, exist_ok=True)
    store = Store(project)
    previous = store.current(unfinished=True)
    latest = store.current()
    if previous and previous["data"]["source"] != "isolated-context-comparison":
        raise ValueError("Evaluation project has a different unfinished operation")
    rid = previous["id"] if previous else store.admit("isolated-context-comparison", {})["id"]
    sandbox = SandboxRunner(store.root, config.load().sandbox_binary)
    report = (json.loads(output.read_text()) if (previous or rerun and latest) and output.is_file() else
              {"scope": "Five paired frozen fixtures; not universal quality equivalence", "passed": False,
               "project": str(project), "observations": [], "errors": []})
    report["runs"] = list(dict.fromkeys([*report.get("runs", [latest["id"]] if latest else []), rid]))
    if rerun:
        stale = [r for r in report["observations"] if r["case"] in rerun and r["variant"] == "after" and r.get("revision", "initial") != revision]
        report.setdefault("superseded_observations", []).extend(stale)
        report["observations"] = [r for r in report["observations"] if r not in stale]
        for name in rerun:
            old = store.get(rid, f"provider-result/compare/{name}/after")
            if old and not any(r["case"] == name for r in report.get("superseded_observations", [])):
                report.setdefault("superseded_observations", []).append({"case": name, "variant": "after",
                    "revision": "initial", "correct": False, "main_usage": old["usage"],
                    "response": ArtifactStore(store.root).json(old["artifact"])})
    write_json(output, report)
    try:
        frozen = store.get(rid, "evaluation/fixtures") or (store.get(latest["id"], "evaluation/fixtures") if latest else None)
        if frozen:
            store.checkpoint(rid, "evaluation/fixtures", frozen)
            types = {c.__name__: c for c in (ReviewResult, ImplementationResult, DecisionResult)}
            fixtures = [(row["name"], row["stage"], row["context"], types[row["schema"]],
                         DecisionRequest.model_validate(row["request"]) if row["request"] else None) for row in frozen]
        else:
            fixtures = await cases(project, store)
            store.checkpoint(rid, "evaluation/fixtures", [{"name": name, "stage": stage, "context": context,
                "schema": schema.__name__, "request": request.model_dump() if request else None}
                for name, stage, context, schema, request in fixtures])
        for index, (name, stage, context, schema, request) in enumerate(fixtures):
            for variant in (("before", "after") if index % 2 == 0 else ("after", "before")):
                if any(r["case"] == name and r["variant"] == variant for r in report["observations"]):
                    continue
                router = (BaselineRouter if variant == "before" else ProviderRouter)(config, store, sandbox)
                if variant == "before":
                    router.compiler = BaselineCompiler()
                effective = router.effective(stage, config.load().active_provider)
                operation = f"compare/{name}/{variant}"
                if name in rerun and variant == "after":
                    operation += "/" + revision
                before, started = store.usage_summary(rid), time.monotonic()
                print(f"START {name} {variant} model={effective['model']} effort={effective['effort']}", flush=True)
                saved = store.get(rid, "provider-result/" + operation)
                response = (schema.model_validate(ArtifactStore(store.root).json(saved["artifact"])) if saved else
                            await router._call(effective, stage, context, schema, project, rid, operation, True))
                main_usage = dict(store.get(rid, "provider-result/" + operation)["usage"])
                corrections = 0
                try:
                    correct = await oracle(name, response, request, project, store, sandbox, operation)
                except DecisionValidationError as exc:
                    # Match the production decision service's single correction.
                    # The rejected response and both calls' usage stay visible.
                    feedback = DecisionService.validation_feedback(exc)
                    report.setdefault("format_rejections", []).append({"case": name, "variant": variant,
                        "feedback": feedback, "response": response.model_dump(), "usage": main_usage})
                    write_json(output, report)
                    correction_key = operation + "/format-correction"
                    correction_context = {**context, "validation_feedback": feedback,
                        "evidence_reference_contract": decision_reference_catalog(request.evidence).contract(request.evidence)}
                    corrected = store.get(rid, "provider-result/" + correction_key)
                    print(f"CORRECT {name} {variant} field={feedback['field']} code={feedback['code']}", flush=True)
                    response = (schema.model_validate(ArtifactStore(store.root).json(corrected["artifact"])) if corrected else
                                await router._call(effective, stage, correction_context, schema, project, rid, correction_key, True))
                    additional = store.get(rid, "provider-result/" + correction_key)["usage"]
                    main_usage = {k: main_usage[k] and additional[k] if k == "complete" else main_usage[k] + additional[k] for k in main_usage}
                    corrections = 1
                    correct = await oracle(name, response, request, project, store, sandbox, operation)
                after = store.usage_summary(rid)
                material = store.get(rid, "materials/" + operation)
                report["observations"].append({"case": name, "variant": variant, "correct": correct,
                    "revision": revision if name in rerun and variant == "after" else "initial",
                    "model": effective["model"], "effort": effective["effort"], "profile_identity": effective["fingerprint"],
                    "fixture_identity": digest(context), "prompt_characters": material["prompt_characters"],
                    "seconds": round(time.monotonic() - started, 2),
                    "new_usage_during_evaluation": {k: after[k] - before[k] for k in after if k != "complete"},
                    "main_usage": main_usage, "format_corrections": corrections,
                    "usage_complete": after["complete"], "response": response.model_dump()})
                write_json(output, report)
                print(f"DONE {name} {variant} correct={correct} characters={material['prompt_characters']}", flush=True)
        rows = report["observations"]
        totals = {variant: sum(r["main_usage"]["input_tokens"] for r in rows if r["variant"] == variant)
                  for variant in ("before", "after")}
        report.update(actual_input_tokens=totals, passed=len(rows) == 10 and all(r["correct"] for r in rows)
                      and totals["after"] < totals["before"], total_usage=store.usage_summary(rid))
        store.update(rid, status="SUCCEEDED" if report["passed"] else "PAUSED")
    except BaseException as exc:
        report["errors"].append({"type": type(exc).__name__})
        store.update(rid, status="PAUSED")
        raise
    finally:
        summaries = [store.usage_summary(run) for run in report["runs"]]
        report["total_usage"] = {k: all(row[k] for row in summaries) if k == "complete" else
                                 sum(row[k] for row in summaries) for k in summaries[0]}
        with store.connect() as db:
            starts = [json.loads(row[0]) for run in report["runs"] for row in db.execute(
                "SELECT data FROM events WHERE run_id=? AND kind='agent.started'", (run,))]
        report["actual_calls"] = {"main": sum(r["stage"] != "material_probe" for r in starts),
                                  "read_probes": sum(r["stage"] == "material_probe" for r in starts)}
        write_json(output, report)
    if not report["passed"]:
        raise AssertionError("Context comparison did not meet the fixed quality and usage oracle")
    resources = ResourceManager(store, config.load().cleanup)
    resources.release_run(rid)
    resources.collect()
    resources.collect_artifacts_safely()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider-project", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rerun", nargs="*", choices=["research", "implementation", "task_review", "decision", "visual_review"], default=[])
    parser.add_argument("--revision", default="initial")
    arguments = parser.parse_args()
    asyncio.run(evaluate(arguments.provider_project.resolve(), arguments.project.resolve(), arguments.output.resolve(),
                         arguments.rerun, arguments.revision))
