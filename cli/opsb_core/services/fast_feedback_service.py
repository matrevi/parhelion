"""Backward-compatibility facade for Parhelion v3 Fast-Feedback Tools.

Implementation lives in `opsb_core.fast_feedback.*`.
"""
from __future__ import annotations

from ..fast_feedback import (
    DEFAULT_COLD3_THRESHOLDS,
    DEFAULT_ELF_THRESHOLDS,
    VALID_OPERATOR_FAMILIES,
    build_pmu_torch_card,
    evaluate_cold3_probe,
    evaluate_elf_guard,
    scaffold_bench_specs,
)

__all__ = [
    "DEFAULT_COLD3_THRESHOLDS",
    "DEFAULT_ELF_THRESHOLDS",
    "VALID_OPERATOR_FAMILIES",
    "build_pmu_torch_card",
    "evaluate_cold3_probe",
    "evaluate_elf_guard",
    "scaffold_bench_specs",
]
