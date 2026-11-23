"""Unit and integration tests for Parhelion v3 Track 1 Fast-Feedback Tools."""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = CLI_DIR.parent
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.commands.tools_cmd import register_fast_tools_subcommands  # noqa: E402
from opsb_core.services.fast_feedback_service import (  # noqa: E402
    VALID_OPERATOR_FAMILIES,
    build_pmu_torch_card,
    evaluate_cold3_probe,
    evaluate_elf_guard,
    scaffold_bench_specs,
)


def _make_workspace(tmp_path: Path, title: str = "DemoOp") -> tuple[Path, Path]:
    ws = tmp_path / "workspace"
    repo = ws / "bench" / title
    (repo / "op_kernel").mkdir(parents=True, exist_ok=True)
    (repo / "op_host").mkdir(parents=True, exist_ok=True)
    (ws / "bench" / "problems.json").parent.mkdir(parents=True, exist_ok=True)
    (ws / "bench" / "problems.json").write_text(
        json.dumps([{"title": title, "slug": title.lower(), "difficulty": 3, "path": f"bench/{title}"}]),
        encoding="utf-8",
    )
    (ws / "results" / title).mkdir(parents=True, exist_ok=True)
    return ws, repo


def _write_run(
    ws: Path,
    title: str,
    sha: str,
    result_data: dict[str, Any] | None = None,
    log_text: str = "",
    result_filename: str = "result.json",
    log_filename: str = "validation.log",
) -> Path:
    rdir = ws / "results" / title / sha
    rdir.mkdir(parents=True, exist_ok=True)
    if result_data is not None:
        (rdir / result_filename).write_text(json.dumps(result_data, indent=2), encoding="utf-8")
    if log_text:
        (rdir / log_filename).write_text(log_text, encoding="utf-8")
    return rdir


@pytest.fixture()
def isolated_parhelion_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ph = tmp_path / "parhelion_home"
    ph.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PARHELION_HOME", str(ph))
    return ph


# ---------------------------------------------------------------------------
# 1. elf_guard tests
# ---------------------------------------------------------------------------

def test_elf_guard_ok_warn_redline_transitions(tmp_path: Path, isolated_parhelion_home: Path) -> None:
    ws, repo = _make_workspace(tmp_path, "ConcatD")
    sha_ok = "1111111" * 5 + "11111"
    sha_warn = "2222222" * 5 + "22222"
    sha_red = "3333333" * 5 + "33333"

    _write_run(
        ws,
        "ConcatD",
        sha_ok,
        result_data={
            "title": "ConcatD",
            "sha": sha_ok,
            "exit_code": 0,
            "phase_a_cpu_sec": 24.5,
            "kernel_o_count": 4,
            "kernel_o_max_bytes": 48000,
            "kernel_o_sum_bytes": 160000,
            "kernel_o_max_text_bytes": 16384,
            "template_instantiations": 12,
        },
        log_text='KERNEL_BINARY_STATS {"count": 4, "max_bytes": 48000, "max_text_bytes": 16384, "template_instantiations": 12}\n',
    )
    res_ok = evaluate_elf_guard(ws, "ConcatD", sha=sha_ok)
    assert res_ok["status"] == "ok"
    assert res_ok["alerts"] == []
    assert res_ok["metrics"]["template_instantiation_count"] == 12

    _write_run(
        ws,
        "ConcatD",
        sha_warn,
        result_data={
            "title": "ConcatD",
            "sha": sha_warn,
            "exit_code": 0,
            "phase_a_cpu_sec": 95.0,
            "kernel_o_count": 6,
            "kernel_o_max_bytes": 102400,
            "kernel_o_sum_bytes": 420000,
            "kernel_o_max_text_bytes": 26000,
            "template_instantiations": 50,
        },
        result_filename="result-build.json",
        log_filename="build.log",
        log_text="BUILD_ONLY_PASSED (95.0s CPU, 0.0s NPU lock)\n",
    )
    res_warn = evaluate_elf_guard(ws, "ConcatD", sha=sha_warn)
    assert res_warn["status"] == "warn"
    warn_codes = {a["code"] for a in res_warn["alerts"]}
    assert {"ICACHE_TEXT_WARN", "ELF_OBJECT_WARN", "COMPILE_TIME_WARN", "TEMPLATE_COUNT_WARN"} <= warn_codes
    assert all(a["severity"] == "warn" for a in res_warn["alerts"])

    _write_run(
        ws,
        "ConcatD",
        sha_red,
        result_data={
            "title": "ConcatD",
            "sha": sha_red,
            "exit_code": 0,
            "phase_a_cpu_sec": 165.4,
            "kernel_o_count": 12,
            "kernel_o_max_bytes": 180000,
            "kernel_o_sum_bytes": 1600000,
            "kernel_o_max_text_bytes": 40960,
            "template_instantiations": 84,
        },
    )
    res_red = evaluate_elf_guard(ws, "ConcatD", sha=sha_red)
    assert res_red["status"] == "redline"
    red_codes = {a["code"] for a in res_red["alerts"]}
    assert {"ICACHE_TEXT_BLOAT", "ELF_OBJECT_BLOAT", "COMPILE_TIME_TLE_RISK", "TEMPLATE_EXPLOSION"} <= red_codes
    icache_alert = next(a for a in res_red["alerts"] if a["code"] == "ICACHE_TEXT_BLOAT")
    assert icache_alert["severity"] == "redline"
    assert "noinline" in icache_alert["suggested_kb_tags"]


def test_elf_guard_static_op_kernel_scan_and_config_override(tmp_path: Path, isolated_parhelion_home: Path) -> None:
    ws, repo = _make_workspace(tmp_path, "PadV3Grad")
    kernel_src = "\n".join(f"if (TILING_KEY_IS({i})) {{ LaunchTile<{i}>(); }}" for i in range(1, 55))
    (repo / "op_kernel" / "pad_v3_grad.cpp").write_text(kernel_src, encoding="utf-8")

    sha = "a" * 40
    _write_run(
        ws,
        "PadV3Grad",
        sha,
        result_data={
            "title": "PadV3Grad",
            "sha": sha,
            "exit_code": 0,
            "phase_a_cpu_sec": 30.0,
            "kernel_o_count": 2,
            "kernel_o_max_bytes": 40000,
            "kernel_o_max_text_bytes": 12000,
        },
    )
    res = evaluate_elf_guard(ws, "PadV3Grad")
    assert res["metrics"]["template_source"] == "op_kernel_scan"
    assert res["metrics"]["template_instantiation_count"] == 54
    assert res["status"] == "warn"
    assert any(a["code"] == "TEMPLATE_COUNT_WARN" for a in res["alerts"])

    # Custom threshold via config.json -> fast_tools.elf_guard promotes 54 to redline
    (isolated_parhelion_home / "config.json").write_text(
        json.dumps({"fast_tools": {"elf_guard": {"warn_templates": 20, "fail_templates": 50}}}),
        encoding="utf-8",
    )
    res_cfg = evaluate_elf_guard(ws, "PadV3Grad")
    assert res_cfg["status"] == "redline"
    assert any(a["code"] == "TEMPLATE_EXPLOSION" for a in res_cfg["alerts"])


# ---------------------------------------------------------------------------
# 2. cold3_probe tests
# ---------------------------------------------------------------------------

def test_cold3_probe_detects_bimodal_dvfs_spike_vs_steady(tmp_path: Path, isolated_parhelion_home: Path) -> None:
    ws, _ = _make_workspace(tmp_path, "StridedSliceGrad")
    sha_steady = "b" * 40
    sha_spike = "c" * 40

    steady_log = (
        "RUNTIME_SOC=soc-a\n"
        'KPERF_JSON [{"mode": "custom", "case": "s_small", "cold3_runs": [4.2, 4.05, 4.0], "median_us": 4.05, "min_us": 4.0, "max_us": 4.2}, '
        '{"mode": "custom", "case": "m_mid", "cold3_runs": [8.4, 8.2, 8.1], "median_us": 8.2, "min_us": 8.1, "max_us": 8.4}]\n'
        'COLD_START_STATS {"cases": 2, "first_iter_us": 6.3, "cold_spread_median_us": 0.25}\n'
    )
    _write_run(ws, "StridedSliceGrad", sha_steady, result_data={"title": "StridedSliceGrad", "sha": sha_steady, "exit_code": 0}, log_text=steady_log)
    res_steady = evaluate_cold3_probe(ws, "StridedSliceGrad", sha=sha_steady)
    assert res_steady["status"] == "ok"
    assert res_steady["bimodal_spike_detected"] is False
    assert res_steady["cold_tax_us"] is not None and res_steady["cold_tax_us"] < 0.2
    assert res_steady["alerts"] == []

    spike_log = (
        "RUNTIME_SOC=soc-a\n"
        'COLD3_CASE {"case": "small_10core_dvfs", "runs": [9.6, 4.3, 4.0]}\n'
        'KPERF_JSON [{"mode": "custom", "case": "mid_steady", "cold3_runs": [6.5, 6.2, 6.1], "median_us": 6.2, "min_us": 6.1, "max_us": 6.5}]\n'
        'COLD3_STATS {"cases": 2, "first_iter_us": 8.05, "cold_3run_mean_us": 6.12, "warm_median_us": 5.1, "cold_spread_median_us": 3.0, "bimodal_spike_detected": true}\n'
    )
    _write_run(ws, "StridedSliceGrad", sha_spike, result_data={"title": "StridedSliceGrad", "sha": sha_spike, "exit_code": 0}, log_text=spike_log)
    res_spike = evaluate_cold3_probe(ws, "StridedSliceGrad", sha=sha_spike)
    assert res_spike["status"] == "redline"
    assert res_spike["bimodal_spike_detected"] is True
    assert res_spike["first_iter_us"] == pytest.approx(8.05, rel=1e-3)
    assert res_spike["cold_3run_mean_us"] == pytest.approx(6.12, rel=1e-3)
    assert res_spike["cold_tax_us"] == pytest.approx(1.02, rel=1e-3)
    dvfs_alert = next(a for a in res_spike["alerts"] if a["code"] == "DVFS_BIMODAL_SPIKE")
    assert "dvfs-10core-spike" in dvfs_alert["suggested_kb_tags"]


def test_cold3_probe_warn_on_moderate_cold_tax_without_spike(tmp_path: Path, isolated_parhelion_home: Path) -> None:
    ws, _ = _make_workspace(tmp_path, "LayerNormV3")
    sha = "d" * 40
    # run1/run3 = 6.9/5.0 = 1.38 < 1.45 (no bimodal spike), but 3-run tax = (6.9+5.1+5.0)/3 - 4.7 = +0.967 us -> warn
    log_txt = (
        'KPERF_JSON [{"mode": "custom", "case": "c_mid", "cold3_runs": [6.9, 5.1, 5.0], "median_us": 4.7, "min_us": 4.6, "max_us": 6.9}]\n'
    )
    _write_run(ws, "LayerNormV3", sha, result_data={"title": "LayerNormV3", "sha": sha, "exit_code": 0}, log_text=log_txt)
    res = evaluate_cold3_probe(ws, "LayerNormV3", sha=sha)
    assert res["bimodal_spike_detected"] is False
    assert res["status"] == "warn"
    assert any(a["code"] == "COLD_LAUNCH_TAX_WARN" for a in res["alerts"])


# ---------------------------------------------------------------------------
# 3. pmu_torch_card tests
# ---------------------------------------------------------------------------

def test_pmu_torch_card_parses_profiler_pmu_and_ranks_headroom(tmp_path: Path, isolated_parhelion_home: Path) -> None:
    ws, _ = _make_workspace(tmp_path, "LayerNormV3")
    sha = "e" * 40

    # Calibration weights & bench spec
    (ws / "results" / "LayerNormV3" / "calibration.json").write_text(
        json.dumps({
            "buckets": [
                {"id": "f32_64x8192", "stratum": "M_MID_UB", "learned_w": 0.30},
                {"id": "f16_4096x1024", "stratum": "L_HBM_TILED", "learned_w": 0.25},
                {"id": "f32_4x6x8", "stratum": "S_SMALL_CORE", "learned_w": 0.15},
            ]
        }),
        encoding="utf-8",
    )
    spec_dir = isolated_parhelion_home / "workspaces"
    # Also test reading `tau_torch_us` from scaffolded bench-specs
    scaffold_bench_specs(ws, "LayerNormV3", operator_family="reduction", apply=True)
    # Add explicit spec bucket matching `f16_4096x1024` with `tau_torch_us`
    perf_spec_path = next(spec_dir.glob("*/bench-specs/LayerNormV3/cases_perf.json"))
    spec_doc = json.loads(perf_spec_path.read_text(encoding="utf-8"))
    spec_doc["hidden_buckets"].append({"id": "f16_4096x1024", "stratum": "L_HBM_TILED", "tau_torch_us": 56.5})
    perf_spec_path.write_text(json.dumps(spec_doc, indent=2), encoding="utf-8")

    real_style_log = "\n".join([
        "RUNTIME_SOC=soc-a",
        "EXTENDED_PASSED 42 cases",
        'KERNEL_BINARY_STATS {"count": 4, "max_bytes": 64000, "max_text_bytes": 18432, "template_instantiations": 16}',
        'PERF {"case": "f32_4x6x8", "mode": "custom", "ops": {"LayerNormV3": {"n": 5, "median_us": 9.6, "min_us": 9.2, "max_us": 10.1, "pipe": {"aiv_vec_ratio": "0.11", "aiv_scalar_ratio": "0.42", "aiv_mte2_ratio": "0.14", "aiv_mte3_ratio": "0.04"}}}}',
        'PERF {"case": "f32_4x6x8", "mode": "torch", "ops": {"aclnnLayerNorm": {"n": 5, "median_us": 3.4, "min_us": 3.2, "pipe": {"aiv_vec_ratio": "0.03", "aiv_scalar_ratio": "0.19", "aiv_mte2_ratio": "0.07", "aiv_mte3_ratio": "0.03"}}}}',
        'PERF {"case": "f32_64x8192", "mode": "custom", "ops": {"LayerNormV3": {"n": 5, "median_us": 29.0, "min_us": 28.2, "max_us": 29.8, "pipe": {"aiv_vec_ratio": "0.18", "aiv_scalar_ratio": "0.52", "aiv_mte2_ratio": "0.22", "aiv_mte3_ratio": "0.08"}}}}',
        'PERF {"case": "f32_64x8192", "mode": "torch", "ops": {"aclnnLayerNorm": {"n": 5, "median_us": 8.8, "min_us": 8.4}}}',
        'PERF {"case": "f16_4096x1024", "mode": "custom", "ops": {"LayerNormV3": {"n": 5, "median_us": 62.0, "min_us": 60.5, "max_us": 63.0, "pipe": {"aiv_vec_ratio": "0.55", "aiv_scalar_ratio": "0.38", "aiv_mte2_ratio": "0.12", "aiv_mte3_ratio": "0.06"}}}}',
    ])
    _write_run(ws, "LayerNormV3", sha, result_data={"title": "LayerNormV3", "sha": sha, "exit_code": 0, "phase_a_cpu_sec": 31.2}, log_text=real_style_log)

    card = build_pmu_torch_card(ws, "LayerNormV3", top_n=3)
    assert card["runtime_soc"] == "soc-a"
    assert card["dominant_bottleneck"] == "scalar-bound"
    assert card["pmu"]["aiv_scalar_ratio"] == pytest.approx((0.42 + 0.52 + 0.38) / 3.0, abs=1e-3)
    assert len(card["top_buckets"]) == 3
    # f32_64x8192 has ratio ~3.295x * w=0.30 -> highest headroom_score (~0.7486)
    assert card["top_buckets"][0]["bucket"] == "f32_64x8192"
    assert card["top_buckets"][0]["ratio_vs_torch"] == pytest.approx(29.0 / 8.8, rel=1e-3)
    assert card["top_buckets"][0]["headroom_score"] == pytest.approx(0.30 * (29.0 / 8.8 - 0.8), rel=1e-3)
    # f16_4096x1024 picked up tau_torch_us=56.5 from bench-specs
    f16_row = next(b for b in card["buckets"] if b["bucket"] == "f16_4096x1024")
    assert f16_row["torch_us"] == pytest.approx(56.5, rel=1e-4)
    assert card["elf_guard"]["status"] == "ok"
    assert card["cold3"]["status"] == "ok"

    md_lines = card["markdown_card"].splitlines()
    assert 6 <= len(md_lines) <= 25
    assert "scalar-bound" in card["markdown_card"]
    assert "f32_64x8192" in card["markdown_card"]


def test_pmu_torch_card_bank_conflict_and_mte2_classification(tmp_path: Path, isolated_parhelion_home: Path) -> None:
    ws, _ = _make_workspace(tmp_path, "ConfusionTransposeD")
    sha_bc = "f" * 40
    log_bc = (
        "RUNTIME_SOC=soc-a\n"
        'KERNEL_JSON {"case": "tr_1024", "mode": "custom", "median_us": 18.0, "min_us": 17.5, "max_us": 18.4, "torch_us": 7.5, '
        '"pmu": {"aiv_vec_ratio": 0.12, "aiv_mte2_ratio": 0.41, "aiv_mte3_ratio": 0.22, "aiv_scalar_ratio": 0.10, "ResourceConflictRatio": 0.28}}\n'
    )
    _write_run(ws, "ConfusionTransposeD", sha_bc, result_data={"title": "ConfusionTransposeD", "sha": sha_bc, "exit_code": 0}, log_text=log_bc)
    card_bc = build_pmu_torch_card(ws, "ConfusionTransposeD", sha=sha_bc)
    assert card_bc["dominant_bottleneck"] == "bank-conflict"
    assert card_bc["top_buckets"][0]["w_hat"] is None
    assert card_bc["top_buckets"][0]["headroom_score"] == pytest.approx(18.0 / 7.5, rel=1e-3)


# ---------------------------------------------------------------------------
# 4. bench_scaffold tests (append-only & family coverage)
# ---------------------------------------------------------------------------

def test_bench_scaffold_strictly_append_only_and_covers_all_strata(tmp_path: Path, isolated_parhelion_home: Path) -> None:
    ws, _ = _make_workspace(tmp_path, "QuantUpdateScatter")

    # Dry-run writes nothing
    preview = scaffold_bench_specs(ws, "QuantUpdateScatter", operator_family="gather_scatter", apply=False)
    assert preview["applied"] is False
    assert not Path(preview["cases_perf_path"]).exists()
    assert len(preview["pending_perf_ids"]) == 10
    assert len(preview["pending_correctness_ids"]) == 5

    # Pre-seed existing cases_perf.json & cases_correctness.json with custom user buckets
    perf_path = Path(preview["cases_perf_path"])
    corr_path = Path(preview["cases_correctness_path"])
    perf_path.parent.mkdir(parents=True, exist_ok=True)

    existing_custom_bucket = {
        "id": "custom_user_kv_cache",
        "stratum": "M_MID_UB",
        "dtype": "bfloat16",
        "prior_w": 0.42,
        "torch_ref_us": 12.34,
        "description": "DO NOT MUTATE",
    }
    existing_public = {"id": "P1", "stratum": "PUBLIC", "weight_pts": 2.0, "tbest_us": 3.74}
    perf_path.write_text(
        json.dumps({"problem": "QuantUpdateScatter", "public_anchors": [existing_public], "hidden_buckets": [existing_custom_bucket]}, indent=2),
        encoding="utf-8",
    )
    existing_corr = {"id": "escape_repro_001", "geometry": "G5_EDGE_ATTR", "distribution": "D5_ESCAPE_REG", "verified": True}
    corr_path.write_text(
        json.dumps({"problem": "QuantUpdateScatter", "cases": [existing_corr]}, indent=2),
        encoding="utf-8",
    )

    applied = scaffold_bench_specs(ws, "QuantUpdateScatter", operator_family="gather_scatter", dtypes=["fp32", "fp16", "bf16"], apply=True)
    assert applied["applied"] is True
    assert applied["existing_perf_count"] == 2
    assert len(applied["appended_perf_ids"]) == 10
    assert len(applied["appended_correctness_ids"]) == 5

    perf_after = json.loads(perf_path.read_text(encoding="utf-8"))
    corr_after = json.loads(corr_path.read_text(encoding="utf-8"))

    # Existing entries preserved verbatim (never deleted or mutated!)
    assert perf_after["public_anchors"][0] == existing_public
    assert perf_after["hidden_buckets"][0] == existing_custom_bucket
    assert corr_after["cases"][0] == existing_corr

    all_strata = {b["stratum"] for b in perf_after["public_anchors"] + perf_after["hidden_buckets"]}
    assert all_strata == {"PUBLIC", "S_SMALL_CORE", "M_MID_UB", "L_HBM_TILED", "X_EXTREME"}

    all_dtypes = {b.get("dtype") for b in perf_after["public_anchors"] + perf_after["hidden_buckets"] if b.get("dtype")}
    assert {"float32", "float16", "bfloat16"} <= all_dtypes

    # Unaligned tail widths 17, 31, 63, 127 covered
    shapes_text = json.dumps(perf_after)
    for tail_w in ("17", "31", "63", "127"):
        assert tail_w in shapes_text

    # Idempotent on re-run
    second = scaffold_bench_specs(ws, "QuantUpdateScatter", operator_family="gather_scatter", apply=True)
    assert second["appended_perf_ids"] == []
    assert second["appended_correctness_ids"] == []


@pytest.mark.parametrize("family", VALID_OPERATOR_FAMILIES)
def test_bench_scaffold_all_families(tmp_path: Path, isolated_parhelion_home: Path, family: str) -> None:
    ws, _ = _make_workspace(tmp_path, f"Op_{family}")
    res = scaffold_bench_specs(ws, f"Op_{family}", operator_family=family, apply=True)
    assert res["applied"] is True
    assert Path(res["cases_perf_path"]).is_file()
    assert Path(res["cases_correctness_path"]).is_file()
    assert res["strata_covered"] == ["PUBLIC", "S_SMALL_CORE", "M_MID_UB", "L_HBM_TILED", "X_EXTREME"]


def test_bench_scaffold_rejects_unknown_family(tmp_path: Path, isolated_parhelion_home: Path) -> None:
    ws, _ = _make_workspace(tmp_path, "BadOp")
    with pytest.raises(ValueError):
        scaffold_bench_specs(ws, "BadOp", operator_family="matmul_magic", apply=False)


# ---------------------------------------------------------------------------
# 5. CLI command registration & runner tests
# ---------------------------------------------------------------------------

def test_tools_cmd_registers_and_runs_subcommands(
    tmp_path: Path,
    isolated_parhelion_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ws, _ = _make_workspace(tmp_path, "CTCLossV2")
    sha = "9" * 40
    _write_run(
        ws,
        "CTCLossV2",
        sha,
        result_data={
            "title": "CTCLossV2",
            "sha": sha,
            "exit_code": 0,
            "phase_a_cpu_sec": 42.0,
            "kernel_o_count": 4,
            "kernel_o_max_bytes": 60000,
            "kernel_o_max_text_bytes": 18000,
            "template_instantiations": 12,
        },
        log_text=(
            "RUNTIME_SOC=soc-a\n"
            'KPERF_JSON [{"mode": "custom", "case": "t50_c32_s20", "cold3_runs": [14.0, 12.8, 12.6], "median_us": 12.8, "min_us": 12.6, "max_us": 14.0}, '
            '{"mode": "torch", "case": "t50_c32_s20", "median_us": 95.0}]\n'
            'PMU_JSON {"case": "t50_c32_s20", "aiv_vec_ratio": 0.45, "aiv_mte2_ratio": 0.15, "aiv_mte3_ratio": 0.08, "aiv_scalar_ratio": 0.20}\n'
        ),
    )

    parser = argparse.ArgumentParser(prog="opsb")
    sub = parser.add_subparsers(dest="cmd", required=True)
    bn = sub.add_parser("bench")
    bn_sub = bn.add_subparsers(dest="bench_cmd", required=True)
    register_fast_tools_subcommands(bn_sub)

    for argv in (
        ["bench", "elf", "CTCLossV2", "--workspace", str(ws), "--json"],
        ["bench", "cold3", "CTCLossV2", "--workspace", str(ws), "--json"],
        ["bench", "pmu", "CTCLossV2", "--workspace", str(ws), "--top", "3", "--json"],
        ["bench", "scaffold", "CTCLossV2", "--family", "sequence_dp", "--apply", "--workspace", str(ws), "--json"],
    ):
        args = parser.parse_args(argv)
        rc = args.func(args)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["title"] == "CTCLossV2"

    # Also test markdown text output for `bench pmu` (<= 25 lines)
    args_md = parser.parse_args(["bench", "pmu", "CTCLossV2", "--workspace", str(ws)])
    assert args_md.func(args_md) == 0
    md_out = capsys.readouterr().out.strip()
    assert "PMU & Torch Card: CTCLossV2" in md_out
    assert len(md_out.splitlines()) <= 25

    # Also test workspace-wide `bench elf` and `bench cold3` without positional problem argument
    for argv in (
        ["bench", "elf", "--workspace", str(ws), "--json"],
        ["bench", "cold3", "--workspace", str(ws), "--json"],
    ):
        args = parser.parse_args(argv)
        assert args.func(args) == 0
        multi = json.loads(capsys.readouterr().out)
        assert multi["count"] == 1
        assert multi["problems"][0]["title"] == "CTCLossV2"


def test_pmu_torch_card_markdown_table_and_latest_passing_selection(
    tmp_path: Path,
    isolated_parhelion_home: Path,
) -> None:
    ws, _ = _make_workspace(tmp_path, "ConcatD")
    sha_pass = "1" * 40
    sha_fail = "2" * 40

    md_log = "\n".join([
        "RUNTIME_SOC=soc-a",
        "EXTENDED_PASSED 18 cases",
        "| case | dtype | N | bytes | blks | med_us | min_us | torch_med | torch_min |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
        "| many32_rows_unalign | float32 | 32 | 262144 | 8 | 12.40 | 11.90 | 30.00 | 29.00 |",
        "| big_axis0 | float32 | 2 | 8388608 | 32 | 14.00 | 13.50 | 7.00 | 6.80 |",
        "PMU case=big_axis0 aiv_vec_ratio=0.10 aiv_mte2_ratio=0.54 aiv_mte3_ratio=0.18 aiv_scalar_ratio=0.12",
    ])
    _write_run(
        ws,
        "ConcatD",
        sha_pass,
        result_data={"title": "ConcatD", "sha": sha_pass, "exit_code": 0, "finished_at": "2026-11-22T10:00:00+00:00"},
        log_text=md_log,
    )
    _write_run(
        ws,
        "ConcatD",
        sha_fail,
        result_data={"title": "ConcatD", "sha": sha_fail, "exit_code": 1, "finished_at": "2026-11-22T12:00:00+00:00"},
        log_text="RUNTIME_SOC=soc-a\nVALIDATION_FAILED\n",
    )

    card = build_pmu_torch_card(ws, "ConcatD")
    assert card["sha"] == sha_pass  # skips newer failing run
    assert card["dominant_bottleneck"] == "mte2-bound"
    assert card["top_buckets"][0]["bucket"] == "big_axis0"
    assert card["top_buckets"][0]["ratio_vs_torch"] == pytest.approx(2.0, rel=1e-4)
    assert len(card["markdown_card"].splitlines()) <= 25


def test_pmu_bottleneck_variants_mte3_vec_cold_balanced_unknown(
    tmp_path: Path,
    isolated_parhelion_home: Path,
) -> None:
    ws, _ = _make_workspace(tmp_path, "MultilabelMarginLoss")

    cases = [
        ("3" * 40, 'PMU_JSON {"case": "c1", "aiv_vec_ratio": 0.12, "aiv_mte2_ratio": 0.18, "aiv_mte3_ratio": 0.52, "aiv_scalar_ratio": 0.14}', "mte3-bound"),
        ("4" * 40, 'PMU_JSON {"case": "c1", "aiv_vec_ratio": 0.62, "aiv_mte2_ratio": 0.20, "aiv_mte3_ratio": 0.11, "aiv_scalar_ratio": 0.15}', "vec-bound"),
        ("5" * 40, 'PMU_JSON {"case": "c1", "aiv_vec_ratio": 0.30, "aiv_mte2_ratio": 0.31, "aiv_mte3_ratio": 0.28, "aiv_scalar_ratio": 0.18}', "balanced"),
        ("6" * 40, 'KPERF_JSON [{"mode": "custom", "case": "c1", "cold3_runs": [9.5, 4.0, 3.8], "median_us": 4.0, "min_us": 3.8}, {"mode": "torch", "case": "c1", "median_us": 3.7}]', "cold-launch-tax"),
        ("7" * 40, 'KPERF_JSON [{"mode": "custom", "case": "c1", "median_us": 25.0, "min_us": 24.5, "max_us": 25.5}]', "unknown"),
    ]
    for sha, line, expected in cases:
        _write_run(ws, "MultilabelMarginLoss", sha, result_data={"title": "MultilabelMarginLoss", "sha": sha, "exit_code": 0}, log_text=line + "\n")
        card = build_pmu_torch_card(ws, "MultilabelMarginLoss", sha=sha)
        assert card["dominant_bottleneck"] == expected


def test_runtime_npu_test_cold3_flag_and_remote_syntax() -> None:
    npu_test_path = REPO_ROOT / "runtime" / "npu-test.py"
    src = npu_test_path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    assert tree is not None

    ns: dict[str, Any] = {}
    exec(compile(src, str(npu_test_path), "exec"), ns)
    remote_src = ns["REMOTE_3PHASE"]
    ast.parse(remote_src)

    assert "--cold3" in src
    assert "COLD3_STATS" in remote_src
    assert "template_instantiations" in remote_src


def test_no_personal_home_hardcodes_in_track1_files() -> None:
    needle = "/" + "Users/" + "winbeau"
    for rel in (
        "cli/opsb_core/services/fast_feedback_service.py",
        "cli/opsb_core/commands/tools_cmd.py",
        "runtime/npu-test.py",
        "cli/tests/test_fast_feedback.py",
    ):
        txt = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert needle not in txt, f"Personal home path leaked into {rel}"
