"""Durable task progress. Model claims and authorization never count as progress."""
from __future__ import annotations

from .replanning import assess
from .util import digest


class Convergence:
    def __init__(self, store, config):
        self.store, self.config = store, config

    def state(self, rid, owner):
        saved = self.store.get(rid, "automation/progress/" + owner)
        if saved is not None:
            return saved
        legacy = self.store.get(rid, "rounds/" + owner, {})
        return {"stalled": legacy["stalled"]} if legacy.get("stalled") else {}

    def observe(self, rid, owner, token, observation=None, *, approved=False, phase="checks"):
        key = "automation/progress/" + owner
        receipt = f"automation/observations/{owner}/{digest(token)}"
        if self.store.get(rid, receipt) is not None:
            return self.state(rid, owner)
        prior = self.state(rid, owner)
        valid = bool(observation and observation.get("criteria") and observation.get("comparable", True))
        if valid:
            actual = {"criteria": {**observation.get("progress_criteria", observation["criteria"]), "review/approved": approved}}
            measured, decision = assess(prior, {"obligations": actual})
            state = {**prior, **measured}
            if decision["baseline"]:
                state["stalled"] = max(state["stalled"], prior.get("stalled", 0))
            state["missing"] = 0
        elif phase == "checks":
            # Unavailable check evidence is distinct from a failure before checks.
            state = {**prior, "missing": prior.get("missing", 0) + 1}
            if prior.get("evaluations"):
                state["stalled"] = prior.get("stalled", 0) + 1
            decision = {"improved": False, "deferred": True}
        else:
            failures = dict(prior.get("stage_failures", {}))
            failures[phase] = failures.get(phase, 0) + 1
            state = {**prior, "stage_failures": failures, "missing": prior.get("missing", 0)}
            decision = {"improved": False, "deferred": True, "phase": phase}
        state["observations"] = prior.get("observations", 0) + 1
        state["last"] = decision
        self.store.checkpoints(rid, {key: state, receipt: {"token": token, "valid": valid, "decision": decision}})
        self.store.event(rid, "automation.progress", {"owner": owner, "stalled": state.get("stalled", 0),
            "missing": state["missing"], "improved": decision.get("improved", False)})
        return state

    def blocked(self, rid, owner):
        """Historical diagnostic indicator only; callers must not use it to authorize work."""
        state = self.state(rid, owner)
        return (state.get("stalled", 0) >= self.config.load().execution.max_stalled_rounds
                or state.get("missing", 0) >= 2)

    def milestone(self, rid, owner, name, proof):
        if name not in {"candidate_applied", "environment_ready", "build_ready", "checks_valid"}:
            raise ValueError("Unknown engineering milestone")
        key = "automation/engineering/" + owner
        state = self.store.get(rid, key, {})
        if name in state.get("achieved", {}):
            return False
        baseline = bool(state.get("legacy_baseline", False))
        state = {**state, "achieved": {**state.get("achieved", {}), name: {
            "proof": proof, "baseline": baseline}}}
        self.store.checkpoint(rid, key, state)
        self.store.event(rid, "automation.engineering", {"owner": owner, "milestone": name,
            "improved": not baseline})
        return not baseline
