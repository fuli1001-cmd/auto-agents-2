from __future__ import annotations

from pathlib import PurePosixPath

from .dependencies import manifest_reference
from .errors import AA2Error
from .models import PlanResult, TaskSpec
from .workspace import path_allowed


def validate_plan(plan: PlanResult, requirement_ids: set[str], *, coverage_verified=False):
    if plan.mode == "verification_only" and (not coverage_verified or requirement_ids):
        raise AA2Error("Verification-only planning requires controller-verified satisfied coverage")
    manifest_reference(plan.environment_file, "environment_file")
    manifest_reference(plan.requirements_file, "requirements_file")
    tasks = {t.id: t for t in plan.tasks}
    if len(tasks) != len(plan.tasks):
        raise AA2Error("Duplicate task IDs")
    covered = set()
    for task in tasks.values():
        if not task.id.replace("-", "").replace("_", "").isalnum():
            raise AA2Error("Invalid task ID")
        for scope in task.allowed_paths:
            if path_allowed(PurePosixPath(scope).as_posix(),
                            ["DESIGN.md", "docs/aa2", "docs/requirements", "design/prototypes"]):
                raise AA2Error(f"Task {task.id} assigns a stage-owned contract to implementation: {scope}. "
                               "Materialize it in its owning gate; retain the acceptance obligation as verification.")
        if not set(task.depends_on) <= tasks.keys():
            raise AA2Error(f"Unknown task dependency: {task.id}")
        if not set(task.requirement_ids) <= requirement_ids:
            raise AA2Error(f"Unknown requirement: {task.id}")
        covered.update(task.requirement_ids)
    if not requirement_ids <= covered:
        raise AA2Error("Plan omits requirements")
    # Kahn's algorithm is independent of input order and Python recursion depth.
    counts = {key: len(set(task.depends_on)) for key, task in tasks.items()}
    dependents = {key: [] for key in tasks}
    for key, task in tasks.items():
        for dependency in set(task.depends_on):
            dependents[dependency].append(key)
    pending = [key for key, count in counts.items() if not count]
    visited = 0
    while pending:
        key = pending.pop()
        visited += 1
        for child in dependents[key]:
            counts[child] -= 1
            if not counts[child]:
                pending.append(child)
    if visited != len(tasks):
        raise AA2Error("Task graph contains a cycle")


def conflict(a: TaskSpec, b: TaskSpec) -> bool:
    # Split repairs share one persisted implementation budget. Dispatching them
    # concurrently would reserve the same round twice and conceal consumption.
    if a.origin_id and a.origin_id == b.origin_id:
        return True
    def overlaps(x, y):
        # Distinct literal directory prefixes prove disjointness even for glob scopes.
        def prefix(value):
            parts = []
            for part in value.rstrip("/").split("/"):
                if any(c in part for c in "*?["):
                    break
                parts.append(part)
            return parts
        px, py = prefix(x), prefix(y)
        if px and py and px[:min(len(px), len(py))] != py[:min(len(px), len(py))]:
            return False
        return path_allowed(x, [y]) or path_allowed(y, [x]) or any(c in x + y for c in "*?[")
    return any(overlaps(x, y) for x in a.allowed_paths for y in b.allowed_paths)


def task_dependencies(task: TaskSpec, tasks: dict[str, TaskSpec]) -> set[str]:
    """All ancestors, including evidence holes left by a bounded repair."""
    result, pending = set(), list(task.depends_on)
    while pending:
        key = pending.pop()
        if key not in result:
            result.add(key)
            if key in tasks:
                pending.extend(tasks[key].depends_on)
    return result


def ready_batch(tasks: list[TaskSpec], done: set[str], concurrency: int) -> list[TaskSpec]:
    selected = []
    by_id = {task.id: task for task in tasks}
    for task in tasks:
        if task.id in done or not task_dependencies(task, by_id) <= done:
            continue
        if not any(conflict(task, other) for other in selected):
            selected.append(task)
        if len(selected) == concurrency:
            break
    return selected
