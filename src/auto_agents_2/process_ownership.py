"""Linux ownership proof for an established preview TCP connection."""
import asyncio
import ipaddress
import os
import sys
from pathlib import Path

from .errors import RetryableInfrastructureError


class PreviewOwnershipError(RetryableInfrastructureError):
    pass


def signature(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[1]), fields[19]
    except (OSError, ValueError, IndexError):
        return None


def owned_sockets(root):
    sockets, seen = set(), set()
    def visit(pid, parent=None):
        before = signature(pid)
        if pid in seen or before is None or parent is not None and before[0] != parent:
            return
        seen.add(pid)
        try:
            descriptors = []
            for path in Path(f"/proc/{pid}/fd").iterdir():
                try:
                    descriptors.append(os.readlink(path))
                except OSError:
                    pass
            children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
        except OSError:
            return
        if signature(pid) != before:
            return
        sockets.update(descriptors)
        for child in children:
            visit(int(child), pid)
    visit(root)
    return sockets


def address(value):
    ip = ipaddress.ip_address(value)
    return ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip


def endpoint(raw):
    host, port = raw.split(":")
    data = bytes.fromhex(host)
    if sys.byteorder == "little":
        data = b"".join(data[i:i + 4][::-1] for i in range(0, len(data), 4))
    return address(data), int(port, 16)


def connection_owned(process, local, peer):
    if process.supervisor.returncode is not None:
        return False
    owned = owned_sockets(process.pid)
    expected = ((address(peer[0]), peer[1]), (address(local[0]), local[1]))
    for table in ("tcp", "tcp6"):
        try:
            lines = Path("/proc/net/" + table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if (fields[3] == "01" and (endpoint(fields[1]), endpoint(fields[2])) == expected
                    and "socket:[" + fields[9] + "]" in owned):
                return True
    return False


async def verify_connection(process, writer):
    # Accept may not yet have created a user-visible descriptor. No HTTP bytes
    # are sent during this bounded visibility wait.
    local, peer = writer.get_extra_info("sockname"), writer.get_extra_info("peername")
    for _ in range(25):
        if await asyncio.to_thread(connection_owned, process, local, peer):
            return
        await asyncio.sleep(0.02)
    raise PreviewOwnershipError("Preview connection does not belong to the managed process tree")
