"""Private Linux subreaper: retain ownership until every adopted child is reaped."""
import ctypes
import json
import os
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


def save(path, document):
    temporary = path.with_suffix(".writing")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(document, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def main():
    control = socket.socket(fileno=int(sys.argv[1]))
    control.set_inheritable(False)
    record = Path(sys.argv[2])
    document = json.loads(record.read_text())
    document.update(pid=os.getpid(), start=Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()[19],
                    status="running")
    save(record, document)
    child = None
    reported = False

    def report(value):
        try:
            control.sendall((json.dumps(value) + "\n").encode())
        except OSError:
            pass

    def reap():
        nonlocal reported
        # waitpid(-1) reaps adopted zombies during normal execution as well as
        # teardown. Account for the original child here, never poll it separately.
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if not pid:
                return
            if child is not None and pid == child.pid:
                child.returncode = os.waitstatus_to_exitcode(status)
                if not reported:
                    report({"exited": child.returncode})
                    reported = True

    try:
        if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0):
            raise RuntimeError("Cannot establish process ownership")
        signal.signal(signal.SIGTERM, lambda *_: None)
        child = subprocess.Popen(sys.argv[3:], close_fds=True)
        for fd in (0, 1, 2):
            os.close(fd)
        report({"started": child.pid})
        while True:
            reap()
            if select.select([control], [], [], 0.02)[0]:
                control.recv(1)
                break
    except (OSError, KeyboardInterrupt):
        pass
    finally:
        try:
            if child is not None:
                os.killpg(os.getpid(), signal.SIGTERM)
        except OSError:
            pass
        children_file = Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                reap()
                if not children_file.read_text().split():
                    break
            except OSError:
                pass
            time.sleep(0.02)
        # No fixed pass limit: an inspection/permission failure keeps the anchor
        # and durable record alive. Other children must still be cleaned up.
        while True:
            try:
                reap()
                children = [int(pid) for pid in children_file.read_text().split()]
            except OSError:
                time.sleep(0.05)
                continue
            if not children:
                break
            for pid in children:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    continue
            time.sleep(0.01)
        document["status"] = "cleaned"
        save(record, document)
        report({"cleaned": True})
        control.close()


if __name__ == "__main__":
    main()
