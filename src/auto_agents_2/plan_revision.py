"""Local plan amendments and conservative task invalidation, without file dependency graphs."""
from __future__ import annotations

from pathlib import PurePosixPath

from pydantic import Field

from .errors import AA2Error
from .models import Command, PlanResult, PreviewSpec, Record, TaskSpec
from .planning import validate_plan
from .util import atomic_write


def materialize_plan(store, rid, work, plan):
    path = work / "docs/aa2/plan.md"
    revision = store.get(rid, "plan_revision/current", {}).get("revision", 1)
    prior = store.get(rid, f"plan_revision/history/{revision - 1}", {}).get("plan", {}).get("document")
    if path.exists() and path.read_text() not in {plan.document, prior}:
        raise AA2Error("Plan document changed outside activation; preserve user edits and reconcile first")
    atomic_write(path, plan.document)


class PlanPatch(Record):
    base_revision: int = Field(ge=1)
    failure_ids: list[str] = Field(min_length=1)
    reason: str
    document: str
    upsert_tasks: list[TaskSpec] = []
    remove_tasks: list[str] = []
    final_checks: list[Command] | None = None
    runtime_prepare: list[Command] | None = None
    runtime_probes: list[Command] | None = None
    preview: PreviewSpec | None = None
    environment_file: str | None = None
    requirements_file: str | None = None


def descendants(plan, seeds):
    affected = set(seeds)
    while True:
        more = {t.id for t in plan.tasks if set(t.depends_on) & affected}
        if more <= affected:
            return affected
        affected |= more


def implementation_changed(old, new):
    return any(getattr(old, key) != getattr(new, key) for key in (
        "description", "requirement_ids", "allowed_paths", "acceptance"))


def _retain_checks(old, new):
    # Retain runnable test targets and minimum counts. Collector-only flags may
    # change; the independent review checks semantic assertions and command changes.
    def obligation(argv):
        kept, skip = [], False
        for index, arg in enumerate(argv):
            if skip:
                skip = False
            elif arg == "-p" and index + 1 < len(argv) and argv[index + 1] in {"no:junitxml", "junitxml"}:
                skip = True
            elif arg in {"--reporter", "--reporters", "--junitxml", "--junit-xml", "--outputFile"}:
                skip = True
            elif arg == "--run" or arg.startswith(("--reporter=", "--reporters=", "--junitxml=", "--junit-xml=", "--outputFile=")):
                continue
            else:
                kept.append(arg)
        return kept

    for command in old:
        generated_inputs = {p for p in command.inputs if {"dist", ".next"} & set(PurePosixPath(p).parts)}
        matches = [c for c in new if c.cwd == command.cwd and obligation(c.argv) == obligation(command.argv)
                   and set(command.outputs) <= set(c.outputs) and generated_inputs <= set(c.inputs)]
        if not matches or max(c.expected_tests for c in matches) < command.expected_tests:
            raise AA2Error("Plan patch removes a check obligation or reduces expected_tests")


def apply_patch(plan: PlanResult, patch: PlanPatch, revision: int, failures: set[str], origins: dict):
    if patch.base_revision != revision:
        raise AA2Error("Stale plan patch base_revision")
    if not set(patch.failure_ids) <= failures:
        raise AA2Error("Plan patch references unknown failures")
    old = {t.id: t for t in plan.tasks}
    updates = {t.id: t for t in patch.upsert_tasks}
    if len(updates) != len(patch.upsert_tasks) or set(patch.remove_tasks) & updates.keys():
        raise AA2Error("Conflicting task operations in plan patch")
    if not set(patch.remove_tasks) <= old.keys():
        raise AA2Error("Plan patch removes an unknown task")
    for task in updates.values():
        origin = origins.get(task.id)
        if origin and task.origin_id != origin or task.origin_id not in set(origins.values()):
            raise AA2Error("Plan patch changes or invents a budget origin")
    tasks = {k: v for k, v in old.items() if k not in patch.remove_tasks}
    tasks.update(updates)
    for task in plan.tasks:
        replacements = [t for t in tasks.values() if t.origin_id == task.origin_id]
        if not replacements or not set(task.requirement_ids) <= {r for t in replacements for r in t.requirement_ids}:
            raise AA2Error("Removed task lacks replacement coverage in its original budget lineage")
        if not set(task.acceptance) <= {a for t in replacements for a in t.acceptance}:
            raise AA2Error("Plan patch removes accepted obligations")
        _retain_checks(task.checks, [c for t in replacements for c in t.checks])
    changes = {k: getattr(patch, k) for k in ("final_checks", "runtime_prepare", "runtime_probes", "preview", "environment_file", "requirements_file")
               if getattr(patch, k) is not None}
    candidate = plan.model_copy(update={"tasks": list(tasks.values()), "document": patch.document, **changes}).model_copy(deep=True)
    _retain_checks(plan.final_checks, candidate.final_checks)
    _retain_checks(plan.runtime_prepare, candidate.runtime_prepare)
    _retain_checks(plan.runtime_probes, candidate.runtime_probes)
    validate_plan(candidate, {r for t in plan.tasks for r in t.requirement_ids})
    changed = {k for k, v in updates.items() if k not in old or v != old[k]} | set(patch.remove_tasks)
    affected = descendants(candidate, changed) | descendants(plan, changed)
    if changes:
        affected |= tasks.keys()  # preparation/verification contract: conservative recheck
    reopen = set()
    for key, task in updates.items():
        predecessors = ([old[key]] if key in old else
                        [old[k] for k in patch.remove_tasks if old[k].origin_id == task.origin_id])
        if len(predecessors) != 1 or implementation_changed(predecessors[0], task):
            reopen.add(key)
    return candidate, affected & tasks.keys(), reopen
