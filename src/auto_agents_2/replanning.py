"""Conservative convergence accounting, separate from workflow acceptance gates.

Only the controller records observations. Each scope's first observed contract is
fixed: renamed checks, new assertions and provider prose cannot manufacture gains.
The historical best is monotone, so regression/recovery cycles get no repeat credit.
"""
from __future__ import annotations

from .evidence import CHECKER_VERSION
from .util import digest


def assess(previous: dict, observations: dict) -> tuple[dict, dict]:
    tracked = {scope: dict(values) for scope, values in previous.get("tracked", {}).items()}
    gains, regressions, deferred = [], [], []
    for scope in tracked.keys() | observations.keys():
        if observations.get(scope, {}).get("comparable") is False:
            deferred.append(scope)
            continue
        actual = observations.get(scope, {}).get("criteria", {})
        if scope not in tracked:
            # A new contract is a baseline, not evidence of an improvement.
            tracked[scope] = dict(actual)
            continue
        for criterion, best in tracked[scope].items():
            current = actual.get(criterion, False)
            if criterion not in actual or best and not current:
                regressions.append([scope, criterion])
            elif current and not best:
                gains.append([scope, criterion])
    baseline = not previous.get("evaluations")
    improved = bool(gains) and not regressions and not deferred and not baseline
    for scope, criterion in gains:
        tracked[scope][criterion] = True
    stalled = 0 if baseline or improved else previous["stalled"] + 1
    state = {"tracked": tracked, "evaluations": previous.get("evaluations", 0) + 1, "stalled": stalled}
    decision = {"baseline": baseline, "improved": improved, "stalled": stalled,
                "gains": gains if improved else [], "regressions": regressions, "deferred": sorted(deferred)}
    return state, decision


def check_criteria(commands, results, source_hash: str, environment_hash: str, *, process_exit=False) -> dict[str, bool]:
    """Use actual exits and controller-owned JUnit cases, never stdout pass counts."""
    actual = {(tuple(r.argv), r.cwd, r.command_hash): r for r in results}
    criteria = {}
    for command in commands:
        data = command.model_dump()
        # Explicit compatibility mapping: the new empty declaration is the old obligation.
        obligation = {k: v for k, v in data.items() if k not in {"inputs", "outputs"} or v}
        key = digest(obligation)
        result = actual.get((tuple(command.argv), command.cwd, digest(data)))
        invalid_evidence = {"execution_inputs_changed", "source_changed", "incomplete_log_capture",
                            "build_outputs_missing_or_invalid", "evidence_collection_failed",
                            "test_report_missing", "test_report_invalid", "test_attachments_invalid", "unsupported_test_runner"}
        valid = bool(result and result.checker_version == CHECKER_VERSION
                     and not invalid_evidence.intersection(result.failure_reasons)
                     and result.command_hash == digest(data) and not result.timed_out and result.source_hash == source_hash
                     and result.environment_hash == environment_hash)
        # Progress must not reward merely reaching expected_tests by adding easy
        # cases. Acceptance still uses the controller exit and minimum count.
        criteria["command/" + key] = bool(valid and (result.process_exit_code if process_exit else result.exit_code) == 0)
        if result:
            for test_id in result.test_ids:
                criteria[f"test/{key}/{test_id}"] = bool(valid and result.report_status == "valid"
                                                       and result.report and test_id in result.passed_test_ids)
    return criteria


def browser_criteria(actions, results) -> dict[str, bool]:
    """Compare assertions plus their preceding actions at both required viewports."""
    criteria = {}
    for width in (1440, 390):
        by_route = {}
        for action in actions:
            by_route.setdefault(action.route, []).append(action.model_dump())
        executed_assertion = False
        for route, sequence in by_route.items():
            row = next((r for r in results if r.get("viewport") == width
                        and (r.get("route") or "/" + r.get("page", "")) == route), {})
            valid = 200 <= row.get("status", 0) < 400 and not row.get("errors")
            completed = row.get("actions", []) if valid else []
            for index, action in enumerate(sequence):
                if action["action"] not in {"expect_visible", "expect_text"}:
                    continue
                prefix = sequence[:index + 1]
                success = completed[:index + 1] == prefix
                criteria[f"assertion/{width}/{digest(prefix)}"] = success
                executed_assertion |= success
        # A fixed milestone also detects supplying previously absent executable
        # evidence. Adding arbitrary extra checks cannot repeatedly earn credit.
        criteria[f"assertions_executed/{width}"] = executed_assertion
    return criteria
