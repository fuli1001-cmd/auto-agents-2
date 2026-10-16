"""Durable generated-output publication, independent of disposable check scratch."""
import json
import os
from pathlib import Path

from .errors import AA2Error
from .util import contained, digest, file_lock, identifier, write_json
from .workspace import input_entry


def identity(path):
    return digest(input_entry(path, path, excluded=set()))


def sync_tree(path):
    """Persist data before publishing the journal that claims it is recoverable."""
    if path.is_symlink():
        raise AA2Error("Build recovery cannot persist symlinked outputs")
    if path.is_dir():
        for child in path.iterdir():
            sync_tree(child)
        fd = os.open(path, os.O_DIRECTORY)
    else:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_ancestry(path, stop):
    while path.is_relative_to(stop):
        fd = os.open(path, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        path = path.parent


def move(source, target):
    source.rename(target)
    for directory in {source.parent, target.parent}:
        fd = os.open(directory, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class BuildPublication:
    def __init__(self, resources):
        self.resources = resources

    def publish(self, command, workspace, generated):
        root = self.resources.store.root / "recovery" / identifier("build")
        run = self.resources.store.current(unfinished=True)
        rid = run["id"] if run else None
        holds = []
        resource = None
        document = {"version": 2, "phase": "preparing", "workspace": str(workspace),
                    "resource": None, "holds": holds, "outputs": []}
        with file_lock(self.resources.store.root / "gc.lock"):
            try:
                root.mkdir(parents=True)
                sync_ancestry(root, self.resources.store.root)
                write_json(root / "journal.json", document)
                resource = self.resources.register(root, "build_recovery", rid)
                document["resource"] = resource
                for path in (root, generated.parent, workspace):
                    if path.is_relative_to(self.resources.store.root):
                        holds.append(self.resources.hold(path, "build publication " + root.name, rid))
                for index, output in enumerate(command.outputs):
                    before, after = contained(workspace, output), generated / output
                    sync_tree(before)
                    sync_tree(after)
                    sync_ancestry(before.parent, self.resources.store.root if before.is_relative_to(self.resources.store.root) else workspace)
                    sync_ancestry(after.parent, self.resources.store.root)
                    document["outputs"].append({"path": output, "backup": str(index),
                        "before": identity(before), "after": identity(after)})
                document["phase"] = "prepared"
                write_json(root / "journal.json", document)
            except BaseException:
                # No destination has been moved during preparation.
                self.resources.release_holds(holds)
                if resource:
                    self.resources.release(resource)
                elif not (root / "journal.json").exists():
                    root.rmdir()
                raise
        try:
            for row in document["outputs"]:
                destination = contained(workspace, row["path"])
                if identity(destination) != row["before"] or identity(generated / row["path"]) != row["after"]:
                    raise AA2Error("Build outputs changed before publication; preserving all copies")
                move(destination, root / row["backup"])
                move(generated / row["path"], destination)
            for row in document["outputs"]:
                sync_tree(contained(workspace, row["path"]))
            document["phase"] = "published"
            write_json(root / "journal.json", document)
        except BaseException as original:
            try:
                self.rollback(root, document)
            except BaseException as cleanup:
                original.add_note("Build rollback incomplete; retained recovery journal: " + str(root / "journal.json")
                                  + " (" + type(cleanup).__name__ + ")")
            raise
        self.release(root, document)

    def release(self, root, document):
        # Holds and resource retirement form one database transaction. No journal
        # rewrite is needed after release; later edits are unrelated to this build.
        with self.resources.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM resource_holds WHERE purpose=?", ("build publication " + root.name,))
            db.executemany("DELETE FROM resource_holds WHERE id=?", [(key,) for key in document["holds"]])
            db.execute("UPDATE resources SET refs=0 WHERE path=?", (str(root),))

    def rollback(self, root, document):
        errors = []
        workspace = Path(document["workspace"])
        for row in reversed(document["outputs"]):
            try:
                destination = contained(workspace, row["path"])
                previous = root / row["backup"]
                if previous.exists():
                    if identity(previous) != row["before"]:
                        raise AA2Error("Retained build backup changed")
                    if destination.exists():
                        if identity(destination) != row["after"]:
                            raise AA2Error("Output changed during rollback; preserving user changes")
                        move(destination, root / (row["backup"] + "-unpublished"))
                    move(previous, destination)
                if identity(destination) != row["before"]:
                    raise AA2Error("Previous output restoration cannot be verified")
            except Exception as exc:
                errors.append(type(exc).__name__)
        if errors:
            raise AA2Error("Build rollback incomplete: " + ", ".join(errors))
        for row in document["outputs"]:
            sync_tree(contained(workspace, row["path"]))
        document["phase"] = "restored"
        write_json(root / "journal.json", document)
        self.release(root, document)

    def recover(self, workspace):
        root = self.resources.store.root / "recovery"
        with self.resources.store.connect() as db:
            purposes = [row[0] for row in db.execute(
                "SELECT purpose FROM resource_holds WHERE path=? AND purpose LIKE 'build publication %'", (str(workspace),))]
        for purpose in purposes:
            name = purpose.removeprefix("build publication ")
            if Path(name).name != name or not (root / name / "journal.json").is_file():
                raise AA2Error("Legacy build recovery has no usable journal; retain its holds and reconcile manually")
        for journal in sorted(root.glob("build_*/journal.json")):
            document = json.loads(journal.read_text())
            if document["workspace"] != str(workspace):
                continue
            with self.resources.store.connect() as db:
                pending = db.execute("SELECT 1 FROM resource_holds WHERE purpose=?", ("build publication " + journal.parent.name,)).fetchone()
                resource = db.execute("SELECT refs FROM resources WHERE path=?", (str(journal.parent),)).fetchone()
            if not pending and resource and not resource[0]:
                continue  # Already finalized; do not inspect subsequent legitimate edits.
            if document["phase"] == "preparing":
                self.release(journal.parent, document)
            elif document["phase"] in {"published", "restored"}:
                expected = "after" if document["phase"] == "published" else "before"
                for row in document["outputs"]:
                    if identity(contained(workspace, row["path"])) != row[expected]:
                        raise AA2Error("Pending build publication changed; retaining recovery copies and holds")
                    sync_tree(contained(workspace, row["path"]))
                self.release(journal.parent, document)
            else:
                self.rollback(journal.parent, document)
