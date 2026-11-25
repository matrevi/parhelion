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
def compile_lane(cfg):
    directory = Path.home() / ".opsb/cpu_builds"
    directory.mkdir(parents=True, exist_ok=True)
    record = directory / f"{os.getpid()}.json"
    cores = os.cpu_count() or 1
    budget = max(1, cores - max(2, cores // 10))
    requested = max(1, int(cfg.get("jobs") or 16))
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


def _priority(ticket):
    return ticket["tier"], ticket["prio"], -ticket["created"]


@contextmanager
def device_lane(cfg):
    directory = Path("/tmp/opsb-npu-tickets")
    directory.mkdir(parents=True, exist_ok=True)
    owner = _identity(cfg)
    ticket = directory / f"{owner['pid']}.json"
    session = Path.home() / ".opsb/die_session.json"
    session.parent.mkdir(parents=True, exist_ok=True)
    fast = bool(cfg.get("fast"))
    created = time.time()
    with Path(cfg["lock"]).open("a") as handle:
        try:
            while True:
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
                if all(_priority(t) <= _priority(mine) for t in _live_records(directory)
                       if all(k in t for k in ("tier", "prio", "created"))):
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        pass
                time.sleep(0.2)
            ticket.unlink(missing_ok=True)
            _lane_write(session, dict(owner, die=0, mode="Q0" if fast else "Q1",
                                      substage="npu_exec", started_epoch=time.time()))
            try:
                yield
            finally:
                session.unlink(missing_ok=True)
        finally:
            ticket.unlink(missing_ok=True)
