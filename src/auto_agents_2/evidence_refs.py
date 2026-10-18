"""References to supplied controller data; reference existence is not acceptance."""
import re
from dataclasses import dataclass, field

from .util import digest


@dataclass(frozen=True)
class EvidenceReference:
    id: str
    path: str
    origin: str
    observation_id: str
    status: str


@dataclass
class ReferenceCatalog:
    controller: set[str] = field(default_factory=set)
    proposals: set[str] = field(default_factory=set)
    entries: dict[str, EvidenceReference] = field(default_factory=dict)
    observation_id: str = ""
    provenance: dict[str, str] = field(default_factory=dict)

    def add(self, alias, path, origin, *, status="recorded"):
        reference = EvidenceReference(digest([self.observation_id, path]), path, origin,
                                      self.observation_id, "unverified" if origin == "proposal" else status)
        # A proposal cannot shadow an already indexed controller record.
        if alias in self.controller and origin == "proposal":
            return
        self.entries[alias] = reference
        (self.proposals if origin == "proposal" else self.controller).add(alias)
        if origin != "proposal":
            self.proposals.discard(alias)

    def resolve(self, reference):
        if reference in self.entries:
            return reference
        prefix = "decision_request.evidence."
        if reference.startswith(prefix) and reference[len(prefix):] in self.entries:
            return reference[len(prefix):]
        return None

    def contract(self, roots):
        return {"version": 4, "roots": sorted(roots), "observation_id": self.observation_id,
                "provenance": self.provenance,
                "syntax": "Cite an existing dot path with [index], a supplied artifact hash or a controller-indexed alias. "
                          "Root paths are valid. Proposal paths describe unverified suggestions only; "
                          "include a controller record when proposing an action. Citations never establish acceptance."}


def reference_catalog(value, *, prefixes=("",), provenance=None, observation_id=""):
    """Index actual paths with provenance; proposal contents never mint artifact IDs."""
    refs = ReferenceCatalog(observation_id=observation_id, provenance=dict(provenance or {}))

    def visit(item, path, proposal=False):
        # Explicit producer annotations take precedence over legacy field adapters.
        if path in refs.provenance:
            proposal = refs.provenance[path] == "proposal"
        if path:
            for prefix in prefixes:
                refs.add(prefix + path, path, "proposal" if proposal else "controller")
        if isinstance(item, dict):
            if not proposal and isinstance(item.get("hash"), str) and re.fullmatch(r"[a-f0-9]{64}", item["hash"]):
                refs.add(item["hash"], path, "controller")
            for key, child in item.items():
                visit(child, f"{path}.{key}" if path else key,
                      proposal or key in {"proposal", "recovery_request", "original_proposal"})
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]", proposal)

    visit(value, "")
    return refs


def controller_context_catalog(context, *, aliases=(), provenance=None):
    """Register controller context slots; producers must annotate new model-owned slots via provenance."""
    origins = {}
    failure = context.get("failure") or {}
    if isinstance(failure, dict):
        origins["failure.proposal"] = "proposal"
    history = context.get("history") or {}
    if isinstance(history, dict):
        for index, row in enumerate(history.get("recent", [])):
            origins[f"history.recent[{index}].outcome.diagnosis"] = "proposal"
            origins[f"history.recent[{index}].outcome.feedback.diagnosis"] = "proposal"
    origins["diagnosis_feedback.diagnosis"] = "proposal"
    for name in ("diagnosis", "original_proposal", "raw_response"):
        if name in context:
            origins[name] = "proposal"
    origins.update(provenance or {})
    refs = reference_catalog(context, provenance=origins,
                             observation_id=failure.get("observation_id", "") if isinstance(failure, dict) else "")
    registered = list(aliases)
    if isinstance(failure, dict):
        registered.extend(failure.get("evidence_refs", []))
    for alias in registered:
        if isinstance(alias, str) and alias not in refs.proposals:
            refs.add(alias, alias, "controller")
    return refs


def supplied_references(value, *, prefixes=("",)):
    return reference_catalog(value, prefixes=prefixes).controller
