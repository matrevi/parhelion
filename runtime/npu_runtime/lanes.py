"""Remote CMake budget, WSPT admission and probe-visible ownership records."""
import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path


def _lane_write(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data))
    temp.replace(path)


def _live_records(directory):
    for path in directory.glob("*.json"):
        try:
            data = json.loads(path.read_text())
            if int(data["pid"]) > 0:
                os.kill(int(data["pid"]), 0)
                yield data
        except (OSError, ValueError, KeyError, TypeError):
            continue


def _identity(cfg):
    return {"pid": os.getpid(), "title": cfg["title"], "sha": cfg["sha"], "run_id": cfg.get("run_id")}


@contextmanager
def case_series(cfg):
    directory = Path.home() / ".opsb/case_runs"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{cfg['run_id']}.json"
    record = dict(_identity(cfg), cwd=os.getcwd(), started_epoch=time.time())
    try:
        yield lambda progress: _lane_write(path, dict(record, case_progress=progress))
    finally:
        path.unlink(missing_ok=True)


def compilation_units(build_dir):
    """Read the configured build graph, including ASC rules from CMake packages."""
    build_dir = Path(build_dir)
    try:
        commands = json.loads((build_dir / "compile_commands.json").read_text())
        if commands:
            return len(commands)
    except (OSError, ValueError):
        pass
    # Some versions of CMake's ASC language module only emit Makefile object rules.
    objects = set()
    for makefile in build_dir.glob("**/CMakeFiles/*.dir/build.make"):
        for line in makefile.read_text().splitlines():
            target, sep, _ = line.partition(":")
            if sep and target.endswith(".o") and not target.startswith(("\t", "#")):
                objects.add(target)
    return len(objects) or None


@contextmanager
def compile_lane(cfg, units=None):
    directory = Path.home() / ".opsb/cpu_builds"
    directory.mkdir(parents=True, exist_ok=True)
    record = directory / f"{os.getpid()}.json"
    cores = os.cpu_count() or 1
    budget = max(1, cores - max(2, cores // 10))
    requested = max(1, int(cfg.get("jobs") or 16))
    if units is not None:
        requested = min(requested, max(1, units))
    while True:
        with (directory / ".allocation.lock").open("a") as allocation:
            fcntl.flock(allocation, fcntl.LOCK_EX)
            reserved = sum(int(r.get("jobs") or 1) for r in _live_records(directory))
            jobs = min(requested, budget - reserved)
            if jobs > 0:
                _lane_write(record, dict(_identity(cfg), state="building", started_epoch=time.time(), jobs=jobs))
                break
        time.sleep(0.2)
    try:
        yield jobs
    finally:
        record.unlink(missing_ok=True)


def ticket_directory():
    return Path(os.environ.get("OPSB_TICKET_DIR") or "/tmp/opsb-npu-tickets")


def reap_dead_tickets(directory):
    """Delete admission tickets whose owning process no longer exists; return how many."""
    removed = 0
    for path in Path(directory).glob("*.json"):
        try:
            pid = int(json.loads(path.read_text())["pid"])
            if pid <= 0:
                continue
            os.kill(pid, 0)
        except ProcessLookupError:
            try:
                path.unlink()
                removed += 1
            except FileNotFoundError:
                pass
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return removed


def _priority(ticket):
    return ticket["tier"], ticket["prio"], -ticket["created"]


def wait_as_queue_head(handle, directory, is_head, deadline=None):
    """Only the elected waiter joins the kernel queue, including behind direct flock users."""
    import signal
    with (directory / ".device-admission").open("a") as admission:
        try:
            fcntl.flock(admission, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        if not is_head():
            return False
        if deadline is None:
            fcntl.flock(handle, fcntl.LOCK_EX)
            return True

        def expired(*_):
            raise TimeoutError("Timed out waiting for an NPU die")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for an NPU die")
        previous = signal.signal(signal.SIGALRM, expired)
        try:
            signal.setitimer(signal.ITIMER_REAL, remaining)
            fcntl.flock(handle, fcntl.LOCK_EX)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
        return True


@contextmanager
def device_lane(cfg):
    directory = ticket_directory()
    directory.mkdir(parents=True, exist_ok=True)
    owner = _identity(cfg)
    owner["case_progress"] = cfg.get("case_progress")
    ticket = directory / f"{owner['pid']}.json"
    session = Path.home() / ".opsb/die_session.json"
    session.parent.mkdir(parents=True, exist_ok=True)
    fast = bool(cfg.get("fast"))
    created = time.time()
    with Path(cfg["lock"]).open("a") as handle:
        try:
            while True:
                reap_dead_tickets(directory)
                wait = max(0, time.time() - created)
                tier = 2 if fast or wait >= 1800 else 1
                if ticket.exists():
                    try:
                        tier = max(tier, int(json.loads(ticket.read_text())["tier"]))
                    except (KeyError, ValueError, OSError):
                        pass
                priority = cfg.get("wspt_base", 1.0) + wait / max(60, cfg.get("aging_tau", 600))
                mine = dict(owner, fast=fast, tier=tier, prio=priority, created=created)
                _lane_write(ticket, mine)
                def is_head():
                    return all(_priority(t) <= _priority(mine) for t in _live_records(directory)
                               if all(k in t for k in ("tier", "prio", "created")))
                if wait_as_queue_head(handle, directory, is_head):
                    break
                time.sleep(0.2)
            acquired = time.monotonic()
            ticket.unlink(missing_ok=True)
            record = dict(owner, die=0, mode="Q0" if fast else "Q1", substage="npu_exec",
                          started_epoch=time.time())
            _lane_write(session, record)
            try:
                def progress(value):
                    record["case_progress"] = value
                    _lane_write(session, record)
                yield progress
            finally:
                session.unlink(missing_ok=True)
                fcntl.flock(handle, fcntl.LOCK_UN)
                record.update(ended_epoch=time.time(), hold_sec=round(time.monotonic() - acquired, 4))
                with (session.parent / "die_ledger.jsonl").open("a") as ledger:
                    ledger.write(json.dumps(record) + "\n")
        finally:
            ticket.unlink(missing_ok=True)
