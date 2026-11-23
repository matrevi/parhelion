"""Core constants, dynamic PARHELION_HOME paths, atomic/locked I/O, and shared helpers for Triad KB."""
from __future__ import annotations

import copy
import fcntl
import json
import math
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable

from .. import config as cfg_mod
from ..config import append_jsonl_locked, read_json, utc_now

VALID_PILLARS = ("breakthrough", "snippets", "community")
WRITABLE_PILLARS = ("snippets", "community")
VALID_PORTABILITIES = ("native-accel", "portable", "remap-to-accel", "anti-pattern")
VALID_STATUSES = ("verified", "unverified", "deprecated", "anti-pattern")
VALID_VERDICTS = ("win", "fail", "anti-pattern")
VALID_REGIMES = ("PUBLIC", "S_SMALL_CORE", "M_MID_UB", "L_HBM_TILED", "X_EXTREME")

PROMOTE_MIN_DELTA_RAW = 1.5
PROMOTE_MIN_SPEEDUP = 1.08
DEMOTE_MIN_FAILS = 3

_INITIAL_CFG_HOME = cfg_mod.PARHELION_HOME
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_METRIC_NOTE_RE = re.compile(r"\b(profiler|online)\b", re.IGNORECASE)

_DTYPE_ALIASES: dict[str, str] = {
    "fp32": "float32", "f32": "float32", "float": "float32", "float32": "float32",
    "fp16": "float16", "f16": "float16", "half": "float16", "float16": "float16",
    "bf16": "bfloat16", "bfloat16": "bfloat16", "bfloat16_t": "bfloat16",
    "i8": "int8", "int8": "int8", "int8_t": "int8", "u8": "uint8", "uint8": "uint8",
    "i32": "int32", "int32": "int32", "i64": "int64", "int64": "int64", "u16": "uint16", "uint16": "uint16",
}

_BOTTLENECK_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("cold-launch-tax", r"cold|launch|dispatch|setblockdim|tpipe|zero-tpipe|workspace|1kb|64b|direct-gm|setdatasize|tiling-regime"),
    ("icache-bloat", r"icache|i-cache|elf|\.text|noinline|instantiation|tle|420s|bloat"),
    ("dvfs-10core-spike", r"dvfs|10-?core|bimodal"),
    ("mte2-bound", r"mte2|hbm|dma|datacopy|coalesc|swizzle|prefetch|prologue"),
    ("mte3-bound", r"mte3|writeback|store"),
    ("bank-conflict", r"bank|48-?bank|bankgroup|coloring|stagger"),
    ("scalar-bound", r"scalar|aiv_scalar|get_value|set_value|setvalue|branch"),
    ("gather-bound", r"gather|vgather|zero-gather"),
    ("transpose-bound", r"transdata|transpose|5hd"),
    ("vec-bound", r"pipe_v|vec|vector|simd|brcb|vconv|dekker|reduce|welford|softmax"),
    ("unaligned-tail", r"datacopypad|unaligned|tail|odd|prime"),
    ("pipeline-stall", r"ring4|ring|ping-?pong|double-?buffer|async|pipeline|tque"),
    ("sync-hazard", r"sentinel|handshake|syncall|pipebarrier|atomic"),
)


def resolve_parhelion_home(parhelion_home: Path | str | None = None) -> Path:
    """Resolve active Parhelion home dynamically (supports env overrides & config patches)."""
    if parhelion_home is not None:
        return Path(parhelion_home).expanduser().resolve()
    env_override = os.environ.get("PARHELION_HOME") or os.environ.get("OPSB_HOME")
    if env_override:
        return Path(env_override).expanduser().resolve()
    if cfg_mod.PARHELION_HOME != _INITIAL_CFG_HOME:
        return Path(cfg_mod.PARHELION_HOME).expanduser().resolve()
    return cfg_mod._resolve_parhelion_home()


def triad_kb_dirs(parhelion_home: Path | str | None = None) -> dict[str, Path]:
    home = resolve_parhelion_home(parhelion_home)
    return {
        "home": home, "breakthrough": home / "breakthrough",
        "snippets": home / "snippets", "community": home / "community",
        "evolution_log": home / "kb-evolution.jsonl",
    }


def _atomic_write_text(path: Path, content: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(content, encoding="utf-8")
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def _atomic_write_json(path: Path, payload: Any) -> None:
    _atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _with_index_lock(index_path: Path, mutator: Callable[[dict[str, Any]], tuple[dict[str, Any], Any]]) -> Any:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with (index_path.parent / ".index.lock").open("a", encoding="utf-8") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            current = read_json(index_path, {"schema_version": 1, "entries": []})
            if not isinstance(current, dict):
                current = {"schema_version": 1, "entries": []}
            if not isinstance(current.get("entries"), list):
                current["entries"] = []
            updated, result = mutator(current)
            updated["schema_version"] = int(updated.get("schema_version") or 1)
            updated["updated_at"] = utc_now().isoformat()
            _atomic_write_json(index_path, updated)
            return result
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _copy_if_missing(src: Path, dst: Path) -> bool:
    if not src.is_file() or dst.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)
    return True


def _is_num(val: Any) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(float(val))


def _normalize_dtype(dtype: str) -> str:
    s = str(dtype or "").strip().lower()
    return _DTYPE_ALIASES.get(s, s)


def _normalize_tag_list(values: Any) -> list[str]:
    if not values:
        return []
    items = [values] if isinstance(values, str) else list(values)
    out: list[str] = []
    for item in items:
        for part in str(item).split(","):
            tok = part.strip().lower()
            if tok and tok not in out:
                out.append(tok)
    return out


def _infer_bottlenecks_from_text(text: str, category: str = "", tags: list[str] | None = None) -> list[str]:
    blob = " ".join([text, category, " ".join(tags or [])]).lower()
    inferred = [b_tag for b_tag, pat in _BOTTLENECK_KEYWORDS if re.search(pat, blob)]
    if not inferred and category:
        cat_map = {
            "tiling-regime": "cold-launch-tax", "async-pipeline": "mte2-bound",
            "dma-coalescing": "mte2-bound", "simd-transpose": "transpose-bound",
            "vectorization": "vec-bound", "deterministic-sync": "sync-hazard", "separable-folding": "vec-bound",
        }
        if mapped := cat_map.get(category.lower()):
            inferred.append(mapped)
    return inferred or ["general-perf"]


def _infer_regimes_from_text(text: str) -> list[str]:
    low = text.lower()
    regimes = [
        reg for pat, reg in (
            (r"small|tiny|cold|dispatch|1kb|64b|micro-entry", "S_SMALL_CORE"),
            (r"mid|ub|192kb|184kb|half-core|20-core|fp16|bf16", "M_MID_UB"),
            (r"hbm|ring|ping-pong|large|big|724|swizzle", "L_HBM_TILED"),
            (r"unalign|odd|prime|tail|datacopypad|extreme|sentinel", "X_EXTREME"),
        )
        if re.search(pat, low)
    ]
    return regimes or ["S_SMALL_CORE", "M_MID_UB", "L_HBM_TILED"]


def _infer_dtypes_from_text(text: str, tags: list[str] | None = None) -> list[str]:
    blob = f"{text} {' '.join(tags or [])}".lower()
    dtypes: list[str] = []
    for kw, norm in (
        ("float32", "float32"), ("fp32", "float32"), ("f32", "float32"),
        ("float16", "float16"), ("fp16", "float16"), ("f16", "float16"),
        ("bfloat16", "bfloat16"), ("bf16", "bfloat16"),
        ("int8", "int8"), ("i8", "int8"), ("int32", "int32"), ("int64", "int64"),
    ):
        if re.search(rf"\b{kw}\b", blob) and norm not in dtypes:
            dtypes.append(norm)
    return dtypes or ["float32", "float16", "bfloat16"]


def _beta_posterior_mean(
    wins: int, fails: int, *, portability: str = "portable", status: str = "unverified",
    prior_alpha: float | None = None, prior_beta: float | None = None,
) -> float:
    if prior_alpha is not None and prior_beta is not None:
        a0, b0 = float(prior_alpha), float(prior_beta)
    elif portability == "native-accel" or status == "verified":
        a0, b0 = 2.0, 1.0
    else:
        a0, b0 = 1.0, 1.0
    w, f = max(0, int(wins)), max(0, int(fails))
    return round((a0 + w) / (a0 + b0 + w + f), 3)


def _resolve_doc_path(pillar_dir: Path, doc_path: str, entry_id: str, pillar: str) -> Path | None:
    cands = [pillar_dir / doc_path] if doc_path else []
    cands.extend([pillar_dir / "code" / f"{entry_id}.h", pillar_dir / f"{entry_id}.h"] if pillar == "snippets" else [pillar_dir / "entries" / f"{entry_id}.md"])
    return next((c.resolve() for c in cands if c.is_file()), None)


def _new_event_id() -> str:
    return f"KBEV-{utc_now().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"


def _append_evolution_event(lock_path: Path, target_path: Path, event: dict[str, Any]) -> None:
    append_jsonl_locked(lock_path, target_path, event)


def _snapshot_for_audit(entry: dict[str, Any]) -> dict[str, Any]:
    return {k: copy.deepcopy(v) for k, v in entry.items() if k != "abs_doc_path"}


def _find_entry_location(dirs: dict[str, Path], entry_id: str) -> tuple[str, Path, int, dict[str, Any]] | None:
    q = entry_id.strip().lower()
    for pillar in VALID_PILLARS:
        idx_path = dirs[pillar] / "index.json"
        raw = read_json(idx_path, {"entries": []})
        for idx, cur in enumerate((raw.get("entries") if isinstance(raw, dict) else []) or []):
            if isinstance(cur, dict) and str(cur.get("id") or "").lower() == q:
                return pillar, idx_path, idx, cur
    return None
