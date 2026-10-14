from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from pathlib import Path

from .config import Cleanup
from .errors import AA2Error, PolicyViolation
from .store import Store
from .util import file_lock, identifier, now


class ResourceManager:
    def __init__(self, store: Store, cleanup: Cleanup):
        self.store, self.policy = store, cleanup

    def register(self, path: Path, kind: str, run_id: str | None = None, refs: int = 1) -> str:
        resolved = path.resolve()
        if path.absolute() != resolved or not resolved.is_relative_to(self.store.root.resolve()):
            raise PolicyViolation("Managed resources cannot use symlinked paths")
        allowed = [self.store.root / p for p in ("tmp", "worktrees", "environments", "cache", "attempts", "sessions")]
        if not any(resolved.is_relative_to(p.resolve()) and resolved != p.resolve() for p in allowed):
            raise PolicyViolation(f"Unmanaged resource path: {path}")
        if path.is_symlink():
            raise PolicyViolation("Resource roots must not be symlinks")
        with self.store.connect() as db:
            row = db.execute("SELECT id FROM resources WHERE path=?", (str(resolved),)).fetchone()
            rid = row[0] if row else identifier("resource")
            db.execute("""INSERT INTO resources VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET refs=MAX(resources.refs,excluded.refs),
                run_id=excluded.run_id,state='live',touched=excluded.touched""",
                       (rid, run_id, kind, str(resolved), "live", refs, now(), now(), "{}"))
            return rid

    def release(self, rid: str):
        with self.store.connect() as db:
            db.execute("UPDATE resources SET refs=MAX(0,refs-1),touched=? WHERE id=?", (now(), rid))

    def release_run(self, run_id: str):
        with self.store.connect() as db:
            db.execute("UPDATE resources SET refs=0,touched=? WHERE run_id=?", (now(), run_id))

    def recover_disposable(self):
        """Call after obtaining the controller lock and reaping previous children."""
        with self.store.connect() as db:
            db.execute("UPDATE resources SET refs=0 WHERE kind='temporary'")
        from .artifact_gc import recover_diagnostic_holds
        recover_diagnostic_holds(self.store)
        self.collect_artifacts_safely()
        return self.collect()

    def finish_delivery(self, rid: str):
        """Called under the controller lock. A failed GC never revokes a delivery."""
        from .workspace import git
        receipt = self.store.get(rid, "delivery/receipt")
        if not receipt or self.store.run(rid)["status"] != "SUCCEEDED":
            return
        from .util import digest
        if digest({k: v for k, v in receipt.items() if k != "digest"}) != receipt["digest"]:
            raise PolicyViolation("Delivery receipt integrity mismatch; preserving resources")
        errors = []
        self.release_run(rid)
        errors.extend(self.collect()["errors"])
        try:
            with self.store.connect() as db:
                remaining = db.execute("SELECT 1 FROM resources WHERE run_id=? AND kind='worktree' AND state!='removed'", (rid,)).fetchone()
            if remaining:
                raise AA2Error("Delivery worktree cleanup is still pending")
            for ref, oid in receipt.get("owned_refs", {}).items():
                if not ref.startswith(f"refs/aa2/{rid}/"):
                    raise PolicyViolation("Invalid owned delivery ref")
                found = git(self.store.root.parent, "for-each-ref", "--format=%(objectname)", ref)
                if found:
                    git(self.store.root.parent, "update-ref", "-d", ref, oid)
            with self.store.connect() as db:
                # Keep compact task verdicts; remove proposal bodies and duplicate contexts.
                for key, value in receipt["evidence"].items():
                    if key.startswith("develop/task/"):
                        db.execute("UPDATE checkpoints SET data=? WHERE run_id=? AND key=?", (json.dumps(value), rid, key))
                for prefix in ("candidate/", "invocation/", "provider-result/", "materials/", "environment/logs/"):
                    db.execute("DELETE FROM checkpoints WHERE run_id=? AND key LIKE ?", (rid, prefix + "%"))
                for prefix in ("clarify_context", "replan_feedback", "readme", "delivery/journal"):
                    db.execute("DELETE FROM checkpoints WHERE run_id=? AND key=?", (rid, prefix))
            self.collect_artifacts()
        except (OSError, AA2Error) as exc:
            errors.append(str(exc))
        self.store.update(rid, cleanup_pending=bool(errors), cleanup_errors=errors)

    def finish_pending(self):
        for run in self.store.successful():
            if run["data"].get("cleanup_pending"):
                self.finish_delivery(run["id"])

    def collect_artifacts(self):
        from .artifact_gc import collect_artifacts
        return collect_artifacts(self.store)

    def collect_artifacts_safely(self):
        try:
            return self.collect_artifacts()
        except (OSError, AA2Error) as exc:
            result = {"removed": 0, "bytes": 0, "errors": [str(exc)]}
            self.store.event(None, "artifacts.cleanup_failed", result)
            return result

    def retire_worktree(self, path: Path, recovery_ref: str):
        from .workspace import git
        if not recovery_ref.startswith("refs/aa2/"):
            raise PolicyViolation("Worktree recovery requires a controller-owned Git reference")
        commit = git(self.store.root.parent, "rev-parse", "--verify", recovery_ref)
        if git(path, "rev-parse", "HEAD") != commit or git(path, "status", "--porcelain"):
            raise PolicyViolation("Cannot reclaim a worktree with unarchived changes")
        with self.store.connect() as db:
            db.execute("UPDATE resources SET refs=0,data=?,touched=? WHERE path=? AND kind='worktree'",
                       (json.dumps({"recovery_ref": recovery_ref}), now(), str(path.resolve())))
        self.collect()

    @contextmanager
    def temporary(self, label: str):
        path = self.store.root / "tmp" / identifier(label)
        path.mkdir(parents=True)
        run = self.store.current(unfinished=True)
        rid = self.register(path, "temporary", run["id"] if run else None)
        try:
            yield path
        finally:
            self.release(rid)
            self.collect()

    def collect(self, dry_run: bool = False) -> dict:
        result = {"removed": 0, "bytes": 0, "candidates": [], "errors": []}
        with file_lock(self.store.root / "gc.lock"):
            with self.store.connect() as db:
                if not dry_run:
                    db.execute("""UPDATE resources SET refs=0 WHERE run_id IN
                               (SELECT id FROM runs WHERE status IN ('SUCCEEDED','CANCELLED'))""")
                rows = [dict(r) for r in db.execute("""SELECT * FROM resources WHERE state!='removed' AND
                    (refs=0 OR run_id IN (SELECT id FROM runs WHERE status IN ('SUCCEEDED','CANCELLED')))
                    AND path NOT IN (SELECT path FROM delivery_pins)
                    ORDER BY touched""")]
            sizes = {r["path"]: tree_size(Path(r["path"])) for r in rows}
            cache_size = sum(sizes[r["path"]] for r in rows if r["kind"] in {"cache", "environment"})
            pressure = shutil.disk_usage(self.store.root).free < self.policy.min_free_bytes
            for row in rows:
                path = Path(row["path"])
                if row["run_id"]:
                    run = self.store.run(row["run_id"])
                    recoverable = bool(json.loads(row["data"]).get("recovery_ref"))
                    if run["status"] not in {"SUCCEEDED", "CANCELLED"} and row["kind"] != "temporary" and not recoverable:
                        continue
                if row["kind"] in {"cache", "environment"}:
                    expired = now() - row["touched"] > self.policy.cache_ttl_seconds
                    if not (expired or pressure or cache_size > self.policy.cache_max_bytes):
                        continue
                if path.is_symlink() or not path.resolve().is_relative_to(self.store.root.resolve()):
                    result["errors"].append(f"Unsafe resource: {path}")
                    continue
                size = sizes[row["path"]]
                result["candidates"].append(str(path))
                if dry_run:
                    continue
                try:
                    with self.store.connect() as db:
                        db.execute("BEGIN IMMEDIATE")
                        fresh = db.execute("SELECT refs FROM resources WHERE id=?", (row["id"],)).fetchone()
                        if fresh[0] != 0 or db.execute("SELECT 1 FROM delivery_pins WHERE path=?", (row["path"],)).fetchone():
                            continue
                        db.execute("UPDATE resources SET state='collecting' WHERE id=?", (row["id"],))
                        # Paths were created under controller-owned roots, never user directories.
                        if row["kind"] == "worktree" and path.exists():
                            from .workspace import git
                            git(self.store.root.parent, "worktree", "remove", "--force", str(path))
                        elif path.is_dir():
                            shutil.rmtree(path)
                        elif path.exists():
                            path.unlink()
                        db.execute("UPDATE resources SET state='removed' WHERE id=?", (row["id"],))
                    result["removed"] += 1
                    result["bytes"] += size
                    cache_size -= size if row["kind"] in {"cache", "environment"} else 0
                except (OSError, AA2Error) as exc:
                    result["errors"].append(str(exc))
        if not dry_run and (result["removed"] or result["errors"]):
            self.store.event(None, "resources.collected", result)
        return result


def tree_size(path: Path) -> int:
    if not path.exists() or path.is_symlink():
        return 0
    if path.is_file():
        return path.stat().st_size
    total, seen = 0, set()
    for file in path.rglob("*"):
        try:
            if file.is_file() and not file.is_symlink():
                stat = file.stat()
                key = (stat.st_dev, stat.st_ino)
                if key not in seen:
                    total += stat.st_blocks * 512
                    seen.add(key)
        except OSError:
            pass
    return total
