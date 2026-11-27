"""``opsb docs``: the local documentation mirror the doc scout reads first (4.4 P34).

The mirror is ``guidance.scout.docs_dir`` when set, else ``<PARHELION_HOME>/docs-mirror``. It starts
empty and grows as workers use the network: every seat is told (scout contract, dossier index) to save an
official page it fetched as ``<mirror>/web/<name>.md`` with ``source: <url>`` on the first line, so the
next seat reads it locally. The worker sandbox and the boundary detector allow that directory. The
commands below are for the administrator:

``opsb docs status``          the directory, file count and size;
``opsb docs add <path>...``   copy local files or directories in (an existing file is replaced);
``opsb docs fetch <url>...``  save one page per URL as a text file headed by its source URL.

Which documentation to mirror is a deployment decision: the repository names no source.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import urllib.request
from pathlib import Path
from typing import Any

FETCH_TIMEOUT_SEC = 30
FETCH_MAX_BYTES = 8 * 1024 * 1024


def mirror_dir() -> Path:
    from .. import config as _cfg
    from ..guidance import settings
    configured = str((settings().get("scout") or {}).get("docs_dir") or "").strip()
    return Path(configured).expanduser() if configured else Path(_cfg._resolve_parhelion_home()) / "docs-mirror"


SAVE_HINT = "save each official page you fetch as {dir}/web/<short-name>.md with `source: <url>` on line 1"


def status() -> dict[str, Any]:
    root = mirror_dir()
    files = [p for p in root.rglob("*") if p.is_file()] if root.is_dir() else []
    return {"ok": True, "docs_dir": str(root), "files": len(files), "bytes": sum(p.stat().st_size for p in files)}


def add(paths: list[str]) -> dict[str, Any]:
    root = mirror_dir()
    root.mkdir(parents=True, exist_ok=True)
    copied = []
    for raw in paths:
        src = Path(raw).expanduser().resolve()
        if not src.exists():
            raise ValueError(f"{raw} does not exist")
        dst = root / src.name
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        copied.append(str(dst))
    return {**status(), "added": copied}


def _name_for(url: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", url.split("://", 1)[-1]).strip("-")[:80] or "page"
    return f"{stem}-{hashlib.sha256(url.encode('utf-8')).hexdigest()[:8]}.txt"


def fetch(urls: list[str]) -> dict[str, Any]:
    root = mirror_dir() / "web"
    root.mkdir(parents=True, exist_ok=True)
    saved = []
    for url in urls:
        if not url.startswith(("https://", "http://")):
            raise ValueError(f"not an http(s) URL: {url}")
        req = urllib.request.Request(url, headers={"User-Agent": "parhelion-docs-mirror"})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as resp:  # noqa: S310 - user-named URL
            body = resp.read(FETCH_MAX_BYTES).decode("utf-8", errors="replace")
        target = root / _name_for(url)
        target.write_text(f"source: {url}\n\n{body}", encoding="utf-8")
        saved.append(str(target))
    return {**status(), "fetched": saved}


def run_docs(args: argparse.Namespace) -> int:
    sub = getattr(args, "docs_cmd", "status") or "status"
    try:
        res = add(args.paths) if sub == "add" else fetch(args.urls) if sub == "fetch" else status()
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}) if args.json else f"Error: {exc}")
        return 2
    print(json.dumps(res, ensure_ascii=False) if args.json
          else f"{res['docs_dir']}: {res['files']} files, {res['bytes']} bytes")
    return 0


def register_docs_subcommands(sub: Any) -> None:
    s = sub.add_parser("docs", help="Local documentation mirror read by the doc scout (4.4 P34)")
    s.add_argument("--json", action="store_true")
    d_sub = s.add_subparsers(dest="docs_cmd")
    d_sub.add_parser("status", help="Show the mirror directory, file count and size")
    a = d_sub.add_parser("add", help="Copy local files or directories into the mirror")
    a.add_argument("paths", nargs="+")
    f = d_sub.add_parser("fetch", help="Save one page per URL into the mirror's web/ directory")
    f.add_argument("urls", nargs="+")
    s.set_defaults(func=run_docs)
