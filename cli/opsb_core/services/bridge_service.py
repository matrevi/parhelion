"""Breakthrough hand-off bridge: subscriber registry, idempotent delivery, receipts.

The bridge turns `breakthrough/entries/*.md` into per-agent work notes and collects
the agents' summaries back into `breakthrough/index.json#summaries`.  It is agent
agnostic by construction:

  bridge/
  ├── agents.json          # subscriber registry (this is the only file humans edit)
  ├── inbox/<agent_id>/    # one immutable .md per (entry content sha, agent)
  ├── outbox/<agent_id>/   # agents drop their <=5 line receipt here
  └── ledger.jsonl         # append-only idempotency ledger

Transport is deliberately dumb (`file` | `exec` | `http`): DSH, Codex, Antigravity,
any editor watcher, or a plain shell script can all participate without the bridge
needing to know anything about them.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from ..config import SUPERVISOR_ROOT, append_jsonl_locked, read_json, read_jsonl, utc_now
from .breakthrough_service import resolve_breakthrough_dir

BRIDGE_SCHEMA_VERSION = 1
VALID_KINDS = ("dsh", "codex", "antigravity", "cli", "script")
VALID_MODES = ("file", "exec", "http")
STATE_ORDER = ("pending", "pulled", "acked")

# Chinese / English labels for `category`, so `scope.tags` can be written in either language.
CATEGORY_LABELS: dict[str, tuple[str, str]] = {
    "simd-transpose": ("SIMD 转置向量化", "SIMD Transpose"),
    "async-pipeline": ("双缓冲异步流水", "Async Pipeline"),
    "separable-folding": ("可分离维度折叠", "Separable Folding"),
    "deterministic-sync": ("跨核确定性同步", "Deterministic Sync"),
    "vectorization": ("向量化指令改造", "Vectorization"),
    "tiling-regime": ("多模态分核切分", "Tiling Regime"),
    "dma-coalescing": ("DMA 合并搬运", "DMA Coalescing"),
}

RECEIPT_INSTRUCTION = (
    "请用 ≤5 行总结：① 可迁移机制 ② 适用条件/失效条件 ③ 对你自己当前题目的下一步；"
    "写入 `ops-supervisor/bridge/outbox/{agent_id}/{entry_id}.md`"
)

_US_PERF_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:µs|us)?\s*(?:->|=>|→)\s*(\d+(?:\.\d+)?)\s*(?:µs|us)"
)


# --------------------------------------------------------------------------------------
# Paths & scaffolding
# --------------------------------------------------------------------------------------


def resolve_bridge_dir(workspace: Path | None = None, explicit: str | None = None) -> Path:
    """Bridge state lives next to the supervisor that owns it (env/flag override wins)."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("OPSB_BRIDGE_DIR")
    if env:
        return Path(env).expanduser().resolve()
    root_bridge = SUPERVISOR_ROOT / "bridge"
    if workspace is not None:
        ws_bridge = Path(workspace) / "bridge"
        if not root_bridge.is_dir() and ws_bridge.is_dir():
            return ws_bridge
    return root_bridge


def scaffold_bridge_dir(bridge_dir: Path) -> Path:
    bridge_dir = Path(bridge_dir)
    for sub in ("inbox", "outbox"):
        (bridge_dir / sub).mkdir(parents=True, exist_ok=True)
    agents_path = bridge_dir / "agents.json"
    if not agents_path.is_file():
        agents_path.write_text(
            json.dumps(
                {"schema_version": BRIDGE_SCHEMA_VERSION, "updated_at": utc_now().isoformat(), "agents": []},
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    (bridge_dir / "ledger.jsonl").touch(exist_ok=True)
    return bridge_dir


def bridge_readme_path() -> Path:
    return SUPERVISOR_ROOT / "bridge" / "README.md"


# --------------------------------------------------------------------------------------
# Subscribers
# --------------------------------------------------------------------------------------


def _safe_id(value: str) -> str:
    """Reject path traversal in registry / CLI supplied identifiers."""
    s = str(value or "").strip()
    if not s or s in (".", ".."):
        raise ValueError("empty agent/entry id")
    if "/" in s or "\\" in s or "\x00" in s:
        raise ValueError(f"unsafe identifier: {value!r}")
    return s


def load_agents(workspace: Path | None = None, bridge_dir: Path | None = None) -> list[dict[str, Any]]:
    """Load `agents.json`, tolerating both `{"agents": [...]}` and a bare list."""
    bd = Path(bridge_dir) if bridge_dir else resolve_bridge_dir(workspace)
    raw = read_json(bd / "agents.json", {"agents": []})
    rows: Any = []
    if isinstance(raw, dict):
        rows = raw.get("agents") or raw.get("subscribers") or []
    elif isinstance(raw, list):
        rows = raw
    out: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return out
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        agent_id = str(row.get("agent_id") or row.get("id") or "").strip()
        if not agent_id:
            continue
        scope = row.get("scope") if isinstance(row.get("scope"), dict) else {}
        transport = row.get("transport") if isinstance(row.get("transport"), dict) else {}
        min_delta = row.get("min_delta")
        try:
            min_delta_f = float(min_delta) if min_delta is not None else 0.0
        except (TypeError, ValueError):
            min_delta_f = 0.0
        out.append(
            {
                "agent_id": agent_id,
                "display": str(row.get("display") or agent_id),
                "kind": str(row.get("kind") or "cli").strip().lower(),
                "enabled": bool(row.get("enabled", True)),
                "min_delta": min_delta_f,
                "scope": {
                    "paths": [str(p) for p in (scope.get("paths") or []) if str(p).strip()],
                    "tags": [str(t) for t in (scope.get("tags") or []) if str(t).strip()],
                },
                "transport": {
                    "mode": str(transport.get("mode") or "file").strip().lower(),
                    "exec": [str(x) for x in (transport.get("exec") or []) if str(x).strip()],
                    "url": str(transport.get("url") or ""),
                    "headers": transport.get("headers") if isinstance(transport.get("headers"), dict) else {},
                },
                "order": idx,
            }
        )
    return out


def find_agent(agents: Iterable[dict[str, Any]], agent_id: str) -> dict[str, Any] | None:
    target = str(agent_id or "").strip()
    for agent in agents:
        if agent["agent_id"] == target:
            return agent
    return None


# --------------------------------------------------------------------------------------
# Breakthrough entries
# --------------------------------------------------------------------------------------


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Minimal YAML front-matter reader (scalars, JSON-quoted strings, inline lists)."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    block = text[3:end]
    out: dict[str, Any] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key = key.strip()
        raw = raw.strip()
        if not key:
            continue
        if raw.startswith("[") and raw.endswith("]"):
            inner = raw[1:-1].strip()
            out[key] = [p.strip().strip('"').strip("'") for p in inner.split(",") if p.strip()]
            continue
        if raw.startswith('"') and raw.endswith('"'):
            try:
                out[key] = json.loads(raw)
                continue
            except Exception:
                out[key] = raw.strip('"')
                continue
        if raw == "":
            out[key] = ""
            continue
        try:
            out[key] = json.loads(raw)
        except Exception:
            out[key] = raw
    return out


def _problem_from_entry_id(entry_id: str, known: Iterable[str]) -> str:
    for prob in known:
        if prob and prob in entry_id:
            return prob
    parts = entry_id.split("-")
    return parts[2] if len(parts) > 2 else ""


def load_entry_docs(workspace: Path) -> list[dict[str, Any]]:
    """Union of `breakthrough/index.json` entries and raw `breakthrough/entries/*.md` files.

    Structured fields come from the index; the content hash always comes from the
    markdown bytes so that an edited document is treated as a new delivery.
    """
    bt_dir = resolve_breakthrough_dir(workspace)
    raw_index = read_json(bt_dir / "index.json", {"entries": []}) or {}
    index_entries: list[dict[str, Any]] = [
        dict(e) for e in (raw_index.get("entries") or []) if isinstance(e, dict) and e.get("id")
    ]
    known_problems = [str(e.get("problem") or "") for e in index_entries if e.get("problem")]
    by_id: dict[str, dict[str, Any]] = {str(e["id"]): e for e in index_entries}

    out: list[dict[str, Any]] = []
    seen_doc_rels: set[str] = set()
    entries_dir = bt_dir / "entries"
    if entries_dir.is_dir():
        for path in sorted(entries_dir.glob("*.md")):
            try:
                data = path.read_bytes()
            except OSError:
                continue
            text = data.decode("utf-8", errors="replace")
            front = _parse_frontmatter(text)
            entry_id = str(front.get("id") or path.stem)
            item = dict(by_id.get(entry_id) or {})
            item["id"] = entry_id
            for key, value in front.items():
                if value in (None, "", []):
                    continue
                item[key] = value
            item.setdefault("problem", _problem_from_entry_id(entry_id, known_problems))
            item.setdefault("status", "candidate")
            item.setdefault("category", "tiling-regime")
            item.setdefault("tags", [])
            item.setdefault("mechanisms", [])
            rel = f"entries/{path.name}"
            item["doc_path"] = rel
            item["_doc_rel"] = rel
            item["_abs_doc"] = str(path)
            item["_sha"] = _sha_bytes(data)
            item["_text"] = text
            item["_doc_missing"] = False
            seen_doc_rels.add(rel)
            out.append(item)

    for entry_id, item in by_id.items():
        rel = str(item.get("doc_path") or "")
        if rel in seen_doc_rels:
            continue
        doc_abs = bt_dir / rel if rel else None
        if doc_abs is not None and doc_abs.is_file():
            try:
                data = doc_abs.read_bytes()
            except OSError:
                data = b""
            item = dict(item)
            item["_doc_rel"] = rel
            item["_abs_doc"] = str(doc_abs)
            item["_sha"] = _sha_bytes(data)
            item["_text"] = data.decode("utf-8", errors="replace")
            item["_doc_missing"] = False
        else:
            # Index-only record (doc not yet rendered): hash the canonical fields.
            item = dict(item)
            canonical = json.dumps(
                {k: item.get(k) for k in ("id", "problem", "sha", "score_before", "score_after", "title")},
                sort_keys=True,
                ensure_ascii=False,
            )
            item["_doc_rel"] = rel
            item["_abs_doc"] = None
            item["_sha"] = _sha_bytes(canonical.encode("utf-8"))
            item["_text"] = ""
            item["_doc_missing"] = True
        out.append(item)
    return out


def _problem_paths(workspace: Path) -> dict[str, str]:
    raw = read_json(Path(workspace) / "bench" / "problems.json", []) or []
    mapping: dict[str, str] = {}
    if isinstance(raw, list):
        for row in raw:
            if isinstance(row, dict) and row.get("title"):
                mapping[str(row["title"])] = str(row.get("path") or f"bench/{row['title']}")
    return mapping


# --------------------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------------------


def _norm_path(value: str) -> str:
    return str(value or "").strip().replace("\\", "/").rstrip("/").lower()


def _path_hit(prefix: str, candidates: Iterable[str]) -> bool:
    p = _norm_path(prefix)
    if not p:
        return False
    if p in ("*", "**", "all"):
        return True
    for candidate in candidates:
        c = _norm_path(candidate)
        if not c:
            continue
        if c == p or c.startswith(p + "/") or p.startswith(c + "/"):
            return True
    return False


def entry_tagset(entry: dict[str, Any]) -> set[str]:
    tags = {str(t).strip().lower() for t in (entry.get("tags") or []) if str(t).strip()}
    category = str(entry.get("category") or "").strip().lower()
    if category:
        tags.add(category)
        zh, en = CATEGORY_LABELS.get(category, ("", ""))
        if zh:
            tags.add(zh.lower())
        if en:
            tags.add(en.lower())
    problem = str(entry.get("problem") or "").strip().lower()
    if problem:
        tags.add(problem)
    return tags


def _tag_hit(agent_tags: Iterable[str], entry_tags: set[str]) -> bool:
    for tag in agent_tags:
        t = str(tag).strip().lower()
        if not t:
            continue
        if t in ("*", "**", "all"):
            return True
        if t in entry_tags:
            return True
    return False


def _notify_ids(entry: dict[str, Any]) -> set[str]:
    notify = entry.get("notify")
    if isinstance(notify, str):
        return {p.strip().lower() for p in notify.split(",") if p.strip()}
    if isinstance(notify, (list, tuple, set)):
        return {str(p).strip().lower() for p in notify if str(p).strip()}
    return set()


def entry_path_candidates(entry: dict[str, Any], workspace: Path, problem_paths: dict[str, str]) -> list[str]:
    problem = str(entry.get("problem") or "").strip()
    candidates = [problem, f"bench/{problem}" if problem else "", f"breakthrough/{entry.get('doc_path') or ''}"]
    mapped = problem_paths.get(problem)
    if mapped:
        candidates.extend([mapped, str(Path(workspace) / mapped)])
    return [c for c in candidates if c]


def route_entry(
    entry: dict[str, Any],
    agents: list[dict[str, Any]],
    workspace: Path,
    problem_paths: dict[str, str],
) -> list[str]:
    """Return the agent ids that must receive this entry.

    Rules, in priority order: explicit `notify:` in the entry, `scope.paths`
    prefix, `scope.tags` intersection (tags include category + localized label +
    the operator name).  `min_delta` filters implicit subscriptions only.
    """
    notify = _notify_ids(entry)
    wildcard_notify = "*" in notify
    tags = entry_tagset(entry)
    candidates = entry_path_candidates(entry, workspace, problem_paths)
    delta = float(entry.get("delta_raw") or 0.0)
    hits: list[str] = []
    for agent in agents:
        if not agent.get("enabled", True):
            continue
        agent_id = agent["agent_id"]
        if wildcard_notify or agent_id.lower() in notify:
            hits.append(agent_id)
            continue
        if delta < float(agent.get("min_delta") or 0.0):
            continue
        scope = agent.get("scope") or {}
        if any(_path_hit(p, candidates) for p in (scope.get("paths") or [])):
            hits.append(agent_id)
            continue
        if _tag_hit(scope.get("tags") or [], tags):
            hits.append(agent_id)
    return hits


# --------------------------------------------------------------------------------------
# Perf (before -> after µs) extraction
# --------------------------------------------------------------------------------------


def collect_perf_summary(workspace: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """Best-effort "before -> after µs" line for the note.

    Priority: explicit `timings_us` -> µs pairs recorded in the entry document itself ->
    the operator's *current* calibration buckets (clearly labelled as a reference, because
    those are the latest commit's timings, not this entry's history).  Never fabricates
    numbers: falls back to an explicit `n/a`.
    """
    explicit = entry.get("timings_us")
    if isinstance(explicit, dict):
        try:
            before = float(explicit.get("before"))
            after = float(explicit.get("after"))
            if before > 0 and after > 0:
                return {
                    "headline": f"{before:.2f} µs → {after:.2f} µs ({before / after:.2f}×)",
                    "aggregate": str(explicit.get("detail") or ""),
                    "source": "entry.timings_us",
                    "kind": "explicit",
                }
        except (TypeError, ValueError):
            pass

    text = str(entry.get("_text") or "")
    matches = _US_PERF_RE.findall(text)
    if matches:
        parsed: list[tuple[float, float]] = []
        for a, b in matches:
            try:
                before, after = float(a), float(b)
            except ValueError:
                continue
            if before > 0 and after > 0:
                parsed.append((before, after))
        if parsed:
            before, after = max(parsed, key=lambda pair: pair[0] / pair[1])
            return {
                "headline": f"{before:.2f} µs → {after:.2f} µs ({before / after:.2f}×)",
                "aggregate": "摘自条目文档正文中记录的分桶耗时",
                "source": str(entry.get("doc_path") or "entry doc"),
                "kind": "document",
            }

    problem = str(entry.get("problem") or "")
    if problem:
        cal = read_json(Path(workspace) / "results" / problem / "calibration.json", {}) or {}
        buckets = cal.get("buckets") if isinstance(cal, dict) else None
        pairs: list[tuple[str, float, float]] = []
        for bucket in buckets or []:
            if not isinstance(bucket, dict) or not bucket.get("observed"):
                continue
            try:
                before = float(bucket.get("theta0_us"))
                after = float(bucket.get("tau_active_us"))
            except (TypeError, ValueError):
                continue
            if before > 0 and after > 0:
                pairs.append((str(bucket.get("id") or "bucket"), before, after))
        if pairs:
            ratios = [(name, before, after, before / after) for name, before, after in pairs]
            best = max(ratios, key=lambda r: r[3])
            geo = math.exp(sum(math.log(r[3]) for r in ratios) / len(ratios))
            improved = sum(1 for r in ratios if r[3] > 1.0)
            return {
                "headline": f"{best[0]} {best[1]:.2f} µs → {best[2]:.2f} µs ({best[3]:.2f}×)（参考值）",
                "aggregate": (
                    f"参考：{problem} 当前最新校准的 {len(ratios)} 个已观测分桶 vs θ0 基线，"
                    f"几何平均 {geo:.2f}×（{improved} 改善 / {len(ratios) - improved} 回退）；"
                    "这是该算子最新提交的实测，不是本条提交的历史对照"
                ),
                "source": f"results/{problem}/calibration.json",
                "kind": "reference",
            }

    return {
        "headline": "n/a（该条目未记录 µs 数据，请读条目内分桶明细）",
        "aggregate": "",
        "source": "",
        "kind": "missing",
    }


# --------------------------------------------------------------------------------------
# Note rendering
# --------------------------------------------------------------------------------------


def _one_line(text: Any, limit: int = 320) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return (s[: limit - 1] + "…") if len(s) > limit else s


def render_note(
    entry: dict[str, Any],
    agent: dict[str, Any],
    workspace: Path,
    bridge_dir: Path,
    entry_sha: str,
) -> str:
    agent_id = agent["agent_id"]
    entry_id = str(entry.get("id") or "unknown")
    problem = str(entry.get("problem") or "")
    delta_raw = float(entry.get("delta_raw") or 0.0)
    delta_weighted = float(entry.get("delta_weighted") or 0.0)
    difficulty = int(entry.get("difficulty") or 1)
    score_before = float(entry.get("score_before") or 0.0)
    score_after = float(entry.get("score_after") or 0.0)
    sha = str(entry.get("sha") or "")
    baseline = str(entry.get("baseline_sha") or "")
    category = str(entry.get("category") or "")
    category_zh = CATEGORY_LABELS.get(category, ("", ""))[0] or category
    tags = [str(t) for t in (entry.get("tags") or []) if str(t).strip()]
    mechanisms = [str(m) for m in (entry.get("mechanisms") or []) if str(m).strip()]
    bottleneck = _one_line(entry.get("bottleneck"), 400)
    rule = _one_line(entry.get("transferable_rule"), 400)
    headline_mech = _one_line(mechanisms[0] if mechanisms else rule, 400)
    perf = collect_perf_summary(workspace, entry)

    doc_rel = str(entry.get("_doc_rel") or entry.get("doc_path") or "")
    doc_abs = entry.get("_abs_doc") or ""
    entry_path_hint = f"ops-supervisor/breakthrough/{doc_rel}" if doc_rel else "ops-supervisor/breakthrough/entries/"

    scope_paths = ", ".join(agent["scope"].get("paths") or []) or "—"
    scope_tags = ", ".join(agent["scope"].get("tags") or []) or "—"
    outbox_rel = f"ops-supervisor/bridge/outbox/{agent_id}/{entry_id}.md"
    outbox_abs = bridge_dir / "outbox" / agent_id / f"{entry_id}.md"
    inbox_rel = f"ops-supervisor/bridge/inbox/{agent_id}/{entry_sha[:8]}-{entry_id}.md"

    lines: list[str] = [
        "---",
        f"bridge_schema: {BRIDGE_SCHEMA_VERSION}",
        f"agent_id: {agent_id}",
        f"entry_id: {entry_id}",
        f"entry_sha: {entry_sha}",
        f"transport: {agent['transport'].get('mode') or 'file'}",
        f"delivered_at: {utc_now().isoformat()}",
        "---",
        "",
        f"# 经验转交 · {problem} · {delta_raw:+.2f} pts（加权 {delta_weighted:+.2f}）",
        "",
        f"你是 **`{agent_id}`**（{agent.get('display') or agent_id}）。这条已验证的 KernelC 突破经验与你声明的范围 "
        f"（paths: {scope_paths} / tags: {scope_tags}）相关。请读完后按第 4 节写出你的总结回执。",
        "",
        "## 1. 条目",
        "",
        "| 字段 | 值 |",
        "| --- | --- |",
        f"| 算子 | `{problem}`（难度 {'^' * difficulty}，{difficulty} 级） |",
        f"| 条目 ID | `{entry_id}` |",
        f"| 条目路径 | `{entry_path_hint}` |",
        f"| 得分 | {score_before:.2f} → {score_after:.2f}（原始 {delta_raw:+.2f} / 加权 {delta_weighted:+.2f}） |",
        f"| 耗时 改前→改后 | {'参考（最新校准，非本条历史）' if perf['kind'] == 'reference' else ''}{perf['headline']} |",
        f"| 类别 | {category}（{category_zh}） |",
        f"| tags | {', '.join(tags) if tags else '—'} |",
        f"| 源 commit | `{sha}`" + (f"（基线 `{baseline[:12]}`）" if baseline else "") + " |",
        f"| 状态 | {entry.get('status') or 'verified'} |",
        "",
    ]
    if perf.get("aggregate"):
        lines += [f"> 耗时数据来源：{perf['source']} · {perf['aggregate']}", ""]
    lines += [
        "## 2. 机制一句话",
        "",
        f"**{headline_mech}**",
        "",
        f"- 可迁移规则：{rule}",
        f"- 原瓶颈：{bottleneck}",
        f"- 精度与同步守卫：{_one_line(entry.get('precision_guard'), 400)}",
        "",
    ]
    if len(mechanisms) > 1:
        lines += ["机制明细：", ""]
        lines += [f"{i + 1}. {_one_line(m, 300)}" for i, m in enumerate(mechanisms[:6])]
        lines.append("")

    lines += [
        "## 3. 你可以直接做的三件事",
        "",
        f"1. 读完整条目：`{doc_abs or entry_path_hint}`（或 `opsb breakthrough show {entry_id}`）。",
        f"2. 判断它是否适用于你手上的算子/形状；不适用也要写下失效条件（第 4 节第 ② 项）。",
        "3. 只在你自己的题目范围内验证，不要跨算子直接拷贝结论。",
        "",
        "## 4. 回执（唯一出口，≤5 行）",
        "",
        f"- 写入路径（相对工作区）：`{outbox_rel}`",
        f"- 本机绝对路径：`{outbox_abs}`",
        f"- 或一条命令：`opsb bridge publish {agent_id} {entry_id} --file -`",
        f"- 本条投递文件：`{inbox_rel}`",
        "",
        RECEIPT_INSTRUCTION.format(agent_id=agent_id, entry_id=entry_id),
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------------------


def ledger_file(bridge_dir: Path) -> Path:
    return Path(bridge_dir) / "ledger.jsonl"


def read_ledger(bridge_dir: Path) -> list[dict[str, Any]]:
    return [r for r in read_jsonl(ledger_file(bridge_dir)) if isinstance(r, dict)]


def append_ledger(bridge_dir: Path, record: dict[str, Any]) -> None:
    record = dict(record)
    record.setdefault("ts", utc_now().isoformat())
    append_jsonl_locked(Path(bridge_dir) / ".ledger.lock", ledger_file(bridge_dir), record)


def fold_states(records: Iterable[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Fold the append-only ledger into one state record per (entry_sha, agent_id)."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for rec in records:
        entry_sha = str(rec.get("entry_sha") or "")
        agent_id = str(rec.get("agent_id") or "")
        if not entry_sha or not agent_id:
            continue
        key = (entry_sha, agent_id)
        cur = out.get(key)
        if cur is None:
            cur = {
                "entry_sha": entry_sha,
                "agent_id": agent_id,
                "entry_id": str(rec.get("entry_id") or ""),
                "delivered_at": None,
                "pulled_at": None,
                "published_at": None,
                "collected_at": None,
                "pushed_at": None,
                "delivered": False,
                "pulled": False,
                "published": False,
                "collected": False,
                "push_ok": False,
                "last_error": "",
                "inbox": "",
                "title": "",
                "problem": "",
                "delta_raw": None,
            }
            out[key] = cur
        state = str(rec.get("state") or "")
        ts = rec.get("ts")
        if rec.get("entry_id") and not cur["entry_id"]:
            cur["entry_id"] = str(rec["entry_id"])
        if state == "delivered":
            cur["delivered"] = True
            cur["delivered_at"] = cur["delivered_at"] or ts
            cur["inbox"] = cur["inbox"] or str(rec.get("inbox") or "")
            cur["title"] = cur["title"] or str(rec.get("title") or "")
            cur["problem"] = cur["problem"] or str(rec.get("problem") or "")
            if rec.get("delta_raw") is not None and cur["delta_raw"] is None:
                cur["delta_raw"] = rec.get("delta_raw")
        elif state == "pulled":
            cur["pulled"] = True
            cur["pulled_at"] = cur["pulled_at"] or ts
        elif state == "published":
            cur["published"] = True
            cur["published_at"] = ts
        elif state == "collected":
            cur["collected"] = True
            cur["collected_at"] = ts
        elif state == "push_ok":
            cur["push_ok"] = True
            cur["pushed_at"] = ts
        elif state == "push_failed":
            cur["last_error"] = str(rec.get("error") or "transport failed")
    return out


def derive_state(state: dict[str, Any], outbox_exists: bool) -> str:
    if state.get("collected") or outbox_exists or state.get("published"):
        return "acked"
    if state.get("pulled"):
        return "pulled"
    return "pending"


# --------------------------------------------------------------------------------------
# Sync (delivery)
# --------------------------------------------------------------------------------------


def sync_deliveries(
    workspace: Path,
    *,
    bridge_dir: Path | str | None = None,
    min_delta: float | None = None,
    agent_ids: Iterable[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Deliver every routed (entry content sha, agent) pair exactly once."""
    workspace = Path(workspace)
    bd = scaffold_bridge_dir(resolve_bridge_dir(workspace, bridge_dir))
    agents = load_agents(workspace, bd)
    wanted = {str(a) for a in agent_ids} if agent_ids else None
    entries = load_entry_docs(workspace)
    problem_paths = _problem_paths(workspace)
    known = {
        (str(r.get("entry_sha") or ""), str(r.get("agent_id") or ""))
        for r in read_ledger(bd)
        if str(r.get("state") or "") == "delivered"
    }

    delivered: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for entry in entries:
        entry_id = str(entry.get("id") or "")
        entry_sha = str(entry.get("_sha") or "")
        if not entry_id or not entry_sha:
            continue
        if min_delta is not None and float(entry.get("delta_raw") or 0.0) < float(min_delta):
            continue
        targets = route_entry(entry, agents, workspace, problem_paths)
        for agent_id in targets:
            if wanted is not None and agent_id not in wanted:
                continue
            agent = find_agent(agents, agent_id)
            if agent is None:
                continue
            key = (entry_sha, agent_id)
            inbox_rel = f"inbox/{agent_id}/{entry_sha[:8]}-{entry_id}.md"
            inbox_abs = bd / inbox_rel
            if key in known:
                skipped.append({"entry_id": entry_id, "agent_id": agent_id, "reason": "already_delivered"})
                continue
            if inbox_abs.is_file():
                # Self-heal: file exists but ledger lost the record.
                if not dry_run:
                    append_ledger(
                        bd,
                        {
                            "state": "delivered",
                            "entry_id": entry_id,
                            "entry_sha": entry_sha,
                            "sha8": entry_sha[:8],
                            "agent_id": agent_id,
                            "inbox": inbox_rel,
                            "mode": agent["transport"].get("mode") or "file",
                            "title": _one_line(entry.get("title"), 120),
                            "problem": entry.get("problem"),
                            "delta_raw": entry.get("delta_raw"),
                            "recovered": True,
                        },
                    )
                known.add(key)
                skipped.append({"entry_id": entry_id, "agent_id": agent_id, "reason": "inbox_exists"})
                continue
            try:
                note = render_note(entry, agent, workspace, bd, entry_sha)
            except Exception as exc:  # pragma: no cover - defensive
                errors.append({"entry_id": entry_id, "agent_id": agent_id, "error": str(exc)[:200]})
                continue
            if not dry_run:
                try:
                    inbox_abs.parent.mkdir(parents=True, exist_ok=True)
                    tmp = inbox_abs.with_name(inbox_abs.name + ".tmp")
                    tmp.write_text(note, encoding="utf-8")
                    os.replace(tmp, inbox_abs)
                    append_ledger(
                        bd,
                        {
                            "state": "delivered",
                            "entry_id": entry_id,
                            "entry_sha": entry_sha,
                            "sha8": entry_sha[:8],
                            "agent_id": agent_id,
                            "inbox": inbox_rel,
                            "mode": agent["transport"].get("mode") or "file",
                            "title": _one_line(entry.get("title"), 120),
                            "problem": entry.get("problem"),
                            "delta_raw": entry.get("delta_raw"),
                        },
                    )
                except Exception as exc:
                    errors.append({"entry_id": entry_id, "agent_id": agent_id, "error": str(exc)[:200]})
                    continue
            known.add(key)
            delivered.append(
                {
                    "entry_id": entry_id,
                    "agent_id": agent_id,
                    "entry_sha": entry_sha,
                    "sha8": entry_sha[:8],
                    "inbox": str(inbox_abs),
                    "inbox_rel": inbox_rel,
                    "mode": agent["transport"].get("mode") or "file",
                }
            )

    return {
        "ok": not errors,
        "bridge_dir": str(bd),
        "agents": len([a for a in agents if a.get("enabled", True)]),
        "entries_scanned": len(entries),
        "delivered_count": len(delivered),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "delivered": delivered,
        "errors": errors,
        "dry_run": dry_run,
    }


def sync_after_breakthrough_change(workspace: Path, *, min_delta: float | None = None) -> dict[str, Any]:
    """Never let a bridge problem break the breakthrough pipeline."""
    try:
        return sync_deliveries(workspace, min_delta=min_delta)
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "error": str(exc)[:300], "delivered_count": 0}


# --------------------------------------------------------------------------------------
# Pull / publish / collect
# --------------------------------------------------------------------------------------


def pull_items(
    workspace: Path,
    agent_id: str,
    *,
    ack: bool = False,
    bridge_dir: Path | str | None = None,
    entry_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    workspace = Path(workspace)
    bd = scaffold_bridge_dir(resolve_bridge_dir(workspace, bridge_dir))
    try:
        agent_id = _safe_id(agent_id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "items": [], "count": 0}
    agents = load_agents(workspace, bd)
    agent = find_agent(agents, agent_id)
    if agent is None:
        return {
            "ok": False,
            "error": f"unknown agent_id: {agent_id}",
            "known_agents": [a["agent_id"] for a in agents],
            "items": [],
            "count": 0,
        }

    wanted_entries = {str(e) for e in entry_ids} if entry_ids else None
    states = fold_states(read_ledger(bd))
    items: list[dict[str, Any]] = []
    for (entry_sha, aid), state in states.items():
        if aid != agent_id or not state.get("delivered"):
            continue
        if state.get("published") or state.get("collected"):
            continue
        if wanted_entries is not None and str(state.get("entry_id") or "") not in wanted_entries:
            continue
        inbox_rel = str(state.get("inbox") or "")
        if not inbox_rel:
            continue
        inbox_abs = bd / inbox_rel
        if not inbox_abs.is_file():
            continue
        if ack and not state.get("pulled"):
            append_ledger(
                bd,
                {
                    "state": "pulled",
                    "entry_id": state.get("entry_id"),
                    "entry_sha": entry_sha,
                    "agent_id": agent_id,
                    "inbox": inbox_rel,
                },
            )
            state["pulled"] = True
            state["pulled_at"] = utc_now().isoformat()
        items.append(
            {
                "entry_id": state.get("entry_id"),
                "entry_sha": entry_sha,
                "sha8": entry_sha[:8],
                "title": state.get("title"),
                "problem": state.get("problem"),
                "delta_raw": state.get("delta_raw"),
                "state": "pulled" if state.get("pulled") else "pending",
                "delivered_at": state.get("delivered_at"),
                "inbox": str(inbox_abs),
                "note": inbox_abs.read_text(encoding="utf-8", errors="replace"),
            }
        )
    items.sort(key=lambda it: str(it.get("delivered_at") or ""), reverse=True)
    return {
        "ok": True,
        "agent_id": agent_id,
        "display": agent.get("display"),
        "kind": agent.get("kind"),
        "transport": agent.get("transport", {}).get("mode"),
        "bridge_dir": str(bd),
        "count": len(items),
        "acked": bool(ack),
        "outbox_dir": str(bd / "outbox" / agent_id),
        "items": items,
    }


def publish_receipt(
    workspace: Path,
    agent_id: str,
    entry_id: str,
    content: str,
    *,
    bridge_dir: Path | str | None = None,
) -> dict[str, Any]:
    workspace = Path(workspace)
    bd = scaffold_bridge_dir(resolve_bridge_dir(workspace, bridge_dir))
    try:
        agent_id = _safe_id(agent_id)
        entry_id = _safe_id(entry_id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    agents = load_agents(workspace, bd)
    if find_agent(agents, agent_id) is None:
        return {"ok": False, "error": f"unknown agent_id: {agent_id}", "known_agents": [a["agent_id"] for a in agents]}

    body = (content or "").strip()
    if not body:
        return {"ok": False, "error": "empty receipt: pass --file <path> or pipe the summary on stdin"}

    entries = {str(e.get("id") or ""): e for e in load_entry_docs(workspace)}
    entry = entries.get(entry_id)
    states = fold_states(read_ledger(bd))
    entry_sha = ""
    for (sha, aid), state in states.items():
        if aid == agent_id and str(state.get("entry_id") or "") == entry_id:
            entry_sha = sha
    if not entry_sha and entry is not None:
        entry_sha = str(entry.get("_sha") or "")
    if entry is None and not entry_sha:
        return {
            "ok": False,
            "error": f"unknown entry_id: {entry_id}",
            "hint": "run `opsb bridge pull " + agent_id + "` to list deliverable entry ids",
        }

    out_dir = bd / "outbox" / agent_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_abs = out_dir / f"{entry_id}.md"
    tmp = out_abs.with_name(out_abs.name + ".tmp")
    tmp.write_text(body + "\n", encoding="utf-8")
    os.replace(tmp, out_abs)
    append_ledger(
        bd,
        {
            "state": "published",
            "entry_id": entry_id,
            "entry_sha": entry_sha,
            "sha8": entry_sha[:8] if entry_sha else "",
            "agent_id": agent_id,
            "outbox": f"outbox/{agent_id}/{entry_id}.md",
            "lines": len([ln for ln in body.splitlines() if ln.strip()]),
        },
    )
    return {
        "ok": True,
        "agent_id": agent_id,
        "entry_id": entry_id,
        "entry_sha": entry_sha,
        "outbox": str(out_abs),
        "lines": len([ln for ln in body.splitlines() if ln.strip()]),
    }


def collect_summaries(
    workspace: Path,
    *,
    bridge_dir: Path | str | None = None,
    agent_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Upsert every `outbox/<agent>/<entry>.md` into `breakthrough/index.json#summaries`."""
    workspace = Path(workspace)
    bd = scaffold_bridge_dir(resolve_bridge_dir(workspace, bridge_dir))
    wanted = {str(a) for a in agent_ids} if agent_ids else None
    agents = {a["agent_id"]: a for a in load_agents(workspace, bd)}
    states = fold_states(read_ledger(bd))
    entries = {str(e.get("id") or ""): e for e in load_entry_docs(workspace)}

    bt_dir = resolve_breakthrough_dir(workspace)
    idx_path = bt_dir / "index.json"
    raw = read_json(idx_path, {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    summaries: list[dict[str, Any]] = [dict(s) for s in (raw.get("summaries") or []) if isinstance(s, dict)]

    collected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    orphaned: list[dict[str, Any]] = []
    outbox_root = bd / "outbox"
    if outbox_root.is_dir():
        for out_abs in sorted(outbox_root.glob("*/*.md")):
            agent_id = out_abs.parent.name
            entry_id = out_abs.stem
            if wanted is not None and agent_id not in wanted:
                continue
            body = out_abs.read_text(encoding="utf-8", errors="replace").strip()
            if not body:
                skipped.append({"entry_id": entry_id, "agent_id": agent_id, "reason": "empty"})
                continue
            entry = entries.get(entry_id)
            if entry is None:
                orphaned.append({"entry_id": entry_id, "agent_id": agent_id})
            entry_sha = ""
            for (sha, aid), state in states.items():
                if aid == agent_id and str(state.get("entry_id") or "") == entry_id:
                    entry_sha = sha
            if not entry_sha and entry is not None:
                entry_sha = str(entry.get("_sha") or "")
            agent = agents.get(agent_id) or {"display": agent_id}
            record = {
                "entry_id": entry_id,
                "agent_id": agent_id,
                "agent_display": agent.get("display") or agent_id,
                "agent_kind": agent.get("kind") or "cli",
                "problem": (entry or {}).get("problem") or "",
                "entry_sha": entry_sha,
                "sha8": entry_sha[:8] if entry_sha else "",
                "summary": body[:8000],
                "lines": len([ln for ln in body.splitlines() if ln.strip()]),
                "summary_path": f"bridge/outbox/{agent_id}/{entry_id}.md",
            }
            existing_idx = next(
                (i for i, s in enumerate(summaries) if s.get("entry_id") == entry_id and s.get("agent_id") == agent_id),
                None,
            )
            if existing_idx is not None:
                prev = summaries[existing_idx]
                if prev.get("summary") == record["summary"] and str(prev.get("entry_sha") or "") == entry_sha:
                    skipped.append({"entry_id": entry_id, "agent_id": agent_id, "reason": "already_collected"})
                    continue
                record["collected_at"] = prev.get("collected_at") or utc_now().isoformat()
                record["updated_at"] = utc_now().isoformat()
                summaries[existing_idx] = record
            else:
                record["collected_at"] = utc_now().isoformat()
                summaries.append(record)
            append_ledger(
                bd,
                {
                    "state": "collected",
                    "entry_id": entry_id,
                    "entry_sha": entry_sha,
                    "sha8": record["sha8"],
                    "agent_id": agent_id,
                    "outbox": record["summary_path"],
                    "lines": record["lines"],
                },
            )
            collected.append(record)

    if collected:
        summaries.sort(key=lambda s: (str(s.get("collected_at") or ""), str(s.get("entry_id") or "")), reverse=True)
        payload = dict(raw)  # preserve entries + every other top-level field
        payload.setdefault("entries", [])
        payload["schema_version"] = int(raw.get("schema_version") or 1)
        payload["summaries"] = summaries
        payload["summaries_updated_at"] = utc_now().isoformat()
        idx_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = idx_path.with_name(idx_path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, idx_path)

    return {
        "ok": True,
        "index_path": str(idx_path),
        "collected_count": len(collected),
        "skipped_count": len(skipped),
        "orphaned_count": len(orphaned),
        "total_summaries": len(summaries),
        "collected": collected,
        "orphaned": orphaned,
    }


# --------------------------------------------------------------------------------------
# Transport escalation (exec / http)
# --------------------------------------------------------------------------------------


def _exec_argv(agent: dict[str, Any], entry_id: str, entry_sha: str, inbox_abs: Path) -> list[str]:
    argv = list((agent.get("transport") or {}).get("exec") or [])
    replaced: list[str] = []
    for token in argv:
        replaced.append(
            str(token)
            .replace("{entry_id}", entry_id)
            .replace("{entry_sha}", entry_sha)
            .replace("{sha8}", entry_sha[:8])
            .replace("{agent_id}", agent["agent_id"])
            .replace("{inbox}", str(inbox_abs))
            .replace("{workspace}", str(inbox_abs.parents[3]) if len(inbox_abs.parents) > 3 else "")
        )
    return replaced


def _push_one(
    agent: dict[str, Any],
    entry_id: str,
    entry_sha: str,
    inbox_abs: Path,
    workspace: Path,
    dry_run: bool,
) -> tuple[bool, str]:
    mode = (agent.get("transport") or {}).get("mode") or "file"
    note = inbox_abs.read_text(encoding="utf-8", errors="replace")
    if mode == "exec":
        argv = _exec_argv(agent, entry_id, entry_sha, inbox_abs)
        if not argv:
            return False, "transport.exec is empty"
        if dry_run:
            return True, "dry-run: " + " ".join(argv)
        env = dict(os.environ)
        env.update(
            {
                "OPSB_BRIDGE_AGENT_ID": agent["agent_id"],
                "OPSB_BRIDGE_ENTRY_ID": entry_id,
                "OPSB_BRIDGE_ENTRY_SHA": entry_sha,
                "OPSB_BRIDGE_INBOX": str(inbox_abs),
                "OPSB_BRIDGE_WORKSPACE": str(workspace),
            }
        )
        try:
            proc = subprocess.run(
                argv,
                input=note,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=120.0,
                env=env,
            )
        except Exception as exc:
            return False, str(exc)[:300]
        if proc.returncode != 0:
            return False, (proc.stderr or proc.stdout or "").strip()[:300] or f"exit {proc.returncode}"
        return True, (proc.stdout or "").strip()[:200] or "ok"

    if mode == "http":
        url = str((agent.get("transport") or {}).get("url") or "")
        if not url:
            return False, "transport.url is empty"
        if dry_run:
            return True, f"dry-run: POST {url}"
        body = json.dumps(
            {
                "agent_id": agent["agent_id"],
                "entry_id": entry_id,
                "entry_sha": entry_sha,
                "note": note,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        headers.update({str(k): str(v) for k, v in ((agent.get("transport") or {}).get("headers") or {}).items()})
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20.0) as resp:
                return 200 <= int(resp.status) < 300, f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            return False, f"HTTP {exc.code}"
        except Exception as exc:
            return False, str(exc)[:300]

    return True, "file transport: nothing to push"


def push_deliveries(
    workspace: Path,
    *,
    bridge_dir: Path | str | None = None,
    agent_ids: Iterable[str] | None = None,
    entry_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Escalate file deliveries to the configured exec/http hook (idempotent per push)."""
    workspace = Path(workspace)
    bd = scaffold_bridge_dir(resolve_bridge_dir(workspace, bridge_dir))
    wanted = {str(a) for a in agent_ids} if agent_ids else None
    agents = {a["agent_id"]: a for a in load_agents(workspace, bd)}
    states = fold_states(read_ledger(bd))
    pushed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    considered = 0

    for (entry_sha, agent_id), state in states.items():
        if not state.get("delivered") or state.get("push_ok") or state.get("collected"):
            continue
        if wanted is not None and agent_id not in wanted:
            continue
        if entry_id and str(state.get("entry_id") or "") != entry_id:
            continue
        agent = agents.get(agent_id)
        if agent is None or not agent.get("enabled", True):
            continue
        mode = (agent.get("transport") or {}).get("mode") or "file"
        if mode not in ("exec", "http"):
            continue
        inbox_rel = str(state.get("inbox") or "")
        inbox_abs = bd / inbox_rel if inbox_rel else None
        if inbox_abs is None or not inbox_abs.is_file():
            continue
        considered += 1
        ok, detail = _push_one(agent, str(state.get("entry_id") or ""), entry_sha, inbox_abs, workspace, dry_run)
        if dry_run:
            pushed.append({"entry_id": state.get("entry_id"), "agent_id": agent_id, "mode": mode, "detail": detail})
            continue
        append_ledger(
            bd,
            {
                "state": "push_ok" if ok else "push_failed",
                "entry_id": state.get("entry_id"),
                "entry_sha": entry_sha,
                "sha8": entry_sha[:8],
                "agent_id": agent_id,
                "mode": mode,
                "error": "" if ok else detail,
            },
        )
        if ok:
            pushed.append({"entry_id": state.get("entry_id"), "agent_id": agent_id, "mode": mode, "detail": detail})
        else:
            failed.append({"entry_id": state.get("entry_id"), "agent_id": agent_id, "mode": mode, "error": detail})

    return {
        "ok": not failed,
        "bridge_dir": str(bd),
        "considered": considered,
        "pushed_count": len(pushed),
        "failed_count": len(failed),
        "pushed": pushed,
        "failed": failed,
        "dry_run": dry_run,
    }


# --------------------------------------------------------------------------------------
# Status (UI + CLI)
# --------------------------------------------------------------------------------------


def bridge_status(
    workspace: Path,
    *,
    bridge_dir: Path | str | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    workspace = Path(workspace)
    bd = scaffold_bridge_dir(resolve_bridge_dir(workspace, bridge_dir))
    agents = load_agents(workspace, bd)
    states = fold_states(read_ledger(bd))
    records = read_ledger(bd)

    totals = {"pending": 0, "pulled": 0, "acked": 0, "delivered": 0, "outbox": 0}
    per_agent: dict[str, list[dict[str, Any]]] = {a["agent_id"]: [] for a in agents}

    for (entry_sha, aid), state in states.items():
        if not state.get("delivered"):
            continue
        if aid not in per_agent:
            per_agent[aid] = []
        entry_id = str(state.get("entry_id") or "")
        outbox_abs = bd / "outbox" / aid / f"{entry_id}.md"
        outbox_exists = outbox_abs.is_file()
        derived = derive_state(state, outbox_exists)
        inbox_rel = str(state.get("inbox") or "")
        inbox_abs = bd / inbox_rel if inbox_rel else None
        outbox_lines = 0
        if outbox_exists:
            try:
                outbox_lines = len([ln for ln in outbox_abs.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()])
            except OSError:
                outbox_lines = 0
        item = {
            "entry_id": entry_id,
            "entry_sha": entry_sha,
            "sha8": entry_sha[:8],
            "title": state.get("title") or "",
            "problem": state.get("problem") or "",
            "delta_raw": state.get("delta_raw"),
            "state": derived,
            "delivered_at": state.get("delivered_at"),
            "pulled_at": state.get("pulled_at"),
            "published_at": state.get("published_at"),
            "collected_at": state.get("collected_at"),
            "inbox": str(inbox_abs) if inbox_abs else "",
            "inbox_exists": bool(inbox_abs and inbox_abs.is_file()),
            "outbox": str(outbox_abs),
            "outbox_exists": outbox_exists,
            "outbox_lines": outbox_lines,
            "push_ok": bool(state.get("push_ok")),
            "last_error": state.get("last_error") or "",
        }
        per_agent[aid].append(item)
        totals["delivered"] += 1
        totals[derived] = totals.get(derived, 0) + 1
        if outbox_exists:
            totals["outbox"] += 1

    bt_dir = resolve_breakthrough_dir(workspace)
    idx = read_json(bt_dir / "index.json", {}) or {}
    summaries = idx.get("summaries") if isinstance(idx, dict) else None
    summary_rows = [s for s in (summaries or []) if isinstance(s, dict)]

    # Receipts the agent volunteered for an entry that was routed elsewhere (or not delivered
    # yet) still deserve to be visible: show them as 已回执 with `self_initiated`.
    for summary in summary_rows:
        aid = str(summary.get("agent_id") or "")
        eid = str(summary.get("entry_id") or "")
        if not aid or not eid:
            continue
        per_agent.setdefault(aid, [])
        if any(str(it.get("entry_id") or "") == eid for it in per_agent[aid]):
            continue
        outbox_abs = bd / "outbox" / aid / f"{eid}.md"
        per_agent[aid].append(
            {
                "entry_id": eid,
                "entry_sha": str(summary.get("entry_sha") or ""),
                "sha8": str(summary.get("sha8") or str(summary.get("entry_sha") or "")[:8]),
                "title": "",
                "problem": str(summary.get("problem") or ""),
                "delta_raw": None,
                "state": "acked",
                "delivered_at": None,
                "pulled_at": None,
                "published_at": summary.get("collected_at"),
                "collected_at": summary.get("collected_at"),
                "inbox": "",
                "inbox_exists": False,
                "outbox": str(outbox_abs),
                "outbox_exists": outbox_abs.is_file(),
                "outbox_lines": int(summary.get("lines") or 0),
                "push_ok": False,
                "last_error": "",
                "self_initiated": True,
            }
        )
        totals["acked"] = totals.get("acked", 0) + 1

    agent_rows: list[dict[str, Any]] = []
    for agent in agents:
        aid = agent["agent_id"]
        if agent_id and aid != agent_id:
            continue
        items = per_agent.get(aid, [])
        order = {"pending": 0, "pulled": 1, "acked": 2}
        items.sort(key=lambda it: (order.get(it["state"], 3), -float(it.get("delta_raw") or 0.0)))
        counts = {"pending": 0, "pulled": 0, "acked": 0}
        for it in items:
            counts[it["state"]] = counts.get(it["state"], 0) + 1
        agent_rows.append(
            {
                "agent_id": aid,
                "display": agent.get("display"),
                "kind": agent.get("kind"),
                "enabled": agent.get("enabled", True),
                "min_delta": agent.get("min_delta", 0.0),
                "scope": agent.get("scope"),
                "transport": agent.get("transport"),
                "counts": {
                    "pending": counts["pending"],
                    "pulled": counts["pulled"],
                    "acked": counts["acked"],
                    "total": len(items),
                },
                "items": items,
            }
        )

    pending_total = sum(r["counts"]["pending"] for r in agent_rows)
    pulled_total = sum(r["counts"]["pulled"] for r in agent_rows)
    acked_total = sum(r["counts"]["acked"] for r in agent_rows)

    return {
        "ok": True,
        "schema_version": BRIDGE_SCHEMA_VERSION,
        "bridge_dir": str(bd),
        "agents_path": str(bd / "agents.json"),
        "ledger_path": str(ledger_file(bd)),
        "outbox_root": str(bd / "outbox"),
        "inbox_root": str(bd / "inbox"),
        "agents": agent_rows,
        "agent_count": len(agent_rows),
        "totals": {
            "pending": pending_total,
            "pulled": pulled_total,
            "acked": acked_total,
            "delivered": totals["delivered"],
            "outbox_files": totals["outbox"],
        },
        "ledger_records": len(records),
        "summaries_count": len(summary_rows),
        "summaries": summary_rows[-20:],
        "breakthrough_dir": str(bt_dir),
        "fetched_at": utc_now().isoformat(),
    }


# --------------------------------------------------------------------------------------
# Watch (foreground, single consumer; headless environments only)
# --------------------------------------------------------------------------------------


def watch(
    workspace: Path,
    *,
    interval: float = 20.0,
    once: bool = False,
    escalate: bool = True,
    bridge_dir: Path | str | None = None,
    on_tick: Any = None,
) -> int:
    """Foreground loop: sync -> escalate file deliveries to exec/http hooks.

    Deliberately *not* a daemon: the macOS app already owns its consumer engine, so
    only headless environments should run this (one instance at a time).
    """
    interval = max(2.0, float(interval))
    while True:
        tick: dict[str, Any] = {"ts": utc_now().isoformat()}
        try:
            tick["sync"] = sync_deliveries(workspace, bridge_dir=bridge_dir)
        except Exception as exc:  # pragma: no cover - defensive
            tick["sync"] = {"ok": False, "error": str(exc)[:300], "delivered_count": 0}
        try:
            tick["push"] = (
                push_deliveries(workspace, bridge_dir=bridge_dir)
                if escalate
                else {"ok": True, "pushed_count": 0, "failed_count": 0, "skipped": True}
            )
        except Exception as exc:  # pragma: no cover - defensive
            tick["push"] = {"ok": False, "error": str(exc)[:300], "pushed_count": 0}
        tick["ok"] = bool(tick["sync"].get("ok")) and bool(tick["push"].get("ok"))
        if on_tick is not None:
            on_tick(tick)
        if once:
            return 0 if tick["ok"] else 1
        time.sleep(interval)
