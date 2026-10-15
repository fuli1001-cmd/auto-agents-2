"""Targeted live-model comparison; never selects a model or enables a runtime mode.

Run with the project Python and an explicitly prepared evaluation project whose
provider configuration uses the user's existing profile. Six isolated calls compare
only duplicate-body presentation; this is a smoke test, not universal quality proof.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from manual_support import finalize

from auto_agents_2.config import ConfigStore
from auto_agents_2.models import Record
from auto_agents_2.prompts import PROMPT_VERSION, PromptCompiler
from auto_agents_2.providers import ProviderRouter
from auto_agents_2.resources import ResourceManager
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.store import Store
from auto_agents_2.util import digest, write_json
from auto_agents_2.workspace import file_digest


class Decision(Record):
    claim_id: str
    supported: bool
    evidence: str


class Verdicts(Record):
    verdicts: list[Decision]


class PreviousPresentation(PromptCompiler):
    """Test-only baseline: identical instructions, previous inline JSON layout."""

    def compile(self, stage, context, model="", override="auto"):
        prompt, family = super().compile(stage, context, model, override)
        prefix, rest = prompt.split("<context>\n", 1)
        _, suffix = rest.split("\n</context>", 1)
        return prefix + "<context>\n" + json.dumps(context, ensure_ascii=False, indent=2) + "\n</context>" + suffix, family


def fixture():
    padding = "Requests and responses use JSON. The service is fictional and this fixture is its complete authoritative specification.\n" * 30
    bodies = {
        "v1": "FIXTURE API VERSION 1\nMaximum page size: 100.\n" + padding + "MANDATORY: exports require scope export:read. Streaming exports are not supported. Live service availability has not been tested.",
        "v2": "FIXTURE API VERSION 2\nMaximum page size: 200.\n" + padding + "MANDATORY: exports require BOTH export:read and project:member. Streaming exports are not supported. Live service availability has not been tested.",
    }
    cases = [
        ("C1", "v1", "The maximum page size is 100.", True),
        ("C2", "v1", "Exports need no permission scope.", False),
        ("C3", "v1", "Streaming exports are supported.", False),
        ("C4", "v1", "The document establishes that the live service was tested successfully.", False),
        ("C5", "v2", "The maximum page size is 200.", True),
        ("C6", "v2", "export:read alone is sufficient permission for exports.", False),
        ("C7", "v2", "Both export:read and project:member are required.", True),
        ("C8", "v2", "The maximum page size is 100, as in version 1.", False),
    ]
    rows = [{"claim_id": cid, "claim": claim, "url": f"fixture://api/{version}#{cid}", "claimed_version": version,
             "retrieval": {"artifact": {"hash": digest(bodies[version])}, "snapshot": version, "extractor_version": "fixture-v1"},
             "excerpt": bodies[version]} for cid, version, claim, _ in cases]
    context = {"review_kind": "research", "retrieved_sources": rows,
               "task": {"description": "Evaluate every claim against the complete inline fictional API fixture. These are owner-supplied frozen test specifications, not live URLs. Do not browse. Return supported=true only if that claim is fully supported for its own version, and cite the relevant exact rule. Include every claim_id exactly once. Each evidence string must consist only of the FIXTURE API VERSION N heading followed by the exact relevant sentence (including MANDATORY: where present). Missing live evidence cannot establish a live test."}}
    return context, {cid: expected for cid, _, _, expected in cases}


RULES = {
    "C1": (1, "Maximum page size: 100."),
    "C2": (1, "MANDATORY: exports require scope export:read."),
    "C3": (1, "Streaming exports are not supported."),
    "C4": (1, "Live service availability has not been tested."),
    "C5": (2, "Maximum page size: 200."),
    "C6": (2, "MANDATORY: exports require BOTH export:read and project:member."),
    "C7": (2, "MANDATORY: exports require BOTH export:read and project:member."),
    "C8": (2, "Maximum page size: 200."),
}


def citations_valid(answer):
    for item in answer.verdicts:
        if item.claim_id not in RULES:
            return False
        version, rule = RULES[item.claim_id]
        expected = f"FIXTURE API VERSION {version} " + rule
        if " ".join(item.evidence.split()) != expected:
            return False
    return True


async def evaluate(project: Path, output: Path):
    config = ConfigStore(project)
    effective = config.resolve("review")
    if not effective["model"]:
        raise RuntimeError("Evaluation requires a user-configured model identity; none will be selected automatically")
    store = Store(project)
    run = store.admit("frozen research presentation comparison", config.load().model_dump(mode="json"))
    rid = run["id"]
    observations = []
    report = {"observations": observations, "passed": False, "phase": "preparation"}
    try:
        router = ProviderRouter(config, store, SandboxRunner(store.root, config.load().sandbox_binary))
        context, expected = fixture()
        observations = []
        report = {"scope": "inline exact-snapshot body deduplication only; not end-to-end workflow equivalence",
                  "model": effective["model"], "effort": effective["effort"], "provider": effective["provider"],
                  "profile_fingerprint": effective["fingerprint"], "binary_hash": file_digest(Path(effective["binary"])),
                  "prompt_version": PROMPT_VERSION, "schema_hash": digest(Verdicts.model_json_schema()),
                  "fixture_hash": digest(context), "expected": expected, "observations": observations,
                  "isolated_sessions": True, "repetitions": 3}
        for repetition in range(3):
            for variant in (("A", "B") if repetition % 2 == 0 else ("B", "A")):
                router.compiler = PreviousPresentation() if variant == "A" else PromptCompiler()
                before = store.usage_summary(rid)
                started = time.monotonic()
                async with asyncio.timeout(180):
                    answer = await router._call(effective, "review", context, Verdicts, project, rid,
                                                f"evaluation/{repetition}/{variant}", True)
                after = store.usage_summary(rid)
                actual = {item.claim_id: item.supported for item in answer.verdicts}
                correct = len(answer.verdicts) == len(expected) and actual == expected and citations_valid(answer)
                observations.append({"repetition": repetition, "variant": variant, "correct": correct,
                                     "prompt_characters": store.get(rid, f"materials/evaluation/{repetition}/{variant}")["prompt_characters"], "seconds": time.monotonic() - started,
                                     "usage": {k: after[k] - before[k] for k in after if k != "complete"},
                                     "usage_complete": after["complete"], "answer": answer.model_dump()})
                write_json(output, report)
                print(f"{variant} repetition {repetition + 1}: {'PASS' if correct else 'FAIL'}", flush=True)
        report["passed"] = all(row["correct"] for row in observations)
        store.update(rid, status="SUCCEEDED" if report["passed"] else "PAUSED")
    except BaseException as exc:
        report.update(passed=False, error_type=type(exc).__name__)
        finalize(lambda: store.update(rid, status="PAUSED"))
        raise
    finally:
        finalize(lambda: report.update(total_usage=store.usage_summary(rid)), lambda: write_json(output, report))
    if not report["passed"]:
        raise AssertionError("Presentation comparison did not satisfy the fixed oracle")
    resources = ResourceManager(store, config.load().cleanup)
    resources.release_run(rid)
    resources.collect()
    resources.collect_artifacts_safely()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(evaluate(args.project.resolve(), args.output.resolve()))
