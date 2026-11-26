"""A/B paired runs on the client side: both commits ship in one archive, the oracle must match,
and the report carries per-case ratios with intervals."""
import io
import json
import subprocess
import sys
import tarfile

import pytest

from scheduler import case_bench as bench


def _commit(repo, message):
    subprocess.run(["git", "-C", repo, "add", "."], check=True)
    subprocess.run(["git", "-C", repo, "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", message], check=True)
    return bench.committed(repo, "HEAD")


@pytest.fixture
def two_commits(tmp_path, monkeypatch):
    monkeypatch.setenv("PARHELION_HOME", str(tmp_path / "home"))
    repo = tmp_path / "Op"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", repo], check=True)
    (repo / "reference.py").write_text("CASES = [{'id':'01','seed':1,'params':{'n':2}}]\nPERF_CASES={'01'}")
    (repo / "sample.py").write_text("fixture")
    (repo / "main.asc").write_text("harness")
    (repo / "kernel.asc").write_text("kernel v1")
    base = _commit(repo, "baseline")
    (repo / "kernel.asc").write_text("kernel v2")
    cand = _commit(repo, "candidate")
    return tmp_path, repo, cand, base


def test_archive_carries_both_commits_and_bootstrap_routes_them(two_commits):
    tmp, repo, cand, base = two_commits
    payload = io.BytesIO()
    bench.write_archive(payload, repo, cand, base)
    payload.seek(0)
    with tarfile.open(fileobj=payload, mode="r:gz") as archive:
        names = set(archive.getnames())
    assert {"cand/kernel.asc", "base/kernel.asc", "cand/reference.py"} <= names
    # The bootstrap extracts cand/ into the run's repo and base/ next to it, then runs the engine.
    engine = ("import pathlib; root = pathlib.Path.cwd().parent; "
              "print((root/'repo'/'kernel.asc').read_text(), (root/'base'/'kernel.asc').read_text())")
    stream = io.BytesIO()
    stream.write((json.dumps({"engine": engine, "lanes": "", "case_batch": "", "case_cache": "",
                              "compare": base}) + "\n").encode())
    payload.seek(0)
    stream.write(payload.read())
    out = subprocess.run([sys.executable, "-c", bench.BOOTSTRAP], input=stream.getvalue(), capture_output=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.decode().split() == ["kernel", "v2", "kernel", "v1"]


def test_paired_dry_run_needs_another_commit_and_the_same_oracle(two_commits):
    tmp, repo, cand, base = two_commits
    report = bench.run(tmp, "Op", repo, cand, "host", compare=base, dry_run=True)
    assert report["compare"] == {"base_sha": base, "rounds": report["settings"]["repeats"]}
    assert report["formal"] is False and report["experiment"] == f"paired:{base[:7]}"
    assert report["settings"]["compare"] == base and report["settings"]["profiler"] is None
    with pytest.raises(ValueError, match="other than the candidate"):
        bench.run(tmp, "Op", repo, cand, "host", compare=cand, dry_run=True)
    (repo / "reference.py").write_text("CASES = [{'id':'01','seed':7,'params':{'n':2}}]\nPERF_CASES={'01'}")
    changed = _commit(repo, "oracle edit")
    with pytest.raises(ValueError, match="same frozen oracle"):
        bench.run(tmp, "Op", repo, changed, "host", compare=base, dry_run=True)


def test_pair_statistics_use_one_timer_per_case(tmp_path):
    report = {"title": "Op", "selected": ["01", "02", "03"], "compare": {"base_sha": "b" * 40}}
    rows = {
        "01": {"id": "01", "source": "official",
               "pairs": {"event": [[20.0, 30.0], [25.0, 26.0]], "profiler": [[4.0, 8.0], [4.1, 8.2]]}},
        "02": {"id": "02", "source": "official", "pairs": {"event": [[10.0, 12.0], [10.2, 12.1], [9.9, 11.8]]}},
        "03": {"id": "03", "source": "official"},
    }
    bench.pair_statistics(tmp_path, report, rows)
    assert rows["01"]["pair"]["source"] == "profiler" and rows["01"]["pair"]["ratio"] == 2.0
    assert rows["02"]["pair"]["source"] == "event" and rows["02"]["pair"]["n"] == 3
    assert "pair" not in rows["03"]
    assert report["compare"]["source"] == "mixed"
    assert report["compare"]["geomean_ratio"] == pytest.approx((2.0 * rows["02"]["pair"]["ratio"]) ** 0.5, abs=1e-3)
