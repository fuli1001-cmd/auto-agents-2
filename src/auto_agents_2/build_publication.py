"""Durable generated-output publication, independent of disposable check scratch."""
import json
import os
from pathlib import Path

from .errors import AA2Error
from .util import contained, digest, file_lock, identifier, write_json
from .workspace import input_entry


def identity(path):
    return digest(input_entry(path, path, excluded=set()))


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
        with file_lock(self.resources.store.root / "gc.lock"):
            resource = self.resources.register(root, "build_recovery", rid)
            for path in (root, generated.parent, workspace):
                if path.is_relative_to(self.resources.store.root):
                    holds.append(self.resources.hold(path, "build publication " + root.name, rid))
            root.mkdir(parents=True)
            document = {"version": 1, "phase": "prepared", "workspace": str(workspace),
                        "resource": resource, "holds": holds, "outputs": []}
            for index, output in enumerate(command.outputs):
                document["outputs"].append({"path": output, "backup": str(index),
                    "before": identity(contained(workspace, output)), "after": identity(generated / output)})
            write_json(root / "journal.json", document)
        try:
            for row in document["outputs"]:
                destination = contained(workspace, row["path"])
                if identity(destination) != row["before"] or identity(generated / row["path"]) != row["after"]:
                    raise AA2Error("Build outputs changed before publication; preserving all copies")
                move(destination, root / row["backup"])
                move(generated / row["path"], destination)
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
        self.resources.release_holds(document["holds"])
        self.resources.release(document["resource"])

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
        document["phase"] = "restored"
        write_json(root / "journal.json", document)
        self.release(root, document)

    def recover(self, workspace):
        root = self.resources.store.root / "recovery"
        for journal in sorted(root.glob("build_*/journal.json")):
            document = json.loads(journal.read_text())
            if document["workspace"] != str(workspace):
                continue
            if document["phase"] in {"published", "restored"}:
                self.release(journal.parent, document)
            else:
                self.rollback(journal.parent, document)
