"""Seamless shared Judge & GitCode authentication + batch collaborator invite."""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..config import DEFAULT_BENCH_ROOT, invalidate_ttl_cache, parse_iso, read_ttl_cache, utc_now, write_ttl_cache

GC_CANDIDATES = [
    Path.home() / ".local" / "share" / "pnpm" / "bin" / "gc",
    Path("/opt/homebrew/bin/gc"),
]


def find_gc_bin() -> str:
    for c in GC_CANDIDATES:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return shutil.which("gc") or str(GC_CANDIDATES[0])


def read_judge_status() -> dict[str, Any]:
    cj_path = Path.home() / ".judge" / "session.json"
    info: dict[str, Any] = {
        "logged_in": False,
        "session_file": str(cj_path),
        "base_url": "https://judge.invalid",
        "user_id": None,
        "logged_in_at": None,
        "expires_at": None,
        "remaining_hours": None,
    }
    if not cj_path.is_file():
        return info
    try:
        st = cj_path.stat()
        d = json.loads(cj_path.read_text(encoding="utf-8"))
        uid = d.get("user_id")
        has_cookies = bool(d.get("cookies"))
        dt = parse_iso(str(d["logged_in_at"])) if d.get("logged_in_at") else datetime.datetime.fromtimestamp(st.st_mtime, tz=datetime.timezone.utc)
        if uid and has_cookies and dt:
            exp = dt + datetime.timedelta(days=7)
            rem_h = round((exp - utc_now()).total_seconds() / 3600.0, 1)
            info.update(
                {
                    "logged_in": rem_h > 0,
                    "base_url": d.get("base_url") or "https://judge.invalid",
                    "user_id": uid,
                    "logged_in_at": dt.isoformat(),
                    "expires_at": exp.isoformat(),
                    "remaining_hours": max(0.0, rem_h),
                }
            )
    except Exception as exc:
        info["error"] = str(exc)[:160]
    return info


def read_gitcode_status(use_cache: bool = True) -> dict[str, Any]:
    if use_cache:
        cached = read_ttl_cache("gc_auth", ttl_sec=120.0)
        if isinstance(cached, dict):
            return cached

    gc_bin = find_gc_bin()
    info: dict[str, Any] = {"logged_in": False, "cli_path": gc_bin, "username": None, "protocol": "https"}
    if Path(gc_bin).is_file():
        try:
            proc = subprocess.run([gc_bin, "auth", "status", "--no-update-check"], capture_output=True, text=True, timeout=3.5)
            combined = f"{proc.stdout}\n{proc.stderr}"
            m_user = re.search(r"Logged in as\s+([A-Za-z0-9_.-]+)", combined)
            m_proto = re.search(r"protocol:\s*([A-Za-z0-9]+)", combined)
            if m_user:
                info["logged_in"] = True
                info["username"] = m_user.group(1)
            if m_proto:
                info["protocol"] = m_proto.group(1)
        except Exception as exc:
            info["error"] = str(exc)[:160]
    write_ttl_cache("gc_auth", info)
    return info


def get_auth_status(use_cache: bool = True) -> dict[str, Any]:
    return {"judge": read_judge_status(), "gitcode": read_gitcode_status(use_cache=use_cache)}


def fetch_judge_captcha() -> dict[str, Any]:
    judge_dir = DEFAULT_BENCH_ROOT / "skills" / "judge-submit"
    venv_py = DEFAULT_BENCH_ROOT / ".venv" / "bin" / "python"
    py_exe = str(venv_py) if venv_py.is_file() else sys.executable
    code = (
        "import json, sys, tempfile, pathlib\n"
        f"sys.path.insert(0, {str(judge_dir)!r})\n"
        "from judge_cli import JudgeClient\n"
        "c = JudgeClient()\n"
        "r = c.session.get(c.base_url + '/api/users/captcha', timeout=15)\n"
        "d = r.json()\n"
        "cid = d.get('captchaId') or d.get('id') or d.get('data', {}).get('captchaId', '')\n"
        "svg = d.get('svg') or d.get('img') or d.get('data', {}).get('svg', '')\n"
        "p = pathlib.Path(tempfile.gettempdir()) / f'opsb-captcha-{cid[:8] or \"latest\"}.svg'\n"
        "if svg: p.write_text(svg)\n"
        "print(json.dumps({'ok': bool(svg), 'captcha_id': cid, 'svg_path': str(p) if svg else None}))\n"
    )
    proc = subprocess.run([py_exe, "-c", code], capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout).strip()[:200]}
    return json.loads(proc.stdout.strip().splitlines()[-1])


def login_judge(account: str, password: str, login_type: str = "auto", captcha_id: str = "", captcha_code: str = "") -> dict[str, Any]:
    judge_dir = DEFAULT_BENCH_ROOT / "skills" / "judge-submit"
    venv_py = DEFAULT_BENCH_ROOT / ".venv" / "bin" / "python"
    py_exe = str(venv_py) if venv_py.is_file() else sys.executable
    script = (
        "import json, os, sys\n"
        f"sys.path.insert(0, {str(judge_dir)!r})\n"
        "from judge_cli import JudgeClient\n"
        "pw = sys.stdin.read().rstrip('\\r\\n')\n"
        "c = JudgeClient()\n"
        "try:\n"
        "    kw = {}\n"
        "    lt = os.environ.get('OPSB_CJ_LTYPE')\n"
        "    cid, cc = os.environ.get('OPSB_CJ_CID'), os.environ.get('OPSB_CJ_CCODE')\n"
        "    if lt and lt != 'auto': kw['login_type'] = lt\n"
        "    if cid and cc: kw['captcha_id'] = cid; kw['captcha_code'] = cc\n"
        "    c.login(os.environ['OPSB_CJ_ACCOUNT'], pw, **kw)\n"
        "    c.save_session()\n"
        "    print(json.dumps({'ok': True, 'user_id': c.user_id}))\n"
        "except Exception as e:\n"
        "    print(json.dumps({'ok': False, 'error': str(e)[:240]})); sys.exit(1)\n"
    )
    env = {**os.environ, "OPSB_CJ_ACCOUNT": account, "OPSB_CJ_LTYPE": login_type, "OPSB_CJ_CID": captcha_id, "OPSB_CJ_CCODE": captcha_code}
    proc = subprocess.run([py_exe, "-c", script], input=password, env=env, capture_output=True, text=True, timeout=25)
    return json.loads((proc.stdout.strip().splitlines() or ["{}"])[-1])


def login_gitcode_with_token(token: str) -> dict[str, Any]:
    gc_bin = find_gc_bin()
    proc = subprocess.run(
        [gc_bin, "auth", "login", "--with-token", "--no-interactive", "--no-update-check"],
        input=token.strip() + "\n",
        capture_output=True,
        text=True,
        timeout=15,
    )
    invalidate_ttl_cache("gc_auth")
    st = read_gitcode_status(use_cache=False)
    ok = proc.returncode == 0 and st.get("logged_in", False)
    return {"ok": ok, "gitcode": st, "error": None if ok else (proc.stderr or proc.stdout).strip()[:200]}


def batch_invite_collaborator(problems: list[dict[str, Any]], owner: str, username: str, permission: str = "push", dry_run: bool = False) -> dict[str, Any]:
    clean_user = username.strip().lstrip("@")
    if not clean_user or not re.match(r"^[A-Za-z0-9_.-]+$", clean_user):
        raise ValueError(f"Invalid GitCode username: {username}")

    gc_bin = find_gc_bin()
    results = []
    body = json.dumps({"permission": permission})
    for p in problems:
        repo = p["repo_slug"]
        endpoint = f"repos/{owner}/{repo}/collaborators/{clean_user}"
        if dry_run:
            results.append({"problem": p["title"], "repo": f"{owner}/{repo}", "status": "dry-run", "detail": f"PUT {endpoint} ({permission})"})
            continue
        try:
            proc = subprocess.run(
                [gc_bin, "api", endpoint, "--method", "PUT", "--input", "-", "--no-interactive", "--no-update-check"],
                input=body,
                capture_output=True,
                text=True,
                timeout=12.0,
            )
            if proc.returncode == 0:
                results.append({"problem": p["title"], "repo": f"{owner}/{repo}", "status": "invited", "detail": f"permission={permission}"})
            else:
                err = (proc.stderr or proc.stdout).strip()[:200]
                st = "already" if "already" in err.lower() or "已存在" in err else "error"
                results.append({"problem": p["title"], "repo": f"{owner}/{repo}", "status": st, "detail": err})
        except Exception as exc:
            results.append({"problem": p["title"], "repo": f"{owner}/{repo}", "status": "error", "detail": str(exc)[:160]})

    return {
        "ok": all(r["status"] in ("invited", "already", "dry-run") for r in results),
        "username": clean_user,
        "owner": owner,
        "permission": permission,
        "results": results,
    }
