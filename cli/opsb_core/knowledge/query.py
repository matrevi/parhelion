"""3-pillar entry normalization, querying, listing, inspection, and prompt-cache digest formatting."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..config import read_json, utc_now
from .seeding import ensure_triad_kb_seeded
from .store import (
    VALID_PILLARS, VALID_PORTABILITIES, VALID_STATUSES,
    _beta_posterior_mean, _infer_bottlenecks_from_text, _infer_dtypes_from_text,
    _infer_regimes_from_text, _is_num, _normalize_dtype, _normalize_tag_list,
    _resolve_doc_path, triad_kb_dirs,
)


def _normalize_entry(raw: dict[str, Any], pillar: str, pillar_dir: Path) -> dict[str, Any]:
    entry_id = str(raw.get("id") or "").strip()
    title = str(raw.get("title") or entry_id).strip()
    raw_tags = [str(t).strip() for t in (raw.get("tags") or []) if str(t).strip()]
    category = str(raw.get("category") or "").strip()

    if pillar == "breakthrough":
        summary = str(raw.get("summary") or raw.get("transferable_rule") or raw.get("bottleneck") or title).strip()
        text_blob = " ".join([title, summary, str(raw.get("bottleneck") or ""), " ".join(str(m) for m in (raw.get("mechanisms") or [])), str(raw.get("transferable_rule") or "")])
        raw_status = str(raw.get("status") or "verified").strip().lower()
        status = raw_status if raw_status in VALID_STATUSES else "unverified"
        portability = str(raw.get("portability") or ("anti-pattern" if status == "anti-pattern" else "native-accel"))
    else:
        summary = str(raw.get("summary") or title).strip()
        text_blob = f"{title} {summary}"
        raw_status = str(raw.get("status") or "unverified").strip().lower()
        status = raw_status if raw_status in VALID_STATUSES else "unverified"
        default_port = "native-accel" if pillar in ("snippets", "hardware") else "portable"
        port_raw = str(raw.get("portability") or default_port).strip().lower()
        portability = port_raw if port_raw in VALID_PORTABILITIES else default_port

    bottlenecks = _normalize_tag_list(raw.get("bottlenecks")) or _infer_bottlenecks_from_text(text_blob, category, raw_tags)
    regimes = [str(r).strip().upper() for r in (raw.get("regimes") or []) if str(r).strip()] or _infer_regimes_from_text(text_blob)
    dtypes = [_normalize_dtype(d) for d in (raw.get("dtypes") or []) if str(d).strip()] or _infer_dtypes_from_text(text_blob, raw_tags)

    evidence = list(raw.get("evidence") or [])
    if pillar == "breakthrough" and not evidence and raw.get("sha"):
        evidence = [{
            "workspace": "", "problem": str(raw.get("problem") or ""), "sha": str(raw.get("sha") or ""),
            "delta_raw": float(raw.get("delta_raw") or 0.0),
            "verdict": "win" if status == "verified" else "fail",
            "note": f"online breakthrough entry ({raw.get('score_before', 0)} -> {raw.get('score_after', 0)})",
            "recorded_at": str(raw.get("timestamp") or utc_now().isoformat()),
        }]

    if "wins" in raw and "fails" in raw:
        wins, fails = max(0, int(raw["wins"])), max(0, int(raw["fails"]))
    elif pillar == "breakthrough":
        wins, fails = (1 if status == "verified" else 0), 0
    else:
        wins = sum(1 for ev in evidence if isinstance(ev, dict) and str(ev.get("verdict") or "").lower() == "win")
        fails = sum(1 for ev in evidence if isinstance(ev, dict) and str(ev.get("verdict") or "").lower() in ("fail", "anti-pattern"))

    prior_alpha, prior_beta = raw.get("prior_alpha"), raw.get("prior_beta")
    if prior_alpha is None or prior_beta is None:
        prior_alpha, prior_beta = (2.0, 1.0) if (portability == "native-accel" or status == "verified") else (1.0, 1.0)

    confidence = (
        round(float(raw["confidence"]), 3)
        if _is_num(raw.get("confidence")) and "wins" in raw and "fails" in raw
        else _beta_posterior_mean(wins, fails, portability=portability, status=status, prior_alpha=float(prior_alpha), prior_beta=float(prior_beta))
    )

    doc_path = str(raw.get("doc_path") or "").strip()
    abs_doc = _resolve_doc_path(pillar_dir, doc_path, entry_id, pillar)
    if not doc_path:
        doc_path = f"code/{entry_id}.h" if pillar == "snippets" else f"entries/{entry_id}.md"

    default_cat = "tiling-regime" if pillar == "snippets" else ("hardware-law" if pillar == "hardware" else "vectorization")
    normalized: dict[str, Any] = {
        "id": entry_id, "pillar": pillar, "title": title, "summary": summary,
        "category": category or default_cat,
        "tags": raw_tags, "bottlenecks": bottlenecks, "regimes": regimes, "dtypes": dtypes,
        "portability": portability, "status": status, "prior_alpha": float(prior_alpha), "prior_beta": float(prior_beta),
        "confidence": confidence, "wins": wins, "fails": fails, "evidence": evidence,
        "related_snippets": [str(s) for s in (raw.get("related_snippets") or []) if str(s).strip()],
        "doc_path": doc_path, "abs_doc_path": str(abs_doc) if abs_doc else None,
    }
    if pillar == "bugs":
        normalized.update(lesson=raw.get("lesson"),fix=raw.get("fix"),workspace=raw.get("workspace"))
    if raw.get("legacy_id"):
        normalized["legacy_id"] = str(raw["legacy_id"]).strip()
    for key in ("problem", "difficulty", "sha", "baseline_sha", "timestamp", "score_before", "score_after", "delta_raw", "delta_weighted"):
        if key in raw:
            normalized[key] = raw[key]
    return normalized


def _load_all_entries(parhelion_home: Path | None = None, *, auto_seed: bool = True) -> tuple[dict[str, Path], list[dict[str, Any]]]:
    dirs = triad_kb_dirs(parhelion_home)
    if auto_seed and any(not (dirs[p] / "index.json").is_file() for p in ("snippets", "hardware", "community")):
        ensure_triad_kb_seeded(parhelion_home=dirs["home"])
    all_entries: list[dict[str, Any]] = []
    for pillar in VALID_PILLARS:
        p_dir = dirs[pillar]
        raw_idx = read_json(p_dir / "index.json", {"entries": []})
        for raw_item in ((raw_idx.get("entries") if isinstance(raw_idx, dict) else []) or []):
            if isinstance(raw_item, dict) and raw_item.get("id"):
                all_entries.append(_normalize_entry(raw_item, pillar, p_dir))
    return dirs, all_entries


def list_kb_entries(
    pillar: str = "all", status: str | None = None, portability: str | None = None,
    parhelion_home: Path | None = None, auto_seed: bool = True,
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
    return {"ok": True, "parhelion_home": str(dirs["home"]), "pillar": p_norm, "count": len(filtered), "entries": filtered}


def get_kb_entry(entry_id: str, parhelion_home: Path | None = None, auto_seed: bool = True) -> dict[str, Any] | None:
    """Fetch full entry metadata and associated .h / .md document content."""
    q = entry_id.strip().lower()
    if not q:
        return None
    _, entries = _load_all_entries(parhelion_home=parhelion_home, auto_seed=auto_seed)
    match = (
        next((e for e in entries if e["id"].lower() == q), None)
        or next((e for e in entries if str(e.get("legacy_id") or "").lower() == q), None)
        or next((e for e in entries if e["id"].lower().startswith(q) or str(e.get("legacy_id") or "").lower().startswith(q)), None)
        or next((e for e in entries if q in e["title"].lower()), None)
    )
    if match is None:
        return None
    out = dict(match)
    abs_p = out.get("abs_doc_path")
    out["content"] = Path(abs_p).read_text(encoding="utf-8", errors="replace") if abs_p and Path(abs_p).is_file() else ""
    return out


def _score_entry_for_query(
    entry: dict[str, Any], *, q_bottlenecks: list[str], q_regime: str | None, q_dtype: str | None, q_tokens: list[str],
) -> tuple[float, float, list[str]]:
    e_bn = set(_normalize_tag_list(entry.get("bottlenecks")))
    all_tags = e_bn | set(_normalize_tag_list(entry.get("tags"))) | {str(entry.get("category") or "").lower()}
    e_regimes = {str(r).upper() for r in (entry.get("regimes") or [])}
    e_dtypes = {_normalize_dtype(d) for d in (entry.get("dtypes") or [])}
    text_blob = " ".join([
        str(entry.get("id") or ""), str(entry.get("title") or ""), str(entry.get("summary") or ""),
        " ".join(all_tags), str(entry.get("problem") or ""),
    ]).lower()

    reasons: list[str] = []
    b_score = 0.0
    if q_bottlenecks:
        hits = 0.0
        for qb in q_bottlenecks:
            if qb in e_bn:
                hits += 1.0
                reasons.append(f"bottleneck:{qb}")
            elif qb in all_tags or qb in text_blob:
                hits += 0.65
                reasons.append(f"tag:{qb}")
        if hits <= 0.0 and not q_tokens:
            return 0.0, 0.0, []
        b_score = 2.5 * (hits / len(q_bottlenecks))

    r_score = 1.0 if (q_regime and q_regime in e_regimes) else 0.0
    if r_score:
        reasons.append(f"regime:{q_regime}")
    d_score = 0.8 if (q_dtype and q_dtype in e_dtypes) else 0.0
    if d_score:
        reasons.append(f"dtype:{q_dtype}")

    t_score = 0.0
    if q_tokens:
        t_hits = sum(1 for tok in q_tokens if tok in text_blob)
        if t_hits > 0:
            t_score = 1.6 * (t_hits / len(q_tokens))
            reasons.append(f"text:{t_hits}/{len(q_tokens)}")
        elif not q_bottlenecks:
            return 0.0, 0.0, []

    if q_bottlenecks or q_regime or q_dtype or q_tokens:
        overlap = b_score + r_score + d_score + t_score
        if (q_bottlenecks and b_score <= 0.0 and t_score <= 0.0) or overlap <= 0.0:
            return 0.0, 0.0, []
    else:
        overlap = 1.0
        reasons.append("default-rank")

    if entry["status"] == "anti-pattern" or entry["portability"] == "anti-pattern":
        return round(overlap * (1.0 + 0.12 * int(entry.get("fails", 0))), 4), round(overlap, 4), reasons

    status = entry.get("status", "unverified")
    status_boost = 1.25 if status == "verified" else (0.95 if status == "unverified" else 0.2)
    pillar = entry.get("pillar", "community")
    pillar_boost = (
        1.08 if pillar == "breakthrough"
        else (1.07 if pillar == "hardware" else (1.06 if pillar == "snippets" else 1.0))
    )
    return round(overlap * float(entry.get("confidence", 0.5)) * status_boost * pillar_boost, 4), round(overlap, 4), reasons


def query_kb(
    bottlenecks: list[str] | str | None = None, regime: str | None = None, dtype: str | None = None,
    pillar: str | None = None, text: str = "", include_anti_patterns: bool = True, limit: int = 8,
    parhelion_home: Path | None = None, auto_seed: bool = True,
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
            entry, q_bottlenecks=q_bottlenecks, q_regime=q_regime, q_dtype=q_dtype, q_tokens=q_tokens,
        )
        if score <= 0.0:
            continue
        item = {**entry, "query_score": score, "overlap_score": overlap, "matched_reasons": reasons}
        (scored_anti if is_anti else scored_results).append(item)

    scored_results.sort(key=lambda x: (-float(x["query_score"]), -float(x["confidence"]), x["status"] != "verified", x["id"]))
    scored_anti.sort(key=lambda x: (-float(x["query_score"]), -int(x.get("fails", 0)), x["id"]))
    results = scored_results[:lim]
    anti_patterns = scored_anti[:lim] if include_anti_patterns else []

    return {
        "ok": True,
        "parhelion_home": str(dirs["home"]),
        "query": {
            "bottlenecks": q_bottlenecks, "regime": q_regime, "dtype": q_dtype,
            "pillar": p_norm or "all", "text": text, "include_anti_patterns": bool(include_anti_patterns), "limit": lim,
        },
        "total_scanned": len(entries),
        "pillar_counts": pillar_counts,
        "result_count": len(results),
        "anti_pattern_count": len(anti_patterns),
        "results": results,
        "entries": results,
        "anti_patterns": anti_patterns,
    }


def format_prompt_cache_digest(query_result: dict[str, Any], max_chars: int = 2400) -> str:
    """Format a compact prompt-cache-safe Markdown KB digest for the first user message."""
    budget = max(120, int(max_chars))
    q_meta = query_result.get("query") or {}
    tags = [
        *(["bottlenecks=" + ",".join(q_meta["bottlenecks"])] if q_meta.get("bottlenecks") else []),
        *([f"regime={q_meta['regime']}"] if q_meta.get("regime") else []),
        *([f"dtype={q_meta['dtype']}"] if q_meta.get("dtype") else []),
    ]
    ctx = f" ({'; '.join(tags)})" if tags else ""
    lines: list[str] = [f"### Parhelion 5-Pillar KB Digest{ctx}"]

    anti_list = list(query_result.get("anti_patterns") or [])
    if anti_list:
        lines.append("#### DO-NOT-REPEAT Warnings (Anti-Patterns)")
        for ap in anti_list[:4]:
            summary = str(ap.get("summary") or "").replace("\n", " ").strip()
            summary = (summary[:147].rstrip() + "...") if len(summary) > 150 else summary
            lines.append(f"- **[ANTI-PATTERN]** `{ap['id']}` (fails={ap.get('fails', 0)}): {ap.get('title')} — {summary}")

    res_list = list(query_result.get("results") or query_result.get("entries") or [])
    if res_list:
        lines.append("#### Verified & Ranked KernelC Guidance")
        for it in res_list:
            summary = str(it.get("summary") or "").replace("\n", " ").strip()
            summary = (summary[:157].rstrip() + "...") if len(summary) > 160 else summary
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
        if len("\n".join([*out_lines, ln])) <= budget:
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
    return text_out[:budget]
