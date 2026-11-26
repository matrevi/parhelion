"""Peer messages (4.2 F): a bounded envelope, attachments for long bodies, one merged delivery.

``parhelion_squad`` sends a teammate an *envelope*: who it is for, the ledger ids it cites, a claim
of at most ``CLAIM_MAX`` characters, one evidence reference and an optional ask. A body above
``ATTACH_MAX`` characters is written once to ``<workspace state>/peer/<msg_id>.md`` and only the
claim and that path travel; a body whose lines repeat what the recipient already received keeps its
refs and loses the quote (Jaccard >= ``DEDUP_JACCARD``). The manager queues envelopes instead of
waking the recipient per message: at its next turn boundary the queue flushes as ONE bus row and ONE
wake (``peer batch``), while administrator directives and judge feedback stay immediate. A stage
``result`` follows the same rule, persists its full text and records one ledger ``decision``; the
recipient's ledger row (``source=peer``) outlives the bus row.

4.3 P27: ``evidence`` is a required ``{class, ref}`` (``opsb_core.evidence``); a message that is not
backed by an online receipt reads behind the ``待验证`` marker. ``to="all"`` is a workspace-wide
broadcast (4.3 P27): the sender is excluded, a worker seat is limited to one broadcast per
``BROADCAST_MIN_SEC`` and the orchestrator is not limited.
"""
from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..drivers.base import resolve_workspace_state_dir
from .. import evidence as _evidence
from ..ledger import model, note
from ..ledger.store import Ledger

CLAIM_MAX = 280
ATTACH_MAX = 1200
EVIDENCE_MAX = 300
DEDUP_JACCARD = 0.8
DEDUP_RECENT = 12
SHINGLE = 3
REF_MAX = 12
INDEX_HARD_BYTES = 256_000
INDEX_KEEP = 500
BROADCAST_MIN_SEC = 600  # 4.3 P27: a worker seat sends at most one workspace broadcast per 10 minutes
BROADCAST_KEEP = 200  # senders remembered in the rate-limit file; oldest entries fall off
CLAIM_HINT = ("claim was taken from the start of the text; pass claim (<= 280 characters) next time and "
              "keep the body out of the message")
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

def peer_dir(workspace: Path | str) -> Path:
    """``<workspace state>/peer``: attachments, pending queues, quote history and the request index."""
    return resolve_workspace_state_dir(Path(workspace).expanduser().resolve()) / "peer"

def _safe(value: Any) -> str:
    return _SAFE.sub("_", str(value or "")).strip("._-")[:64] or "peer"

def attachment_path(workspace: Path | str, msg_id: str) -> Path:
    return peer_dir(workspace) / f"{_safe(msg_id)}.md"

def pending_path(workspace: Path | str, recipient: str) -> Path:
    return peer_dir(workspace) / "pending" / f"{_safe(recipient)}.jsonl"

def quotes_path(workspace: Path | str, recipient: str) -> Path:
    return peer_dir(workspace) / "quotes" / f"{_safe(recipient)}.jsonl"

def clip(value: Any, limit: int) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"

def parse_refs(value: Any) -> list[str]:
    items = value if isinstance(value, (list, tuple, set)) else re.split(r"[,\s]+", str(value or ""))
    out: list[str] = []
    for item in items:
        ref = str(item or "").strip()
        if not ref:
            continue
        if not model.REF_RE.match(ref):
            raise ValueError(f"refs must be ledger ids (L-0001 or worker/L-0001); got {ref!r}")
        if ref not in out:
            out.append(ref)
    if len(out) > REF_MAX:
        raise ValueError(f"refs is limited to {REF_MAX} ledger ids")
    return out

def envelope(workspace: Path | str, *, sender: str, recipient: str, claim: Any = "", message: Any = "",
             refs: Any = None, evidence: Any = None, ask: Any = "") -> dict[str, Any]:
    text = str(message or "").strip()
    given = " ".join(str(claim or "").split())  # a claim is one line
    item_evidence = _evidence.require(evidence)
    if len(given) > CLAIM_MAX:
        raise ValueError(f"claim is limited to {CLAIM_MAX} characters; put the rest in message or cite "
                         "the path in evidence.ref")
    if not given and not text:
        raise ValueError("claim or message is required")
    item: dict[str, Any] = {
        "msg_id": uuid.uuid4().hex[:12], "from": sender, "to": recipient,
        "claim": given or clip(text, CLAIM_MAX), "refs": parse_refs(refs), "evidence": item_evidence,
        "ask": clip(ask, CLAIM_MAX) if str(ask or "").strip() else "", "detail": "", "full": "",
        "chars": len(text), "derived_claim": not given,
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    if item["derived_claim"] and len(text) > CLAIM_MAX:
        item["hint"] = CLAIM_HINT
    if len(text) > ATTACH_MAX:
        item["full"] = str(_write_full(workspace, item, text))
        item["hint"] = item.get("hint") or f"body stored as an attachment ({len(text)} characters)"
    elif text and text != item["claim"]:
        item["detail"] = text
    return item

def _write_full(workspace: Path | str, item: dict[str, Any], text: str) -> Path:
    path = attachment_path(workspace, str(item["msg_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = _evidence.line(item["evidence"]) if item.get("evidence") else "-"
    head = [f"# peer {item['msg_id']}", f"from: {item['from']}", f"to: {item['to']}", f"created: {item['ts']}",
            f"refs: {', '.join(item['refs']) or '-'}", f"evidence: {seen}", f"claim: {item['claim']}"]
    path.write_text("\n".join(head) + "\n\n" + text + "\n", encoding="utf-8")
    return path

def block(item: dict[str, Any]) -> str:
    """What the recipient reads for one envelope: the evidence mark, who, refs, claim, evidence, ask."""
    lines = [f"[peer {item['msg_id']}] from={item['from']} refs={','.join(item['refs']) or '-'} "
             f"evidence={_evidence.line(item.get('evidence'))}", f"claim: {item['claim']}"]
    lines += [f"{label}: {item[field]}" for label, field in (("ask", "ask"), ("text", "detail")) if item.get(field)]
    if item.get("full"):
        lines.append(f"full: {item['full']} ({item['chars']} chars)")
    if item.get("deduped"):
        lines.append("quote: duplicate of an earlier message, dropped; use refs")
    mark = _evidence.marker(item.get("evidence"))  # 4.3 P27: a non-receipt claim is flagged first
    if mark:
        lines.insert(0, mark)
    return "\n".join(lines)

def batch_body(items: Sequence[dict[str, Any]]) -> str:
    if len(items) == 1:
        return block(items[0])
    return "\n".join([f"[peer batch ×{len(items)}] merged at your turn boundary; answer them together.",
                      *(block(item) for item in items)])

def shingles(text: Any, size: int = SHINGLE) -> set[tuple[str, ...]]:
    """The set of line n-grams of ``text`` (lines are whitespace-normalized, empties dropped)."""
    lines = [line for line in (" ".join(row.split()) for row in str(text or "").splitlines()) if line]
    if len(lines) < size:
        return {tuple(lines)} if lines else set()
    return {tuple(lines[i:i + size]) for i in range(len(lines) - size + 1)}

def strip_repeats(items: Iterable[dict[str, Any]], recent: Iterable[str]) -> list[dict[str, Any]]:
    """Drop a body whose lines repeat ``recent`` (Jaccard >= ``DEDUP_JACCARD``); keep its refs."""
    seen = [sh for text in recent for sh in (shingles(text),) if sh]
    out = list(items)
    for item in out:
        detail = str(item.get("detail") or "")
        if not detail or item.get("full"):
            continue
        mine = shingles(detail)
        if mine and any(len(mine & other) / len(mine | other) >= DEDUP_JACCARD for other in seen):
            item["detail"], item["deduped"] = "", True
        else:
            seen.append(mine)
    return out

def recent_bodies(workspace: Path | str, recipient: str, limit: int = DEDUP_RECENT) -> list[str]:
    """The newest quoted bodies already delivered to ``recipient`` (the dedupe reference)."""
    rows = _rows(quotes_path(workspace, recipient))[-max(1, int(limit)):]
    return [str(row["text"]) for row in rows if str(row.get("text") or "")]

@contextlib.contextmanager
def _locked(path: Path, *, create: bool = True):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | (os.O_CREAT if create else 0), 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

def _slurp(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while chunk := os.read(fd, 1 << 16):
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")

def _append(fd: int, lines: Iterable[str]) -> None:
    """Append JSON lines, trimming the file to its newest ``INDEX_KEEP`` lines past the hard cap."""
    os.lseek(fd, 0, os.SEEK_END)
    for line in lines:
        os.write(fd, (line + "\n").encode("utf-8"))
    if os.fstat(fd).st_size > INDEX_HARD_BYTES:
        keep = _slurp(fd).splitlines()[-INDEX_KEEP:]
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        for row in keep:
            os.write(fd, (row + "\n").encode("utf-8"))

def queue(workspace: Path | str, recipient: str, items: Iterable[dict[str, Any]]) -> int:
    lines = [json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in items]
    if lines:
        with _locked(pending_path(workspace, recipient)) as fd:
            _append(fd, lines)
    return len(lines)

def take_pending(workspace: Path | str, recipient: str) -> list[dict[str, Any]]:
    """Pop every queued envelope of one recipient atomically (read then truncate under the lock)."""
    path = pending_path(workspace, recipient)
    if not path.is_file():
        return []
    try:
        with _locked(path, create=False) as fd:
            raw = _slurp(fd)
            os.ftruncate(fd, 0)
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("msg_id"):
            out.append(item)
    return out

def _rows(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows

def seen(workspace: Path | str, key: str) -> bool:
    return bool(key) and any(str(row.get("key") or "") == key for row in _rows(peer_dir(workspace) / "index.jsonl"))

def remember(workspace: Path | str, key: str, msg_id: str) -> None:
    if key:
        with _locked(peer_dir(workspace) / "index.jsonl") as fd:
            _append(fd, [json.dumps({"key": key, "msg_id": msg_id}, ensure_ascii=False)])

def remember_quotes(workspace: Path | str, recipient: str, items: Iterable[dict[str, Any]]) -> None:
    """Record what one delivery quoted, so a later repeat can be stripped down to its refs."""
    lines = [json.dumps({"msg_id": str(item.get("msg_id") or ""), "text": str(item["detail"])},
                        ensure_ascii=False, separators=(",", ":"))
             for item in items if str(item.get("detail") or "").strip()]
    if lines:
        with _locked(quotes_path(workspace, recipient)) as fd:
            _append(fd, lines)

def record(workspace: Path | str, problem: str, recipient: str, item: dict[str, Any]) -> str:
    """One recipient-ledger row (``source=peer``) citing the sender's message id and evidence class."""
    doc = _evidence.normalize(item.get("evidence"))
    evidence = [item["full"]] if item.get("full") else ([doc["ref"]] if doc["ref"] else [])
    try:
        row = note.write(Ledger(workspace, problem, recipient), {
            "type": "insight", "claim": item["claim"], "confidence": "unverified",
            "why": f"peer message {item['msg_id']} from {item['from']} (evidence {doc['class']})",
            "evidence": evidence, "links": list(item.get("refs") or []),
        }, source="peer")
    except Exception:
        return ""  # a ledger that cannot be written must not swallow the message
    return str(row.get("id") or "")

def send(*, workspace: Path | str, sender: str, problem: str, targets: Sequence[str],
         broadcast: bool, request_id: str = "", claim: Any = "", message: Any = "", refs: Any = None,
         evidence: Any = None, ask: Any = "", flush: Callable[[str], None] | None = None,
         problems: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Queue one envelope per target and return the tool reply (a duplicate request is not queued).

    ``problems`` maps a target to *its own* problem, so a workspace-wide broadcast writes each
    recipient's ledger row into that recipient's problem instead of the sender's.
    """
    queued: list[dict[str, Any]] = []
    duplicates: list[str] = []
    for target in targets:
        key = f"peer:{sender}:{target}:{request_id}" if request_id else ""
        if key and seen(workspace, key):
            duplicates.append(target)
            continue
        item = envelope(workspace, sender=sender, recipient=target, claim=claim, message=message,
                        refs=refs, evidence=evidence, ask=ask)
        strip_repeats([item], recent_bodies(workspace, target))
        queue(workspace, target, [item])
        remember(workspace, key, item["msg_id"])
        ledger_problem = (problems or {}).get(target) or problem
        queued.append({"recipient": target, "msg_id": item["msg_id"], "claim": item["claim"],
                       "chars": item["chars"], "attachment": item.get("full", ""), "hint": item.get("hint", ""),
                       "deduped": bool(item.get("deduped")),
                       "evidence": _evidence.line(item.get("evidence")),
                       "ledger_id": record(workspace, ledger_problem, target, item)})
        if flush is not None:
            try:
                flush(target)
            except Exception:
                pass
    out: dict[str, Any] = {"ok": True, "duplicate": not queued, "queued": queued, "duplicates": duplicates,
                           "delivery": "next turn boundary"}
    hint = next((q["hint"] for q in queued if q["hint"]), "")
    if hint:
        out["hint"] = hint
    if broadcast:
        out["recipients"] = [q["recipient"] for q in queued]
        out["broadcast"] = True
    elif queued:
        out.update({k: queued[0][k] for k in ("recipient", "msg_id", "attachment", "deduped", "ledger_id",
                                              "evidence")})
    elif duplicates:
        out["recipient"] = duplicates[0]
    return out

def broadcast_path(workspace: Path | str) -> Path:
    """``<peer>/broadcast.json``: sender -> epoch of its last workspace-wide broadcast."""
    return peer_dir(workspace) / "broadcast.json"


def _broadcast_state(workspace: Path | str) -> dict[str, float]:
    try:
        doc = json.loads(broadcast_path(workspace).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, float] = {}
    for key, value in (doc or {}).items() if isinstance(doc, dict) else []:
        try:
            out[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def broadcast_gate(workspace: Path | str, sender: str, *, role: str = "worker",
                   now: float | None = None) -> dict[str, Any]:
    """Whether ``sender`` may send a workspace-wide broadcast now (4.3 P27).

    A worker seat gets one broadcast per ``BROADCAST_MIN_SEC``; the orchestrator is not limited.
    The reply is always a dict so a caller can explain a refusal instead of delivering.
    """
    stamp = float(now if now is not None else time.time())
    limited = str(role or "").strip().lower() not in ("", "orchestrator")
    last = _broadcast_state(workspace).get(str(sender), 0.0) if limited else 0.0
    wait = max(0.0, float(last) + BROADCAST_MIN_SEC - stamp) if last else 0.0
    return {"allowed": not limited or wait <= 0.0, "limited": limited, "retry_after_sec": round(wait, 1),
            "window_sec": BROADCAST_MIN_SEC, "last_epoch": last or None}


def note_broadcast(workspace: Path | str, sender: str, *, now: float | None = None) -> None:
    """Record one delivered broadcast so the next one from this seat respects the window."""
    stamp = float(now if now is not None else time.time())
    with _locked(broadcast_path(workspace).with_suffix(".lock")) as _fd:
        state = _broadcast_state(workspace)
        state[str(sender)] = stamp
        keep = dict(sorted(state.items(), key=lambda kv: kv[1])[-BROADCAST_KEEP:])
        path = broadcast_path(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(keep, separators=(",", ":")), encoding="utf-8")


def result_envelope(workspace: Path | str, *, agent_id: str, text: str, problem: str = "") -> dict[str, Any]:
    item: dict[str, Any] = {"msg_id": uuid.uuid4().hex[:12], "from": agent_id, "to": "orchestrator",
                            "claim": clip(text, CLAIM_MAX), "refs": [], "evidence": "", "ask": "", "detail": "",
                            "chars": len(text), "derived_claim": True, "problem": problem,
                            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    item["full"] = str(_write_full(workspace, item, text))
    if len(text) <= ATTACH_MAX:
        item["detail"] = text
    return item

def result_body(item: dict[str, Any]) -> str:
    """The bus body of a result: verbatim under ``ATTACH_MAX``, otherwise claim plus attachment path."""
    if item.get("detail"):
        return str(item["detail"])
    return f"[result {item['msg_id']}] claim: {item['claim']}\nfull: {item['full']} ({item['chars']} chars)"

def result_row(store: Any, workspace: Path | str, *, agent_id: str, text: Any, direction: str,
               commit_sha: str = "", problem: str = "") -> dict[str, Any]:
    """Persist one turn reply: full text on disk, claim plus path on the bus when it is long."""
    body = str(text or "")
    if not body.strip():
        return {}
    try:
        payload = result_body(result_envelope(workspace, agent_id=agent_id, text=body, problem=problem))
    except Exception:
        payload = body  # never lose a turn result to an attachment failure
    return store.append_message(agent_id, direction, "result", payload, commit_sha=commit_sha, problem=problem)

def result_decision(workspace: Path | str, *, agent_id: str, problem: str, item: dict[str, Any],
                    sha: str = "") -> str:
    """The ledger ``decision`` one stage result writes, citing the attachment that holds the text."""
    try:
        row = note.write(Ledger(workspace, problem, agent_id), {
            "type": "decision", "claim": item["claim"], "confidence": "inferred",
            "evidence": [item["full"]], "sha": sha, "why": f"stage result {item['msg_id']}",
        }, source="worker")
    except Exception:
        return ""
    return str(row.get("id") or "")
