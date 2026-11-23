"""Self-Evolving Triad Knowledge Base service (`breakthrough` + `snippets` + `community`)."""
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
from ..config import SUPERVISOR_ROOT, append_jsonl_locked, read_json, read_jsonl, utc_now

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
    "fp32": "float32",
    "f32": "float32",
    "float": "float32",
    "float32": "float32",
    "fp16": "float16",
    "f16": "float16",
    "half": "float16",
    "float16": "float16",
    "bf16": "bfloat16",
    "bfloat16": "bfloat16",
    "bfloat16_t": "bfloat16",
    "i8": "int8",
    "int8": "int8",
    "int8_t": "int8",
    "u8": "uint8",
    "uint8": "uint8",
    "i32": "int32",
    "int32": "int32",
    "i64": "int64",
    "int64": "int64",
    "u16": "uint16",
    "uint16": "uint16",
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
        "home": home,
        "breakthrough": home / "breakthrough",
        "snippets": home / "snippets",
        "community": home / "community",
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
    lock_path = index_path.parent / ".index.lock"
    with lock_path.open("a", encoding="utf-8") as lf:
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


def compute_confidence(
    wins: int,
    fails: int,
    *,
    portability: str = "portable",
    status: str = "unverified",
    prior_alpha: float | None = None,
    prior_beta: float | None = None,
) -> float:
    """Bayesian Beta posterior mean confidence in [0.0, 1.0]."""
    if prior_alpha is not None and prior_beta is not None:
        alpha0 = float(prior_alpha)
        beta0 = float(prior_beta)
    elif portability == "native-accel" or status == "verified":
        alpha0, beta0 = 2.0, 1.0
    else:
        alpha0, beta0 = 1.0, 1.0
    w = max(0, int(wins))
    f = max(0, int(fails))
    return round((alpha0 + w) / (alpha0 + beta0 + w + f), 3)


def validate_evidence(evidence: Any, default_verdict: str | None = None) -> dict[str, Any]:
    """Validate mandatory hardware/online evidence gate; raises ValueError on unbacked claims."""
    if not isinstance(evidence, dict) or not evidence:
        raise ValueError(
            "Evidence payload must be a non-empty dict with problem, sha (7-40 hex), verdict, and measurable metric."
        )

    problem = str(evidence.get("problem") or "").strip()
    if not problem:
        raise ValueError("Evidence requires non-empty 'problem' name.")

    sha = str(evidence.get("sha") or "").strip()
    if not _SHA_RE.match(sha):
        raise ValueError(f"Evidence 'sha' must be 7-40 hex characters, got {sha!r}.")

    verdict_raw = str(evidence.get("verdict") or default_verdict or "").strip().lower()
    if verdict_raw not in VALID_VERDICTS:
        raise ValueError(f"Evidence 'verdict' must be one of {VALID_VERDICTS}, got {verdict_raw!r}.")
    if default_verdict and evidence.get("verdict"):
        dv = str(default_verdict).strip().lower()
        if dv != verdict_raw:
            raise ValueError(f"Conflicting verdict {dv!r} vs evidence.verdict {verdict_raw!r}.")

    has_delta_raw = _is_num(evidence.get("delta_raw"))
    has_delta_us = _is_num(evidence.get("delta_us"))
    has_speedup = _is_num(evidence.get("speedup")) and float(evidence["speedup"]) > 0.0
    note = str(evidence.get("note") or "").strip()
    has_note_metric = bool(_METRIC_NOTE_RE.search(note))

    if not (has_delta_raw or has_delta_us or has_speedup or has_note_metric):
        raise ValueError(
            "Unbacked KB write rejected: evidence must include 'delta_raw', 'delta_us', 'speedup', "
            "or a 'note' citing 'profiler' or 'online'."
        )

    cleaned: dict[str, Any] = {
        "workspace": str(evidence.get("workspace") or "").strip(),
        "problem": problem,
        "sha": sha,
        "verdict": verdict_raw,
        "recorded_at": str(evidence.get("recorded_at") or utc_now().isoformat()),
    }
    if has_delta_raw:
        cleaned["delta_raw"] = round(float(evidence["delta_raw"]), 4)
    if has_delta_us:
        cleaned["delta_us"] = round(float(evidence["delta_us"]), 4)
    if has_speedup:
        cleaned["speedup"] = round(float(evidence["speedup"]), 4)
    if note:
        cleaned["note"] = note
    return cleaned


def _infer_bottlenecks_from_text(text: str, category: str = "", tags: list[str] | None = None) -> list[str]:
    blob = " ".join([text, category, " ".join(tags or [])]).lower()
    inferred: list[str] = []
    for b_tag, pattern in _BOTTLENECK_KEYWORDS:
        if re.search(pattern, blob) and b_tag not in inferred:
            inferred.append(b_tag)
    if not inferred and category:
        cat_map = {
            "tiling-regime": "cold-launch-tax",
            "async-pipeline": "mte2-bound",
            "dma-coalescing": "mte2-bound",
            "simd-transpose": "transpose-bound",
            "vectorization": "vec-bound",
            "deterministic-sync": "sync-hazard",
            "separable-folding": "vec-bound",
        }
        mapped = cat_map.get(category.lower())
        if mapped:
            inferred.append(mapped)
    return inferred or ["general-perf"]


def _infer_regimes_from_text(text: str) -> list[str]:
    low = text.lower()
    regimes: list[str] = []
    if re.search(r"small|tiny|cold|dispatch|1kb|64b|micro-entry", low):
        regimes.append("S_SMALL_CORE")
    if re.search(r"mid|ub|192kb|184kb|half-core|20-core|fp16|bf16", low):
        regimes.append("M_MID_UB")
    if re.search(r"hbm|ring|ping-pong|large|big|724|swizzle", low):
        regimes.append("L_HBM_TILED")
    if re.search(r"unalign|odd|prime|tail|datacopypad|extreme|sentinel", low):
        regimes.append("X_EXTREME")
    return regimes or ["S_SMALL_CORE", "M_MID_UB", "L_HBM_TILED"]


def _infer_dtypes_from_text(text: str, tags: list[str] | None = None) -> list[str]:
    blob = f"{text} {' '.join(tags or [])}".lower()
    dtypes: list[str] = []
    for kw, norm in (
        ("float32", "float32"),
        ("fp32", "float32"),
        ("f32", "float32"),
        ("float16", "float16"),
        ("fp16", "float16"),
        ("f16", "float16"),
        ("bfloat16", "bfloat16"),
        ("bf16", "bfloat16"),
        ("int8", "int8"),
        ("i8", "int8"),
        ("int32", "int32"),
        ("int64", "int64"),
    ):
        if re.search(rf"\b{kw}\b", blob) and norm not in dtypes:
            dtypes.append(norm)
    return dtypes or ["float32", "float16", "bfloat16"]


def _resolve_doc_path(pillar_dir: Path, doc_path: str, entry_id: str, pillar: str) -> Path | None:
    candidates: list[Path] = []
    if doc_path:
        candidates.append(pillar_dir / doc_path)
    if pillar == "snippets":
        candidates.append(pillar_dir / "code" / f"{entry_id}.h")
        candidates.append(pillar_dir / f"{entry_id}.h")
    else:
        candidates.append(pillar_dir / "entries" / f"{entry_id}.md")
    for cand in candidates:
        if cand.is_file():
            return cand.resolve()
    return None


def _normalize_entry(raw: dict[str, Any], pillar: str, pillar_dir: Path) -> dict[str, Any]:
    entry_id = str(raw.get("id") or "").strip()
    title = str(raw.get("title") or entry_id).strip()
    raw_tags = [str(t).strip() for t in (raw.get("tags") or []) if str(t).strip()]
    category = str(raw.get("category") or "").strip()

    if pillar == "breakthrough":
        summary = str(raw.get("summary") or raw.get("transferable_rule") or raw.get("bottleneck") or title).strip()
        text_blob = " ".join(
            [
                title,
                summary,
                str(raw.get("bottleneck") or ""),
                " ".join(str(m) for m in (raw.get("mechanisms") or [])),
                str(raw.get("transferable_rule") or ""),
            ]
        )
        raw_status = str(raw.get("status") or "verified").strip().lower()
        status = raw_status if raw_status in VALID_STATUSES else ("verified" if raw_status == "verified" else "unverified")
        portability = str(raw.get("portability") or ("anti-pattern" if status == "anti-pattern" else "native-accel"))
    else:
        summary = str(raw.get("summary") or title).strip()
        text_blob = f"{title} {summary}"
        raw_status = str(raw.get("status") or "unverified").strip().lower()
        status = raw_status if raw_status in VALID_STATUSES else "unverified"
        default_port = "native-accel" if pillar == "snippets" else "portable"
        portability = str(raw.get("portability") or default_port).strip().lower()
        if portability not in VALID_PORTABILITIES:
            portability = default_port

    bottlenecks = _normalize_tag_list(raw.get("bottlenecks")) or _infer_bottlenecks_from_text(text_blob, category, raw_tags)
    regimes = [str(r).strip().upper() for r in (raw.get("regimes") or []) if str(r).strip()] or _infer_regimes_from_text(text_blob)
    dtypes = [_normalize_dtype(d) for d in (raw.get("dtypes") or []) if str(d).strip()] or _infer_dtypes_from_text(text_blob, raw_tags)

    evidence = list(raw.get("evidence") or [])
    if pillar == "breakthrough" and not evidence and raw.get("sha"):
        evidence = [
            {
                "workspace": "",
                "problem": str(raw.get("problem") or ""),
                "sha": str(raw.get("sha") or ""),
                "delta_raw": float(raw.get("delta_raw") or 0.0),
                "verdict": "win" if status == "verified" else "fail",
                "note": f"online breakthrough entry ({raw.get('score_before', 0)} -> {raw.get('score_after', 0)})",
                "recorded_at": str(raw.get("timestamp") or utc_now().isoformat()),
            }
        ]

    if "wins" in raw and "fails" in raw:
        wins = max(0, int(raw["wins"]))
        fails = max(0, int(raw["fails"]))
    elif pillar == "breakthrough":
        wins = 1 if status == "verified" else 0
        fails = 0
    else:
        wins = sum(1 for ev in evidence if isinstance(ev, dict) and str(ev.get("verdict") or "").lower() == "win")
        fails = sum(1 for ev in evidence if isinstance(ev, dict) and str(ev.get("verdict") or "").lower() in ("fail", "anti-pattern"))

    prior_alpha = raw.get("prior_alpha")
    prior_beta = raw.get("prior_beta")
    if prior_alpha is None or prior_beta is None:
        if portability == "native-accel" or status == "verified":
            prior_alpha, prior_beta = 2.0, 1.0
        else:
            prior_alpha, prior_beta = 1.0, 1.0

    confidence = (
        round(float(raw["confidence"]), 3)
        if _is_num(raw.get("confidence")) and "wins" in raw and "fails" in raw
        else compute_confidence(
            wins,
            fails,
            portability=portability,
            status=status,
            prior_alpha=float(prior_alpha),
            prior_beta=float(prior_beta),
        )
    )

    doc_path = str(raw.get("doc_path") or "").strip()
    abs_doc = _resolve_doc_path(pillar_dir, doc_path, entry_id, pillar)
    if not doc_path:
        doc_path = f"code/{entry_id}.h" if pillar == "snippets" else f"entries/{entry_id}.md"

    normalized: dict[str, Any] = {
        "id": entry_id,
        "pillar": pillar,
        "title": title,
        "summary": summary,
        "category": category or ("tiling-regime" if pillar == "snippets" else "vectorization"),
        "tags": raw_tags,
        "bottlenecks": bottlenecks,
        "regimes": regimes,
        "dtypes": dtypes,
        "portability": portability,
        "status": status,
        "prior_alpha": float(prior_alpha),
        "prior_beta": float(prior_beta),
        "confidence": confidence,
        "wins": wins,
        "fails": fails,
        "evidence": evidence,
        "related_snippets": [str(s) for s in (raw.get("related_snippets") or []) if str(s).strip()],
        "doc_path": doc_path,
        "abs_doc_path": str(abs_doc) if abs_doc else None,
    }
    for passthrough in ("problem", "difficulty", "sha", "baseline_sha", "timestamp", "score_before", "score_after", "delta_raw", "delta_weighted"):
        if passthrough in raw:
            normalized[passthrough] = raw[passthrough]
    return normalized


def ensure_triad_kb_seeded(
    parhelion_home: Path | None = None,
    templates_root: Path | None = None,
) -> dict[str, Any]:
    """Non-destructively seed snippets/ and community/ from templates/ if missing."""
    dirs = triad_kb_dirs(parhelion_home)
    home = dirs["home"]
    tpl_root = Path(templates_root).expanduser().resolve() if templates_root is not None else (SUPERVISOR_ROOT / "templates")

    home.mkdir(parents=True, exist_ok=True)
    copied_files: list[str] = []
    seeded_pillars: list[str] = []
    skipped_pillars: list[str] = []

    for pillar in WRITABLE_PILLARS:
        src_dir = tpl_root / pillar
        dst_dir = dirs[pillar]
        (dst_dir / ("code" if pillar == "snippets" else "entries")).mkdir(parents=True, exist_ok=True)
        src_idx = src_dir / "index.json"
        dst_idx = dst_dir / "index.json"

        if not dst_idx.is_file():
            if src_idx.is_file():
                def _seed_idx(cur: dict[str, Any], _src: Path = src_idx) -> tuple[dict[str, Any], bool]:
                    loaded = read_json(_src, {"schema_version": 1, "entries": []})
                    if isinstance(loaded, dict) and isinstance(loaded.get("entries"), list):
                        return loaded, True
                    return cur, False

                if _with_index_lock(dst_idx, _seed_idx):
                    seeded_pillars.append(pillar)
                    copied_files.append(str(dst_idx))
            else:
                _with_index_lock(dst_idx, lambda cur: (cur, True))
                seeded_pillars.append(pillar)
        else:
            skipped_pillars.append(pillar)

        if src_dir.is_dir():
            for sub_name in ("code", "entries"):
                src_sub = src_dir / sub_name
                if src_sub.is_dir():
                    for src_file in sorted(src_sub.iterdir()):
                        if src_file.is_file():
                            dst_file = dst_dir / sub_name / src_file.name
                            if _copy_if_missing(src_file, dst_file):
                                copied_files.append(str(dst_file))
            for src_file in sorted(src_dir.iterdir()):
                if src_file.is_file() and src_file.suffix in (".h", ".md"):
                    dst_file = dst_dir / src_file.name
                    if _copy_if_missing(src_file, dst_file):
                        copied_files.append(str(dst_file))

    return {
        "ok": True,
        "parhelion_home": str(home),
        "templates_root": str(tpl_root),
        "seeded_pillars": seeded_pillars,
        "skipped_pillars": skipped_pillars,
        "copied_files": copied_files,
        "copied_count": len(copied_files),
    }


def _load_all_entries(
    parhelion_home: Path | None = None,
    *,
    auto_seed: bool = True,
) -> tuple[dict[str, Path], list[dict[str, Any]]]:
    dirs = triad_kb_dirs(parhelion_home)
    if auto_seed and (not (dirs["snippets"] / "index.json").is_file() or not (dirs["community"] / "index.json").is_file()):
        ensure_triad_kb_seeded(parhelion_home=dirs["home"])

    all_entries: list[dict[str, Any]] = []
    for pillar in VALID_PILLARS:
        p_dir = dirs[pillar]
        raw_idx = read_json(p_dir / "index.json", {"entries": []})
        items = raw_idx.get("entries") if isinstance(raw_idx, dict) else []
        for raw_item in items or []:
            if isinstance(raw_item, dict) and raw_item.get("id"):
                all_entries.append(_normalize_entry(raw_item, pillar, p_dir))
    return dirs, all_entries


def list_kb_entries(
    pillar: str = "all",
    status: str | None = None,
    portability: str | None = None,
    parhelion_home: Path | None = None,
    auto_seed: bool = True,
) -> dict[str, Any]:
    """List normalized KB entries across one or all 3 pillars."""
    p_norm = (pillar or "all").strip().lower()
    if p_norm != "all" and p_norm not in VALID_PILLARS:
        raise ValueError(f"Invalid pillar {pillar!r}; expected one of {('all', *VALID_PILLARS)}")
    s_norm = status.strip().lower() if status and status.strip().lower() != "all" else None
    if s_norm and s_norm not in VALID_STATUSES:
        raise ValueError(f"Invalid status {status!r}; expected one of {VALID_STATUSES}")
    port_norm = portability.strip().lower() if portability and portability.strip().lower() != "all" else None
    if port_norm and port_norm not in VALID_PORTABILITIES:
        raise ValueError(f"Invalid portability {portability!r}; expected one of {VALID_PORTABILITIES}")

    dirs, entries = _load_all_entries(parhelion_home=parhelion_home, auto_seed=auto_seed)
    filtered = [
        e for e in entries
        if (p_norm == "all" or e["pillar"] == p_norm)
        and (s_norm is None or e["status"] == s_norm)
        and (port_norm is None or e["portability"] == port_norm)
    ]
    filtered.sort(key=lambda e: (e["status"] == "anti-pattern", -float(e["confidence"]), -int(e["wins"]), e["id"]))
    return {
        "ok": True,
        "parhelion_home": str(dirs["home"]),
        "pillar": p_norm,
        "count": len(filtered),
        "entries": filtered,
    }


def get_kb_entry(
    entry_id: str,
    parhelion_home: Path | None = None,
    auto_seed: bool = True,
) -> dict[str, Any] | None:
    """Fetch full entry metadata and associated .h / .md document content."""
    q = entry_id.strip().lower()
    if not q:
        return None
    _, entries = _load_all_entries(parhelion_home=parhelion_home, auto_seed=auto_seed)
    match = next((e for e in entries if e["id"].lower() == q), None)
    if match is None:
        match = next((e for e in entries if e["id"].lower().startswith(q)), None)
    if match is None:
        match = next((e for e in entries if q in e["title"].lower()), None)
    if match is None:
        return None
    out = dict(match)
    abs_p = out.get("abs_doc_path")
    out["content"] = Path(abs_p).read_text(encoding="utf-8", errors="replace") if abs_p and Path(abs_p).is_file() else ""
    return out


def _score_entry_for_query(
    entry: dict[str, Any],
    *,
    q_bottlenecks: list[str],
    q_regime: str | None,
    q_dtype: str | None,
    q_tokens: list[str],
) -> tuple[float, float, list[str]]:
    """Return `(final_score, overlap_score, reasons)`."""
    e_bottlenecks = set(_normalize_tag_list(entry.get("bottlenecks")))
    e_tags = set(_normalize_tag_list(entry.get("tags")))
    all_tags = e_bottlenecks | e_tags | {str(entry.get("category") or "").lower()}
    e_regimes = {str(r).upper() for r in (entry.get("regimes") or [])}
    e_dtypes = {_normalize_dtype(d) for d in (entry.get("dtypes") or [])}
    text_blob = " ".join(
        [
            str(entry.get("id") or ""),
            str(entry.get("title") or ""),
            str(entry.get("summary") or ""),
            " ".join(all_tags),
            str(entry.get("problem") or ""),
        ]
    ).lower()

    reasons: list[str] = []
    b_score = 0.0
    if q_bottlenecks:
        hits = 0.0
        for qb in q_bottlenecks:
            if qb in e_bottlenecks:
                hits += 1.0
                reasons.append(f"bottleneck:{qb}")
            elif qb in all_tags or qb in text_blob:
                hits += 0.65
                reasons.append(f"tag:{qb}")
        if hits <= 0.0 and not q_tokens:
            return 0.0, 0.0, []
        b_score = 2.5 * (hits / len(q_bottlenecks))

    r_score = 0.0
    if q_regime:
        if q_regime in e_regimes:
            r_score = 1.0
            reasons.append(f"regime:{q_regime}")

    d_score = 0.0
    if q_dtype:
        if q_dtype in e_dtypes:
            d_score = 0.8
            reasons.append(f"dtype:{q_dtype}")

    t_score = 0.0
    if q_tokens:
        t_hits = sum(1 for tok in q_tokens if tok in text_blob)
        if t_hits > 0:
            t_score = 1.6 * (t_hits / len(q_tokens))
            reasons.append(f"text:{t_hits}/{len(q_tokens)}")
        elif not q_bottlenecks:
            return 0.0, 0.0, []

    has_any_filter = bool(q_bottlenecks or q_regime or q_dtype or q_tokens)
    if has_any_filter:
        overlap = b_score + r_score + d_score + t_score
        if q_bottlenecks and b_score <= 0.0 and t_score <= 0.0:
            return 0.0, 0.0, []
        if overlap <= 0.0:
            return 0.0, 0.0, []
    else:
        overlap = 1.0
        reasons.append("default-rank")

    is_anti = entry["status"] == "anti-pattern" or entry["portability"] == "anti-pattern"
    if is_anti:
        final_score = round(overlap * (1.0 + 0.12 * int(entry.get("fails", 0))), 4)
        return final_score, round(overlap, 4), reasons

    status = entry.get("status", "unverified")
    status_boost = 1.25 if status == "verified" else (0.95 if status == "unverified" else 0.2)
    pillar = entry.get("pillar", "community")
    pillar_boost = 1.08 if pillar == "breakthrough" else (1.06 if pillar == "snippets" else 1.0)
    conf = float(entry.get("confidence", 0.5))
    final_score = round(overlap * conf * status_boost * pillar_boost, 4)
    return final_score, round(overlap, 4), reasons


def query_kb(
    bottlenecks: list[str] | str | None = None,
    regime: str | None = None,
    dtype: str | None = None,
    pillar: str | None = None,
    text: str = "",
    include_anti_patterns: bool = True,
    limit: int = 8,
    parhelion_home: Path | None = None,
    auto_seed: bool = True,
) -> dict[str, Any]:
    """Query all 3 Triad KB pillars by hardware symptom, regime, dtype, and text."""
    p_norm = pillar.strip().lower() if pillar and pillar.strip().lower() != "all" else None
    if p_norm and p_norm not in VALID_PILLARS:
        raise ValueError(f"Invalid pillar {pillar!r}; expected one of {('all', *VALID_PILLARS)}")

    q_bottlenecks = _normalize_tag_list(bottlenecks)
    q_regime = regime.strip().upper() if regime and regime.strip() else None
    q_dtype = _normalize_dtype(dtype) if dtype and dtype.strip() else None
    q_tokens = [tok for tok in re.split(r"[\s,;/]+", str(text or "").strip().lower()) if tok]
    lim = max(1, int(limit))

    dirs, entries = _load_all_entries(parhelion_home=parhelion_home, auto_seed=auto_seed)
    pillar_counts = {p: 0 for p in VALID_PILLARS}
    for e in entries:
        pillar_counts[e["pillar"]] = pillar_counts.get(e["pillar"], 0) + 1

    scored_results: list[dict[str, Any]] = []
    scored_anti: list[dict[str, Any]] = []

    for entry in entries:
        if p_norm and entry["pillar"] != p_norm:
            continue
        is_anti = entry["status"] == "anti-pattern" or entry["portability"] == "anti-pattern"
        if is_anti and not include_anti_patterns:
            continue
        score, overlap, reasons = _score_entry_for_query(
            entry,
            q_bottlenecks=q_bottlenecks,
            q_regime=q_regime,
            q_dtype=q_dtype,
            q_tokens=q_tokens,
        )
        if score <= 0.0:
            continue
        item = dict(entry)
        item["query_score"] = score
        item["overlap_score"] = overlap
        item["matched_reasons"] = reasons
        if is_anti:
            scored_anti.append(item)
        else:
            scored_results.append(item)

    scored_results.sort(
        key=lambda x: (-float(x["query_score"]), -float(x["confidence"]), x["status"] != "verified", x["id"])
    )
    scored_anti.sort(
        key=lambda x: (-float(x["query_score"]), -int(x.get("fails", 0)), x["id"])
    )

    results = scored_results[:lim]
    anti_patterns = scored_anti[:lim] if include_anti_patterns else []

    return {
        "ok": True,
        "parhelion_home": str(dirs["home"]),
        "query": {
            "bottlenecks": q_bottlenecks,
            "regime": q_regime,
            "dtype": q_dtype,
            "pillar": p_norm or "all",
            "text": text,
            "include_anti_patterns": bool(include_anti_patterns),
            "limit": lim,
        },
        "total_scanned": len(entries),
        "pillar_counts": pillar_counts,
        "result_count": len(results),
        "anti_pattern_count": len(anti_patterns),
        "results": results,
        "anti_patterns": anti_patterns,
    }


def format_prompt_cache_digest(query_result: dict[str, Any], max_chars: int = 2400) -> str:
    """Format a compact prompt-cache-safe Markdown KB digest for the first user message."""
    budget = max(120, int(max_chars))
    q_meta = query_result.get("query") or {}
    tags: list[str] = []
    if q_meta.get("bottlenecks"):
        tags.append("bottlenecks=" + ",".join(q_meta["bottlenecks"]))
    if q_meta.get("regime"):
        tags.append(f"regime={q_meta['regime']}")
    if q_meta.get("dtype"):
        tags.append(f"dtype={q_meta['dtype']}")
    ctx = f" ({'; '.join(tags)})" if tags else ""

    lines: list[str] = [f"### Parhelion Triad KB Digest{ctx}"]

    anti_list = list(query_result.get("anti_patterns") or [])
    if anti_list:
        lines.append("#### DO-NOT-REPEAT Warnings (Anti-Patterns)")
        for ap in anti_list[:4]:
            summary = str(ap.get("summary") or "").replace("\n", " ").strip()
            if len(summary) > 150:
                summary = summary[:147].rstrip() + "..."
            lines.append(
                f"- **[ANTI-PATTERN]** `{ap['id']}` (fails={ap.get('fails', 0)}): {ap.get('title')} — {summary}"
            )

    res_list = list(query_result.get("results") or [])
    if res_list:
        lines.append("#### Verified & Ranked KernelC Guidance")
        for it in res_list:
            summary = str(it.get("summary") or "").replace("\n", " ").strip()
            if len(summary) > 160:
                summary = summary[:157].rstrip() + "..."
            rel = it.get("related_snippets") or []
            rel_txt = f" | snippets: {','.join(rel[:2])}" if rel else ""
            doc_txt = f" | doc: `{it.get('doc_path')}`" if it.get("doc_path") else ""
            lines.append(
                f"- `[{it.get('pillar')}|{it.get('status')}|conf={float(it.get('confidence', 0.0)):.2f}|{it.get('portability')}]` "
                f"`{it['id']}` — **{it.get('title')}**: {summary}{rel_txt}{doc_txt}"
            )
    elif not anti_list:
        lines.append("_No matching Triad KB entries._")

    out_lines: list[str] = []
    for ln in lines:
        cand = "\n".join([*out_lines, ln])
        if len(cand) <= budget:
            out_lines.append(ln)
        else:
            break

    text_out = "\n".join(out_lines)
    if len(text_out) < len("\n".join(lines)):
        suffix = "\n...(truncated)"
        if len(text_out) + len(suffix) <= budget:
            text_out += suffix
        elif budget > len(suffix):
            text_out = text_out[: budget - len(suffix)].rstrip() + suffix
    if len(text_out) > budget:
        text_out = text_out[:budget]
    return text_out


def _slugify_id(prefix: str, title: str, sha: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-")
    if not slug:
        slug = sha[:8].lower()
    return f"{prefix}-{slug}"


def _render_snippet_stub(entry_id: str, title: str, summary: str, ev: dict[str, Any]) -> str:
    return (
        f"#pragma once\n"
        f"// {entry_id}.h\n"
        f"// Title: {title}\n"
        f"// Summary: {summary}\n"
        f"// Evidence: problem={ev['problem']} sha={ev['sha']} verdict={ev['verdict']}\n\n"
        f"#include \"kernel_operator.h\"\n\n"
        f"namespace ParhelionSnippets {{\n"
        f"// Verified building block template for {entry_id}\n"
        f"}}  // namespace ParhelionSnippets\n"
    )


def _render_community_markdown(entry: dict[str, Any], body: str = "") -> str:
    b_tags = ", ".join(entry.get("bottlenecks") or [])
    r_tags = ", ".join(entry.get("regimes") or [])
    d_tags = ", ".join(entry.get("dtypes") or [])
    rel = ", ".join(entry.get("related_snippets") or [])

    ev_rows = ""
    if entry.get("evidence"):
        rows = []
        for ev in entry["evidence"]:
            metric_bits = []
            if "delta_raw" in ev:
                metric_bits.append(f"delta_raw={ev['delta_raw']:+.2f}")
            if "delta_us" in ev:
                metric_bits.append(f"delta_us={ev['delta_us']:+.2f}")
            if "speedup" in ev:
                metric_bits.append(f"speedup={ev['speedup']:.2f}x")
            if ev.get("note"):
                metric_bits.append(str(ev["note"]))
            rows.append(
                f"| `{ev.get('problem', '')}` | `{str(ev.get('sha', ''))[:7]}` | `{ev.get('verdict', '')}` | {'; '.join(metric_bits)} |"
            )
        ev_rows = (
            "\n## Evidence Ledger\n"
            "| Problem | SHA | Verdict | Detail |\n"
            "|---|---|---|---|\n"
            + "\n".join(rows)
            + "\n"
        )

    body_txt = body.strip() if body.strip() else entry.get("summary", "")
    rel_block = f"\n## Related Snippets\n- {rel}\n" if rel else ""
    return (
        f"---\n"
        f"id: {entry['id']}\n"
        f"pillar: {entry['pillar']}\n"
        f"portability: {entry['portability']}\n"
        f"status: {entry['status']}\n"
        f"confidence: {entry['confidence']}\n"
        f"bottlenecks: [{b_tags}]\n"
        f"regimes: [{r_tags}]\n"
        f"dtypes: [{d_tags}]\n"
        f"---\n\n"
        f"# {entry['title']}\n\n"
        f"{body_txt}\n"
        f"{rel_block}"
        f"{ev_rows}"
    )


def _new_event_id() -> str:
    ts_part = utc_now().strftime("%Y%m%dT%H%M%SZ")
    return f"KBEV-{ts_part}-{uuid.uuid4().hex[:6]}"


def _append_evolution_event(lock_path: Path, target_path: Path, event: dict[str, Any]) -> None:
    append_jsonl_locked(lock_path, target_path, event)


def _snapshot_for_audit(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        k: copy.deepcopy(v)
        for k, v in entry.items()
        if k != "abs_doc_path"
    }


def record_kb_entry(
    pillar: str,
    payload: dict[str, Any],
    parhelion_home: Path | None = None,
) -> dict[str, Any]:
    """Create a new evidence-gated snippets or community entry and log to kb-evolution.jsonl."""
    p_norm = (pillar or "").strip().lower()
    if p_norm not in WRITABLE_PILLARS:
        raise ValueError(f"record_kb_entry supports {WRITABLE_PILLARS}, got {pillar!r}.")
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict.")

    title = str(payload.get("title") or "").strip()
    if not title:
        raise ValueError("KB entry requires non-empty 'title'.")
    summary = str(payload.get("summary") or payload.get("description") or "").strip()
    if not summary:
        raise ValueError("KB entry requires non-empty 'summary'.")

    # Strict Evidence Gate
    raw_ev = payload.get("evidence")
    ev_list: list[dict[str, Any]] = []
    if isinstance(raw_ev, list):
        if not raw_ev:
            raise ValueError("Evidence list sdkot be empty.")
        ev_list = [validate_evidence(item) for item in raw_ev]
    elif isinstance(raw_ev, dict):
        ev_list = [validate_evidence(raw_ev)]
    elif any(k in payload for k in ("problem", "sha", "verdict")):
        ev_list = [
            validate_evidence(
                {
                    "workspace": payload.get("workspace", ""),
                    "problem": payload.get("problem"),
                    "sha": payload.get("sha"),
                    "verdict": payload.get("verdict"),
                    "delta_raw": payload.get("delta_raw"),
                    "delta_us": payload.get("delta_us"),
                    "speedup": payload.get("speedup"),
                    "note": payload.get("note", ""),
                }
            )
        ]
    else:
        raise ValueError(
            "Unbacked KB write rejected: 'evidence' dict/list (with problem, sha, verdict, metric) is required."
        )

    wins = sum(1 for e in ev_list if e["verdict"] == "win")
    fails = sum(1 for e in ev_list if e["verdict"] in ("fail", "anti-pattern"))
    has_anti_verdict = any(e["verdict"] == "anti-pattern" for e in ev_list)

    port_req = str(payload.get("portability") or "").strip().lower()
    if port_req and port_req not in VALID_PORTABILITIES:
        raise ValueError(f"Invalid portability {port_req!r}; expected one of {VALID_PORTABILITIES}")

    req_status = str(payload.get("status") or "").strip().lower()
    if req_status and req_status not in VALID_STATUSES:
        raise ValueError(f"Invalid status {req_status!r}; expected one of {VALID_STATUSES}")

    is_anti = has_anti_verdict or port_req == "anti-pattern" or req_status == "anti-pattern" or (fails >= DEMOTE_MIN_FAILS and wins == 0)

    if is_anti:
        portability = "anti-pattern"
        status = "anti-pattern"
    else:
        portability = port_req or ("native-accel" if p_norm == "snippets" else "portable")
        if req_status:
            status = req_status
        elif p_norm == "snippets":
            status = "verified" if wins > 0 else "unverified"
        else:
            promoted_init = any(
                e["verdict"] == "win"
                and (
                    (e.get("delta_raw") is not None and float(e["delta_raw"]) >= PROMOTE_MIN_DELTA_RAW)
                    or (e.get("speedup") is not None and float(e["speedup"]) >= PROMOTE_MIN_SPEEDUP)
                )
                for e in ev_list
            )
            status = "verified" if promoted_init else "unverified"

    prior_alpha = 2.0 if (portability == "native-accel" or (p_norm == "snippets" and status == "verified")) else 1.0
    prior_beta = 1.0
    confidence = compute_confidence(
        wins,
        fails,
        portability=portability,
        status=status,
        prior_alpha=prior_alpha,
        prior_beta=prior_beta,
    )

    dirs = triad_kb_dirs(parhelion_home)
    ensure_triad_kb_seeded(parhelion_home=dirs["home"])
    p_dir = dirs[p_norm]
    idx_path = p_dir / "index.json"

    prefix = "SNP" if p_norm == "snippets" else "COM"
    explicit_id = str(payload.get("id") or "").strip()
    entry_id = explicit_id or _slugify_id(prefix, title, ev_list[0]["sha"])

    bottlenecks = _normalize_tag_list(payload.get("bottlenecks")) or _infer_bottlenecks_from_text(
        f"{title} {summary}",
        str(payload.get("category") or ""),
        _normalize_tag_list(payload.get("tags")),
    )
    regimes = [str(r).strip().upper() for r in (payload.get("regimes") or []) if str(r).strip()] or ["M_MID_UB"]
    dtypes = [_normalize_dtype(d) for d in (payload.get("dtypes") or []) if str(d).strip()] or ["float32", "float16", "bfloat16"]
    tags = [str(t).strip() for t in (payload.get("tags") or []) if str(t).strip()]
    related_snippets = [str(s).strip() for s in (payload.get("related_snippets") or []) if str(s).strip()]
    category = str(payload.get("category") or ("tiling-regime" if p_norm == "snippets" else "vectorization")).strip()

    doc_rel = f"code/{entry_id}.h" if p_norm == "snippets" else f"entries/{entry_id}.md"
    entry_record: dict[str, Any] = {
        "id": entry_id,
        "pillar": p_norm,
        "title": title,
        "summary": summary,
        "category": category,
        "tags": tags,
        "bottlenecks": bottlenecks,
        "regimes": regimes,
        "dtypes": dtypes,
        "portability": portability,
        "status": status,
        "prior_alpha": prior_alpha,
        "prior_beta": prior_beta,
        "confidence": confidence,
        "wins": wins,
        "fails": fails,
        "evidence": ev_list,
        "related_snippets": related_snippets,
        "doc_path": doc_rel,
    }

    def _insert(cur: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        entries = list(cur.get("entries") or [])
        existing_ids = {str(e.get("id") or "").lower() for e in entries if isinstance(e, dict)}
        if explicit_id and explicit_id.lower() in existing_ids:
            raise ValueError(f"KB entry {explicit_id!r} already exists; use record_kb_feedback() to evolve it.")
        if entry_record["id"].lower() in existing_ids:
            entry_record["id"] = f"{entry_id}-{ev_list[0]['sha'][:7].lower()}"
            entry_record["doc_path"] = (
                f"code/{entry_record['id']}.h" if p_norm == "snippets" else f"entries/{entry_record['id']}.md"
            )
        entries.append(entry_record)
        cur["entries"] = entries
        return cur, entry_record

    saved = _with_index_lock(idx_path, _insert)
    doc_abs = p_dir / saved["doc_path"]
    if p_norm == "snippets":
        code_body = str(payload.get("code") or payload.get("content") or "").strip()
        if not code_body:
            code_body = _render_snippet_stub(saved["id"], title, summary, ev_list[0])
        else:
            code_body = code_body + "\n"
        _atomic_write_text(doc_abs, code_body)
    else:
        md_body = str(payload.get("markdown") or payload.get("body") or payload.get("content") or "").strip()
        _atomic_write_text(doc_abs, _render_community_markdown(saved, body=md_body))

    event = {
        "event_id": _new_event_id(),
        "timestamp": utc_now().isoformat(),
        "action": "create",
        "pillar": p_norm,
        "entry_id": saved["id"],
        "before": None,
        "after": _snapshot_for_audit(saved),
        "evidence": ev_list[-1],
        "created_snippet_id": saved["id"] if p_norm == "snippets" else None,
    }
    _append_evolution_event(dirs["home"] / ".kb-evolution.lock", dirs["evolution_log"], event)

    norm = _normalize_entry(saved, p_norm, p_dir)
    return {"ok": True, "entry": norm, "event": event}


def _find_entry_location(
    dirs: dict[str, Path],
    entry_id: str,
) -> tuple[str, Path, int, dict[str, Any]] | None:
    q = entry_id.strip().lower()
    for pillar in VALID_PILLARS:
        p_dir = dirs[pillar]
        idx_path = p_dir / "index.json"
        raw = read_json(idx_path, {"entries": []})
        entries = raw.get("entries") if isinstance(raw, dict) else []
        for idx, cur in enumerate(entries or []):
            if isinstance(cur, dict) and str(cur.get("id") or "").lower() == q:
                return pillar, idx_path, idx, cur
    return None


def record_kb_feedback(
    entry_id: str,
    verdict: str,
    evidence: dict[str, Any],
    promote_snippet_code: str | None = None,
    parhelion_home: Path | None = None,
) -> dict[str, Any]:
    """Record win/fail/anti-pattern feedback on an existing KB entry with auto-promotion/demotion."""
    v_norm = str(verdict or "").strip().lower()
    if v_norm not in VALID_VERDICTS:
        raise ValueError(f"verdict must be one of {VALID_VERDICTS}, got {verdict!r}.")

    ev = validate_evidence(evidence, default_verdict=v_norm)
    dirs = triad_kb_dirs(parhelion_home)
    ensure_triad_kb_seeded(parhelion_home=dirs["home"])

    loc = _find_entry_location(dirs, entry_id)
    if loc is None:
        raise ValueError(f"KB entry {entry_id!r} not found across {VALID_PILLARS}.")
    pillar, idx_path, _, _ = loc
    p_dir = dirs[pillar]

    def _mutate(cur: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        entries = list(cur.get("entries") or [])
        q = entry_id.strip().lower()
        target_idx = next(
            (i for i, e in enumerate(entries) if isinstance(e, dict) and str(e.get("id") or "").lower() == q),
            None,
        )
        if target_idx is None:
            raise ValueError(f"KB entry {entry_id!r} disappeared during update.")

        raw_entry = dict(entries[target_idx])
        norm_before = _normalize_entry(raw_entry, pillar, p_dir)
        before_snap = _snapshot_for_audit(raw_entry)
        # Ensure normalized fields are persisted cleanly on mutation
        merged = dict(norm_before)
        merged.pop("abs_doc_path", None)
        # Preserve original breakthrough metadata if present
        for k, v in raw_entry.items():
            if k not in merged:
                merged[k] = v

        old_status = merged["status"]
        wins = int(merged.get("wins", 0))
        fails = int(merged.get("fails", 0))
        if v_norm == "win":
            wins += 1
        else:
            fails += 1

        ev_hist = list(merged.get("evidence") or [])
        ev_hist.append(ev)

        new_status = old_status
        new_portability = merged["portability"]
        action = "feedback"
        transition = "unchanged"

        delta_raw = ev.get("delta_raw")
        speedup = ev.get("speedup")
        meets_promote = (
            (delta_raw is not None and float(delta_raw) >= PROMOTE_MIN_DELTA_RAW)
            or (speedup is not None and float(speedup) >= PROMOTE_MIN_SPEEDUP)
        )

        if v_norm == "anti-pattern" or (fails >= DEMOTE_MIN_FAILS and wins == 0):
            new_status = "anti-pattern"
            new_portability = "anti-pattern"
            if old_status != "anti-pattern":
                action = "demote"
                transition = f"{old_status}->anti-pattern"
        elif pillar == "community" and old_status == "unverified" and v_norm == "win" and meets_promote:
            new_status = "verified"
            action = "promote"
            transition = "unverified->verified"

        new_conf = compute_confidence(
            wins,
            fails,
            portability=new_portability,
            status=new_status,
            prior_alpha=float(merged.get("prior_alpha", 1.0)),
            prior_beta=float(merged.get("prior_beta", 1.0)),
        )

        merged["wins"] = wins
        merged["fails"] = fails
        merged["evidence"] = ev_hist
        merged["status"] = new_status
        merged["portability"] = new_portability
        merged["confidence"] = new_conf

        entries[target_idx] = merged
        cur["entries"] = entries
        return cur, {
            "before": before_snap,
            "after_raw": merged,
            "action": action,
            "transition": transition,
        }

    mut_res = _with_index_lock(idx_path, _mutate)
    updated_raw = mut_res["after_raw"]

    # Refresh community markdown doc's frontmatter/evidence section cleanly if present
    if pillar == "community":
        doc_abs = _resolve_doc_path(p_dir, str(updated_raw.get("doc_path") or ""), updated_raw["id"], pillar)
        existing_body = ""
        if doc_abs and doc_abs.is_file():
            raw_md = doc_abs.read_text(encoding="utf-8", errors="replace")
            if raw_md.startswith("---\n"):
                parts = raw_md.split("---\n", 2)
                if len(parts) == 3:
                    existing_body = parts[2]
            else:
                existing_body = raw_md
            # Strip prior auto-appended Evidence Ledger before re-rendering
            existing_body = re.split(r"\n## Evidence Ledger\n", existing_body, maxsplit=1)[0]
            existing_body = re.sub(r"^#\s+.*\n+", "", existing_body.lstrip(), count=1).strip()
        target_md = doc_abs or (p_dir / "entries" / f"{updated_raw['id']}.md")
        _atomic_write_text(target_md, _render_community_markdown(updated_raw, body=existing_body))

    created_snippet_id: str | None = None
    if promote_snippet_code and promote_snippet_code.strip() and v_norm == "win":
        snip_base = re.sub(r"^(COM|BT)-", "SNP-", updated_raw["id"])
        if not snip_base.startswith("SNP-"):
            snip_base = f"SNP-{snip_base}"
        existing_snips = {
            str(e.get("id") or "").lower()
            for e in (read_json(dirs["snippets"] / "index.json", {"entries": []}).get("entries") or [])
            if isinstance(e, dict)
        }
        snip_id = snip_base if snip_base.lower() not in existing_snips else f"{snip_base}-{ev['sha'][:7].lower()}"
        snip_res = record_kb_entry(
            "snippets",
            {
                "id": snip_id,
                "title": f"{updated_raw['title']} (Promoted KernelC Snippet)",
                "summary": updated_raw["summary"],
                "category": updated_raw.get("category", "vectorization"),
                "bottlenecks": updated_raw.get("bottlenecks"),
                "regimes": updated_raw.get("regimes"),
                "dtypes": updated_raw.get("dtypes"),
                "portability": "native-accel",
                "code": promote_snippet_code,
                "evidence": ev,
            },
            parhelion_home=dirs["home"],
        )
        created_snippet_id = snip_res["entry"]["id"]

        def _link_snippet(cur: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            entries = list(cur.get("entries") or [])
            for item in entries:
                if isinstance(item, dict) and item.get("id") == updated_raw["id"]:
                    rel = list(item.get("related_snippets") or [])
                    if created_snippet_id not in rel:
                        rel.append(created_snippet_id)
                    item["related_snippets"] = rel
                    return cur, item
            return cur, updated_raw

        updated_raw = _with_index_lock(idx_path, _link_snippet)

    event = {
        "event_id": _new_event_id(),
        "timestamp": utc_now().isoformat(),
        "action": mut_res["action"],
        "transition": mut_res["transition"],
        "pillar": pillar,
        "entry_id": updated_raw["id"],
        "before": mut_res["before"],
        "after": _snapshot_for_audit(updated_raw),
        "evidence": ev,
        "created_snippet_id": created_snippet_id,
    }
    _append_evolution_event(dirs["home"] / ".kb-evolution.lock", dirs["evolution_log"], event)

    return {
        "ok": True,
        "entry": _normalize_entry(updated_raw, pillar, p_dir),
        "action": mut_res["action"],
        "transition": mut_res["transition"],
        "created_snippet_id": created_snippet_id,
        "event": event,
    }


def promote_entry(
    entry_id: str,
    evidence: dict[str, Any],
    promote_snippet_code: str | None = None,
    parhelion_home: Path | None = None,
) -> dict[str, Any]:
    """Evidence-gated helper promoting an entry via verified win feedback."""
    return record_kb_feedback(
        entry_id=entry_id,
        verdict="win",
        evidence=evidence,
        promote_snippet_code=promote_snippet_code,
        parhelion_home=parhelion_home,
    )


def get_kb_evolution_history(
    limit: int = 50,
    parhelion_home: Path | None = None,
) -> dict[str, Any]:
    """Read kb-evolution.jsonl newest-first with revert tracking."""
    dirs = triad_kb_dirs(parhelion_home)
    rows = read_jsonl(dirs["evolution_log"])
    reverted_ids = {
        str(r.get("reverted_event_id") or "")
        for r in rows
        if r.get("action") == "revert" and r.get("reverted_event_id")
    }
    enriched: list[dict[str, Any]] = []
    for row in reversed(rows):
        item = dict(row)
        item["reverted"] = str(item.get("event_id") or "") in reverted_ids
        enriched.append(item)
    lim = max(1, int(limit))
    return {
        "ok": True,
        "log_path": str(dirs["evolution_log"]),
        "total_events": len(enriched),
        "events": enriched[:lim],
    }


def revert_event(event_id: str, parhelion_home: Path | None = None) -> dict[str, Any]:
    """Revert a specific mutation event from kb-evolution.jsonl cleanly."""
    dirs = triad_kb_dirs(parhelion_home)
    rows = read_jsonl(dirs["evolution_log"])
    reverted_ids = {
        str(r.get("reverted_event_id") or "")
        for r in rows
        if r.get("action") == "revert" and r.get("reverted_event_id")
    }
    target = next((r for r in rows if str(r.get("event_id") or "") == event_id.strip()), None)
    if target is None:
        raise ValueError(f"Evolution event {event_id!r} not found.")
    if target.get("action") == "revert":
        raise ValueError(f"Cannot revert a revert event {event_id!r}.")
    if event_id.strip() in reverted_ids:
        raise ValueError(f"Evolution event {event_id!r} was already reverted.")

    pillar = str(target.get("pillar") or "")
    target_entry_id = str(target.get("entry_id") or "")
    if pillar not in VALID_PILLARS:
        raise ValueError(f"Invalid pillar {pillar!r} in evolution event {event_id!r}.")
    p_dir = dirs[pillar]
    idx_path = p_dir / "index.json"

    if target.get("action") == "create":
        def _remove_created(cur: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
            entries = list(cur.get("entries") or [])
            removed = next((e for e in entries if isinstance(e, dict) and e.get("id") == target_entry_id), None)
            cur["entries"] = [e for e in entries if not (isinstance(e, dict) and e.get("id") == target_entry_id)]
            return cur, removed

        removed_entry = _with_index_lock(idx_path, _remove_created)
        if removed_entry and removed_entry.get("doc_path"):
            doc_p = p_dir / str(removed_entry["doc_path"])
            if doc_p.is_file():
                doc_p.unlink()
    else:
        before = target.get("before")
        if not isinstance(before, dict):
            raise ValueError(f"Evolution event {event_id!r} has no 'before' snapshot to restore.")

        def _restore_before(cur: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            entries = list(cur.get("entries") or [])
            replaced = False
            for i, e in enumerate(entries):
                if isinstance(e, dict) and e.get("id") == target_entry_id:
                    entries[i] = copy.deepcopy(before)
                    replaced = True
                    break
            if not replaced:
                entries.append(copy.deepcopy(before))
            cur["entries"] = entries
            return cur, before

        _with_index_lock(idx_path, _restore_before)
        created_snip = target.get("created_snippet_id")
        if created_snip:
            snip_dir = dirs["snippets"]
            snip_idx = snip_dir / "index.json"

            def _remove_snip(cur: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
                entries = list(cur.get("entries") or [])
                rem = next((e for e in entries if isinstance(e, dict) and e.get("id") == created_snip), None)
                cur["entries"] = [e for e in entries if not (isinstance(e, dict) and e.get("id") == created_snip)]
                return cur, rem

            rem_snip = _with_index_lock(snip_idx, _remove_snip)
            if rem_snip and rem_snip.get("doc_path"):
                sp = snip_dir / str(rem_snip["doc_path"])
                if sp.is_file():
                    sp.unlink()

    revert_row = {
        "event_id": _new_event_id(),
        "timestamp": utc_now().isoformat(),
        "action": "revert",
        "pillar": pillar,
        "entry_id": target_entry_id,
        "reverted_event_id": target["event_id"],
        "before": target.get("after"),
        "after": target.get("before"),
    }
    _append_evolution_event(dirs["home"] / ".kb-evolution.lock", dirs["evolution_log"], revert_row)
    return {
        "ok": True,
        "reverted_event_id": target["event_id"],
        "entry_id": target_entry_id,
        "pillar": pillar,
        "event": revert_row,
    }


def rollback_last(parhelion_home: Path | None = None) -> dict[str, Any]:
    """Revert the most recent non-reverted KB mutation event."""
    dirs = triad_kb_dirs(parhelion_home)
    rows = read_jsonl(dirs["evolution_log"])
    reverted_ids = {
        str(r.get("reverted_event_id") or "")
        for r in rows
        if r.get("action") == "revert" and r.get("reverted_event_id")
    }
    for row in reversed(rows):
        ev_id = str(row.get("event_id") or "")
        if row.get("action") != "revert" and ev_id and ev_id not in reverted_ids:
            return revert_event(ev_id, parhelion_home=dirs["home"])
    raise ValueError("No revertible KB evolution events found.")


def project_readonly_skills(
    workspace: Path,
    target_rel: str = ".agents/skills",
    parhelion_home: Path | None = None,
) -> dict[str, Any]:
    """Project canonical read-only (0400) KB skill files into <workspace>/<target_rel>/parhelion-kb/."""
    ws = Path(workspace).expanduser().resolve()
    dirs, entries = _load_all_entries(parhelion_home=parhelion_home, auto_seed=True)
    dest_dir = ws / target_rel / "parhelion-kb"
    snippets_dest = dest_dir / "snippets"
    dest_dir.mkdir(parents=True, exist_ok=True)
    snippets_dest.mkdir(parents=True, exist_ok=True)

    written_files: list[str] = []

    skill_md = (
        "---\n"
        "name: parhelion-kb\n"
        "description: Read-only canonical KernelC Triad Knowledge Base index (Breakthroughs, Snippets, Community & Anti-Patterns).\n"
        "---\n\n"
        "# Parhelion Triad Knowledge Base (`parhelion-kb`)\n\n"
        "- Query dynamically by bottleneck/regime/dtype via `opsb kb query` or MCP `parhelion_kb_query`.\n"
        "- Record new verified patterns or negative anti-patterns only with verifiable `problem`, `sha`, `verdict`, and measurable metrics (`opsb kb record` / `opsb kb feedback`).\n"
        "- Inspect `KB_INDEX.md` and `snippets/*.h` in this directory for read-only verified building blocks.\n"
    )
    skill_path = dest_dir / "SKILL.md"
    _atomic_write_text(skill_path, skill_md, mode=0o400)
    written_files.append(str(skill_path))

    # Build KB_INDEX.md grouped by anti-patterns, snippets, breakthroughs, community
    anti = [e for e in entries if e["status"] == "anti-pattern" or e["portability"] == "anti-pattern"]
    snips = [e for e in entries if e["pillar"] == "snippets" and e not in anti]
    bts = [e for e in entries if e["pillar"] == "breakthrough" and e not in anti]
    coms = [e for e in entries if e["pillar"] == "community" and e not in anti]

    idx_lines = [
        "# Parhelion Triad KB Snapshot Index (Read-Only)",
        "",
        f"- Total entries: `{len(entries)}` (`breakthrough={len(bts)}`, `snippets={len(snips)}`, `community={len(coms)}`, `anti-patterns={len(anti)}`)",
        "",
        "## Anti-Patterns (DO NOT REPEAT)",
    ]
    for e in anti:
        idx_lines.append(f"- `{e['id']}` (fails={e['fails']}): {e['title']} — {e['summary']}")
    idx_lines.extend(["", "## Verified KernelC Snippets"])
    for e in snips:
        idx_lines.append(f"- `{e['id']}` (conf={e['confidence']:.2f}, {e['status']}): {e['title']} (`snippets/{e['id']}.h`)")
        abs_doc = e.get("abs_doc_path")
        if abs_doc and Path(abs_doc).is_file():
            dst_h = snippets_dest / f"{e['id']}.h"
            _atomic_write_text(dst_h, Path(abs_doc).read_text(encoding="utf-8", errors="replace"), mode=0o400)
            written_files.append(str(dst_h))
    idx_lines.extend(["", "## Community Cards"])
    for e in coms:
        idx_lines.append(f"- `{e['id']}` [{e['portability']}|{e['status']}|conf={e['confidence']:.2f}]: {e['title']}")
    if bts:
        idx_lines.extend(["", "## Top Verified Breakthroughs"])
        for e in sorted(bts, key=lambda x: float(x.get("delta_raw", 0.0)), reverse=True)[:15]:
            idx_lines.append(f"- `{e['id']}` ({e.get('problem', '')} {float(e.get('delta_raw', 0.0)):+.2f} pts): {e['title']}")

    kb_idx_path = dest_dir / "KB_INDEX.md"
    _atomic_write_text(kb_idx_path, "\n".join(idx_lines) + "\n", mode=0o400)
    written_files.append(str(kb_idx_path))

    # Symlink .claude/skills -> ../.agents/skills when targeting default .agents/skills
    claude_skills = ws / ".claude" / "skills"
    claude_symlink_status = "unchanged"
    canonical_skills = (ws / target_rel).resolve()
    if not claude_skills.exists() and not claude_skills.is_symlink():
        claude_skills.parent.mkdir(parents=True, exist_ok=True)
        rel_target = os.path.relpath(canonical_skills, claude_skills.parent)
        claude_skills.symlink_to(rel_target)
        claude_symlink_status = f"created->{rel_target}"
    elif claude_skills.is_symlink():
        claude_symlink_status = f"existing->{os.readlink(claude_skills)}"
    elif claude_skills.is_dir():
        sub_link = claude_skills / "parhelion-kb"
        if not sub_link.exists() and not sub_link.is_symlink():
            rel_sub = os.path.relpath(dest_dir, claude_skills)
            sub_link.symlink_to(rel_sub)
            claude_symlink_status = f"linked-sub->{rel_sub}"
        else:
            claude_symlink_status = "existing-dir"

    return {
        "ok": True,
        "workspace": str(ws),
        "parhelion_home": str(dirs["home"]),
        "target_dir": str(dest_dir),
        "file_mode": "0400",
        "written_files": written_files,
        "file_count": len(written_files),
        "claude_skills_symlink": claude_symlink_status,
    }
