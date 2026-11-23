"""Regression tests for Parhelion v3 Wave C fixes (V3-001, V3-003..008, V3-014, V3-017, V3-018)."""
from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "cli") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "cli"))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from opsb_core.cli_app import build_parser
from opsb_core.daemon.server import OpsbDaemonRuntime, _bind_server, _make_handler
from opsb_core.fast_feedback import build_pmu_torch_card, evaluate_cold3_probe, evaluate_elf_guard
from opsb_core.knowledge import ensure_triad_kb_seeded, get_kb_entry, query_kb
from opsb_core.probe import npu_stream_probe as sp


def _load_npu_test_mod():
    spec = importlib.util.spec_from_file_location("opsb_npu_test_mod", str(REPO_ROOT / "runtime" / "npu-test.py"))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_ws(tmp_path: Path, title: str = "LayerNormV3") -> tuple[Path, Path]:
    ws = tmp_path / "ws"
    (ws / "bench" / title / "op_kernel").mkdir(parents=True)
    (ws / "bench" / title / "op_host").mkdir(parents=True)
    (ws / "results" / title).mkdir(parents=True)
    probs = [{"title": title, "slug": title.lower(), "path": f"bench/{title}", "difficulty": 3}]
    (ws / "bench" / "problems.json").write_text(json.dumps(probs), encoding="utf-8")
    return ws, ws / "bench" / title


def _write_run(ws: Path, title: str, sha: str, rj: dict, log_text: str) -> Path:
    d = ws / "results" / title / sha
    d.mkdir(parents=True, exist_ok=True)
    (d / "result.json").write_text(json.dumps(rj), encoding="utf-8")
    (d / "validation.log").write_text(log_text, encoding="utf-8")
    return d


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "parhelion_home"
    h.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PARHELION_HOME", str(h))
    monkeypatch.setenv("OPSB_HOME", str(h))
    return h


def test_v3_001_all_zero_pmu_treated_as_unavailable_and_pmu_flags(tmp_path: Path, isolated_home: Path) -> None:
    ws, _ = _make_ws(tmp_path, "LayerNormV3")
    sha = "1" * 40
    log = (
        "[PHASE-C] Lock-free verify + profiler parse finished in 2.10s (Phase-B Die-0 hold was 8.2s)\n"
        'KPERF_JSON [{"mode": "custom", "case": "b1", "median_us": 18.0, "torch_us": 9.0, '
        '"pmu": {"aiv_vec_ratio": 0.0, "aiv_mte2_ratio": 0.0, "aiv_mte3_ratio": 0.0, "aiv_scalar_ratio": 0.0}}]\n'
    )
    _write_run(ws, "LayerNormV3", sha, {"title": "LayerNormV3", "sha": sha, "exit_code": 0, "profiler_regime": "fast_single_session_l0"}, log)
    card = build_pmu_torch_card(ws, "LayerNormV3", sha=sha)
    assert card["pmu_available"] is False
    assert card["pmu"]["pmu_available"] is False
    assert card["pmu"]["pmu_mode"] == "l0_timing_only"
    assert card["dominant_bottleneck"] != "balanced"
    assert "--pmu" in card["markdown_card"]

    parser = build_parser()
    ns = parser.parse_args(["test", "LayerNormV3", "--pmu"])
    assert ns.pmu is True
    nt = _load_npu_test_mod()
    ast.parse(nt.REMOTE_3PHASE)


def test_v3_006_missing_torch_never_outranks_measured_pos_corr(tmp_path: Path, isolated_home: Path) -> None:
    ws, _ = _make_ws(tmp_path, "ConcatD")
    sha = "2" * 40
    (ws / "results" / "ConcatD" / "calibration.json").write_text(
        json.dumps({
            "buckets": [
                {"id": "missing_torch_hiw", "stratum": "M_MID_UB", "learned_w": 0.45, "empirical_corr": 0.85},
                {"id": "measured_pos", "stratum": "S_SMALL_CORE", "learned_w": 0.12, "empirical_corr": 0.91},
                {"id": "measured_anti", "stratum": "L_HBM_TILED", "learned_w": 0.40, "empirical_corr": -0.65},
                {"id": "missing_nocorr", "stratum": "X_EXTREME", "learned_w": 0.35, "empirical_corr": None},
            ]
        }),
        encoding="utf-8",
    )
    log = (
        'KPERF_JSON ['
        '{"mode": "custom", "case": "missing_torch_hiw", "median_us": 20.0},'
        '{"mode": "custom", "case": "measured_pos", "median_us": 12.0, "torch_us": 10.0},'
        '{"mode": "custom", "case": "measured_anti", "median_us": 40.0, "torch_us": 10.0},'
        '{"mode": "custom", "case": "missing_nocorr", "median_us": 30.0}'
        ']\n'
    )
    _write_run(ws, "ConcatD", sha, {"title": "ConcatD", "sha": sha, "exit_code": 0}, log)
    card = build_pmu_torch_card(ws, "ConcatD", sha=sha, top_n=4)
    order = [b["bucket"] for b in card["buckets"]]
    by_id = {b["bucket"]: b for b in card["buckets"]}
    assert order[0] == "measured_pos"
    assert by_id["missing_torch_hiw"]["headroom_score"] == pytest.approx(0.05 * 0.45)
    assert by_id["missing_nocorr"]["headroom_score"] == 0.0
    assert by_id["measured_anti"]["headroom_score"] == 0.0


def test_v3_003_daemon_suppresses_abrupt_client_disconnect(tmp_path: Path) -> None:
    ws, _ = _make_ws(tmp_path, "PadV3Grad")
    runtime = OpsbDaemonRuntime(workspace=ws, ssh_host="npu", port=0, token="tok")
    handler_cls = _make_handler(runtime, threading.Event())
    server, port = _bind_server(0, handler_cls)
    srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
    srv_thread.start()
    err_buf = io.StringIO()
    try:
        with mock.patch("sys.stderr", err_buf):
            sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
            sock.sendall(b"GET /v1/stream/hot HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Opsb-Token: tok\r\n\r\n")
            sock.recv(128)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
            sock.close()
            # Also drop before sending full HTTP request
            s2 = socket.create_connection(("127.0.0.1", port), timeout=2.0)
            s2.close()
            time.sleep(0.15)
    finally:
        server.shutdown()
        server.server_close()
        runtime.stream_mgr.stop()
    assert "Traceback" not in err_buf.getvalue()
    assert "ConnectionResetError" not in err_buf.getvalue()
    assert "BrokenPipeError" not in err_buf.getvalue()


def test_v3_004_git_head_preference_kernelc_macros_and_symbol_icache(tmp_path: Path, isolated_home: Path) -> None:
    ws, repo = _make_ws(tmp_path, "StridedSliceGrad")
    subprocess.run(["git", "-C", str(repo), "init"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    kfile = repo / "op_kernel" / "strided_slice_grad.cpp"
    kfile.write_text(
        "TPL_UINT_DECL(mode, 1, 2);\nTPL_BOOL_DECL(isFast);\n"
        "TPL_SEL(0, 1);\nTPL_SEL(1, 0);\n__attribute__((noinline)) void ColdPath() {}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "c1"], check=True, stdout=subprocess.DEVNULL)
    sha_head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    sha_other = "f" * 40

    _write_run(
        ws, "StridedSliceGrad", sha_head,
        {"title": "StridedSliceGrad", "sha": sha_head, "exit_code": 0, "kernel_o_max_text_bytes": 40960,
         "max_hot_symbol_bytes": 14336,
         "top_symbols": [{"symbol": "HotMode", "bytes": 14336, "file": "k.o"}, {"symbol": "ColdPath", "bytes": 12000, "file": "k.o"}]},
        "EXTENDED_PASSED\n",
    )
    time.sleep(0.02)
    _write_run(ws, "StridedSliceGrad", sha_other, {"title": "StridedSliceGrad", "sha": sha_other, "exit_code": 0, "kernel_o_max_text_bytes": 8000}, "EXTENDED_PASSED\n")

    res = evaluate_elf_guard(ws, "StridedSliceGrad")
    assert res["sha"] == sha_head
    assert res["sha_source"] == "git_head"
    assert res["metrics"]["static_scan"]["kernelc_tpl_macros"] >= 4
    assert res["metrics"]["template_instantiation_count"] >= 4
    ic_alert = next(a for a in res["alerts"] if a["code"] == "ICACHE_TEXT_BLOAT")
    assert ic_alert["severity"] == "warn"
    assert ic_alert["attribution"] == "multi_mode_or_noinline_split"


def test_v3_005_cold3_synthetic_spread_and_block_dim_attribution(tmp_path: Path, isolated_home: Path) -> None:
    ws, _ = _make_ws(tmp_path, "PReluGradUpdate")
    sha_l0 = "a" * 40
    _write_run(
        ws, "PReluGradUpdate", sha_l0, {"title": "PReluGradUpdate", "sha": sha_l0, "exit_code": 0},
        'KPERF_JSON [{"mode": "custom", "case": "c40", "block_dim": 40, "median_us": 10.0, "min_us": 9.8, "max_us": 25.0}]\n',
    )
    res_l0 = evaluate_cold3_probe(ws, "PReluGradUpdate", sha=sha_l0)
    assert res_l0["bimodal_spike_detected"] is False
    assert res_l0["synthetic_order"] is True
    assert res_l0["status"] == "warn"
    assert [a["code"] for a in res_l0["alerts"]] == ["SPREAD_JITTER_WARN"]
    assert res_l0["alerts"][0]["synthetic_order"] is True

    sha_c40 = "b" * 40
    _write_run(
        ws, "PReluGradUpdate", sha_c40, {"title": "PReluGradUpdate", "sha": sha_c40, "exit_code": 0},
        'KPERF_JSON [{"mode": "custom", "case": "c40", "block_dim": 40, "cold3_runs": [24.0, 10.0, 9.8]}]\n',
    )
    res_c40 = evaluate_cold3_probe(ws, "PReluGradUpdate", sha=sha_c40)
    assert res_c40["bimodal_spike_detected"] is True
    dvfs = next(a for a in res_c40["alerts"] if a["code"] == "DVFS_BIMODAL_SPIKE")
    assert "block_dim=40" in dvfs["message"]
    assert "10-core DVFS" not in dvfs["message"]
    assert "dvfs-10core-spike" not in dvfs["suggested_kb_tags"]

    sha_c10 = "c" * 40
    _write_run(
        ws, "PReluGradUpdate", sha_c10, {"title": "PReluGradUpdate", "sha": sha_c10, "exit_code": 0},
        'KPERF_JSON [{"mode": "custom", "case": "c10", "block_dim": 10, "cold3_runs": [18.0, 9.5, 9.0]}]\n',
    )
    res_c10 = evaluate_cold3_probe(ws, "PReluGradUpdate", sha=sha_c10)
    dvfs10 = next(a for a in res_c10["alerts"] if a["code"] == "DVFS_BIMODAL_SPIKE")
    assert "10-core DVFS" in dvfs10["message"]
    assert "dvfs-10core-spike" in dvfs10["suggested_kb_tags"]


def test_v3_007_build_signature_detects_branch_and_header_changes(tmp_path: Path) -> None:
    nt = _load_npu_test_mod()
    repo = tmp_path / "op_repo"
    (repo / "op_kernel").mkdir(parents=True)
    (repo / "op_kernel" / "common.h").write_text("// h", encoding="utf-8")

    sig1, r1 = nt.evaluate_build_signature(repo, "main", "soc-a", "1111111", prev_sig=None, is_ancestor=True)
    assert r1 == []
    _, r_inc = nt.evaluate_build_signature(repo, "main", "soc-a", "2222222", prev_sig=sig1, is_ancestor=True)
    assert r_inc == []
    _, r_br = nt.evaluate_build_signature(repo, "feat/exp", "soc-a", "2222222", prev_sig=sig1, is_ancestor=True)
    assert any(x.startswith("branch:main->feat/exp") for x in r_br)
    _, r_non = nt.evaluate_build_signature(repo, "main", "soc-a", "3333333", prev_sig=sig1, is_ancestor=False)
    assert any(x.startswith("non_ancestor:") for x in r_non)

    (repo / "op_kernel" / "extra.h").write_text("// new", encoding="utf-8")
    _, r_hdr = nt.evaluate_build_signature(repo, "main", "soc-a", "2222222", prev_sig=sig1, is_ancestor=True)
    assert "op_kernel_headers_changed" in r_hdr


def test_v3_008_and_v3_017_kb_entries(isolated_home: Path) -> None:
    ensure_triad_kb_seeded(parhelion_home=isolated_home)
    snp = get_kb_entry("SNP-direct-gm-tiling-64b", parhelion_home=isolated_home)
    assert "ACL 507035" in snp["content"]
    assert "GetUserWorkspace" in snp["content"]
    assert "16 MB" in snp["content"]

    q = query_kb(bottlenecks=["cold-launch-tax"], regime="S_SMALL_CORE", parhelion_home=isolated_home)
    anti_ids = [a["id"] for a in q["anti_patterns"]]
    assert "HW-dummy-halfcores-launch-illusion" in anti_ids
    assert get_kb_entry("COM-dummy-halfcores-launch-illusion", parhelion_home=isolated_home)["id"] == "HW-dummy-halfcores-launch-illusion"
    com = get_kb_entry("COM-dummy-halfcores-launch-illusion", parhelion_home=isolated_home)
    assert "-11.83" in com["content"]
    assert "(maxCores + 1) / 2" in com["content"] or "(maxCores+1)/2" in com["summary"]


def test_v3_014_stream_probe_deduplicates_pid_trees_and_lanes(tmp_path: Path) -> None:
    fake_smi = {
        "available": True, "smi_version": "25.5.0",
        "devices": [{"npu_id": 0, "chip_id": 0, "soc_name": "soc-a", "health": "OK", "bus_id": "0",
                     "power_w": 95.0, "temp_c": 36, "aicore_pct": 10, "aivector_pct": 60, "hbm_bw_pct": 5, "ctrlcpu_pct": 2,
                     "hbm_used_mib": 1000, "hbm_total_mib": 65536}],
        "npu_processes": [], "error": None,
    }
    sha_hex = "c" * 40
    # PID 100 = driver wrapper holding validation.lock + sibling 101 holding device0.lock + 102 child ccec
    procs = {
        100: {"pid": 100, "ppid": 1, "elapsed_sec": 30, "cmd": "python3 -c bench-repo-PadV3Grad"},
        101: {"pid": 101, "ppid": 100, "elapsed_sec": 12, "cmd": f"python3 extended.py PadV3Grad {sha_hex}"},
        102: {"pid": 102, "ppid": 100, "elapsed_sec": 10, "cmd": f"/usr/bin/ccec /srv/work/PadV3Grad {sha_hex}"},
        200: {"pid": 200, "ppid": 1, "elapsed_sec": 8, "cmd": f"bash /srv/work/PadV3Grad/build.sh /.backfill/ {sha_hex}"},
    }
    locks = [
        {"pid": 101, "command": "python3", "type": "FLOCK", "mode": "WRITE", "waiting": False, "path": "/srv/work-device0.lock"},
        {"pid": 100, "command": "python3", "type": "FLOCK", "mode": "READ", "waiting": False, "path": "/srv/work-validation.lock"},
    ]
    with (
        mock.patch.object(sp.DcmiProbe, "init", return_value=False),
        mock.patch.object(sp, "read_npu_smi_fallback", return_value=fake_smi),
        mock.patch.object(sp, "read_locks_procfs", return_value=locks),
        mock.patch.object(sp, "read_processes_procfs", return_value=procs),
        mock.patch.object(sp, "_parse_proc_stat", return_value={"cpu": (100, 200)}),
        mock.patch.object(sp, "read_memory", return_value={"total_mib": 1000, "used_mib": 200, "available_mib": 800, "total_gib": 1.0, "used_gib": 0.2, "cached_gib": 0.1}),
    ):
        sess = sp.StreamProbeSession(stream_mode=False)
        sess.opsb_home = tmp_path / ".opsb"
        (sess.opsb_home / "cpu_builds").mkdir(parents=True, exist_ok=True)
        (sess.opsb_home / "cpu_builds" / "PadV3Grad.json").write_text(
            json.dumps({"pid": 100, "title": "PadV3Grad", "sha": sha_hex, "started_epoch": time.time() - 10}),
            encoding="utf-8",
        )
        (sess.opsb_home / "die_session.json").write_text(
            json.dumps({"die": 0, "pid": 100, "title": "PadV3Grad", "sha": sha_hex, "mode": "Q1", "substage": "npu_exec"}),
            encoding="utf-8",
        )
        frame = sess.sample_frame()

    tasks = frame["tasks"]
    q1_pad = [t for t in tasks if t["problem"] == "PadV3Grad" and t["lane"] == "Q1"]
    b0_pad = [t for t in tasks if t["problem"] == "PadV3Grad" and t["lane"] == "B0"]
    assert len(q1_pad) == 1 and q1_pad[0]["state"] == "ACTIVE"
    assert len(b0_pad) == 1 and b0_pad[0]["state"] == "CPU_BUILD"


def test_v3_018_diagnose_accepts_sha(tmp_path: Path, isolated_home: Path) -> None:
    from scheduler.cli_bridge import SchedulerEngine, _build_parser

    ws, _ = _make_ws(tmp_path, "LayerNormV3")
    (ws / "results" / "submit-queue-outcome.jsonl").write_text("", encoding="utf-8")
    (ws / "results" / "score-snapshot.json").write_text(json.dumps({"problems": []}), encoding="utf-8")
    (ws / "bench" / "LayerNormV3" / "bench").mkdir(exist_ok=True)
    (ws / "bench" / "LayerNormV3" / "bench" / "cases_perf.json").write_text(
        json.dumps({"problem": "LayerNormV3", "public_anchors": [{"id": "c1", "stratum": "PUBLIC", "tau_torch_us": 5.0}], "hidden_buckets": []}),
        encoding="utf-8",
    )
    sha = "d" * 40
    _write_run(ws, "LayerNormV3", sha, {"title": "LayerNormV3", "sha": sha, "exit_code": 0, "profiler_regime": "fast_single_session_l0"}, 'KPERF_JSON [{"mode": "custom", "case": "c1", "median_us": 4.2}]\n')

    bridge_args = _build_parser().parse_args(["diagnose", "--title", "LayerNormV3", "--sha", sha[:7]])
    assert bridge_args.sha == sha[:7]
    cli_args = build_parser().parse_args(["bench", "diagnose", "LayerNormV3", "--sha", sha[:7], "--json"])
    assert cli_args.sha == sha[:7]

    eng = SchedulerEngine(workspace=ws, state_dir=tmp_path / "sched_state")
    doc = eng.diagnose(title="LayerNormV3", sha=sha[:7])
    assert doc["sha"] == sha
    assert doc["commit_validation"]["found"] is True
    assert doc["commit_validation"]["passed"] is True
    assert doc["commit_validation"]["measured_bucket_count"] == 1
