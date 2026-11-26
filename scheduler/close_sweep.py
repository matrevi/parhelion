"""Closing reconciliation of a contest (4.3 P25): three lists, the window, the slot hint.

The last hours of a contest are lost to differences nobody compares: the board may sit below the
problem's own historical best, the historical-best commit may live only on a race branch while
``main`` moved past it, and the ``best/*`` tag may still name yesterday's SHA. This module answers
those three questions per problem and nothing else:

* **restore** — on a ``latest`` board, the closing score is below the problem's raw historical best
  (or the board is zero/failed), so a rollback is worth pricing. A ``best`` board keeps its best
  score, so its list is empty by construction.
* **merge** — the historical-best SHA is not on the problem's ``main``. The list names the branches
  that contain it; nothing is merged, ever.
* **tag** — no ``best/<score>-<sha7>`` tag points at the historical best (the tags are stale).

Two comparisons decide the board currency. The sweep replays the receipt journal as of the sweep
instant (``min(now, closing.contest_end)``) instead of reading ``results/score-snapshot.json``: the
snapshot is re-scored against a tbest that keeps moving after the contest, so it is not the closing
result the G5 gap is defined on (00-PLAN §2). The live refresh is still shown per row as
``live_score`` when a snapshot exists.

Git is read the way ``cli.opsb_core.services.git_fast`` reads HEAD: loose refs and ``packed-refs``
directly, no subprocess, except one bounded ``git branch --contains`` per problem that needs the
branch list. The repositories are treated as read-only; the sweep never writes to them and never
merges.

The closing window (``closing.contest_end``, ``closing.window_min``, ``closing.restore_boost``)
lives in ``~/.parhelion/config.json`` under ``scheduler.closing``; ``contest_end`` is filled from
the contest metadata when the config leaves it empty. The window only reorders restore priorities
inside the gate — it is not a threshold and quota never enters it.
"""
from __future__ import annotations

import datetime
import json
import pathlib
import re
from typing import Any

from .estimator import _parse_iso
from .intent_pricing import DEFAULT_SIGMA
from .parhelion_paths import read_parhelion_scheduler_config

DEFAULT_WINDOW_MIN = 90.0
DEFAULT_BOOST = 3.0
DEFAULT_MIN_GAP = 0.0
FAILED_STATUS = frozenset({"Wrong Answer", "Time Limit Exceeded", "Runtime Error", "Compile Error",
                           "Memory Limit Exceeded", "System Error", "Fail"})
END_KEYS = ("end_time", "endTime", "contest_end", "close_time")
TAG_NAME = re.compile(r"(\d+(?:\.\d+)?)(?:-([0-9a-f]{7,40}))?$")
SLOT_LOW_PCT = 60.0
SLOT_DAY_PCT = 50.0
BEIJING = datetime.timezone(datetime.timedelta(hours=8))


def _utc(value: datetime.datetime) -> datetime.datetime:
    return value.astimezone(datetime.timezone.utc) if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _sha7(value: Any) -> str:
    return str(value or "")[:7].lower()


# ---------------------------------------------------------------- closing window configuration

def _metadata_end(workspace: pathlib.Path) -> tuple[str, str]:
    """``(end ISO, source)`` from the contest metadata on disk; ``("", "")`` when it is absent."""
    evidence = pathlib.Path(workspace) / "results" / "scoring-evidence"
    for name, source in (("contest-detail.json", "contest-detail"), ("problems-all.json", "problems-all")):
        try:
            doc = json.loads((evidence / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        items = doc.values() if isinstance(doc, dict) else doc if isinstance(doc, list) else []
        if name == "contest-detail.json" and isinstance(doc, dict):
            items = [doc]
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in END_KEYS:
                stamp = _parse_iso(str(item.get(key) or ""))
                if stamp is not None:
                    return _utc(stamp).isoformat(), source
    return "", ""


def closing_config(workspace: pathlib.Path | str) -> dict[str, Any]:
    """``scheduler.closing`` with the contest end filled from the metadata when the config is empty."""
    raw = read_parhelion_scheduler_config().get("closing")
    raw = raw if isinstance(raw, dict) else {}
    end = str(raw.get("contest_end") or "").strip()
    source = "config"
    if not end:
        end, source = _metadata_end(pathlib.Path(workspace))
    return {
        "contest_end": end or None,
        "contest_end_source": source if end else "none",
        "window_min": float(raw.get("window_min") or DEFAULT_WINDOW_MIN),
        "restore_boost": float(raw.get("restore_boost") or DEFAULT_BOOST),
        "restore_min_gap": float(raw.get("restore_min_gap") or DEFAULT_MIN_GAP),
    }


def window_state(config: dict[str, Any], now: datetime.datetime | None = None) -> dict[str, Any]:
    """The closing window around ``contest_end``: when it opens, whether it is open, how long is left."""
    ref = _utc(now or _now())
    end = _parse_iso(str(config.get("contest_end") or ""))
    out: dict[str, Any] = {"window_min": float(config.get("window_min") or DEFAULT_WINDOW_MIN),
                           "restore_boost": float(config.get("restore_boost") or DEFAULT_BOOST),
                           "in_window": False, "starts_at": None, "contest_end": None,
                           "minutes_left": None, "window_id": ""}
    if end is None:
        return out
    end = _utc(end)
    start = end - datetime.timedelta(minutes=out["window_min"])
    out.update(contest_end=end.isoformat(), starts_at=start.isoformat(),
               minutes_left=round((end - ref).total_seconds() / 60.0, 1),
               in_window=start <= ref <= end, window_id=start.isoformat())
    return out


def repo_facts(repo: pathlib.Path, best_sha: str, want_branches: bool) -> dict[str, Any]:
    """``main`` HEAD, the ``best/*`` tag and, when asked, the branches holding ``best_sha``."""
    from cli.opsb_core.services.git_fast import best_tag_of, branches_containing, main_head, read_refs

    if not (repo / ".git").is_dir():
        return {"repo": str(repo), "main_sha": "", "tag": {}, "branches": [], "git_error": "no repository"}
    refs = read_refs(repo / ".git")
    out: dict[str, Any] = {"repo": str(repo), "main_sha": main_head(repo / ".git"),
                           "tag": best_tag_of(refs), "branches": [], "git_error": ""}
    if want_branches:
        out["branches"], out["git_error"] = branches_containing(repo, best_sha)
    return out


# ---------------------------------------------------------------- the three lists

def _mode(meta: dict[str, Any]) -> str:
    return "latest" if str(meta.get("ranking_submission_mode") or "").strip().lower() == "latest" else "best"


def _sigma(workspace: pathlib.Path, title: str) -> tuple[float, str, int | None]:
    from . import judge_noise

    noise = judge_noise.sigma_for(workspace, title) or {}
    if noise.get("sigma") is not None:
        return float(noise["sigma"]), str(noise.get("scope") or "problem"), noise.get("df")
    return DEFAULT_SIGMA, "constant", None


def _repo_of(workspace: pathlib.Path, meta: dict[str, Any], title: str) -> pathlib.Path:
    if meta.get("path"):
        return workspace / str(meta["path"])
    from cli.opsb_core.device_env import device_text

    return workspace / device_text("work_prefix") / title


def sweep(workspace: pathlib.Path | str, estimator: Any, *, now: datetime.datetime | None = None,
          config: dict[str, Any] | None = None) -> dict[str, Any]:
    """The closing reconciliation document: restore, merge and tag lists plus the window state."""
    ws = pathlib.Path(workspace)
    cfg = config or closing_config(ws)
    window = window_state(cfg, now)
    ref = _utc(now or _now())
    end = _parse_iso(str(cfg.get("contest_end") or ""))
    as_of = min(ref, _utc(end)) if end is not None else ref  # replay the board as of the close
    min_gap = float(cfg.get("restore_min_gap") or DEFAULT_MIN_GAP)
    snapshot = getattr(estimator, "snapshot", {}) or {}

    restore: list[dict[str, Any]] = []
    merge: list[dict[str, Any]] = []
    tag: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    for title in sorted(estimator.problems_meta):
        meta = estimator.problems_meta.get(title) or {}
        mode = _mode(meta)
        ledger = estimator.get_problem_ledger(title, as_of=as_of)
        best_sha = str(ledger.historical_best_sha or "")
        best_score = round(float(ledger.raw_historical_best_score or 0.0), 2)
        board_score = round(float(ledger.current_score or 0.0), 2)
        gap = round(best_score - board_score, 2) if best_sha else 0.0
        live = snapshot.get(title) if isinstance(snapshot.get(title), dict) else {}
        sigma, scope, df = _sigma(ws, title)
        row: dict[str, Any] = {
            "title": title, "mode": mode, "best_sha": _sha7(best_sha), "best_score": best_score,
            "board_sha": _sha7(ledger.active_board_sha), "board_score": board_score,
            "board_status": str(ledger.board_status or ""), "gap": gap,
            "weighted_gap": round(float(ledger.difficulty) * max(0.0, gap), 2),
            "live_score": round(float(live["score"]), 2) if isinstance(live.get("score"), (int, float)) else None,
            "sigma_hat": round(sigma, 2), "sigma_scope": scope, "sigma_df": df,
        }
        facts = repo_facts(_repo_of(ws, meta, title), best_sha, want_branches=bool(best_sha))
        row["main_sha"] = _sha7(facts["main_sha"])
        row["branches"] = facts["branches"]
        if facts["git_error"]:
            row["git_error"] = facts["git_error"]
        failed = row["board_status"] in FAILED_STATUS
        if mode == "latest" and best_sha and (row["board_score"] <= 0 or failed or gap > min_gap):
            restore.append({**row, "critical": row["board_score"] <= 0 or failed,
                            "beyond_sigma": gap > sigma})
        if best_sha and "main" not in facts["branches"] and not row["main_sha"].startswith(_sha7(best_sha)):
            merge.append({"title": title, "best_sha": _sha7(best_sha), "best_score": best_score,
                          "main_sha": row["main_sha"], "branches": facts["branches"],
                          "git_error": facts["git_error"]})
        tags = facts["tag"]
        if best_sha and not _tag_matches(tags.get("tag_on_best") or "", best_score, best_sha):
            tag.append({"title": title, "best_sha": _sha7(best_sha), "best_score": best_score,
                        "tag": tags.get("tag") or "", "tag_sha": tags.get("tag_sha") or "",
                        "target": f"best/{best_score:.2f}-{_sha7(best_sha)}"})
        problems.append(row)

    restore.sort(key=lambda r: (0 if r["critical"] else 1, -r["weighted_gap"], r["title"]))
    merge.sort(key=lambda r: r["title"])
    tag.sort(key=lambda r: r["title"])
    return {
        "view": "close", "as_of": as_of.isoformat(), "board_mode": _mode_counts(problems),
        "closing": window, "restore": restore, "merge": merge, "tag": tag, "problems": problems,
        "totals": {
            "restore": len(restore), "restore_gap": round(sum(r["gap"] for r in restore), 2),
            "restore_weighted_gap": round(sum(r["weighted_gap"] for r in restore), 2),
            "restore_beyond_sigma": sum(1 for r in restore if r["beyond_sigma"]),
            "merge": len(merge), "tag": len(tag),
        },
    }


def _tag_matches(short: str, score: float, sha: str) -> bool:
    """A ``best/`` tag names its own score and SHA (``<score>-<sha7>``), so a stale tag shows up."""
    match = TAG_NAME.match(str(short or ""))
    if not match:
        return False
    try:
        tagged = float(match.group(1))
    except ValueError:
        return False
    return abs(tagged - score) < 0.005 and bool(match.group(2)) and _sha7(sha).startswith(match.group(2)[:7])


def _mode_counts(problems: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in problems:
        out[row["mode"]] = out.get(row["mode"], 0) + 1
    return out


# ---------------------------------------------------------------- per-slot usage and the hint

def slot_rows(slots: list[dict[str, Any]], limit_default: int = 50) -> list[dict[str, Any]]:
    """Per-slot ``used``/``limit``/``utilization`` from account-slot rows, by slot number only."""
    rows: list[dict[str, Any]] = []
    for index, slot in enumerate(slots, start=1):
        limit = slot.get("daily_limit") or slot.get("limit") or limit_default
        used = slot.get("used_today")
        used = used if isinstance(used, (int, float)) else slot.get("used") or 0
        limit = int(limit or limit_default)
        rows.append({"slot": index, "used": int(used), "limit": limit,
                     "utilization": round(100.0 * float(used) / limit, 1) if limit > 0 else 0.0})
    return rows


def day_elapsed_pct(now: datetime.datetime | None = None) -> float:
    """Share of the Beijing scoring day already spent, in percent."""
    ref = (_utc(now or _now())).astimezone(BEIJING)
    midnight = ref.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (ref - midnight).total_seconds() / 86400.0
    return round(100.0 * max(0.0, min(1.0, elapsed)), 1)


def slot_notice(rows: list[dict[str, Any]], day_pct: float) -> str:
    """One display line when a slot is under-used and the day is more than half gone; else ``""``."""
    idle = [row for row in rows if float(row.get("utilization") or 0.0) < SLOT_LOW_PCT]
    if not idle or day_pct <= SLOT_DAY_PCT:
        return ""
    worst = min(idle, key=lambda row: (float(row.get("utilization") or 0.0), row.get("slot") or 0))
    spread = ", ".join(f"slot {row['slot']} {row['used']}/{row['limit']}" for row in rows)
    return (f"slot {worst['slot']} at {worst['utilization']:.0f}% with {day_pct:.0f}% of the day gone "
            f"({spread}); display only, dispatch and gate thresholds are unchanged")


# ---------------------------------------------------------------- the window notice

def window_message(doc: dict[str, Any]) -> str:
    """The strategic message body for the orchestrator inbox: the close view in one paragraph."""
    totals = doc.get("totals") or {}
    window = doc.get("closing") or {}
    parts = [f"Closing window open until {(window.get('contest_end') or '')[:16]}Z "
             f"({window.get('minutes_left')} min left): restore {totals.get('restore')} problem(s) "
             f"for {totals.get('restore_gap')} pts ({totals.get('restore_weighted_gap')} weighted)"]
    if doc.get("restore"):
        parts.append("top: " + ", ".join(f"{r['title']} -{r['gap']}" for r in doc["restore"][:5]))
    if doc.get("merge"):
        parts.append("best SHA only on a branch: " + ", ".join(
            f"{r['title']} {r['best_sha']} ({'/'.join(r['branches'])})" for r in doc["merge"][:5]))
    if doc.get("tag"):
        parts.append(f"{len(doc['tag'])} best/* tag(s) stale")
    parts.append("Run `opsb close-sweep --enqueue` to price them through the gate; view=close has the table")
    return "; ".join(parts)


def window_notice(workspace: pathlib.Path | str, doc: dict[str, Any]) -> dict[str, Any]:
    """Send the window message once per window; the bus ``event_key`` makes the retry a no-op."""
    window = doc.get("closing") or {}
    if not window.get("in_window"):
        return {"sent": False, "reason": "outside the closing window"}
    key = f"closing:{window.get('window_id') or ''}"
    try:
        from cli.opsb_core.agents.bus_store import AgentBusStore

        store = AgentBusStore(pathlib.Path(workspace))
        if store.has_message(key):
            return {"sent": False, "reason": "already sent for this window", "event_key": key}
        msg = store.append_message("platform", "worker_to_orch", "closing_window", window_message(doc),
                                   priority="strategic", event_key=key)
    except Exception as exc:  # a missing bus never blocks the reconciliation
        return {"sent": False, "reason": f"bus unavailable: {type(exc).__name__}", "event_key": key}
    return {"sent": bool(msg), "reason": "" if msg else "already sent for this window", "event_key": key}
