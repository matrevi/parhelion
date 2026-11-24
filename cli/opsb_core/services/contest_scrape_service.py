"""Judge public-contest scraper: resolve a contest link/id into its problem list.

The public contest pages are an SPA; the same data is served by three read-only
JSON endpoints:

``GET /api/groups/public``                      -> the public group id
``GET /api/contests/group/<group_id>``          -> every public contest of that group
``GET /api/problems/contest/<contest_id>``      -> the contest's problems

Nothing here writes to Judge and no login cookie is required.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

DEFAULT_BASE_URL = "https://judge.invalid"
_USER_AGENT = "parhelion/1.0 (contest scaffold)"
_OBJECT_ID_RE = re.compile(r"^[0-9a-f]{24}$", re.IGNORECASE)


class ContestScrapeError(RuntimeError):
    """Raised when a contest link/id sdkot be resolved into a problem list."""


def parse_contest_ref(ref: str, base_url: str = DEFAULT_BASE_URL) -> dict[str, str]:
    """Normalize a contest link, object id or bare name into ``{base_url, contest_name, contest_id}``.

    Accepted shapes::

        https://judge.invalid/public/<contest>[/...]
        https://judge.invalid/<group>/<contest>[/...]
        <24-hex object id>
        <contest name>
    """
    raw = (ref or "").strip()
    if not raw:
        raise ContestScrapeError("empty contest link/id")
    if "://" in raw:
        parsed = urlparse(raw)
        base = f"{parsed.scheme}://{parsed.netloc}" or base_url.rstrip("/")
        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            raise ContestScrapeError(f"sdkot find a contest name in {raw!r}")
        if parts[0] == "public":
            parts = parts[1:]
        elif len(parts) >= 2:
            parts = parts[1:]  # drop the group token, keep the contest token
        if not parts:
            raise ContestScrapeError(f"sdkot find a contest name in {raw!r}")
        token = parts[0]
        return {"base_url": base.rstrip("/"), "contest_name": "" if _OBJECT_ID_RE.match(token) else token,
                "contest_id": token if _OBJECT_ID_RE.match(token) else ""}
    if _OBJECT_ID_RE.match(raw):
        return {"base_url": base_url.rstrip("/"), "contest_name": "", "contest_id": raw}
    return {"base_url": base_url.rstrip("/"), "contest_name": raw, "contest_id": ""}


def _get_json(base_url: str, path: str, timeout: float) -> Any:
    url = f"{base_url.rstrip('/')}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise ContestScrapeError(f"GET {url} -> HTTP {exc.code}") from exc
    except Exception as exc:  # noqa: BLE001 - network failure surfaces as scrape error
        raise ContestScrapeError(f"GET {url} failed: {exc}") from exc
    try:
        return json.loads(body)
    except ValueError as exc:
        raise ContestScrapeError(f"GET {url} returned non-JSON payload") from exc


def fetch_contest_problems(
    ref: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Resolve ``ref`` into ``{contest: {...}, problems: [...]}`` ready for the scaffolder."""
    parsed = parse_contest_ref(ref, base_url=base_url)
    base = parsed["base_url"]
    contest_id = parsed["contest_id"]
    contest_name = parsed["contest_name"]
    contest: dict[str, Any] = {}

    if not contest_id:
        group = _get_json(base, "/api/groups/public", timeout)
        group_id = str(group.get("_id") or group.get("id") or "")
        if not group_id:
            raise ContestScrapeError("public group id missing in /api/groups/public")
        contests = _get_json(base, f"/api/contests/group/{group_id}", timeout)
        if not isinstance(contests, list):
            raise ContestScrapeError("unexpected /api/contests/group payload")
        match = next(
            (c for c in contests if str(c.get("name") or "").strip().lower() == contest_name.lower()),
            None,
        )
        if match is None:
            names = ", ".join(str(c.get("name") or "?") for c in contests[:8])
            raise ContestScrapeError(f"contest {contest_name!r} not found in public group (e.g. {names} ...)")
        contest = match
        contest_id = str(match.get("_id") or match.get("id") or "")
        if not contest_id:
            raise ContestScrapeError(f"contest {contest_name!r} has no id")

    rows = _get_json(base, f"/api/problems/contest/{contest_id}", timeout)
    if not isinstance(rows, list) or not rows:
        raise ContestScrapeError(f"contest {contest_id} exposes no public problems")

    problems: list[dict[str, Any]] = []
    for row in rows:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        tags = row.get("tags") if isinstance(row.get("tags"), list) else []
        problems.append(
            {
                "title": title,
                "slug": str(row.get("name") or "").strip(),
                "problem_id": str(row.get("_id") or row.get("id") or ""),
                "difficulty": int(row.get("difficulty") or 2),
                "kernel_pattern": str(row.get("kernel_pattern") or ""),
                "sdk_version": str(row.get("sdk_version") or ""),
                "soc_version": str(row.get("soc_version") or ""),
                "tags": [str(t) for t in tags],
                "problem_url": f"{base}/public/{contest.get('name') or contest_name or contest_id}/{row.get('name') or ''}".rstrip("/"),
            }
        )
    if not problems:
        raise ContestScrapeError(f"contest {contest_id} returned no usable problem titles")

    return {
        "ok": True,
        "source": base,
        "contest": {
            "id": contest_id,
            "name": str(contest.get("name") or contest_name or ""),
            "title": str(contest.get("title") or ""),
            "start_time": str(contest.get("start_time") or ""),
            "problem_count": len(problems),
        },
        "problems": problems,
    }
