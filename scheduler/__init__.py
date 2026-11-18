"""Unified Task Sequence Scheduler & 4-Stage Submission Gate for OpsSupervisor.

Pure Python 3 standard-library package embedded by both `OpsSupervisor.app`
and the `opsb` CLI to manage:
  1. Expected Score Estimation & Empirical Frontier Headroom (`estimator.py`)
  2. 4-Stage Statistical & Safety Submission Gate + Token Bucket (`gate.py`)
  3. Split-Phase + MLFQ (Q0/Q1) + WSPT Value-Density NPU Scheduler (`npu_scheduler.py`)
  4. B0 Baseline Bucket Backfill Lane (`backfill.py`, `backfill_status.py`)
  5. Active CLI & JSON IPC Bridge (`cli_bridge.py`)
"""

from .backfill import (
    LANE_BACKFILL,
    TICKET_TIER,
    BackfillConfig,
    BackfillLedger,
    BackfillScheduler,
    bucket_key,
    build_perf_plan,
    coalesce_sessions,
    session_key_for,
    ticket_rank,
)
from .calibration import BenchmarkCalibrator, project_capped_simplex
from .estimator import (
    CROSS_CORE_OPS,
    PROBLEM_DIFFICULTY,
    ProblemLedger,
    ScoreEstimate,
    ScoreEstimator,
    parse_validation_perf,
    resolve_sha,
)
from .gate import (
    STATUS_TAGS,
    CircuitBreakerStatus,
    GateConfig,
    GateVerdict,
    QuotaStatus,
    StageResult,
    SubmissionGate,
)
from .npu_scheduler import (
    NPU_TASK_TAGS,
    NpuTask,
    NpuTaskScheduler,
    ProblemPriority,
)


def __getattr__(name: str):
    if name == "SchedulerEngine":
        from .cli_bridge import SchedulerEngine
        return SchedulerEngine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "BackfillConfig",
    "BackfillLedger",
    "BackfillScheduler",
    "BenchmarkCalibrator",
    "LANE_BACKFILL",
    "TICKET_TIER",
    "bucket_key",
    "build_perf_plan",
    "coalesce_sessions",
    "session_key_for",
    "ticket_rank",
    "CROSS_CORE_OPS",
    "NPU_TASK_TAGS",
    "PROBLEM_DIFFICULTY",
    "STATUS_TAGS",
    "CircuitBreakerStatus",
    "GateConfig",
    "GateVerdict",
    "NpuTask",
    "NpuTaskScheduler",
    "ProblemLedger",
    "ProblemPriority",
    "QuotaStatus",
    "SchedulerEngine",
    "ScoreEstimate",
    "ScoreEstimator",
    "StageResult",
    "SubmissionGate",
    "parse_validation_perf",
    "project_capped_simplex",
    "resolve_sha",
]
