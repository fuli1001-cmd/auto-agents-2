"""Descriptor-based reads of files produced across a sandbox boundary."""
from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def directory_fd(path: Path, *, create=False):
    path = path.absolute()
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            if part == "..":
                raise ValueError("Parent traversal is not a safe directory")
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def regular_exists(parent, name):
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Expected an owned regular file")
    return True


def write_regular(parent, name, value):
    """Atomically replace a leaf through an already validated parent descriptor."""
    from .util import identifier
    regular_exists(parent, name)
    temporary = identifier(".write")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


def regular_file(parent, name):
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError("Expected a single filename")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Expected a regular file")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def read_regular(path: Path, limit: int) -> bytes:
    """Read one bounded, regular file without following any path component links."""
    with directory_fd(path.parent) as parent, regular_file(parent, path.name) as stream:
        if os.fstat(stream.fileno()).st_size > limit:
            raise ValueError("File exceeds the supported size")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("File exceeds the supported size")
        return data


def regular_files(directory: Path):
    """Yield open files; directory and leaf links are rejected, never traversed."""
    def visit(parent, prefix):
        for name in sorted(os.listdir(parent)):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            relative = prefix / name
            if stat.S_ISDIR(info.st_mode):
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    yield from visit(fd, relative)
                finally:
                    os.close(fd)
            else:
                with regular_file(parent, name) as stream:
                    yield relative.as_posix(), stream
    with directory_fd(directory) as parent:
        yield from visit(parent, Path())
