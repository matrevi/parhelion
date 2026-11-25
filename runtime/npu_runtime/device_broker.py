"""Resident device-host broker: one process owns the device lock and runs jobs in WSPT order.

Clients submit whole case batches over a unix socket (one connection per job). The broker
executes one job at a time while holding the shared device lock file, so tools that take the
flock directly stay mutually exclusive. A case that exceeds its budget is reported as TLE, the
lock is released, and the remaining cases are queued again as the next segment of the job.

The file is shipped verbatim to the device host and also runs as its own script, so it uses
only the standard library and must not import sibling modules.
"""
import argparse
import fcntl
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

IDLE_EXIT_SEC = 1800
STARVATION_SEC = 1800
START_WAIT_SEC = 5.0


def broker_dir():
    return Path(os.environ.get("OPSB_BROKER_DIR") or Path.home() / ".opsb/broker")


def ticket_dir():
    return Path(os.environ.get("OPSB_TICKET_DIR") or "/tmp/opsb-npu-tickets")


def source_version(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _write_json(path, data):
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data))
    temp.replace(path)


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reap_dead_tickets(directory):
    """Delete admission tickets whose owning process no longer exists; return how many."""
    removed = 0
    for path in Path(directory).glob("*.json"):
        try:
            pid = int(json.loads(path.read_text())["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if pid > 0 and not _pid_alive(pid):
            try:
                path.unlink()
                removed += 1
            except FileNotFoundError:
                pass
    return removed


class SampleProcess:
    """The sample reads ``"{timed} {path}"`` lines on stdin and answers ``"rc us"`` on OPSB_BATCH_FD."""

    def __init__(self, executable, cfg):
        self.executable, self.cfg = executable, cfg
        self.proc = None

    def start(self):
        self.log = tempfile.TemporaryFile()
        self.channel, child = socket.socketpair()
        try:
            self.proc = subprocess.Popen(
                [self.executable, self.cfg["device"]], stdin=subprocess.PIPE,
                stdout=self.log, stderr=self.log, pass_fds=(child.fileno(),),
                env={**os.environ, "OPSB_BATCH_FD": str(child.fileno())},
            )
        finally:
            child.close()
        self.reply = self.channel.makefile("rb")

    def kill(self):
        """Signal the running sample from another thread; the executing thread then cleans up."""
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def close(self, kill=False):
        if self.proc is None:
            return
        if kill:
            self.proc.kill()
        try:
            self.proc.stdin.close()
        except BrokenPipeError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.reply.close()
        self.channel.close()
        self.log.close()
        self.proc = None

    def execute(self, path, timed, deadline):
        if self.proc is None:
            self.start()
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            self.channel.settimeout(remaining)
            self.proc.stdin.write(f"{int(timed)} {path}\n".encode())
            self.proc.stdin.flush()
            fields = self.reply.readline(128).split()
            if len(fields) == 2 and fields[0] == b"0":
                return {"status": "PASS", "us": float(fields[1])}
            detail = os.pread(self.log.fileno(), 1500, max(0, os.fstat(self.log.fileno()).st_size - 1500))
            result = {"status": "RE", "detail": detail.decode(errors="replace")}
        except TimeoutError:
            result = {"status": "TLE", "detail": f"Device execution exceeded the {self.cfg['timeout']}s case budget"}
        except (OSError, ValueError) as exc:
            result = {"status": "RE", "detail": str(exc)}
        self.close(kill=True)
        return result


# ---------------------------------------------------------------------------- client side

def _exchange(path, request, timeout=2.0):
    """Send one request and return the first reply object, or None when nobody answers."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout)
            conn.connect(str(path))
            conn.sendall((json.dumps(request) + "\n").encode())
            with conn.makefile("rb") as reader:
                line = reader.readline(1 << 16)
        return json.loads(line) if line else None
    except (OSError, ValueError):
        return None


def _matches(info, version, lock):
    return bool(info and info.get("version") == version and info.get("lock") == lock
                and not info.get("draining"))


def ensure_broker(cfg, source_text):
    """Return the socket path of a broker running ``source_text``, starting one if needed, else None."""
    try:
        directory = broker_dir()
        path = directory / "broker.sock"
        version = source_version(source_text)
        info = _exchange(path, {"op": "hello"})
        if _matches(info, version, cfg["lock"]):
            return str(path)
        if info is not None:
            # A different broker owns the socket: let it finish its accepted jobs, then replace it.
            _exchange(path, {"op": "drain"})
            budget = cfg.get("job_timeout") or min(1800, cfg.get("timeout", 60) * max(1, len(cfg.get("selected") or ())))
            deadline = time.monotonic() + float(budget)
            while _exchange(path, {"op": "hello"}) is not None:
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.1)
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "broker.start.lock").open("a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            info = _exchange(path, {"op": "hello"})
            if info is not None:
                return str(path) if _matches(info, version, cfg["lock"]) else None
            script = directory / f"broker-{version}.py"
            if not script.exists() or script.read_text(encoding="utf-8") != source_text:
                temp = script.with_name(script.name + ".tmp")
                temp.write_text(source_text, encoding="utf-8")
                temp.replace(script)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            with (directory / "broker.log").open("ab") as log:
                subprocess.Popen(
                    [sys.executable, "-u", str(script), "--dir", str(directory),
                     "--lock", cfg["lock"], "--device", str(cfg["device"])],
                    start_new_session=True, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    cwd=str(directory),
                )
            deadline = time.monotonic() + START_WAIT_SEC
            while time.monotonic() < deadline:
                if _matches(_exchange(path, {"op": "hello"}), version, cfg["lock"]):
                    return str(path)
                time.sleep(0.05)
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None


# ---------------------------------------------------------------------------- broker side

class Job:
    def __init__(self, spec, conn):
        self.spec, self.conn = spec, conn
        self.run_id = str(spec["run_id"])
        self.client_pid = int(spec["client_pid"])
        self.base_tier = int(spec.get("tier", 1))
        self.base_prio = float(spec.get("prio", 1.0))
        self.aging_tau = float(spec.get("aging_tau", 600))
        self.created = float(spec.get("created") or time.time())
        self.fast = bool(spec.get("fast", False))
        self.case_timeout = float(spec["case_timeout"])
        self.job_timeout = float(spec.get("job_timeout") or min(1800, self.case_timeout * len(spec["cases"])))
        self.reuse_session = bool(spec.get("reuse_session", True))
        self.executable = str(spec["executable"])
        self.device = str(spec.get("device", "0"))
        self.pending = deque(spec["cases"])
        for case in self.pending:
            int(case["index"]), str(case["id"]), list(case["parts"])
        self.total = len(self.pending)
        self.segments = 0
        self.wait = self.hold = 0.0
        self.queued_at = time.monotonic()
        self.position = None
        self.cancelled = self.finished = False
        self.sample = None
        self.send_lock = threading.Lock()

    def key(self, now=None):
        waited = max(0.0, (now or time.time()) - self.created)
        tier = max(self.base_tier, 2) if waited >= STARVATION_SEC else self.base_tier
        prio = self.base_prio + waited / max(60.0, self.aging_tau)
        return tier, prio, -self.created

    def ticket(self):
        tier, prio, _ = self.key()
        head = self.pending[0] if self.pending else {}
        return dict(pid=self.client_pid, title=self.spec.get("title"), sha=self.spec.get("sha"),
                    run_id=self.run_id, fast=self.fast, tier=tier, prio=prio, created=self.created,
                    case_progress=dict(index=head.get("index"), total=self.total, id=head.get("id"),
                                       phase="waiting"),
                    broker=True)

    def send(self, event, **fields):
        try:
            with self.send_lock:
                self.conn.sendall((json.dumps(dict(event=event, **fields)) + "\n").encode())
            return True
        except OSError:
            self.cancelled = True
            return False


class Broker:
    def __init__(self, directory, lock, device, idle=IDLE_EXIT_SEC):
        self.dir, self.lock_path, self.device, self.idle = Path(directory), lock, device, idle
        self.version = source_version(Path(__file__).read_text(encoding="utf-8"))
        self.cond = threading.Condition(threading.RLock())
        self.queue = []
        self.running = None
        self.draining = self.closing = False
        self.last_active = time.monotonic()
        self.tickets = ticket_dir()
        self.session = Path.home() / ".opsb/die_session.json"

    # -- bookkeeping (always under self.cond)
    def _ticket_path(self, job):
        return self.tickets / f"{job.client_pid}.json"

    def _write_ticket(self, job):
        try:
            self.tickets.mkdir(parents=True, exist_ok=True)
            _write_json(self._ticket_path(job), job.ticket())
        except OSError:
            pass

    def _drop_ticket(self, job):
        self._ticket_path(job).unlink(missing_ok=True)

    def _prune(self):
        for job in [j for j in self.queue if j.cancelled]:
            self.queue.remove(job)
            self._drop_ticket(job)

    def _refresh(self, tickets=False):
        self._prune()
        now = time.time()
        order = sorted(self.queue, key=lambda j: j.key(now), reverse=True)
        for position, job in enumerate(order, 1):
            if tickets:
                self._write_ticket(job)
            if job.position != position:
                job.position = position
                job.send("queued", position=position, ahead=[j.run_id for j in order[:position - 1]][:5])
        self._prune()

    # -- connection handling
    def handle(self, conn):
        job = None
        try:
            with conn.makefile("rb") as reader:
                request = json.loads(reader.readline(1 << 24) or b"null")
            op = request.get("op") if isinstance(request, dict) else None
            if op == "hello":
                with self.cond:
                    reply = dict(event="hello", version=self.version, pid=os.getpid(), lock=self.lock_path,
                                 device=self.device, draining=self.draining or self.closing,
                                 queued=len(self.queue), running=self.running.run_id if self.running else None)
                conn.sendall((json.dumps(reply) + "\n").encode())
            elif op == "drain":
                with self.cond:
                    self.draining = True
                    self.cond.notify_all()
                conn.sendall((json.dumps(dict(event="draining", pid=os.getpid())) + "\n").encode())
            elif op == "submit":
                try:
                    job = Job(request["job"], conn)
                except (KeyError, ValueError, TypeError) as exc:
                    raise ValueError(f"Invalid job: {exc!r}") from exc
                with self.cond:
                    if self.draining or self.closing:
                        job.finished = True
                        raise ValueError("Broker is draining; submit to its replacement")
                    self.queue.append(job)
                    self.last_active = time.monotonic()
                    self._write_ticket(job)
                    self._refresh()
                    self.cond.notify_all()
                # The client never writes again; EOF means its run process went away.
                try:
                    while conn.recv(4096):
                        pass
                except OSError:
                    pass
            else:
                raise ValueError(f"Unknown op {op!r}")
        except (OSError, ValueError, AttributeError) as exc:
            try:
                conn.sendall((json.dumps(dict(event="error", detail=str(exc))) + "\n").encode())
            except OSError:
                pass
        finally:
            if job is not None:
                self.cancel(job)
            conn.close()

    def cancel(self, job):
        with self.cond:
            if job.finished:
                return
            job.cancelled = True
            if job in self.queue:
                self.queue.remove(job)
                self._drop_ticket(job)
                self._refresh()
            sample = job.sample
            self.cond.notify_all()
        if sample is not None:
            sample.kill()

    # -- execution
    def _take(self):
        with self.cond:
            self._prune()
            if not self.queue:
                return None
            now = time.time()
            job = max(self.queue, key=lambda j: j.key(now))
            self.queue.remove(job)
            self._drop_ticket(job)
            self.running = job
            job.position = None
            self._refresh()
            return job

    def _write_session(self, record):
        try:
            self.session.parent.mkdir(parents=True, exist_ok=True)
            _write_json(self.session, record)
        except OSError:
            pass

    def _run_case(self, job, sample, case, record, deadline):
        parts = case["parts"]
        timed = bool(case.get("timed"))
        base = dict(index=case["index"], total=job.total, id=case["id"], phase="execute",
                    started_epoch=time.time(), timeout_sec=job.case_timeout)
        groups = {"warm": [], "run": []}
        for number, part in enumerate(parts, 1):
            for phase in ("warm", "run"):
                paths = part.get("warm" if phase == "warm" else "runs") or []
                values = []
                for repeat, path in enumerate(paths, 1):
                    if job.cancelled:
                        return None
                    step = dict(index=case["index"], id=case["id"], part=number, parts=len(parts),
                                repeat=repeat, repeats=len(paths), phase=phase)
                    record["case_progress"] = dict(base, part=number, parts=len(parts), repeat=repeat,
                                                   repeats=len(paths), stage=phase)
                    self._write_session(record)
                    job.send("progress", **step)
                    result = sample.execute(path, timed, deadline)
                    if result["status"] != "PASS":
                        return result
                    values.append(result["us"])
                    if not job.reuse_session:
                        sample.close()
                groups[phase].append(values)
        return {"status": "PASS", "samples": [sum(g) for g in zip(*groups["run"])],
                "cold": [sum(g) for g in zip(*groups["warm"])], "detail": ""}

    def _close_rest(self, job, status, detail):
        while job.pending:
            case = job.pending.popleft()
            job.send("case", index=case["index"], id=case["id"], status=status, samples=[], cold=[], detail=detail)

    def _segment(self, job):
        """Run cases while the caller holds the device lock; return True when the job must requeue."""
        job.segments += 1
        waited = time.monotonic() - job.queued_at
        job.wait += waited
        started = time.monotonic()
        record = dict(pid=job.client_pid, title=job.spec.get("title"), sha=job.spec.get("sha"),
                      run_id=job.run_id, die=0, mode="Q0" if job.fast else "Q1", substage="npu_exec",
                      started_epoch=time.time(), broker=True,
                      case_progress=dict(index=job.pending[0]["index"], total=job.total, id=job.pending[0]["id"],
                                         phase="execute") if job.pending else None,
                      holder_pid=os.getpid(), segment=job.segments)
        self._write_session(record)
        job.send("started", wait_sec=round(waited, 3), segment=job.segments)
        sample = SampleProcess(job.executable, {"device": job.device, "timeout": job.spec["case_timeout"]})
        with self.cond:
            job.sample = sample
        requeue = False
        try:
            while job.pending and not job.cancelled:
                budget = job.job_timeout - job.hold - (time.monotonic() - started)
                if budget <= 0:
                    self._close_rest(job, "TLE", f"Job exceeded its {job.spec.get('job_timeout')}s device budget")
                    break
                case = job.pending.popleft()
                by_job = budget < job.case_timeout
                result = self._run_case(job, sample, case, record, time.monotonic() + min(job.case_timeout, budget))
                if result is None or job.cancelled:
                    break
                if result["status"] == "TLE" and by_job:
                    result["detail"] = f"Job exceeded its {job.spec.get('job_timeout')}s device budget"
                job.send("case", index=case["index"], id=case["id"], status=result["status"],
                         samples=result.get("samples", []), cold=result.get("cold", []),
                         detail=result.get("detail", ""))
                if result["status"] == "RE":
                    self._close_rest(job, "NOT_RUN", "Earlier device execution failed")
                elif result["status"] == "TLE":
                    if by_job:
                        self._close_rest(job, "TLE", result["detail"])
                    requeue = bool(job.pending)
                    break
        finally:
            with self.cond:
                job.sample = None
            sample.close(kill=job.cancelled)
            self.session.unlink(missing_ok=True)
        record["hold_sec"] = round(time.monotonic() - started, 4)
        job.hold += time.monotonic() - started
        return record, requeue and not job.cancelled

    def _finish(self, job):
        with self.cond:
            job.finished = True
            if self.running is job:
                self.running = None
            self.last_active = time.monotonic()
        job.send("done", wait_sec=round(job.wait, 3), hold_sec=round(job.hold, 3), segments=job.segments)
        try:
            job.conn.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def serve_one(self):
        with open(self.lock_path, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                job = self._take()
                if job is None:
                    return
                try:
                    record, requeue = self._segment(job)
                except Exception as exc:  # A broker bug must not strand the client or the lock.
                    job.send("error", detail=f"Broker failure: {exc!r}")
                    job.pending.clear()
                    record, requeue = None, False
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
        if record is not None:
            record.update(ended_epoch=time.time())
            try:
                with (self.session.parent / "die_ledger.jsonl").open("a") as ledger:
                    ledger.write(json.dumps(record) + "\n")
            except OSError:
                pass
            job.send("released", hold_sec=record["hold_sec"], segment=job.segments,
                     requeued=len(job.pending) if requeue else 0)
        if requeue:
            with self.cond:
                self.running = None
                if not job.cancelled:
                    job.queued_at = time.monotonic()
                    self.queue.append(job)
                    self._write_ticket(job)
                    self._refresh()
            return
        self._finish(job)

    def _ticker(self):
        while not self.closing:
            with self.cond:
                if self.queue:
                    self._refresh(tickets=True)
            time.sleep(1.0)

    def _accept(self, server):
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def serve(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        reap_dead_tickets(self.tickets)
        path = self.dir / "broker.sock"
        pid_file = self.dir / "broker.pid"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        server.bind(str(path))
        server.listen(64)
        pid_file.write_text(str(os.getpid()))
        threading.Thread(target=self._accept, args=(server,), daemon=True).start()
        threading.Thread(target=self._ticker, daemon=True).start()
        try:
            while True:
                with self.cond:
                    self._prune()
                    if not self.queue:
                        if self.draining or time.monotonic() - self.last_active >= self.idle:
                            self.closing = True
                            break
                        self.cond.wait(1.0)
                        continue
                self.serve_one()
        finally:
            self.closing = True
            path.unlink(missing_ok=True)
            try:
                if pid_file.read_text().strip() == str(os.getpid()):
                    pid_file.unlink()
            except OSError:
                pass
            server.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", required=True)
    parser.add_argument("--lock", required=True)
    parser.add_argument("--device", default="0")
    args = parser.parse_args(argv)
    idle = float(os.environ.get("OPSB_BROKER_IDLE") or IDLE_EXIT_SEC)
    Broker(args.dir, args.lock, args.device, idle=idle).serve()


if __name__ == "__main__":
    main()
