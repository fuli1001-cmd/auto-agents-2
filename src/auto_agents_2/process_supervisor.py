"""Private process-group anchor. Invoked directly with an inherited control socket.

Only this still-live group leader signals its own group. Workload completion and
supervisor teardown are deliberately separate events; control EOF also tears down.
"""
import ctypes
import json
import os
import select
import signal
import socket
import subprocess
import sys
import time


def main():
    control = socket.socket(fileno=int(sys.argv[1]))
    control.set_inheritable(False)
    # Adopt orphaned descendants so escaped sessions also have an owned parent.
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0):
        raise RuntimeError("Cannot establish process ownership")
    signal.signal(signal.SIGTERM, lambda *_: None)
    child = subprocess.Popen(sys.argv[2:], close_fds=True)
    for fd in (0, 1, 2):
        os.close(fd)

    def report(value):
        control.sendall((json.dumps(value) + "\n").encode())

    reported = False
    try:
        report({"started": child.pid})
        while True:
            code = child.poll()
            if code is not None and not reported:
                report({"exited": code})
                reported = True
            if select.select([control], [], [], 0.02)[0]:
                control.recv(1)  # Stop request or EOF both request teardown.
                break
    except (OSError, KeyboardInterrupt):
        pass
    finally:
        os.killpg(os.getpid(), signal.SIGTERM)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if child.poll() is not None:
                if not reported:
                    try:
                        report({"exited": child.returncode})
                    except OSError:
                        pass
                    reported = True
                break
            time.sleep(0.02)
        # These are direct unreaped children: their IDs cannot be reused until
        # this supervisor reaps them. Repeat after adoption of grandchildren.
        children_file = f"/proc/{os.getpid()}/task/{os.getpid()}/children"
        for _ in range(200):
            with open(children_file) as stream:
                children = [int(pid) for pid in stream.read().split()]
            if not children:
                break
            for pid in children:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            for pid in children:
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
            time.sleep(0.01)
        os.killpg(os.getpid(), signal.SIGKILL)


if __name__ == "__main__":
    main()
