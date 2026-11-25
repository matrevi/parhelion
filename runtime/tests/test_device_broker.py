"""Device broker, warm timing and lane fallback, driven by a fake sample program."""
import fcntl
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

RUNTIME = Path(__file__).resolve().parents[1] / "npu_runtime"
sys.path.insert(0, str(RUNTIME))

import case_batch  # noqa: E402
import device_broker  # noqa: E402
import lanes  # noqa: E402

SOURCE = (RUNTIME / "device_broker.py").read_text(encoding="utf-8")

# Speaks the SampleProcess protocol. The directory holding each path names the behaviour:
# "us-<v>" answers v µs (warm paths answer v+100), "sleep-<s>" delays, "hang" never answers,
# "fail" exits with an error code. Tokens combine with "_".
FAKE_SAMPLE = r'''#!{python}
import os, sys, time
out = os.fdopen(int(os.environ["OPSB_BATCH_FD"]), "w", buffering=1)
log = os.environ.get("FAKE_SAMPLE_LOG")
def note(text):
    if log:
        with open(log, "a") as f:
            f.write(text + "\n")
note(f"start {{os.getpid()}}")
for line in sys.stdin:
    timed, path = line.rstrip("\n").split(" ", 1)
    note(f"exec {{os.getpid()}} {{timed}} {{path}}")
    us = 0.0
    for token in os.path.basename(os.path.dirname(path)).split("_"):
        kind, _, value = token.partition("-")
        if kind == "sleep":
            time.sleep(float(value))
        elif kind == "hang":
            time.sleep(120)
        elif kind == "fail":
            print("injected failure", file=sys.stderr)
            out.write("3 0\n")
            sys.exit(3)
        elif kind == "us":
            us = float(value)
    out.write(f"0 {{us + (100 if os.path.basename(path).startswith('warm') else 0)}}\n")
'''

# Stands in for reference_cases.py in prepare(): FAKE_BEHAVIORS lists one behaviour per case.
FAKE_ENGINE = r'''
import json, os, sys
from pathlib import Path
index, case = int(sys.argv[1]), Path(sys.argv[2])
base = case / os.environ["FAKE_BEHAVIORS"].split(",")[index]
base.mkdir()
(base / "case.desc").write_text("desc")
(base / "input").mkdir()
(case / "parts.json").write_text(json.dumps([{"path": str(base), "tolerances": {}}]))
'''


def wait_until(predicate, timeout=10.0, step=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("condition not reached")


def hello(path):
    return device_broker._exchange(path, {"op": "hello"})


def stop_broker(path):
    info = hello(path)
    if not info:
        return
    device_broker._exchange(path, {"op": "drain"})
    try:
        wait_until(lambda: hello(path) is None, timeout=10)
    except AssertionError:
        os.kill(info["pid"], signal.SIGKILL)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "broker").mkdir()
    # Unix socket paths are limited to ~104 bytes; reach the tmp_path directory through a short link.
    short = Path(tempfile.mkdtemp(prefix="pb-", dir="/tmp"))
    link = short / "b"
    link.symlink_to(tmp_path / "broker")
    sample = tmp_path / "sample"
    sample.write_text(FAKE_SAMPLE.format(python=sys.executable))
    sample.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OPSB_BROKER_DIR", str(link))
    monkeypatch.setenv("OPSB_TICKET_DIR", str(tmp_path / "tickets"))
    monkeypatch.setenv("OPSB_BROKER_IDLE", "120")
    monkeypatch.setenv("FAKE_SAMPLE_LOG", str(tmp_path / "sample.log"))
    ns = SimpleNamespace(tmp=tmp_path, home=home, broker=link, sock=link / "broker.sock",
                         tickets=tmp_path / "tickets", lock=tmp_path / "device.lock", sample=sample,
                         log=tmp_path / "sample.log")
    yield ns
    stop_broker(ns.sock)
    shutil.rmtree(short, ignore_errors=True)


def base_cfg(env, **extra):
    return dict(lock=str(env.lock), device="0", timeout=5, title="Demo", sha="abc1234", run_id="r" * 32,
                **extra)


def start(env, source=SOURCE):
    path = device_broker.ensure_broker(base_cfg(env), source)
    assert path == str(env.sock)
    return path


def make_case(env, index, behavior, warmup=0, repeats=1, timed=True, parts=1):
    base = env.tmp / "cases" / f"{index:02d}"
    return dict(index=index, id=f"{index:02d}", timed=timed, warmup=warmup,
                parts=[dict(warm=[str(base / f"p{p}" / behavior / f"warm-{k}") for k in range(warmup)],
                            runs=[str(base / f"p{p}" / behavior / f"run-{r}") for r in range(repeats)])
                       for p in range(parts)])


def make_job(env, run_id, cases, **extra):
    job = dict(run_id=run_id, title="Demo", sha="abc1234", client_pid=os.getpid(), tier=1, prio=1.0,
               created=time.time(), executable=str(env.sample), device="0", reuse_session=True,
               case_timeout=5, job_timeout=60, cases=cases)
    job.update(extra)
    return job


class Client:
    def __init__(self, path, job):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.sock.sendall((json.dumps({"op": "submit", "job": job}) + "\n").encode())
        self.events, self.cv = [], threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            with self.sock.makefile("rb") as reader:
                for line in reader:
                    with self.cv:
                        self.events.append(json.loads(line))
                        self.cv.notify_all()
        except (OSError, ValueError):
            pass

    def wait_for(self, kind, timeout=10.0):
        with self.cv:
            assert self.cv.wait_for(lambda: any(e["event"] == kind for e in self.events), timeout), self.events
            return [e for e in self.events if e["event"] == kind]

    def of(self, kind):
        with self.cv:
            return [e for e in self.events if e["event"] == kind]

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def ledger(env):
    path = env.home / ".opsb/die_ledger.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_single_job_reports_warm_and_hot_samples(env):
    client = Client(start(env), make_job(env, "job-a", [make_case(env, 1, "us-5", warmup=2, repeats=3, parts=2)]))
    client.wait_for("done")
    kinds = [e["event"] for e in client.events if e["event"] != "progress"]
    assert kinds == ["queued", "started", "case", "released", "done"]
    (case,) = client.of("case")
    assert case["status"] == "PASS"
    assert case["samples"] == [10.0, 10.0, 10.0]  # two parts of 5 µs, one per repeat
    assert case["cold"] == [210.0, 210.0]  # warm samples are summed the same way
    phases = [(e["part"], e["phase"], e["repeat"]) for e in client.of("progress")]
    assert phases == [(1, "warm", 1), (1, "warm", 2), (1, "run", 1), (1, "run", 2), (1, "run", 3),
                      (2, "warm", 1), (2, "warm", 2), (2, "run", 1), (2, "run", 2), (2, "run", 3)]
    assert client.of("done")[0]["segments"] == 1
    assert client.of("queued")[0]["position"] == 1


def test_queue_order_follows_tier_then_priority(env):
    path = start(env)
    with env.lock.open("a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        clients = {}
        for name, tier, prio in (("low", 1, 1.0), ("fast", 2, 1.0), ("heavy", 1, 3.0)):
            clients[name] = Client(path, make_job(env, name, [make_case(env, 1, "us-1")], tier=tier, prio=prio))
            clients[name].wait_for("queued")
        wait_until(lambda: hello(path)["queued"] == 3)
        ticket = json.loads((env.tickets / f"{os.getpid()}.json").read_text())
        assert {"pid", "title", "sha", "run_id", "fast", "tier", "prio", "created", "case_progress"} <= set(ticket)
        wait_until(lambda: clients["fast"].of("queued")[-1]["position"] == 1)
        assert clients["low"].of("queued")[-1] == {"event": "queued", "position": 3, "ahead": ["fast", "heavy"]}
        fcntl.flock(holder, fcntl.LOCK_UN)
    for client in clients.values():
        client.wait_for("done")
    assert [row["run_id"] for row in ledger(env)] == ["fast", "heavy", "low"]


def test_case_timeout_releases_lock_and_requeues_the_rest(env):
    cases = [make_case(env, 1, "us-1"), make_case(env, 2, "hang"), make_case(env, 3, "us-2")]
    client = Client(start(env), make_job(env, "tle", cases, case_timeout=1))
    done = client.wait_for("done")[0]
    assert done["segments"] == 2
    assert [(c["id"], c["status"]) for c in client.of("case")] == [("01", "PASS"), ("02", "TLE"), ("03", "PASS")]
    assert [(r["segment"], r["requeued"]) for r in client.of("released")] == [(1, 1), (2, 0)]
    assert [s["segment"] for s in client.of("started")] == [1, 2]
    assert "1s case budget" in client.of("case")[1]["detail"]
    assert [row["segment"] for row in ledger(env)] == [1, 2]


def test_runtime_error_marks_remaining_cases_not_run(env):
    cases = [make_case(env, 1, "us-1"), make_case(env, 2, "fail"), make_case(env, 3, "us-1")]
    client = Client(start(env), make_job(env, "re", cases))
    assert client.wait_for("done")[0]["segments"] == 1
    statuses = [(c["status"], c["detail"]) for c in client.of("case")]
    assert statuses[0] == ("PASS", "")
    assert statuses[1][0] == "RE" and "injected failure" in statuses[1][1]
    assert statuses[2] == ("NOT_RUN", "Earlier device execution failed")


def test_job_budget_turns_remaining_cases_into_tle(env):
    cases = [make_case(env, 1, "sleep-0.6_us-1"), make_case(env, 2, "sleep-2_us-1"), make_case(env, 3, "us-1")]
    client = Client(start(env), make_job(env, "budget", cases, job_timeout=1))
    assert client.wait_for("done")[0]["segments"] == 1
    results = client.of("case")
    assert [c["status"] for c in results] == ["PASS", "TLE", "TLE"]
    assert all("1s device budget" in c["detail"] for c in results[1:])


def test_disconnect_removes_queued_job(env):
    path = start(env)
    with env.lock.open("a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        client = Client(path, make_job(env, "gone", [make_case(env, 1, "us-1")]))
        client.wait_for("queued")
        ticket = env.tickets / f"{os.getpid()}.json"
        assert ticket.exists()
        client.close()
        wait_until(lambda: hello(path)["queued"] == 0)
        assert not ticket.exists()
        fcntl.flock(holder, fcntl.LOCK_UN)
    assert ledger(env) == []


def test_disconnect_kills_running_sample_and_releases_lock(env):
    client = Client(start(env), make_job(env, "killed", [make_case(env, 1, "hang")], case_timeout=60))
    client.wait_for("progress")
    pid = int(wait_until(lambda: env.log.exists() and env.log.read_text().split()[1]))
    client.close()

    def lock_free():
        with env.lock.open("a") as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            return True

    wait_until(lock_free)

    def sample_gone():
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False

    wait_until(sample_gone)
    wait_until(lambda: [row["run_id"] for row in ledger(env)] == ["killed"])
    assert not (env.home / ".opsb/die_session.json").exists()


def test_broker_with_other_version_is_drained_and_replaced(env):
    path = start(env)
    old = hello(path)
    client = Client(path, make_job(env, "inflight", [make_case(env, 1, "sleep-1_us-1")]))
    client.wait_for("started")
    variant = SOURCE + "\n# variant\n"
    assert device_broker.ensure_broker(base_cfg(env), variant) == path
    assert client.wait_for("done")[0]["segments"] == 1  # the accepted job finished first
    new = hello(path)
    assert new["version"] == device_broker.source_version(variant) != old["version"]
    assert new["pid"] != old["pid"]
    assert (env.broker / f"broker-{new['version']}.py").read_text() == variant


def test_unwritable_broker_dir_falls_back_to_device_lane(env, monkeypatch):
    readonly = env.tmp / "readonly"
    readonly.mkdir()
    readonly.chmod(0o500)
    monkeypatch.setenv("OPSB_BROKER_DIR", str(readonly / "broker"))
    try:
        assert device_broker.ensure_broker(base_cfg(env), SOURCE) is None
        repo, plans = prepared(env, monkeypatch, "us-4,us-0")
        result = case_batch.execute_batch(repo, base_cfg(env, repeats=3, warmup=2), plans)
    finally:
        readonly.chmod(0o700)
    assert result["broker"] is False and result["segments"] == result["acquisitions"] == 1
    assert plans[0]["samples"] == [[4.0, 4.0, 4.0]] and plans[0]["cold"] == [[104.0, 104.0]]
    assert plans[1]["samples"] == [[0.0]] and plans[1]["cold"] == [[]]
    assert ledger(env)[0].get("broker") is None  # written by the 3.6.2 lane


def prepared(env, monkeypatch, behaviors, selected=None):
    repo = env.tmp / "repo"
    (repo / "build-casebench").mkdir(parents=True, exist_ok=True)
    shutil.copy(env.sample, repo / "build-casebench/sample")
    (env.tmp / "reference_cases.py").write_text(FAKE_ENGINE)
    monkeypatch.setenv("FAKE_BEHAVIORS", behaviors)
    root = env.tmp / "work"
    root.mkdir(exist_ok=True)
    selected = selected or [{"id": "01", "timed": True}, {"id": "02", "timed": False, "perf": True}]
    cfg = base_cfg(env, selected=selected, repeats=3, warmup=2, timing="perf")
    return repo, case_batch.prepare(repo, cfg, root, lambda progress: None)


def test_prepare_builds_warm_and_run_directories(env, monkeypatch):
    _, plans = prepared(env, monkeypatch, "us-1,us-2,us-3",
                        [{"id": "01", "timed": True}, {"id": "02", "timed": False, "perf": True}, {"id": "03", "perf": True}])
    assert [p["timed"] for p in plans] == [True, False, True]  # explicit flag wins, else timing/perf
    part = plans[0]["parts"][0]
    assert [p.name for p in part["warm"]] == ["warm-0", "warm-1"]
    assert [p.name for p in part["runs"]] == ["run-0", "run-1", "run-2"]
    for path in part["warm"] + part["runs"]:
        assert (path / "case.desc").is_symlink() and (path / "input").is_symlink() and (path / "output").is_dir()
    assert plans[1]["parts"][0]["warm"] == [] and len(plans[1]["parts"][0]["runs"]) == 1


def test_execute_batch_through_broker_and_verify_cold_samples(env, monkeypatch):
    repo, plans = prepared(env, monkeypatch, "us-4,us-0")
    progress = []
    result = case_batch.execute_batch(repo, base_cfg(env, repeats=3, warmup=2), plans, progress.append)
    assert result["broker"] is True and result["segments"] == 1
    assert plans[0]["samples"] == [[4.0, 4.0, 4.0]] and plans[0]["cold"] == [[104.0, 104.0]]
    assert {p.get("stage") for p in progress if p.get("phase") == "execute"} == {"warm", "run"}
    assert ledger(env)[0]["broker"] is True
    monkeypatch.setitem(sys.modules, "numpy", types.ModuleType("numpy"))  # no tolerances: numpy unused
    checked = case_batch.verify(plans[0], {}, None)
    assert checked["device_us"] == [4.0, 4.0, 4.0] and checked["cold_us"] == [104.0, 104.0]
    assert case_batch.verify(plans[1], {}, None)["cold_us"] == []


def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_broker_start_reaps_dead_tickets(env):
    env.tickets.mkdir()
    dead, live = env.tickets / "dead.json", env.tickets / "live.json"
    dead.write_text(json.dumps({"pid": dead_pid(), "tier": 1, "prio": 1.0, "created": 1.0}))
    live.write_text(json.dumps({"pid": os.getpid(), "tier": 1, "prio": 1.0, "created": 1.0}))
    start(env)
    assert not dead.exists() and live.exists()


@pytest.mark.parametrize("reap", [lanes.reap_dead_tickets, device_broker.reap_dead_tickets])
def test_reap_dead_tickets_counts_removed(tmp_path, reap):
    (tmp_path / "a.json").write_text(json.dumps({"pid": dead_pid()}))
    (tmp_path / "b.json").write_text(json.dumps({"pid": os.getpid()}))
    (tmp_path / "c.json").write_text("{not json")
    assert reap(tmp_path) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["b.json", "c.json"]


def test_device_lane_honours_ticket_dir_override(env, monkeypatch):
    env.tickets.mkdir()
    (env.tickets / "stale.json").write_text(json.dumps({"pid": dead_pid(), "tier": 9, "prio": 9.0, "created": 0}))
    with lanes.device_lane(base_cfg(env)):
        assert not (env.tickets / "stale.json").exists()
    assert ledger(env)[0]["run_id"] == "r" * 32


def test_die_session_is_written_while_holding_the_lock(env):
    client = Client(start(env), make_job(env, "session", [make_case(env, 1, "sleep-1_us-3")]))
    client.wait_for("progress")
    session_path = env.home / ".opsb/die_session.json"
    session = json.loads(wait_until(lambda: session_path.exists() and session_path.read_text()))
    assert {"pid": os.getpid(), "title": "Demo", "sha": "abc1234", "run_id": "session", "die": 0, "mode": "Q1",
            "substage": "npu_exec"}.items() <= session.items()
    assert session["started_epoch"] > 0 and session["case_progress"]["id"] == "01"
    client.wait_for("done")
    assert not session_path.exists()
    (row,) = ledger(env)
    assert row["run_id"] == "session" and row["broker"] is True
    assert row["hold_sec"] >= 1.0 and row["ended_epoch"] >= row["started_epoch"]


def test_job_priority_ages_and_starved_jobs_become_fast(env):
    now = time.time()
    fresh = device_broker.Job(make_job(env, "fresh", [], created=now, prio=1.0, aging_tau=600), None)
    starved = device_broker.Job(make_job(env, "starved", [], created=now - 1900, prio=1.0, aging_tau=600), None)
    assert fresh.key(now) == (1, 1.0, -now)
    tier, prio, _ = starved.key(now)
    assert tier == 2 and prio == pytest.approx(1.0 + 1900 / 600)
