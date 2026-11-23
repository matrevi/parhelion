"""MCP tool handlers for Triad Knowledge Base (`parhelion_kb_query`, `record`, `feedback`)."""
from __future__ import annotations

import fcntl
import importlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .. import config as _cfg
from ..drivers.base import resolve_parhelion_home
from ..services.breakthrough_service import load_catalog

SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "entry"


def _validate_evidence(
    *, problem: str, sha: str, verdict: str,
    delta_raw: float | None = None, delta_us: float | None = None, note: str = "",
) -> dict[str, Any]:
    if not str(problem or "").strip():
        raise ValueError("Evidence gate rejected write: non-empty 'problem' is required.")
    sha_s = str(sha or "").strip()
    if not SHA_RE.match(sha_s):
        raise ValueError("Evidence gate rejected write: 'sha' must be 7-40 hex characters.")
    v = str(verdict or "").strip().lower()
    if v not in ("win", "fail", "anti-pattern"):
        raise ValueError("Evidence gate rejected write: 'verdict' must be 'win', 'fail', or 'anti-pattern'.")
    note_s = str(note or "").strip()
    if delta_raw is None and delta_us is None and ("profiler" not in note_s.lower()) and ("online" not in note_s.lower()):
        raise ValueError(
            "Evidence gate rejected write: provide at least one measurable metric "
            "('delta_raw', 'delta_us', or 'note' citing 'profiler' / 'online')."
        )
    return {
        "problem": str(problem).strip(), "sha": sha_s, "verdict": v,
        "delta_raw": float(delta_raw) if delta_raw is not None else None,
        "delta_us": float(delta_us) if delta_us is not None else None,
        "note": note_s, "recorded_at": _cfg.utc_now().isoformat(),
    }


def _bayesian_confidence(wins: int, fails: int, *, prior_alpha: float = 1.0, prior_beta: float = 1.0) -> float:
    return round((prior_alpha + wins) / (prior_alpha + prior_beta + wins + fails), 3)


def _fallback_read_index(pillar: str) -> tuple[Path, dict[str, Any]]:
    idx_path = resolve_parhelion_home() / pillar / "index.json"
    raw = _cfg.read_json(idx_path, {"schema_version": 1, "entries": []})
    if not isinstance(raw, dict):
        raw = {"schema_version": 1, "entries": []}
    if not isinstance(raw.get("entries"), list):
        raw["entries"] = []
    return idx_path, raw


def _fallback_write_index(idx_path: Path, payload: dict[str, Any]) -> None:
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    with idx_path.with_suffix(".lock").open("a", encoding="utf-8") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            tmp = idx_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(tmp, idx_path)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _append_kb_audit(event: dict[str, Any]) -> None:
    ph = resolve_parhelion_home()
    _cfg.append_jsonl_locked(ph / ".kb-evolution.lock", ph / "kb-evolution.jsonl", event)


def _load_triad_kb_module() -> Any | None:
    try:
        mod = importlib.import_module("opsb_core.services.triad_kb_service")
    except ImportError:
        return None
    if callable(ensure_fn := getattr(mod, "ensure_triad_kb_seeded", None)):
        try:
            ensure_fn(parhelion_home=resolve_parhelion_home())
        except TypeError:
            ensure_fn()
    return mod


def tool_parhelion_kb_query(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    bottlenecks = [str(x) for x in (args.get("bottlenecks") or []) if str(x).strip()]
    regime, dtype, pillar = args.get("regime"), args.get("dtype"), args.get("pillar")
    text = str(args.get("text") or "")
    include_anti = bool(args.get("include_anti_patterns", True))
    limit = int(args.get("limit") or 8)
    want_digest = bool(args.get("digest", False))

    if (kb_mod := _load_triad_kb_module()) is not None and callable(q_fn := getattr(kb_mod, "query_kb", None)):
        res = q_fn(
            bottlenecks=bottlenecks or None, regime=regime, dtype=dtype,
            pillar=None if pillar in (None, "all") else pillar,
            text=text, include_anti_patterns=include_anti, limit=limit,
        )
        if isinstance(res, dict):
            items = list(res.get("results") if "results" in res else (res.get("entries") or []))
            res["results"] = items
            res["entries"] = items
            res.setdefault("result_count", len(items))
            if want_digest and callable(fmt_fn := getattr(kb_mod, "format_prompt_cache_digest", None)):
                res["digest"] = fmt_fn(res)
            return res

    entries: list[dict[str, Any]] = []
    anti_patterns: list[dict[str, Any]] = []
    pillars_to_scan = ("snippets", "hardware", "community") if pillar in (None, "all") else ((pillar,) if pillar in ("snippets", "hardware", "community") else ())
    for pil in pillars_to_scan:
        _, raw_idx = _fallback_read_index(pil)
        for item in raw_idx["entries"]:
            if not isinstance(item, dict):
                continue
            item_copy = {**item, "pillar": pil}
            if item_copy.get("status") == "anti-pattern" or item_copy.get("portability") == "anti-pattern":
                if include_anti:
                    anti_patterns.append(item_copy)
            else:
                entries.append(item_copy)

    if pillar in (None, "all", "breakthrough"):
        for bt in load_catalog(workspace).get("entries") or []:
            entries.append({
                "id": bt.get("id"), "pillar": "breakthrough", "title": bt.get("title"),
                "summary": bt.get("transferable_rule") or bt.get("bottleneck"),
                "bottlenecks": bt.get("tags") or [bt.get("category")], "regimes": [], "dtypes": [],
                "portability": "native-accel", "status": bt.get("status", "verified"),
                "confidence": 0.9, "doc_path": bt.get("abs_doc_path") or bt.get("doc_path"),
            })

    def _score(ent: dict[str, Any]) -> float:
        blob = json.dumps(ent, ensure_ascii=False).lower()
        score = float(ent.get("confidence") or 0.5)
        if bottlenecks:
            ent_bn = {str(x).lower() for x in (ent.get("bottlenecks") or [])}
            score += sum(1 for b in bottlenecks if b.lower() in ent_bn or b.lower() in blob) * 1.5
        if regime and str(regime).lower() in blob:
            score += 1.0
        if dtype and str(dtype).lower() in blob:
            score += 0.5
        if text and text.lower() in blob:
            score += 1.0
        return score

    entries.sort(key=_score, reverse=True)
    anti_patterns.sort(key=_score, reverse=True)
    top_entries = entries[:limit]
    top_anti = anti_patterns[:limit] if include_anti else []
    out: dict[str, Any] = {
        "entries": top_entries, "results": top_entries, "result_count": len(top_entries),
        "anti_patterns": top_anti, "anti_pattern_count": len(top_anti),
        "total_matched": len(entries), "source": "parhelion_fallback",
    }
    if want_digest:
        dig_lines = [
            "## Parhelion KB Digest",
            *(f"- [ANTI-PATTERN] {ap.get('id')}: {ap.get('summary')}" for ap in top_anti[:2]),
            *(f"- [{e.get('pillar')}] {e.get('id')}: {e.get('summary')}" for e in top_entries[:4]),
        ]
        out["digest"] = "\n".join(dig_lines)
    return out


def tool_parhelion_kb_record(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    pillar = str(args.get("pillar") or "").strip().lower()
    if pillar not in ("snippets", "hardware", "community"):
        raise ValueError("Invalid 'pillar': must be 'snippets', 'hardware', or 'community'.")
    title, summary = str(args.get("title") or "").strip(), str(args.get("summary") or "").strip()
    if not title or not summary:
        raise ValueError("'title' and 'summary' must be non-empty.")

    evidence = _validate_evidence(
        problem=str(args.get("problem") or ""), sha=str(args.get("sha") or ""), verdict=str(args.get("verdict") or ""),
        delta_raw=args.get("delta_raw"), delta_us=args.get("delta_us"), note=str(args.get("note") or ""),
    )
    evidence["workspace"] = str(workspace)
    portability = "anti-pattern" if evidence["verdict"] == "anti-pattern" else str(args.get("portability") or ("native-accel" if pillar in ("snippets", "hardware") else "portable"))
    payload: dict[str, Any] = {
        "title": title, "summary": summary,
        "bottlenecks": [str(x) for x in (args.get("bottlenecks") or []) if str(x).strip()],
        "regimes": [str(x) for x in (args.get("regimes") or []) if str(x).strip()],
        "dtypes": [str(x) for x in (args.get("dtypes") or []) if str(x).strip()],
        "portability": portability, "code": args.get("code"),
        "markdown": args.get("doc_markdown"), "doc_markdown": args.get("doc_markdown"), "evidence": evidence,
    }

    if (kb_mod := _load_triad_kb_module()) is not None and callable(rec_fn := getattr(kb_mod, "record_kb_entry", None)):
        return rec_fn(pillar, payload)

    prefix = "SNP" if pillar == "snippets" else ("HW" if pillar == "hardware" else "COM")
    entry_id = f"{prefix}-{_slugify(title)}"
    ph = resolve_parhelion_home()
    idx_path, raw_idx = _fallback_read_index(pillar)
    wins = 1 if evidence["verdict"] == "win" else 0
    fails = 1 if evidence["verdict"] in ("fail", "anti-pattern") else 0
    if evidence["verdict"] == "anti-pattern" or portability == "anti-pattern":
        status = "anti-pattern"
    elif (pillar == "snippets" and wins > 0) or (wins > 0 and (evidence.get("delta_raw") or 0) >= 1.5):
        status = "verified"
    else:
        status = "unverified"

    doc_rel = f"code/{entry_id}.h" if pillar == "snippets" else f"entries/{entry_id}.md"
    doc_abs = ph / pillar / doc_rel
    doc_abs.parent.mkdir(parents=True, exist_ok=True)
    body = str(args.get("code") or f"// {title}\n// {summary}\n") if pillar == "snippets" else str(args.get("doc_markdown") or f"# {title}\n\n{summary}\n")
    doc_abs.write_text(body, encoding="utf-8")

    entry = {
        "id": entry_id, "pillar": pillar, "title": title, "summary": summary,
        "bottlenecks": payload["bottlenecks"], "regimes": payload["regimes"], "dtypes": payload["dtypes"],
        "portability": portability, "status": status, "confidence": _bayesian_confidence(wins, fails),
        "wins": wins, "fails": fails, "evidence": [evidence], "doc_path": doc_rel,
    }
    raw_idx["entries"] = [e for e in raw_idx["entries"] if e.get("id") != entry_id] + [entry]
    raw_idx["updated_at"] = _cfg.utc_now().isoformat()
    _fallback_write_index(idx_path, raw_idx)
    _append_kb_audit({"ts": _cfg.utc_now().isoformat(), "action": "record", "entry_id": entry_id, "pillar": pillar, "evidence": evidence})
    return {"ok": True, "entry": entry}


def tool_parhelion_kb_feedback(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    entry_id = str(args.get("entry_id") or "").strip()
    if not entry_id:
        raise ValueError("'entry_id' is required for parhelion_kb_feedback")
    evidence = _validate_evidence(
        problem=str(args.get("problem") or ""), sha=str(args.get("sha") or ""), verdict=str(args.get("verdict") or ""),
        delta_raw=args.get("delta_raw"), delta_us=args.get("delta_us"), note=str(args.get("note") or ""),
    )
    evidence["workspace"] = str(workspace)
    promote_code = args.get("promote_snippet_code")

    if (kb_mod := _load_triad_kb_module()) is not None and callable(fb_fn := getattr(kb_mod, "record_kb_feedback", None)):
        return fb_fn(entry_id, evidence["verdict"], evidence, promote_snippet_code=promote_code)

    for pil in ("hardware", "community", "snippets"):
        idx_path, raw_idx = _fallback_read_index(pil)
        for idx, ent in enumerate(raw_idx["entries"]):
            if not isinstance(ent, dict) or ent.get("id") != entry_id:
                continue
            wins = int(ent.get("wins") or 0) + (1 if evidence["verdict"] == "win" else 0)
            fails = int(ent.get("fails") or 0) + (0 if evidence["verdict"] == "win" else 1)
            ent.update({"wins": wins, "fails": fails, "confidence": _bayesian_confidence(wins, fails)})
            ent.setdefault("evidence", []).append(evidence)
            if evidence["verdict"] == "anti-pattern" or (fails >= 3 and wins == 0):
                ent["status"] = "anti-pattern"
            elif ent.get("status") == "unverified" and evidence["verdict"] == "win" and (evidence.get("delta_raw") or 0) >= 1.5:
                ent["status"] = "verified"
            raw_idx["entries"][idx] = ent
            raw_idx["updated_at"] = _cfg.utc_now().isoformat()
            _fallback_write_index(idx_path, raw_idx)
            _append_kb_audit({"ts": _cfg.utc_now().isoformat(), "action": "feedback", "entry_id": entry_id, "pillar": pil, "evidence": evidence})
            return {"ok": True, "entry": ent}
    raise ValueError(f"KB entry '{entry_id}' not found")
