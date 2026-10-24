"""Human prototype consent, distinct from provider review and browser evidence."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from .errors import AA2Error
from .models import BrowserAction
from .prototype_contract import source_context
from .prototype_review import review_basis, review_current
from .util import digest
from .workspace import legacy_tree_hash, tree_hash

APPROVAL_CONTEXT_VERSION = 1


def prototype_identity(work: Path, actions: list[dict]) -> dict:
    design = (work / "DESIGN.md").read_text() if (work / "DESIGN.md").is_file() else ""
    content = digest([tree_hash(work / "design/prototypes"), design])
    legacy = digest([legacy_tree_hash(work / "design/prototypes"), design])
    actions_hash = digest(actions)
    return {"hash": content, "legacy_hash": legacy, "actions_hash": actions_hash,
            "identity": digest([content, actions_hash])}


def exact_actions(actions: list[dict], visuals: list[dict], *, expected_routes: list[str]) -> bool:
    if (not expected_routes or not visuals or any(not isinstance(row, dict) or
            not isinstance(row.get("status"), int) or not 200 <= row["status"] < 400 or row.get("errors") for row in visuals)):
        return False
    expected = defaultdict(list)
    for action in actions:
        expected[action["route"]].append(action)
    if not set(expected) <= set(expected_routes) or len(visuals) != len(set(expected_routes)) * 2:
        return False
    for width in (1440, 390):
        for route in expected_routes:
            sequence = expected[route]
            rows = [row for row in visuals if row.get("viewport") == width
                    and (row.get("route") or "/" + row.get("page", "")) == route]
            if len(rows) != 1 or rows[0].get("actions") != sequence:
                return False
    return not any(row.get("actions") and (row.get("route") or "/" + row.get("page", "")) not in expected
                   for row in visuals)


class PrototypeApprovals:
    def __init__(self, store, artifacts):
        self.store, self.artifacts = store, artifacts

    @staticmethod
    def routes(work, actions):
        from .design import prototype_routes
        try:
            return [route for route, _ in prototype_routes(work / "design/prototypes",
                [BrowserAction.model_validate(action) for action in actions])]
        except (AA2Error, OSError, ValueError):
            return []

    @staticmethod
    def _approved(question):
        if question["answer"] is None or question["superseded"]:
            return False
        interaction = json.loads(question["interaction"])
        if interaction.get("kind") == "choice":
            return any(option.get("key") == question["answer"] and option.get("value") == "approve"
                       for option in interaction.get("options", []))
        return question["answer"].strip() in {"approve", "通过", "批准"}

    def human(self, rid, work, actions, record=None):
        """Return verifiable consent; a review boolean or a document label cannot grant it."""
        binding = prototype_identity(work, actions)
        successful = [row["id"] for row in self.store.successful()]
        allowed_runs = {rid, *successful}
        candidates = [(rid, record if record is not None else self.store.get(rid, "prototype_approval", {}))]
        candidates.extend((run, self.store.get(run, "prototype_approval", {})) for run in successful if run != rid)
        # A crash after the user answered but before the approval checkpoint is safe
        # to recover: the question itself binds the current content AND actions.
        with self.store.connect() as db:
            questions = db.execute("SELECT * FROM questions WHERE run_id=? AND key='prototype-approval' AND artifact_hash=?",
                                   (rid, binding["identity"])).fetchall()
        for question in questions:
            if self._approved(question):
                return self._proof(question, "content_and_actions", binding)
        for owner, candidate in candidates:
            if not candidate or candidate.get("hash") not in {binding["hash"], binding["legacy_hash"]}:
                continue
            if candidate.get("actions_hash") not in {None, binding["actions_hash"]}:
                continue
            origin = candidate.get("human", {}).get("run_id", owner)
            if origin not in allowed_runs:
                continue
            identity = candidate.get("identity")
            legacy_consent = candidate.get("human", {}).get("binding") == "legacy_content"
            if identity and not legacy_consent:
                if identity != digest([candidate["hash"], binding["actions_hash"]]):
                    continue
                target, kind = identity, "content_and_actions"
            else:
                # Legacy questions bound only the old content digest. Exact executed
                # action sequences supply the original action contract, never prose.
                if (not exact_actions(actions, candidate.get("visuals", []), expected_routes=self.routes(work, actions))
                        or (work / "DESIGN.md").is_symlink()
                        or (work / "design").is_symlink() or (work / "design/prototypes").is_symlink()
                        or any(p.is_symlink() for p in (work / "design/prototypes").rglob("*"))):
                    continue
                target = candidate.get("human", {}).get("artifact_hash", candidate["hash"])
                if target != binding["legacy_hash"]:
                    continue
                kind = "legacy_content"
            with self.store.connect() as db:
                questions = db.execute("SELECT * FROM questions WHERE run_id=? AND key='prototype-approval' AND artifact_hash=?",
                                       (origin, target)).fetchall()
            for question in questions:
                if self._approved(question):
                    return self._proof(question, kind, binding)
        return None

    @staticmethod
    def _proof(question, kind, binding):
        return {"run_id": question["run_id"], "question_id": question["id"], "answer": question["answer"],
                "decision": "approve", "artifact_hash": question["artifact_hash"], "binding": kind,
                "content_hash": binding["hash"], "actions_hash": binding["actions_hash"]}

    def review_inputs(self, rid, work, actions, visuals):
        source = source_context(self.store, self.artifacts, rid)["prototype_requirement_source"]
        basis = review_basis(source, self.store.get(rid, "clarify", {}), prototype_identity(work, actions), visuals)
        return basis, source

    def automation_current(self, record, binding, actions, browser_version, *, expected_routes, review_inputs=None):
        if not isinstance(record, dict):
            return False
        if (any(record.get(key) != binding[key] for key in ("hash", "identity", "actions_hash"))
                or record.get("browser_version") != browser_version
                or not exact_actions(actions, record.get("visuals", []), expected_routes=expected_routes)):
            return False
        try:
            if (review_inputs is None or review_inputs[0]["candidate_hash"] != binding["identity"]
                    or review_inputs[0]["observations_hash"] != digest(record["visuals"])
                    or not review_current(record["review"], *review_inputs)):
                return False
            images = [image for row in record["visuals"] for image in [row, *row.get("tiles", [])]]
            for row in images:
                ref = row.get("image_artifact")
                if (not isinstance(ref, dict) or ref.get("kind") != "screenshot"
                        or not isinstance(ref.get("hash"), str) or not isinstance(ref.get("bytes"), int)):
                    return False
                raw = self.artifacts.bytes(ref)
                if len(raw) != ref["bytes"] or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                    return False
        except (OSError, ValueError, KeyError, TypeError):
            return False
        return True

    def current(self, rid, work, actions, browser_version):
        record = self.store.get(rid, "prototype_approval", {})
        binding = prototype_identity(work, actions)
        human = self.human(rid, work, actions, record)
        if (human and human["content_hash"] == binding["hash"] and human["actions_hash"] == binding["actions_hash"]
                and self.automation_current(record, binding, actions, browser_version, expected_routes=self.routes(work, actions),
                    review_inputs=self.review_inputs(rid, work, actions, record.get("visuals", [])))):
            return {**record, "human": human}
        return None

    def recheck_binding(self, rid, work):
        """Evidence refresh may reuse consent, but cannot request or manufacture it."""
        prototype = self.store.get(rid, "prototype")
        if not prototype:
            return None
        actions = prototype.get("browser_actions", [])
        human = self.human(rid, work, actions)
        return {**prototype_identity(work, actions), "human": human} if human else None

    def publish(self, rid, work, actions, human, review, visuals, browser_version):
        binding = prototype_identity(work, actions)
        if human is None or human != self.human(rid, work, actions):
            raise ValueError("Prototype consent does not match the current candidate")
        previous = self.store.get(rid, "prototype_approval", {})
        record = {**binding, "human": human, "review": review, "visuals": visuals,
                  "browser_version": browser_version, "context_version": APPROVAL_CONTEXT_VERSION}
        if not self.automation_current(record, binding, actions, browser_version, expected_routes=self.routes(work, actions),
                review_inputs=self.review_inputs(rid, work, actions, visuals)):
            raise ValueError("Prototype approval requires complete browser evidence and an independent review")
        updates = {"prototype_approval": record}
        if previous and previous != record:
            updates["candidate/prototype-approval-history/" + digest(previous)] = previous
        self.store.checkpoints(rid, updates)
        return record

    def context(self, record):
        return {"status": "approved", "context_version": APPROVAL_CONTEXT_VERSION,
                "hash": record["hash"], "identity": record["identity"], "actions_hash": record["actions_hash"],
                "human": record["human"], "browser_version": record["browser_version"],
                "independent_review": record["review"]}
