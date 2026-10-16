"""Private helper entry point. No model calls or network access."""
import base64
import json
import os
import stat
import sys
from pathlib import Path

from .artifacts import ArtifactStore
from .errors import AA2Error, CollectionError
from .requirements import MAX_REQUIREMENTS_BYTES, extract_document
from .safe_files import directory_fd, read_regular, regular_file
from .util import digest, now, write_json


def attachments(artifacts, directory, policy, progress=None):
    rows, total, count, entries = [], 0, 0, 0
    def record(**actual):
        if progress is not None:
            write_json(progress, {"files": count, "bytes": total, "attachments": rows, "limits": policy, **actual})
    def reject(reason, **actual):
        record(reason=reason, **actual)
        raise CollectionError("Test attachment collection exceeded its resource budget",
            {"reason": reason, "files": count, "bytes": total, "limits": policy, **actual})
    def visit(fd, prefix, depth):
        nonlocal total, count, entries
        if depth > 64:
            reject("directory_depth", actual_depth=depth)
        # scandir is incremental: a directory with millions of entries does not
        # first allocate millions of names. Bound metadata as well as file bodies.
        with os.scandir(fd) as children:
            for entry in children:
                entries += 1
                if entries > max(1024, policy["max_files"] * 8):
                    reject("metadata_entries", actual_entries=entries)
                info = entry.stat(follow_symlinks=False)
                relative = prefix / entry.name
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        visit(child, relative, depth + 1)
                    finally:
                        os.close(child)
                    continue
                count += 1
                record(path=relative.as_posix(), phase="opening")
                if count > policy["max_files"]:
                    reject("max_files", path=relative.as_posix())
                with regular_file(fd, entry.name) as stream:
                    size = os.fstat(stream.fileno()).st_size
                    remaining = policy["max_total_bytes"] - total
                    if size > policy["max_file_bytes"]:
                        reject("max_file_bytes", path=relative.as_posix(), actual_file_bytes=size)
                    if size > remaining:
                        reject("max_total_bytes", path=relative.as_posix(), actual_total_bytes=total + size)
                    try:
                        ref = artifacts.put_stream(stream, "test_diagnostic", max_bytes=min(remaining, policy["max_file_bytes"]))
                    except ValueError as exc:
                        reject("file_changed_or_exceeded", path=relative.as_posix(), error_type=type(exc).__name__)
                    total += ref["bytes"]
                    rows.append({"path": relative.as_posix(), "purpose": "diagnostic", "artifact": ref})
                    record(phase="archived")
    with directory_fd(directory) as fd:
        visit(fd, Path(), 0)
    return {"attachments": rows, "files": count, "bytes": total, "limits": policy, "status": "complete"}


def execute(root, request):
    artifacts = ArtifactStore(root)
    action = request["action"]
    if action == "attachments":
        return attachments(artifacts, Path(request["path"]), request["policy"],
                           Path(request["progress"]) if request.get("progress") else None)
    if action == "local_requirements":
        try:
            raw = read_regular(Path(request["source"]), MAX_REQUIREMENTS_BYTES)
        except ValueError as exc:
            if str(exc) == "File exceeds the supported size":
                raise CollectionError("Requirements document exceeds 4 MiB") from exc
            raise
        kind = "text/plain"
    else:
        raw = base64.b64decode(request["raw"], validate=True)
        if len(raw) > MAX_REQUIREMENTS_BYTES:
            raise ValueError("Document exceeds acquisition limit")
        kind = request["mime"]
    text, encoding = extract_document(raw, kind)
    if action in {"local_requirements", "requirements"}:
        if not text.strip():
            raise ValueError("Requirements document is empty")
        return {"source": request["source"], "captured_at": now(), "hash": digest(text),
                "artifact": artifacts.put(text, "raw_requirements")}
    if action != "research":
        raise ValueError("Unknown collection operation")
    raw_ref = artifacts.put(raw, "research_response")
    extracted = artifacts.put(text, "research_extracted")
    metadata = {"source": request["source"], "final_url": request["final_url"], "mime_type": kind,
                "encoding": encoding, "captured_at": now(), "extractor_version": "structured-text-v3",
                "raw": raw_ref, "artifact": extracted, "hash": extracted["hash"]}
    return {**metadata, "manifest": artifacts.put(metadata, "research_manifest"), "excerpt": text}


def main():
    try:
        raw = sys.stdin.buffer.read(8 * 1024**2 + 1)
        if len(raw) > 8 * 1024**2:
            raise ValueError("Collection request exceeds limit")
        result = execute(Path(sys.argv[1]), json.loads(raw))
        reply = {"result": result}
    except CollectionError as exc:
        reply = {"error": str(exc), "details": exc.details}
    except AA2Error as exc:
        reply = {"error": str(exc)}
    except Exception as exc:
        # Do not copy document contents, secret paths or library diagnostics to logs.
        reply = {"error": "Evidence acquisition failed: " + type(exc).__name__, "details": {"reason": "invalid_input"}}
    sys.stdout.write(json.dumps(reply))


if __name__ == "__main__":
    main()
