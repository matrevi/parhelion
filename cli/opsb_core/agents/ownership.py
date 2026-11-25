"""Exclusive runner leases and recovery of native worker process groups."""
import fcntl
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path


def claim_runner(state_dir, agent_id):
    leases = state_dir / "runner-leases"
    leases.mkdir(exist_ok=True)
    lease = (leases / agent_id).open("a")
    try:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lease.close()
        raise RuntimeError(f"Worker {agent_id} is owned by another platform process") from None
    return lease


def _has_identity(pid, agent_id):
    marker = b"PARHELION_AGENT_ID=" + agent_id.encode()
    if sys.platform == "darwin":
        import ctypes
        # KERN_PROCARGS2 separates argv from the initial environment, unlike ps.
        buf = ctypes.create_string_buffer(1024 * 1024)
        size = ctypes.c_size_t(len(buf))
        mib = (ctypes.c_int * 3)(1, 49, pid)
        if ctypes.CDLL(None).sysctl(mib, 3, buf, ctypes.byref(size), None, 0):
            return False
        raw = buf.raw[:size.value]
        if len(raw) < 4 or b"\0" not in raw[4:]:
            return False
        argc = int.from_bytes(raw[:4], sys.byteorder)
        fields = raw[raw.index(b"\0", 4):].lstrip(b"\0").split(b"\0")
        return marker in fields[argc:]
    try:
        return marker in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return False


def owned_groups(agent_ids):
    if not agent_ids:
        return {}
    # Only retain group ids. Environment values must never enter logs or API responses.
    snapshot = subprocess.check_output(["ps", "axeww", "-o", "pid=,pgid=,command="],
                                       text=True, errors="replace", timeout=10)
    groups = {}
    for line in snapshot.splitlines():
        match = re.search(r"\sPARHELION_AGENT_ID=(\S+)(?=\s|$)", line)
        if not match or match[1] not in agent_ids:
            continue
        pid, group, _ = line.split(None, 2)
        group = int(group)
        if group > 1 and group != os.getpgrp() and _has_identity(int(pid), match[1]):
            # A tagged descendant may outlive the session leader. Never signal a
            # group led by an unrelated process (for instance an interactive shell).
            try:
                os.getpgid(group)
            except ProcessLookupError:
                pass
            else:
                if int(pid) != group and not _has_identity(group, match[1]):
                    continue
            groups.setdefault(match[1], set()).add(group)
    return groups


def reap_groups(agent_id, groups):
    if not groups:
        return
    # The process ids in the database can survive a reboot or be recycled.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        remaining = groups & owned_groups({agent_id}).get(agent_id, set())
        if not remaining:
            return
        for group in remaining:
            try:
                os.killpg(group, sig)
            except ProcessLookupError:
                pass
        time.sleep(0.2)
    if groups & owned_groups({agent_id}).get(agent_id, set()):
        raise RuntimeError(f"Worker {agent_id} still has a live process group")
