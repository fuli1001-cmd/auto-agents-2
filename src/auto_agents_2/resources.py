from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

from .config import Cleanup
from .errors import AA2Error, PolicyViolation
from .store import Store
from .util import file_lock, identifier, now


def valid_receipt(receipt):
    from .util import digest
    return isinstance(receipt, dict) and digest({k: v for k, v in receipt.items() if k != "digest"}) == receipt.get("digest")


class ResourceManager:
    def __init__(self, store: Store, cleanup: Cleanup):
        self.store, self.policy = store, cleanup

    def register(self, path: Path, kind: str, run_id: str | None = None, refs: int = 1) -> str:
        resolved = path.resolve()
        if path.absolute() != resolved or not resolved.is_relative_to(self.store.root.resolve()):
            raise PolicyViolation("Managed resources cannot use symlinked paths")
        allowed = [self.store.root / p for p in ("tmp", "worktrees", "environments", "cache", "attempts", "sessions", "recovery")]
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

    def hold(self, path, purpose, run_id=None):
        if path.absolute() != path.resolve() or not path.is_relative_to(self.store.root):
            raise PolicyViolation("Resource hold must name an owned path without symlinks")
        key = identifier("hold")
        with self.store.connect() as db:
            db.execute("INSERT INTO resource_holds VALUES(?,?,?,?)", (key, str(path), run_id, purpose))
        return key

    def release_holds(self, holds):
        with self.store.connect() as db:
            db.executemany("DELETE FROM resource_holds WHERE id=?", [(key,) for key in holds])

    @staticmethod
    def held_paths(db):
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='resource_holds'").fetchone():
            return [Path(row[0]) for row in db.execute("SELECT path FROM resource_holds")]
        return []

    def process_paths(self):
        paths = []
        for record in (self.store.root / "processes").glob("*.json"):
            try:
                data = json.loads(record.read_text())
                if data.get("status") != "cleaned":
                    paths.extend(Path(value) for value in data.get("paths", [data.get("cwd", str(self.store.root))]))
            except (OSError, ValueError, TypeError):
                paths.append(self.store.root)  # Unreadable ownership is not absence.
        return paths

    def protect_pending_deliveries(self):
        """Quarantine corrupt receipt resources before any startup collection."""
        from .util import digest
        for run in self.store.successful():
            try:
                receipt = self.store.get(run["id"], "delivery/receipt")
            except ValueError:
                receipt = {}
            if receipt is not None and not valid_receipt(receipt):
                with self.store.connect() as db:
                    rows = list(db.execute("SELECT path FROM resources WHERE run_id=? AND state!='removed'", (run["id"],)))
                    for row in rows:
                        db.execute("INSERT OR IGNORE INTO resource_holds VALUES(?,?,?,?)",
                                   ("receipt/" + run["id"] + "/" + digest(row[0]), row[0], run["id"], "invalid delivery receipt"))
                self.store.update(run["id"], cleanup_pending=True, cleanup_errors=["Delivery receipt integrity mismatch; resources retained"])

    def release_run(self, run_id: str):
        with self.store.connect() as db:
            db.execute("UPDATE resources SET refs=0,touched=? WHERE run_id=?", (now(), run_id))

    def recover_disposable(self):
        """Call after obtaining the controller lock and reaping previous children."""
        self.protect_pending_deliveries()
        with self.store.connect() as db:
            db.execute("UPDATE resources SET refs=0 WHERE kind='temporary'")
        from .artifact_gc import recover_diagnostic_holds
        recover_diagnostic_holds(self.store)
        self.collect_artifacts_safely()
        return self.collect()

    def finish_delivery(self, rid: str):
        """Called under the controller lock. A failed GC never revokes a delivery."""
        from .workspace import git
        try:
            receipt = self.store.get(rid, "delivery/receipt")
            completed = self.store.run(rid)["status"] == "SUCCEEDED"
        except (OSError, sqlite3.Error, ValueError):
            self.protect_pending_deliveries()
            return  # complete_delivery already durably set cleanup_pending.
        if receipt is None or not completed:
            return
        errors = []
        try:
            if not valid_receipt(receipt):
                self.protect_pending_deliveries()
                raise PolicyViolation("Delivery receipt integrity mismatch; preserving resources")
            with self.store.connect() as db:
                db.execute("DELETE FROM resource_holds WHERE run_id=? AND purpose='invalid delivery receipt'", (rid,))
            self.release_run(rid)
            errors.extend(self.collect()["errors"])
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
        except (OSError, AA2Error, sqlite3.Error) as exc:
            errors.append(str(exc))
        try:
            self.store.update(rid, cleanup_pending=bool(errors), cleanup_errors=errors)
        except (OSError, sqlite3.Error):
            pass  # The durable pending flag remains set for a later retry.

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
        except (OSError, AA2Error, sqlite3.Error) as exc:
            result = {"removed": 0, "bytes": 0, "errors": [str(exc)]}
            from .progress import safe_event
            safe_event(self.store, None, "artifacts.cleanup_failed", result)
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
        rid = None
        try:
            run = self.store.current(unfinished=True)
            rid = self.register(path, "temporary", run["id"] if run else None)
            yield path
        finally:
            original = sys.exception()
            try:
                if rid is None:
                    shutil.rmtree(path)
                else:
                    self.release(rid)
                    self.collect()
            except (OSError, sqlite3.Error, AA2Error) as exc:
                if original is None:
                    raise
                original.add_note("Temporary resource cleanup also failed: " + type(exc).__name__)

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
                retained = [dict(r) for r in db.execute("SELECT * FROM resources WHERE state!='removed'")]
                pins = [Path(row[0]) for row in db.execute("SELECT path FROM delivery_pins")]
                pins.extend(self.held_paths(db))
                pins.extend(self.process_paths())
                terminal = {row[0] for row in db.execute("SELECT id FROM runs WHERE status IN ('SUCCEEDED','CANCELLED')")}
                for row in retained:
                    if row["run_id"] in terminal:
                        row["refs"] = 0
            sizes = {r["path"]: tree_size(Path(r["path"])) for r in rows}
            cache_roots = sorted((Path(r["path"]) for r in retained if r["kind"] in {"cache", "environment"}),
                                 key=lambda p: len(p.parts))
            counted = []
            for path in cache_roots:
                if not any(path.is_relative_to(parent) for parent in counted):
                    counted.append(path)
            cache_size = sum(tree_size(path) for path in counted)
            pressure = shutil.disk_usage(self.store.root).free < self.policy.min_free_bytes
            removed_paths = {}
            for row in sorted(rows, key=lambda r: (-len(Path(r["path"]).parts), r["touched"])):
                path = Path(row["path"])
                if row["run_id"]:
                    run = self.store.run(row["run_id"])
                    recoverable = bool(json.loads(row["data"]).get("recovery_ref"))
                    if run["status"] not in {"SUCCEEDED", "CANCELLED"} and row["kind"] not in {"temporary", "build_recovery"} and not recoverable:
                        continue
                if row["kind"] in {"cache", "environment"}:
                    expired = now() - row["touched"] > self.policy.cache_ttl_seconds
                    if not (expired or pressure or cache_size > self.policy.cache_max_bytes):
                        continue
                if path.absolute() != path.resolve() or not path.resolve().is_relative_to(self.store.root.resolve()):
                    result["errors"].append(f"Unsafe resource: {path}")
                    continue
                size = max(0, sizes[row["path"]] - sum(size for removed, size in removed_paths.items() if removed.is_relative_to(path)))
                if any(Path(r["path"]) != path and Path(r["path"]).is_relative_to(path) for r in retained):
                    continue  # Registered descendants must be reclaimed under their own policy first.
                if any(path.is_relative_to(p) or p.is_relative_to(path)
                       for p in [*pins, *(Path(r["path"]) for r in retained if r["refs"] > 0)]):
                    continue
                result["candidates"].append(str(path))
                if dry_run:
                    cache_size -= size if row["kind"] in {"cache", "environment"} else 0
                    retained = [r for r in retained if r["id"] != row["id"]]
                    removed_paths[path] = size
                    continue
                try:
                    with self.store.connect() as db:
                        db.execute("BEGIN IMMEDIATE")
                        fresh = db.execute("SELECT refs FROM resources WHERE id=?", (row["id"],)).fetchone()
                        if not fresh or fresh[0] != 0 or db.execute("SELECT 1 FROM delivery_pins WHERE path=?", (row["path"],)).fetchone():
                            continue
                        protected = [Path(r["path"]) for r in db.execute(
                            "SELECT path FROM resources WHERE state!='removed' AND refs>0 UNION SELECT path FROM delivery_pins")]
                        protected.extend(self.held_paths(db))
                        protected.extend(self.process_paths())
                        if any(p.is_relative_to(path) or path.is_relative_to(p) for p in protected):
                            continue
                        descendants = [Path(r[0]) for r in db.execute("SELECT path FROM resources WHERE state!='removed' AND id!=?", (row["id"],))]
                        if any(p.is_relative_to(path) for p in descendants):
                            continue
                        size = tree_size(path)
                        if path.absolute() != path.resolve() or not path.resolve().is_relative_to(self.store.root.resolve()):
                            raise PolicyViolation("Resource path changed before collection")
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
                    retained = [r for r in retained if r["id"] != row["id"]]
                    removed_paths[path] = size
                    result["removed"] += 1
                    result["bytes"] += size
                    cache_size -= size if row["kind"] in {"cache", "environment"} else 0
                except (OSError, AA2Error, sqlite3.Error) as exc:
                    result["errors"].append(str(exc))
        if not dry_run and (result["removed"] or result["errors"]):
            from .progress import safe_event
            safe_event(self.store, None, "resources.collected", result)
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
