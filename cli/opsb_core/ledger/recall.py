"""``parhelion_recall``: a small inverted index over the ledger, the handoffs and one worker's session log.

Sources (all read-only):

``<contest>/results/ledger/<problem>/*.jsonl``     every ledger row of the problem;
``<workspace state>/handoffs/*.jsonl``            the narrative and checks of each compaction;
``<session logs>/mesh-<agent>*.jsonl``            this worker's archived session flow.

The index is a ``{token: [line numbers]}`` map per file, cached under
``<workspace state>/recall/``. The sources are never written: the cache lives in the state dir, and a
file whose (size, mtime) is unchanged is not re-read. Only the last ``MAX_BYTES`` of a session log is
indexed, because a worker log reaches tens of megabytes and an old insight is the point of the query.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from ..drivers.base import resolve_session_logs_dir, resolve_workspace_state_dir

TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}")
STOP = frozenset("""
the and for with that this from have has had not are was were you your our its his her they them then
than there their what when which while would could should about after before again also been being both
does done each even every first more most much must only other over same some such very just like make
made use used using need needs keep kept still case cases test tests tested testing level standard final
smoke sha commit commits worker workers problem result results pass passed fail failed speedup baseline
parhelion gate submit submitted note summary report kernel code file files time times here next last
into onto per via now new old run runs running json line lines true false null none out
""".split())
MAX_BYTES = 512 * 1024
MAX_LINES = 40_000
SNIPPET_CHARS = 200
RESULTS_DEFAULT = 8
RESULTS_MAX = 40
INDEX_VERSION = 1


def _state_dir(workspace: Path | str) -> Path:
    return resolve_workspace_state_dir(Path(workspace).expanduser())


def cache_dir(workspace: Path | str) -> Path:
    return _state_dir(workspace) / "recall"


def tokens_of(text: str) -> list[str]:
    """Index terms of one line: lowercase words and CJK runs, minus the noise words."""
    out = []
    for raw in TOKEN.findall(text or ""):
        token = raw.lower()
        if token in STOP or (len(token) < 3 and not _cjk(token)):
            continue
        out.append(token)
    return out


def _cjk(token: str) -> bool:
    return bool(token) and all("\u4e00" <= ch <= "\u9fff" for ch in token)


def _stem(token: str) -> str:
    """Six-letter stem so ``rejected`` and ``reject`` meet."""
    return token[:6] if not _cjk(token) else token[:4]


def ledger_files(workspace: Path | str, problem: str) -> list[Path]:
    from . import store
    root = store.problem_dir(Path(workspace).expanduser(), problem) if problem else None
    return sorted(root.glob("*.jsonl")) if root and root.is_dir() else []


def handoff_files(workspace: Path | str) -> list[Path]:
    root = _state_dir(workspace) / "handoffs"
    return sorted(root.glob("*.jsonl")) if root.is_dir() else []


def session_files(agent_id: str) -> list[Path]:
    """This worker's archived mesh logs (read-only): ``mesh-<agent>*.jsonl`` in the session log dir."""
    name = str(agent_id or "").strip()
    if not name or "/" in name or ".." in name:
        return []
    root = resolve_session_logs_dir()
    return sorted(root.glob(f"mesh-{name}*.jsonl")) if root.is_dir() else []


def _bounded_text(path: Path) -> tuple[str, int]:
    """``(text, line offset)``: the file's text, or its last ``MAX_BYTES`` bytes and the lines before them."""
    try:
        size = path.stat().st_size
        offset = 0
        with path.open("rb") as fh:
            if size > MAX_BYTES:
                skipped = size - MAX_BYTES
                while skipped > 0:
                    chunk = fh.read(min(1 << 16, skipped))
                    if not chunk:
                        break
                    skipped -= len(chunk)
                    offset += chunk.count(b"\n")
            return fh.read().decode("utf-8", errors="replace"), offset
    except OSError:
        return "", 0


def _document(path: Path, *, label: str) -> dict[str, Any]:
    """``{path, kind, stamp, lines, index}``: searchable ``[line number, text]`` pairs and their postings."""
    try:
        stat = path.stat()
    except OSError:
        return {}
    stamp = f"{stat.st_size}:{int(stat.st_mtime)}"
    text, offset = _bounded_text(path)
    lines: list[list[Any]] = []
    index: dict[str, list[int]] = {}
    for number, line in enumerate(text.splitlines()[:MAX_LINES], start=offset + 1):
        body = _searchable(line, label)
        if not body:
            continue
        terms = {_stem(t) for t in tokens_of(body)}
        if not terms:
            continue
        lines.append([number, body])
        for term in terms:
            index.setdefault(term, []).append(len(lines) - 1)
    return {"path": str(path), "kind": label, "stamp": stamp, "lines": lines, "index": index}


def _searchable(line: str, label: str) -> str:
    """The text to index of one physical line: a JSON row keeps its message texts, a plain row itself."""
    text = line.strip()
    if not text:
        return ""
    if label.startswith("ledger") and text.startswith("{"):
        try:
            row = json.loads(text)
        except ValueError:
            return text[:900]
        if not isinstance(row, dict):
            return text[:900]
        parts = [f"{row.get('id')} {row.get('type')} {row.get('claim')}", str(row.get("why") or ""),
                 str(row.get("approach") or ""), str(row.get("why_failed") or ""),
                 str(row.get("actionable_next") or ""), str(row.get("lesson") or ""),
                 str(row.get("text") or ""), json.dumps(row.get("scope") or {}, ensure_ascii=False)]
        return " · ".join(p for p in parts if p.strip())
    if label.startswith("handoff") and text.startswith("{"):
        try:
            row = json.loads(text)
        except ValueError:
            return text[:900]
        if isinstance(row, dict):
            return " · ".join(str(row.get(k) or "") for k in ("ts", "kind", "status", "narrative_final"))
    if label.startswith("session") and text.startswith("{"):
        # ACP rows are {ts, dir, msg}; keep only the text the model actually wrote or read.
        try:
            row = json.loads(text)
        except ValueError:
            return text[:900]
        return _session_text(row)[:900]
    return text[:900]


def _session_text(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    msg = row.get("msg") if isinstance(row.get("msg"), dict) else {}
    chunks: list[str] = []
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    prompt = params.get("prompt")
    if isinstance(prompt, str):
        chunks.append(prompt)
    elif isinstance(prompt, list):
        chunks.extend(str((c or {}).get("text") or "") for c in prompt if isinstance(c, dict))
    update = params.get("update") if isinstance(params.get("update"), dict) else {}
    content = update.get("content")
    if isinstance(content, dict):
        chunks.append(str(content.get("text") or ""))
    elif isinstance(content, str):
        chunks.append(content)
    for key in ("title", "rawInput"):
        if update.get(key):
            chunks.append(json.dumps(update[key], ensure_ascii=False, default=str)[:400])
    return " ".join(c for c in chunks if c)


def _cache_path(workspace: Path | str, agent_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(agent_id or "all"))[:64]
    return cache_dir(workspace) / f"index-{safe}.json"


def load_documents(workspace: Path | str, problem: str, agent_id: str = "", *, refresh: bool = False) -> list[dict[str, Any]]:
    """The indexed documents of one worker: ledger, handoffs and session logs (cached by file stamp)."""
    ws = Path(workspace).expanduser()
    path = _cache_path(ws, agent_id)
    cached = _read_cache(path) if not refresh else {}
    sources: list[tuple[Path, str]] = [(p, "ledger") for p in ledger_files(ws, problem)]
    sources += [(p, "handoff") for p in handoff_files(ws)]
    sources += [(p, "session") for p in session_files(agent_id)]
    docs: list[dict[str, Any]] = []
    changed = False
    for source, label in sources:
        key = str(source)
        previous = cached.get(key) if isinstance(cached.get(key), dict) else None
        try:
            stamp = f"{source.stat().st_size}:{int(source.stat().st_mtime)}"
        except OSError:
            continue
        if previous and previous.get("stamp") == stamp and not refresh:
            docs.append(previous)
            continue
        doc = _document(source, label=label)
        if doc:
            docs.append(doc)
            changed = True
    if changed or refresh or not path.is_file():
        _write_cache(path, {doc["path"]: doc for doc in docs})
    return docs


def _read_cache(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc.get("files") if isinstance(doc, dict) and doc.get("version") == INDEX_VERSION else {}


def _write_cache(path: Path, files: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": INDEX_VERSION, "files": files}, ensure_ascii=False),
                        encoding="utf-8")
    except OSError:
        pass  # a read-only state dir only costs a re-index


def search(workspace: Path | str, problem: str, agent_id: str, query: str, *,
           limit: int = RESULTS_DEFAULT, refresh: bool = False) -> list[dict[str, Any]]:
    """Up to ``limit`` hits: a snippet, its source ``file#line`` and the matched terms, best first."""
    terms = [_stem(t) for t in tokens_of(query)]
    if not terms:
        return []
    wanted = list(dict.fromkeys(terms))
    hits: list[tuple[float, dict[str, Any]]] = []
    for doc in load_documents(workspace, problem, agent_id, refresh=refresh):
        index = doc.get("index") if isinstance(doc.get("index"), dict) else {}
        counts: dict[int, list[str]] = {}
        for term in wanted:
            for number in index.get(term) or []:
                counts.setdefault(number, []).append(term)
        if not counts:
            continue
        for number, matched in counts.items():
            lines = doc.get("lines") or []
            if number >= len(lines):
                continue
            line_no, body = lines[number]
            score = len(matched) / len(wanted) + min(len(matched), 4) * 0.05
            hits.append((score, {"source": f"{doc.get('path')}#{line_no}", "kind": doc.get("kind") or "",
                                 "text": str(body)[:SNIPPET_CHARS], "matched": matched}))
    hits.sort(key=lambda h: (-h[0], h[1]["source"]))
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _score, hit in hits:
        key = hit["source"]
        if key in seen:
            continue
        seen.add(key)
        out.append(hit)
        if len(out) >= max(1, min(RESULTS_MAX, int(limit))):
            break
    return out


def clip(results: Iterable[dict[str, Any]], limit: int = RESULTS_DEFAULT) -> list[dict[str, Any]]:
    return list(results)[: max(1, min(RESULTS_MAX, int(limit)))]
