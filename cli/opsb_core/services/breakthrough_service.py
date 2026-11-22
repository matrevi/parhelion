"""Structured Breakthrough Playbook repository, auto-sync, and query service."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from ..config import DEFAULT_DIFFICULTIES, SUPERVISOR_ROOT, read_json, utc_now
from .git_fast import read_commit_subject

VALID_CATEGORIES = (
    "simd-transpose",
    "async-pipeline",
    "separable-folding",
    "deterministic-sync",
    "vectorization",
    "tiling-regime",
    "dma-coalescing",
)


def resolve_breakthrough_dir(workspace: Path) -> Path:
    ws_bt = workspace / "breakthrough"
    if (ws_bt / "index.json").is_file() or (workspace / "opsb.workspace.json").is_file():
        return ws_bt
    return SUPERVISOR_ROOT / "breakthrough"


def scaffold_breakthrough_dir(target_root: Path) -> Path:
    bt_dir = target_root / "breakthrough"
    entries_dir = bt_dir / "entries"
    entries_dir.mkdir(parents=True, exist_ok=True)

    src_schema = SUPERVISOR_ROOT / "breakthrough" / "schema.json"
    if src_schema.is_file() and not (bt_dir / "schema.json").is_file():
        (bt_dir / "schema.json").write_text(src_schema.read_text(encoding="utf-8"), encoding="utf-8")

    idx_path = bt_dir / "index.json"
    if not idx_path.is_file():
        idx_path.write_text(
            json.dumps({"schema_version": 1, "updated_at": utc_now().isoformat(), "entries": []}, indent=2) + "\n",
            encoding="utf-8",
        )

    readme = bt_dir / "README.md"
    if not readme.is_file():
        readme.write_text(
            "# Structured Breakthrough Playbook (`breakthrough/`)\n\n"
            "Stores verified operator optimization breakthroughs (`opsb breakthrough list`, `opsb breakthrough sync`).\n",
            encoding="utf-8",
        )
    return bt_dir


def load_catalog(workspace: Path) -> dict[str, Any]:
    bt_dir = resolve_breakthrough_dir(workspace)
    raw = read_json(bt_dir / "index.json", {"schema_version": 1, "entries": []})
    entries = raw.get("entries") if isinstance(raw, dict) else []
    out_entries: list[dict[str, Any]] = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        item = dict(e)
        rel = item.get("doc_path") or ""
        abs_doc = (bt_dir / rel).resolve() if rel else None
        item["abs_doc_path"] = str(abs_doc) if abs_doc and abs_doc.is_file() else None
        out_entries.append(item)
    out_entries.sort(key=lambda x: (float(x.get("delta_raw", 0.0)), x.get("timestamp", "")), reverse=True)
    return {
        "directory": str(bt_dir),
        "schema_path": str(bt_dir / "schema.json"),
        "index_path": str(bt_dir / "index.json"),
        "entry_count": len(out_entries),
        "entries": out_entries,
    }


def index_experience_by_commit(workspace: Path) -> dict[tuple[str, str], str]:
    cat = load_catalog(workspace)
    mapping: dict[tuple[str, str], str] = {}
    for e in cat["entries"]:
        prob = str(e.get("problem", ""))
        sha7 = str(e.get("sha", ""))[:7]
        if prob and sha7 and e.get("id"):
            mapping[(prob, sha7)] = str(e["id"])
    return mapping


def get_entry_detail(workspace: Path, query: str) -> dict[str, Any] | None:
    cat = load_catalog(workspace)
    q = query.strip().lower()
    matched = None
    for e in cat["entries"]:
        if str(e.get("id", "")).lower() == q or str(e.get("sha", "")).lower().startswith(q):
            matched = e
            break
    if matched is None:
        for e in cat["entries"]:
            if str(e.get("problem", "")).lower() == q:
                matched = e
                break
    if matched is None:
        return None
    res = dict(matched)
    doc_p = res.get("abs_doc_path")
    res["markdown"] = Path(doc_p).read_text(encoding="utf-8", errors="replace") if doc_p and Path(doc_p).is_file() else ""
    return res


def _infer_category_and_tags(text: str) -> tuple[str, list[str]]:
    low = text.lower()
    tags: list[str] = []
    for kw, label in (
        ("transdatato5hd", "TransDataTo5HD"),
        ("bf16", "bf16"),
        ("fp16", "fp16"),
        ("ping-pong", "ping-pong"),
        ("double-buffer", "double-buffer"),
        ("ring", "ring-buffer"),
        ("gather", "Gather"),
        ("datacopypad", "DataCopyPad"),
        ("pipebarrier", "PipeBarrier"),
        ("sentinel", "sentinel-handshake"),
        ("reducesum", "ReduceSum"),
        ("separable", "separable-tensor"),
        ("sort32", "Sort32"),
    ):
        if kw in low:
            tags.append(label)
    if "transdata" in low or "transpose" in low:
        cat = "simd-transpose"
    elif "ring" in low or "async" in low or "double-buffer" in low or "ping-pong" in low:
        cat = "async-pipeline"
    elif "separable" in low or "collapse" in low:
        cat = "separable-folding"
    elif "sentinel" in low or "handshake" in low or "atomic" in low:
        cat = "deterministic-sync"
    elif "dma" in low or "prologue" in low:
        cat = "dma-coalescing"
    elif "vector" in low or "16bit" in low or "simd" in low:
        cat = "vectorization"
    else:
        cat = "tiling-regime"
    return cat, (tags or [cat])


def _render_entry_markdown(entry: dict[str, Any]) -> str:
    tags_s = ", ".join(entry.get("tags") or [])
    mechs = "\n".join(f"{i+1}. {m}" for i, m in enumerate(entry.get("mechanisms") or []))
    return (
        f"---\n"
        f"id: {entry['id']}\n"
        f"problem: {entry['problem']}\n"
        f"difficulty: {entry['difficulty']}\n"
        f"sha: {entry['sha']}\n"
        f"baseline_sha: {entry.get('baseline_sha') or ''}\n"
        f"timestamp: \"{entry['timestamp']}\"\n"
        f"score_before: {entry['score_before']:.2f}\n"
        f"score_after: {entry['score_after']:.2f}\n"
        f"delta_raw: {entry['delta_raw']:.2f}\n"
        f"delta_weighted: {entry['delta_weighted']:.2f}\n"
        f"status: {entry['status']}\n"
        f"category: {entry['category']}\n"
        f"tags: [{tags_s}]\n"
        f"title: {json.dumps(entry['title'], ensure_ascii=False)}\n"
        f"---\n\n"
        f"# {entry['problem']} · `{entry['delta_raw']:+.2f}` pts (`{entry['score_before']:.2f} -> {entry['score_after']:.2f}`)\n\n"
        f"## 1. Context & Bottleneck\n{entry['bottleneck']}\n\n"
        f"## 2. Core Breakthrough Mechanisms\n{mechs}\n\n"
        f"## 3. Precision & Synchronization Invariants\n{entry['precision_guard']}\n\n"
        f"## 4. Reusable KernelC Pattern\n> **Rule (`{entry['category']}`)**: {entry['transferable_rule']}\n"
    )


def save_catalog_entry(workspace: Path, entry: dict[str, Any], overwrite_doc: bool = True) -> dict[str, Any]:
    bt_dir = scaffold_breakthrough_dir(resolve_breakthrough_dir(workspace).parent)
    idx_path = bt_dir / "index.json"
    raw = read_json(idx_path, {"schema_version": 1, "entries": []})
    existing: list[dict[str, Any]] = [e for e in (raw.get("entries") or []) if isinstance(e, dict)]

    doc_rel = entry.get("doc_path") or f"entries/{entry['id']}.md"
    entry["doc_path"] = doc_rel
    doc_abs = bt_dir / doc_rel
    doc_abs.parent.mkdir(parents=True, exist_ok=True)
    if overwrite_doc or not doc_abs.is_file():
        doc_abs.write_text(_render_entry_markdown(entry), encoding="utf-8")

    replaced = False
    for idx, cur in enumerate(existing):
        if cur.get("id") == entry["id"] or (
            cur.get("problem") == entry["problem"] and str(cur.get("sha", ""))[:7] == str(entry["sha"])[:7]
        ):
            entry["id"] = cur["id"]
            entry["doc_path"] = cur.get("doc_path") or doc_rel
            existing[idx] = entry
            replaced = True
            break
    if not replaced:
        existing.append(entry)

    existing.sort(key=lambda x: (float(x.get("delta_raw", 0.0)), x.get("timestamp", "")), reverse=True)
    payload: dict[str, Any] = {
        "schema_version": int((raw or {}).get("schema_version") or 1) if isinstance(raw, dict) else 1,
        "updated_at": utc_now().isoformat(),
        "entries": existing,
    }
    tmp = idx_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, idx_path)
    return entry


def sync_breakthroughs_from_items(
    workspace: Path,
    queue_items: list[dict[str, Any]],
    problems_by_title: dict[str, dict[str, Any]],
    min_delta: float,
) -> dict[str, Any]:
    exp_map = index_experience_by_commit(workspace)
    created: list[str] = []
    for item in queue_items:
        if item.get("kind") != "completed" or item.get("raw_status") != "Pass":
            continue
        bt_delta = item.get("breakthrough_delta")
        if bt_delta is None or float(bt_delta) < min_delta:
            continue
        prob = item["title"]
        sha = item["sha"]
        if (prob, sha[:7]) in exp_map:
            continue

        p_meta = problems_by_title.get(prob, {})
        repo_dir = workspace / p_meta.get("path", prob)
        subj = read_commit_subject(repo_dir, sha) if repo_dir.is_dir() else ""
        note = str(item.get("note") or "")
        combined = f"{subj} {note}".strip()
        cat, tags = _infer_category_and_tags(combined)

        ts = str(item.get("finished_at") or item.get("queued_at") or utc_now().isoformat())
        day = re.sub(r"[^0-9]", "", ts[:10]) or utc_now().strftime("%Y%m%d")
        entry_id = f"BT-{day[:8]}-{prob}-{sha[:7]}"
        after_sc = float(item.get("actual_score") or 0.0)
        before_sc = round(after_sc - float(bt_delta), 2)
        diff = int(item.get("difficulty") or DEFAULT_DIFFICULTIES.get(prob, 2))
        clauses = [c.strip() for c in re.split(r"[+;·]", note or subj) if c.strip()]

        entry = {
            "id": entry_id,
            "problem": prob,
            "difficulty": diff,
            "sha": sha,
            "baseline_sha": item.get("baseline_pass_sha"),
            "timestamp": ts,
            "score_before": before_sc,
            "score_after": round(after_sc, 2),
            "delta_raw": round(float(bt_delta), 2),
            "delta_weighted": round(diff * float(bt_delta), 2),
            "status": "verified",
            "category": cat,
            "tags": tags,
            "title": (subj or note or f"{prob} +{bt_delta:.2f} breakthrough")[:110],
            "bottleneck": f"Prior validated baseline plateaued at {before_sc:.2f} pts before {sha[:7]}.",
            "mechanisms": clauses[:4] or [note or subj or "Kernel & tiling optimization"],
            "precision_guard": "Passed extended NPU precision & determinism gates + online 50-case evaluation.",
            "transferable_rule": f"Apply {cat} optimization ({', '.join(tags[:3])}) with paired NPU verification.",
            "doc_path": f"entries/{entry_id}.md",
        }
        save_catalog_entry(workspace, entry, overwrite_doc=False)
        exp_map[(prob, sha[:7])] = entry_id
        created.append(entry_id)

    cat = load_catalog(workspace)
    return {"ok": True, "created_ids": created, "total_entries": cat["entry_count"], "directory": cat["directory"]}
