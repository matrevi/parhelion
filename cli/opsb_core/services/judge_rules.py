"""Judge rule snapshots (4.3 P22): one problem's public rule metadata under ``results/<title>``.

The rule endpoint (``/api/problems/{pid}``) is already read by the receipt reconciler and by the
submission poll, and it carries what the gate and the reports need: the scoring mode, whether a
baseline is used, whether the board keeps the best score or the last submission, the judge revision
and the testcase roster. It also carries the problem statement, the code template and the author's
account id, none of which belong on disk. :func:`normalize` therefore copies a fixed whitelist and
drops everything else, and :func:`save` writes ``results/<title>/.judge-rule.json`` only when that
whitelist changes, so a reconciler that runs every minute does not churn the file.

``fetched_at`` is the time the current content was first read: an unchanged rule keeps its original
timestamp, which makes the field tell "since when has the platform known this rule".

An absent or partial snapshot is never an error. Every reader takes a missing field as "Judge did
not publish it" and falls back to ``problems-all.json`` and then to the workspace manifest, exactly
as 4.2.1 did (``scheduler.estimator._load_problems``).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from .. import config as _cfg

SNAPSHOT = ".judge-rule.json"
SCHEMA = 1
SCALARS = ("score_mode", "use_baseline", "ranking_submission_mode", "iterations", "judge_revision",
           "ranking_testcase_count")
# Judge has not published a per-case time limit in the contests this platform has run; the field is
# recorded when a generation of the endpoint starts to, and omitted otherwise.
LIMIT_FIELDS = ("time_limit", "time_limit_ms", "time_limit_sec", "time_limit_s", "judge_time_limit",
                "limits")


def path_for(workspace: Path | str, title: str) -> Path:
    return Path(workspace) / "results" / str(title) / SNAPSHOT


def _visibility(*sources: dict[str, Any]) -> bool | None:
    """True when the roster entry is hidden, False when it is public, None when it says nothing."""
    for source in sources:
        for key in ("hidden", "is_hidden"):
            if source.get(key) is not None:
                return bool(source[key])
        for key in ("public", "is_public"):
            if source.get(key) is not None:
                return not bool(source[key])
    return None


def roster(rule: dict[str, Any]) -> dict[str, Any] | None:
    """Case count by type, plus by visibility when every roster entry declares it."""
    cases = rule.get("testcases")
    if not isinstance(cases, list) or not cases:
        return None
    summary: dict[str, Any] = {"total": 0, "by_type": {}}
    public = hidden = 0
    for ref in cases:
        if not isinstance(ref, dict):
            return None
        inner = ref.get("testcase_id") if isinstance(ref.get("testcase_id"), dict) else {}
        kind = str(inner.get("type") or ref.get("type") or "unknown")
        summary["by_type"][kind] = summary["by_type"].get(kind, 0) + 1
        summary["total"] += 1
        hidden_case = _visibility(inner, ref)
        hidden += int(hidden_case is True)
        public += int(hidden_case is False)
    if public + hidden == summary["total"]:
        summary["public"], summary["hidden"] = public, hidden
    return summary


def _limit(rule: dict[str, Any]) -> dict[str, Any] | None:
    for key in LIMIT_FIELDS:
        value = rule.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float, str)):
            return {"field": key, "value": value}
        if isinstance(value, dict) and value:
            return {"field": key, "value": value}
    return None


def normalize(rule: Any, *, fetched_at: str, problem_id: str = "") -> dict[str, Any] | None:
    """The whitelisted snapshot of one rule payload, or None when it is not a rule at all."""
    if not isinstance(rule, dict) or not rule:
        return None
    if "score_mode" not in rule and "testcases" not in rule:
        return None
    out: dict[str, Any] = {"schema_version": SCHEMA}
    pid = str(rule.get("_id") or rule.get("problem_id") or problem_id or "").strip()
    if pid:
        out["problem_id"] = pid
    for key in SCALARS:
        value = rule.get(key)
        if isinstance(value, bool) or isinstance(value, (int, float, str)):
            out[key] = str(value).strip().lower() if key == "ranking_submission_mode" else value
    cases = roster(rule)
    if cases is not None:
        out["testcases"] = cases
    limit = _limit(rule)
    if limit is not None:
        out["time_limit"] = limit
    out["fetched_at"] = fetched_at
    return out


def load(workspace: Path | str, title: str) -> dict[str, Any] | None:
    """The stored snapshot of one problem, or None when it was never written or is unreadable."""
    try:
        doc = json.loads(path_for(workspace, title).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def snapshots(workspace: Path | str) -> dict[str, dict[str, Any]]:
    """Every stored snapshot under ``results/``, keyed by problem title."""
    root = Path(workspace) / "results"
    out: dict[str, dict[str, Any]] = {}
    try:
        titles = [entry for entry in root.iterdir() if entry.is_dir()]
    except OSError:
        return out
    for entry in titles:
        doc = load(workspace, entry.name)
        if doc is not None:
            out[entry.name] = doc
    return out


def board_modes(workspace: Path | str) -> dict[str, str]:
    """``{title: ranking_submission_mode}`` from the snapshots; problems without one are absent."""
    return {title: str(doc["ranking_submission_mode"]) for title, doc in snapshots(workspace).items()
            if doc.get("ranking_submission_mode")}


def _content(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in snapshot.items() if key != "fetched_at"}


def _write(path: Path, snapshot: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def save(workspace: Path | str, title: str, rule: Any, *, fetched_at: str = "") -> dict[str, Any]:
    """Store one rule when its normalized content changed; never raises on a bad payload."""
    title = str(title or "").strip()
    if not title:
        return {"saved": False, "reason": "no title"}
    snapshot = normalize(rule, fetched_at=fetched_at or _cfg.utc_now().isoformat())
    if snapshot is None:
        return {"saved": False, "reason": "not a rule payload"}
    try:
        current = load(workspace, title)
        if current is not None and _content(current) == _content(snapshot):
            return {"saved": False, "unchanged": True, "snapshot": current}
        _write(path_for(workspace, title), snapshot)
    except OSError as exc:
        return {"saved": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {"saved": True, "snapshot": snapshot}


def sigma_hat(workspace: Path | str, title: str | None = None) -> dict[str, Any]:
    """``judge_noise.sigma_for(title)``, or the contest-wide pooled estimate when ``title`` is None."""
    try:
        _cfg.ensure_scheduler_path()
        from scheduler import judge_noise

        if title:
            return judge_noise.sigma_for(workspace, title) or {}
        pooled = judge_noise.estimate(workspace).get("pooled") or {}
        return ({"sigma": pooled["sigma"], "df": pooled.get("df"), "scope": "contest"}
                if pooled.get("sigma") is not None else {})
    except Exception:  # noqa: BLE001 - a noise estimate is a convenience, never a precondition
        return {}


def status_line(workspace: Path | str, problems: list[dict[str, Any]]) -> dict[str, Any]:
    """4.3 P22: the one brief line — board mode and judge noise for the problems this reply lists.

    One problem (a worker's own, or an explicit ``problem``) reports that problem's mode and sigma;
    the whole-workspace brief reports the mode tally and the contest-wide pooled sigma, because
    fourteen per-problem sigmas do not fit the 2 KB brief.
    """
    titles = [str(p.get("title") or "") for p in problems if isinstance(p, dict) and p.get("title")]
    if not titles:
        return {}
    modes = board_modes(workspace)
    if len(titles) == 1:
        line: dict[str, Any] = {"board_mode": modes.get(titles[0]) or "unknown"}
        noise = sigma_hat(workspace, titles[0])
    else:
        tally: dict[str, int] = {}
        for title in titles:
            mode = modes.get(title)
            if mode:
                tally[mode] = tally.get(mode, 0) + 1
        line = {"board_modes": dict(sorted(tally.items()))}
        noise = sigma_hat(workspace)
    if noise:
        line.update({"sigma_hat": noise.get("sigma"), "scope": noise.get("scope"), "df": noise.get("df")})
    return line


def reconstruct_latest_enabled(cfg: dict[str, Any] | None = None) -> bool:
    """``judge.reconstruct_latest`` (default true): replay scores under the ``latest`` board mode."""
    try:
        doc = cfg if cfg is not None else _cfg.ConfigRepository.load()
    except Exception:  # noqa: BLE001 - a broken config must not change scoring behaviour
        return True
    block = doc.get("judge") if isinstance(doc, dict) else None
    if not isinstance(block, dict) or "reconstruct_latest" not in block:
        return True
    return bool(block.get("reconstruct_latest"))


def _problems(workspace: Path) -> list[dict[str, Any]]:
    from .contest_service import resolve_workspace_meta

    rows = resolve_workspace_meta(workspace).get("problems") or []
    return [{"title": str(p.get("title") or ""), "problem_id": str(p.get("problem_id") or "")}
            for p in rows if isinstance(p, dict) and p.get("title")]


def refresh(
    workspace: Path | str,
    titles: list[str] | None = None,
    *,
    timeout: float = 10.0,
    fetch: Callable[[str, str], Any] | None = None,
) -> dict[str, Any]:
    """Read each problem's rule with the platform's own login state and store the snapshot.

    The caller never sees or handles a credential: the session files under ``~/.parhelion`` are
    resolved here. Without a logged-in account the report names the problems that were skipped and
    returns normally, because a missing login is a state, not a failure.
    """
    from .judge_score_fetch import fetch_json_with_cookie
    from .orphan_receipts import resolve_sessions_for_account

    ws = Path(workspace).expanduser().resolve()
    targets = _problems(ws)
    if titles:
        wanted = {str(t).strip() for t in titles if str(t).strip()}
        targets = [p for p in targets if p["title"] in wanted]
    out: dict[str, Any] = {"ok": True, "workspace": str(ws), "logged_in": False,
                           "snapshots": [], "missing": [], "errors": []}
    sessions = resolve_sessions_for_account("primary")
    if not sessions or not targets:
        out["logged_in"] = bool(sessions)
        out["missing"] = [p["title"] for p in targets]
        out["reason"] = "no logged-in Judge account" if not sessions else "no problems in the manifest"
        return out
    out["logged_in"] = True
    base, cookie, _uid = sessions[0]
    for problem in targets:
        title, pid = problem["title"], problem["problem_id"]
        if not pid:
            out["missing"].append(title)
            continue
        try:
            rule = (fetch or (lambda b, p, t=timeout: fetch_json_with_cookie(b, cookie, f"/api/problems/{p}", timeout=t)))(base, pid)
        except Exception as exc:  # noqa: BLE001 - one unreadable rule must not stop the sweep
            out["errors"].append({"title": title, "error": type(exc).__name__})
            continue
        stored = save(ws, title, rule)
        snapshot = stored.get("snapshot") or {}
        out["snapshots"].append({
            "title": title, "saved": bool(stored.get("saved")),
            "mode": snapshot.get("ranking_submission_mode"),
            "score_mode": snapshot.get("score_mode"),
            "cases": (snapshot.get("testcases") or {}).get("total"),
            "fetched_at": snapshot.get("fetched_at"),
        })
    return out
