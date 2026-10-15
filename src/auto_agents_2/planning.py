from __future__ import annotations

from .dependencies import manifest_reference
from .errors import AA2Error
from .models import PlanResult, TaskSpec
from .workspace import path_allowed


def validate_plan(plan: PlanResult, requirement_ids: set[str]):
    manifest_reference(plan.environment_file, "environment_file")
    manifest_reference(plan.requirements_file, "requirements_file")
    tasks = {t.id: t for t in plan.tasks}
    if len(tasks) != len(plan.tasks):
        raise AA2Error("Duplicate task IDs")
    covered = set()
    for task in tasks.values():
        if not task.id.replace("-", "").replace("_", "").isalnum():
            raise AA2Error("Invalid task ID")
        if not set(task.depends_on) <= tasks.keys():
            raise AA2Error(f"Unknown task dependency: {task.id}")
        if not set(task.requirement_ids) <= requirement_ids:
            raise AA2Error(f"Unknown requirement: {task.id}")
        covered.update(task.requirement_ids)
    if not requirement_ids <= covered:
        raise AA2Error("Plan omits requirements")
    visited, active = set(), set()

    def visit(task_id):
        if task_id in active:
            raise AA2Error("Task graph contains a cycle")
        if task_id in visited:
            return
        active.add(task_id)
        for dependency in tasks[task_id].depends_on:
            visit(dependency)
        active.remove(task_id)
        visited.add(task_id)

    for key in tasks:
        visit(key)


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


def ready_batch(tasks: list[TaskSpec], done: set[str], concurrency: int) -> list[TaskSpec]:
    selected = []
    for task in tasks:
        if task.id in done or not set(task.depends_on) <= done:
            continue
        if not any(conflict(task, other) for other in selected):
            selected.append(task)
        if len(selected) == concurrency:
            break
    return selected
