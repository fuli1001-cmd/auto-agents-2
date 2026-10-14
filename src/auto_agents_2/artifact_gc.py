"""Collect diagnostic bodies after their last consumer, retaining decision evidence."""
from __future__ import annotations

import json

from .diagnostics import LOG_KINDS
from .util import digest, file_lock, now


def collect_artifacts(store):
    result = {"removed": 0, "bytes": 0}
    root = store.root / "artifacts"
    with file_lock(store.root / "artifact-gc.lock"):
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = list(db.execute("SELECT c.key,c.data,r.status FROM checkpoints c JOIN runs r ON r.id=c.run_id"))
            states = {r["id"]: r["status"] for r in db.execute("SELECT id,status FROM runs")}
            kinds, owners, edges = {}, {}, {}
            for sha, kind in db.execute("SELECT hash,kind FROM artifact_kinds"):
                kinds.setdefault(sha, set()).add(kind)
            for sha, rid in db.execute("SELECT hash,run_id FROM artifact_owners"):
                owners.setdefault(sha, set()).add(rid)
            for parent, child in db.execute("SELECT parent,child FROM artifact_edges"):
                edges.setdefault(parent, []).append(child)
            # No producer can still publish results after the controller terminates a run.
            db.execute("DELETE FROM diagnostic_holds WHERE purpose='producer' AND run_id IN "
                       "(SELECT id FROM runs WHERE status IN ('SUCCEEDED','CANCELLED'))")
            live = {row[0] for row in db.execute("SELECT hash FROM diagnostic_holds")}
            visited, known_logs = set(), {sha for sha, labels in kinds.items() if labels & LOG_KINDS}

            def mark(value, keep_logs):
                if isinstance(value, dict):
                    sha = value.get("hash")
                    if isinstance(sha, str) and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha):
                        kind = value.get("kind")
                        diagnostic = kind in LOG_KINDS or (kind is None and kinds.get(sha) and kinds[sha] <= LOG_KINDS)
                        if diagnostic:
                            known_logs.add(sha)
                            if kind in LOG_KINDS:
                                db.execute("INSERT OR IGNORE INTO artifact_kinds VALUES(?,?)", (sha, kind))
                                kinds.setdefault(sha, set()).add(kind)
                            if keep_logs:
                                live.add(sha)
                        else:
                            mark_object(sha, keep_logs)
                    for child in value.values():
                        mark(child, keep_logs)
                elif isinstance(value, list):
                    for child in value:
                        mark(child, keep_logs)

            def mark_object(sha, keep_logs):
                live.add(sha)
                if (sha, keep_logs) in visited:
                    return
                visited.add((sha, keep_logs))
                path = root / sha[:2] / sha
                try:
                    with path.open("rb") as stream:
                        prefix = stream.read(4096)
                        data = prefix + stream.read() if prefix.lstrip().startswith((b"{", b"[")) else None
                    if data is not None and digest(data) == sha:
                        mark(json.loads(data), keep_logs)
                        return
                except (OSError, ValueError, UnicodeError):
                    pass
                # Opaque/legacy dependency edges cannot prove a diagnostic is optional.
                for child in edges.get(sha, []):
                    mark_object(child, True)

            for row in rows:
                value = json.loads(row["data"])
                active = row["status"] not in {"SUCCEEDED", "CANCELLED"}
                if row["key"] == "issues":
                    for issue in value.values():
                        mark(issue, issue.get("status") == "open")
                else:
                    mark(value, active or row["key"].startswith("recovery/required/"))
            candidates = []
            for sha, runs in owners.items():
                if sha in live or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
                    continue
                diagnostic = sha in known_logs and kinds.get(sha, set()) <= LOG_KINDS
                if not diagnostic and not all(states.get(rid) == "SUCCEEDED" for rid in runs):
                    continue
                path = root / sha[:2] / sha
                if path.absolute() != path.resolve():
                    continue
                # Commit the retirement record BEFORE unlinking, so a crash cannot make
                # deliberate log expiration indistinguishable from evidence corruption.
                if sha in known_logs:
                    if not path.is_file() and not db.execute(
                            "SELECT 1 FROM retired_diagnostics WHERE hash=?", (sha,)).fetchone():
                        continue  # Accidental loss must never become authorized expiration.
                    db.execute("INSERT OR IGNORE INTO retired_diagnostics VALUES(?,?,?)",
                               (sha, now(), "No active diagnostic consumer; structured evidence retained"))
                    candidates.append((sha, True))
                else:
                    size = path.stat().st_size if path.exists() else 0
                    path.unlink(missing_ok=True)
                    db.execute("DELETE FROM artifact_owners WHERE hash=?", (sha,))
                    db.execute("DELETE FROM artifact_edges WHERE parent=?", (sha,))
                    result["removed"] += 1
                    result["bytes"] += size
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for sha, diagnostic in candidates:
                if diagnostic and not db.execute("SELECT 1 FROM retired_diagnostics WHERE hash=?", (sha,)).fetchone():
                    continue  # A producer or reader reacquired it between the two transactions.
                if db.execute("SELECT 1 FROM diagnostic_holds WHERE hash=?", (sha,)).fetchone():
                    continue
                path = root / sha[:2] / sha
                if path.absolute() != path.resolve():
                    continue
                size = path.stat().st_size if path.exists() else 0
                path.unlink(missing_ok=True)
                db.execute("DELETE FROM artifact_owners WHERE hash=?", (sha,))
                db.execute("DELETE FROM artifact_edges WHERE parent=?", (sha,))
                result["removed"] += 1
                result["bytes"] += size
    return result


def recover_diagnostic_holds(store):
    """Only after taking the controller lock and reaping orphan processes."""
    with store.connect() as db:
        readers = list(db.execute("SELECT id FROM diagnostic_holds WHERE purpose='reader'"))
        db.executemany("DELETE FROM diagnostic_holds WHERE id=?", [(r[0],) for r in readers])
        rows = list(db.execute("SELECT h.*,r.status FROM diagnostic_holds h JOIN runs r ON r.id=h.run_id "
                               "WHERE purpose='producer'"))
    for row in rows:
        if row["status"] not in {"SUCCEEDED", "CANCELLED"}:
            path = store.root / "artifacts" / row["hash"][:2] / row["hash"]
            if path.is_file():
                ref = {"hash": row["hash"], "kind": "check_log", "bytes": path.stat().st_size, "lease_id": row["id"]}
                store.checkpoint(row["run_id"], "diagnostics/recovered/" + row["id"],
                                 {"log": ref, "outcome": "unknown; never acceptance evidence"})
