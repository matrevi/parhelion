"""Evidence-gated Triad KB mutations, Bayesian confidence scoring, and auto-promote/demote."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..config import read_json, utc_now
from .query import _normalize_entry
from .seeding import ensure_triad_kb_seeded
from .store import (
    DEMOTE_MIN_FAILS, PROMOTE_MIN_DELTA_RAW, PROMOTE_MIN_SPEEDUP, VALID_PILLARS,
    VALID_PORTABILITIES, VALID_STATUSES, VALID_VERDICTS, WRITABLE_PILLARS,
    _METRIC_NOTE_RE, _SHA_RE, _append_evolution_event, _atomic_write_text,
    _beta_posterior_mean, _find_entry_location, _infer_bottlenecks_from_text, _is_num,
    _new_event_id, _normalize_dtype, _normalize_tag_list, _resolve_doc_path,
    _snapshot_for_audit, _with_index_lock, triad_kb_dirs,
)


def compute_confidence(
    wins: int, fails: int, *, portability: str = "portable", status: str = "unverified",
    prior_alpha: float | None = None, prior_beta: float | None = None,
) -> float:
    """Bayesian Beta posterior mean confidence in [0.0, 1.0]."""
    return _beta_posterior_mean(wins, fails, portability=portability, status=status, prior_alpha=prior_alpha, prior_beta=prior_beta)


def validate_evidence(evidence: Any, default_verdict: str | None = None) -> dict[str, Any]:
    """Validate mandatory hardware/online evidence gate; raises ValueError on unbacked claims."""
    if not isinstance(evidence, dict) or not evidence:
        raise ValueError("Evidence payload must be a non-empty dict with problem, sha (7-40 hex), verdict, and measurable metric.")
    problem = str(evidence.get("problem") or "").strip()
    if not problem:
        raise ValueError("Evidence requires non-empty 'problem' name.")
    sha = str(evidence.get("sha") or "").strip()
    if not _SHA_RE.match(sha):
        raise ValueError(f"Evidence 'sha' must be 7-40 hex characters, got {sha!r}.")

    verdict_raw = str(evidence.get("verdict") or default_verdict or "").strip().lower()
    if verdict_raw not in VALID_VERDICTS:
        raise ValueError(f"Evidence 'verdict' must be one of {VALID_VERDICTS}, got {verdict_raw!r}.")
    if default_verdict and evidence.get("verdict") and str(default_verdict).strip().lower() != verdict_raw:
        raise ValueError(f"Conflicting verdict {default_verdict!r} vs evidence.verdict {verdict_raw!r}.")

    has_delta_raw = _is_num(evidence.get("delta_raw"))
    has_delta_us = _is_num(evidence.get("delta_us"))
    has_speedup = _is_num(evidence.get("speedup")) and float(evidence["speedup"]) > 0.0
    note = str(evidence.get("note") or "").strip()
    if not (has_delta_raw or has_delta_us or has_speedup or bool(_METRIC_NOTE_RE.search(note))):
        raise ValueError(
            "Unbacked KB write rejected: evidence must include 'delta_raw', 'delta_us', 'speedup', "
            "or a 'note' citing 'profiler' or 'online'."
        )

    cleaned: dict[str, Any] = {
        "workspace": str(evidence.get("workspace") or "").strip(), "problem": problem, "sha": sha,
        "verdict": verdict_raw, "recorded_at": str(evidence.get("recorded_at") or utc_now().isoformat()),
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


def _slugify_id(prefix: str, title: str, sha: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-") or sha[:8].lower()
    return f"{prefix}-{slug}"


def _render_snippet_stub(entry_id: str, title: str, summary: str, ev: dict[str, Any]) -> str:
    return (
        f"#pragma once\n// {entry_id}.h\n// Title: {title}\n// Summary: {summary}\n"
        f"// Evidence: problem={ev['problem']} sha={ev['sha']} verdict={ev['verdict']}\n\n"
        f"#include \"kernel_operator.h\"\n\nnamespace ParhelionSnippets {{\n"
        f"// Verified building block template for {entry_id}\n}}  // namespace ParhelionSnippets\n"
    )


def _render_community_markdown(entry: dict[str, Any], body: str = "") -> str:
    b_tags, r_tags, d_tags = ", ".join(entry.get("bottlenecks") or []), ", ".join(entry.get("regimes") or []), ", ".join(entry.get("dtypes") or [])
    rel = ", ".join(entry.get("related_snippets") or [])
    ev_rows = ""
    if entry.get("evidence"):
        rows = []
        for ev in entry["evidence"]:
            bits = [
                *([f"delta_raw={ev['delta_raw']:+.2f}"] if "delta_raw" in ev else []),
                *([f"delta_us={ev['delta_us']:+.2f}"] if "delta_us" in ev else []),
                *([f"speedup={ev['speedup']:.2f}x"] if "speedup" in ev else []),
                *([str(ev["note"])] if ev.get("note") else []),
            ]
            rows.append(f"| `{ev.get('problem', '')}` | `{str(ev.get('sha', ''))[:7]}` | `{ev.get('verdict', '')}` | {'; '.join(bits)} |")
        ev_rows = "\n## Evidence Ledger\n| Problem | SHA | Verdict | Detail |\n|---|---|---|---|\n" + "\n".join(rows) + "\n"
    body_txt = body.strip() or entry.get("summary", "")
    rel_block = f"\n## Related Snippets\n- {rel}\n" if rel else ""
    legacy_line = f"legacy_id: {entry['legacy_id']}\n" if entry.get("legacy_id") else ""
    return (
        f"---\nid: {entry['id']}\n{legacy_line}pillar: {entry.get('pillar', 'community')}\nportability: {entry.get('portability', 'portable')}\n"
        f"status: {entry.get('status', 'unverified')}\nconfidence: {entry.get('confidence', 0.5)}\n"
        f"bottlenecks: [{b_tags}]\nregimes: [{r_tags}]\ndtypes: [{d_tags}]\n---\n\n"
        f"# {entry['title']}\n\n{body_txt}\n{rel_block}{ev_rows}"
    )


def _extract_evidence_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_ev = payload.get("evidence")
    if isinstance(raw_ev, list):
        if not raw_ev:
            raise ValueError("Evidence list sdkot be empty.")
        return [validate_evidence(item) for item in raw_ev]
    if isinstance(raw_ev, dict):
        return [validate_evidence(raw_ev)]
    if any(k in payload for k in ("problem", "sha", "verdict")):
        return [validate_evidence({k: payload.get(k) for k in ("workspace", "problem", "sha", "verdict", "delta_raw", "delta_us", "speedup", "note")})]
    raise ValueError("Unbacked KB write rejected: 'evidence' dict/list (with problem, sha, verdict, metric) is required.")


def _meets_promote(ev: dict[str, Any]) -> bool:
    return (
        (ev.get("delta_raw") is not None and float(ev["delta_raw"]) >= PROMOTE_MIN_DELTA_RAW)
        or (ev.get("speedup") is not None and float(ev["speedup"]) >= PROMOTE_MIN_SPEEDUP)
    )


def record_kb_entry(pillar: str, payload: dict[str, Any], parhelion_home: Path | None = None) -> dict[str, Any]:
    """Create a new evidence-gated snippets or community entry and log to kb-evolution.jsonl."""
    p_norm = (pillar or "").strip().lower()
    if p_norm not in WRITABLE_PILLARS:
        raise ValueError(f"record_kb_entry supports {WRITABLE_PILLARS}, got {pillar!r}.")
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict.")
    title = str(payload.get("title") or "").strip()
    summary = str(payload.get("summary") or payload.get("description") or "").strip()
    if not title or not summary:
        raise ValueError("KB entry requires non-empty 'title' and 'summary'.")

    ev_list = _extract_evidence_list(payload)
    wins = sum(1 for e in ev_list if e["verdict"] == "win")
    fails = sum(1 for e in ev_list if e["verdict"] in ("fail", "anti-pattern"))
    port_req = str(payload.get("portability") or "").strip().lower()
    if port_req and port_req not in VALID_PORTABILITIES:
        raise ValueError(f"Invalid portability {port_req!r}; expected one of {VALID_PORTABILITIES}")
    req_status = str(payload.get("status") or "").strip().lower()
    if req_status and req_status not in VALID_STATUSES:
        raise ValueError(f"Invalid status {req_status!r}; expected one of {VALID_STATUSES}")

    is_anti = any(e["verdict"] == "anti-pattern" for e in ev_list) or port_req == "anti-pattern" or req_status == "anti-pattern" or (fails >= DEMOTE_MIN_FAILS and wins == 0)
    if is_anti:
        portability, status = "anti-pattern", "anti-pattern"
    else:
        portability = port_req or ("native-accel" if p_norm in ("snippets", "hardware") else "portable")
        if req_status:
            status = req_status
        elif p_norm == "snippets":
            status = "verified" if wins > 0 else "unverified"
        else:
            status = "verified" if any(e["verdict"] == "win" and _meets_promote(e) for e in ev_list) else "unverified"

    prior_alpha = 2.0 if (portability == "native-accel" or (p_norm in ("snippets", "hardware") and status == "verified")) else 1.0
    prior_beta = 1.0
    confidence = compute_confidence(wins, fails, portability=portability, status=status, prior_alpha=prior_alpha, prior_beta=prior_beta)

    dirs = triad_kb_dirs(parhelion_home)
    ensure_triad_kb_seeded(parhelion_home=dirs["home"])
    p_dir, idx_path = dirs[p_norm], dirs[p_norm] / "index.json"

    prefix = "SNP" if p_norm == "snippets" else ("HW" if p_norm == "hardware" else "COM")
    explicit_id = str(payload.get("id") or "").strip()
    entry_id = explicit_id or _slugify_id(prefix, title, ev_list[0]["sha"])
    bottlenecks = _normalize_tag_list(payload.get("bottlenecks")) or _infer_bottlenecks_from_text(
        f"{title} {summary}", str(payload.get("category") or ""), _normalize_tag_list(payload.get("tags")),
    )
    default_cat = "tiling-regime" if p_norm == "snippets" else ("hardware-law" if p_norm == "hardware" else "vectorization")
    entry_record: dict[str, Any] = {
        "id": entry_id, "pillar": p_norm, "title": title, "summary": summary,
        "category": str(payload.get("category") or default_cat).strip(),
        "tags": [str(t).strip() for t in (payload.get("tags") or []) if str(t).strip()],
        "bottlenecks": bottlenecks,
        "regimes": [str(r).strip().upper() for r in (payload.get("regimes") or []) if str(r).strip()] or ["M_MID_UB"],
        "dtypes": [_normalize_dtype(d) for d in (payload.get("dtypes") or []) if str(d).strip()] or ["float32", "float16", "bfloat16"],
        "portability": portability, "status": status, "prior_alpha": prior_alpha, "prior_beta": prior_beta,
        "confidence": confidence, "wins": wins, "fails": fails, "evidence": ev_list,
        "related_snippets": [str(s).strip() for s in (payload.get("related_snippets") or []) if str(s).strip()],
        "doc_path": f"code/{entry_id}.h" if p_norm == "snippets" else f"entries/{entry_id}.md",
    }

    def _insert(cur: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        entries = list(cur.get("entries") or [])
        existing_ids = {str(e.get("id") or "").lower() for e in entries if isinstance(e, dict)}
        if explicit_id and explicit_id.lower() in existing_ids:
            raise ValueError(f"KB entry {explicit_id!r} already exists; use record_kb_feedback() to evolve it.")
        if entry_record["id"].lower() in existing_ids:
            entry_record["id"] = f"{entry_id}-{ev_list[0]['sha'][:7].lower()}"
            entry_record["doc_path"] = f"code/{entry_record['id']}.h" if p_norm == "snippets" else f"entries/{entry_record['id']}.md"
        entries.append(entry_record)
        cur["entries"] = entries
        return cur, entry_record

    saved = _with_index_lock(idx_path, _insert)
    doc_abs = p_dir / saved["doc_path"]
    if p_norm == "snippets":
        code_body = str(payload.get("code") or payload.get("content") or "").strip()
        _atomic_write_text(doc_abs, (code_body + "\n") if code_body else _render_snippet_stub(saved["id"], title, summary, ev_list[0]))
    else:
        md_body = str(payload.get("markdown") or payload.get("doc_markdown") or payload.get("body") or payload.get("content") or "").strip()
        _atomic_write_text(doc_abs, _render_community_markdown(saved, body=md_body))

    event = {
        "event_id": _new_event_id(), "timestamp": utc_now().isoformat(), "action": "create",
        "pillar": p_norm, "entry_id": saved["id"], "before": None, "after": _snapshot_for_audit(saved),
        "evidence": ev_list[-1], "created_snippet_id": saved["id"] if p_norm == "snippets" else None,
    }
    _append_evolution_event(dirs["home"] / ".kb-evolution.lock", dirs["evolution_log"], event)
    return {"ok": True, "entry": _normalize_entry(saved, p_norm, p_dir), "event": event}


def _refresh_markdown_doc(p_dir: Path, entry: dict[str, Any], pillar: str) -> None:
    doc_abs = _resolve_doc_path(p_dir, str(entry.get("doc_path") or ""), entry["id"], pillar)
    existing_body = ""
    if doc_abs and doc_abs.is_file():
        raw_md = doc_abs.read_text(encoding="utf-8", errors="replace")
        parts = raw_md.split("---\n", 2) if raw_md.startswith("---\n") else []
        existing_body = parts[2] if len(parts) == 3 else raw_md
        existing_body = re.split(r"\n## Evidence Ledger\n", existing_body, maxsplit=1)[0]
        existing_body = re.sub(r"^#\s+.*\n+", "", existing_body.lstrip(), count=1).strip()
    _atomic_write_text(doc_abs or (p_dir / "entries" / f"{entry['id']}.md"), _render_community_markdown(entry, body=existing_body))


def record_kb_feedback(
    entry_id: str, verdict: str, evidence: dict[str, Any],
    promote_snippet_code: str | None = None, parhelion_home: Path | None = None,
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
    pillar, idx_path, _, matched_raw = loc
    canonical_id = str(matched_raw.get("id") or entry_id)
    p_dir = dirs[pillar]

    def _mutate(cur: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        entries = list(cur.get("entries") or [])
        q = canonical_id.strip().lower()
        target_idx = next(
            (
                i for i, e in enumerate(entries)
                if isinstance(e, dict)
                and (str(e.get("id") or "").lower() == q or str(e.get("legacy_id") or "").lower() == entry_id.strip().lower())
            ),
            None,
        )
        if target_idx is None:
            raise ValueError(f"KB entry {entry_id!r} disappeared during update.")

        raw_entry = dict(entries[target_idx])
        before_snap = _snapshot_for_audit(raw_entry)
        merged = {k: v for k, v in _normalize_entry(raw_entry, pillar, p_dir).items() if k != "abs_doc_path"}
        for k, v in raw_entry.items():
            merged.setdefault(k, v)

        old_status = merged["status"]
        wins = int(merged.get("wins", 0)) + (1 if v_norm == "win" else 0)
        fails = int(merged.get("fails", 0)) + (0 if v_norm == "win" else 1)
        new_status, new_port, action, transition = old_status, merged["portability"], "feedback", "unchanged"
        if v_norm == "anti-pattern" or (fails >= DEMOTE_MIN_FAILS and wins == 0):
            new_status, new_port = "anti-pattern", "anti-pattern"
            if old_status != "anti-pattern":
                action, transition = "demote", f"{old_status}->anti-pattern"
        elif pillar in ("community", "hardware") and old_status == "unverified" and v_norm == "win" and _meets_promote(ev):
            new_status, action, transition = "verified", "promote", "unverified->verified"

        merged.update({
            "wins": wins, "fails": fails, "evidence": [*list(merged.get("evidence") or []), ev],
            "status": new_status, "portability": new_port,
            "confidence": compute_confidence(
                wins, fails, portability=new_port, status=new_status,
                prior_alpha=float(merged.get("prior_alpha", 1.0)), prior_beta=float(merged.get("prior_beta", 1.0)),
            ),
        })
        entries[target_idx] = merged
        cur["entries"] = entries
        return cur, {"before": before_snap, "after_raw": merged, "action": action, "transition": transition}

    mut_res = _with_index_lock(idx_path, _mutate)
    updated_raw = mut_res["after_raw"]
    if pillar in ("community", "hardware"):
        _refresh_markdown_doc(p_dir, updated_raw, pillar)

    created_snippet_id: str | None = None
    if promote_snippet_code and promote_snippet_code.strip() and v_norm == "win":
        snip_base = re.sub(r"^(COM|HW|BT)-", "SNP-", updated_raw["id"])
        snip_base = snip_base if snip_base.startswith("SNP-") else f"SNP-{snip_base}"
        existing_snips = {str(e.get("id") or "").lower() for e in (read_json(dirs["snippets"] / "index.json", {"entries": []}).get("entries") or []) if isinstance(e, dict)}
        snip_id = snip_base if snip_base.lower() not in existing_snips else f"{snip_base}-{ev['sha'][:7].lower()}"
        snip_res = record_kb_entry("snippets", {
            "id": snip_id, "title": f"{updated_raw['title']} (Promoted KernelC Snippet)",
            "summary": updated_raw["summary"], "category": updated_raw.get("category", "vectorization"),
            "bottlenecks": updated_raw.get("bottlenecks"), "regimes": updated_raw.get("regimes"),
            "dtypes": updated_raw.get("dtypes"), "portability": "native-accel",
            "code": promote_snippet_code, "evidence": ev,
        }, parhelion_home=dirs["home"])
        created_snippet_id = snip_res["entry"]["id"]

        def _link_snippet(cur: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            for item in cur.get("entries") or []:
                if isinstance(item, dict) and item.get("id") == updated_raw["id"]:
                    rel = list(item.get("related_snippets") or [])
                    if created_snippet_id not in rel:
                        rel.append(created_snippet_id)
                    item["related_snippets"] = rel
                    return cur, item
            return cur, updated_raw

        updated_raw = _with_index_lock(idx_path, _link_snippet)

    event = {
        "event_id": _new_event_id(), "timestamp": utc_now().isoformat(),
        "action": mut_res["action"], "transition": mut_res["transition"],
        "pillar": pillar, "entry_id": updated_raw["id"],
        "before": mut_res["before"], "after": _snapshot_for_audit(updated_raw),
        "evidence": ev, "created_snippet_id": created_snippet_id,
    }
    _append_evolution_event(dirs["home"] / ".kb-evolution.lock", dirs["evolution_log"], event)
    return {
        "ok": True, "entry": _normalize_entry(updated_raw, pillar, p_dir),
        "action": mut_res["action"], "transition": mut_res["transition"],
        "created_snippet_id": created_snippet_id, "event": event,
    }


def promote_entry(
    entry_id: str, evidence: dict[str, Any], promote_snippet_code: str | None = None, parhelion_home: Path | None = None,
) -> dict[str, Any]:
    """Evidence-gated helper promoting an entry via verified win feedback."""
    return record_kb_feedback(entry_id=entry_id, verdict="win", evidence=evidence, promote_snippet_code=promote_snippet_code, parhelion_home=parhelion_home)
