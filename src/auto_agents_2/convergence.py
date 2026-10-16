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

    def observe(self, rid, owner, token, observation=None, *, approved=False):
        key = "automation/progress/" + owner
        receipt = f"automation/observations/{owner}/{digest(token)}"
        if self.store.get(rid, receipt) is not None:
            return self.state(rid, owner)
        prior = self.state(rid, owner)
        valid = bool(observation and observation.get("criteria") and observation.get("comparable", True))
        if valid:
            actual = {"criteria": {**observation.get("progress_criteria", observation["criteria"]), "review/approved": approved}}
            state, decision = assess(prior, {"obligations": actual})
            if decision["baseline"]:
                state["stalled"] = max(state["stalled"], prior.get("stalled", 0))
            state["missing"] = 0
        else:
            # One recapture is allowed. Repeated absence is a stop, not a new baseline.
            state = {**prior, "missing": prior.get("missing", 0) + 1}
            if prior.get("evaluations"):
                state["stalled"] = prior.get("stalled", 0) + 1
            decision = {"improved": False, "deferred": True}
        state["observations"] = prior.get("observations", 0) + 1
        state["last"] = decision
        self.store.checkpoints(rid, {key: state, receipt: {"token": token, "valid": valid, "decision": decision}})
        self.store.event(rid, "automation.progress", {"owner": owner, "stalled": state.get("stalled", 0),
            "missing": state["missing"], "improved": decision.get("improved", False)})
        return state

    def blocked(self, rid, owner):
        state = self.state(rid, owner)
        return (state.get("stalled", 0) >= self.config.load().execution.max_stalled_rounds
                or state.get("missing", 0) >= 2)
