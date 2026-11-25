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

from ..config import (
    DEFAULT_BENCH_ROOT,
    SUPERVISOR_ROOT,
    _resolve_parhelion_home,
    discover_default_workspace,
    invalidate_ttl_cache,
    parse_iso,
    read_json,
    read_ttl_cache,
    resolve_python_exe,
    utc_now,
    write_ttl_cache,
)
from ..device_env import device_text

LEGACY_SESSIONS_DIR = Path.home() / ".judge" / "sessions"


def _sessions_dir() -> Path:
    return _resolve_parhelion_home() / "sessions"


def _accounts_registry_path() -> Path:
    return _resolve_parhelion_home() / "cj_accounts.json"


def _resolve_judge_submit_dir() -> Path:
    candidates: list[Path] = [
        SUPERVISOR_ROOT / "runtime" / "judge",
    ]
    ws = discover_default_workspace()
    if ws is not None:
        candidates.append(ws / "skills" / "judge-submit")
    candidates.append(SUPERVISOR_ROOT.parent / "skills" / "judge-submit")
    candidates.append(SUPERVISOR_ROOT / "skills" / "judge-submit")
    for c in candidates:
        if (c / "judge_cli.py").is_file():
            return c
    return candidates[0]

GC_CANDIDATES = [
    Path.home() / ".local" / "share" / "pnpm" / "bin" / "gc",
    Path("/opt/homebrew/bin/gc"),
]


def find_gc_bin() -> str:
    for c in GC_CANDIDATES:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return shutil.which("gc") or str(GC_CANDIDATES[0])


def _read_session_file_status(cj_path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {
        "logged_in": False,
        "session_file": str(cj_path),
        "base_url": device_text("judge_base"),
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
        raw_la = d.get("logged_in_at")
        dt = None
        if isinstance(raw_la, (int, float)) and raw_la > 0:
            dt = datetime.datetime.fromtimestamp(float(raw_la), tz=datetime.timezone.utc)
        elif isinstance(raw_la, str) and raw_la.strip():
            try:
                dt = datetime.datetime.fromtimestamp(float(raw_la.strip()), tz=datetime.timezone.utc)
            except ValueError:
                dt = parse_iso(raw_la)
        if dt is None:
            dt = datetime.datetime.fromtimestamp(st.st_mtime, tz=datetime.timezone.utc)
        if uid and has_cookies and dt:
            exp = dt + datetime.timedelta(days=7)
            rem_h = round((exp - utc_now()).total_seconds() / 3600.0, 1)
            info.update(
                {
                    "logged_in": rem_h > 0,
                    "base_url": d.get("base_url") or device_text("judge_base"),
                    "user_id": uid,
                    "logged_in_at": dt.isoformat(),
                    "expires_at": exp.isoformat(),
                    "remaining_hours": max(0.0, rem_h),
                }
            )
    except Exception as exc:
        info["error"] = str(exc)[:160]
    return info


def read_judge_status() -> dict[str, Any]:
    candidates = []
    env_p = os.environ.get("JUDGE_SESSION_FILE")
    if env_p:
        candidates.append(Path(env_p).expanduser())
    candidates.append(_sessions_dir() / "primary.json")
    candidates.append(Path.home() / ".judge" / "session.json")
    for cand in candidates:
        if cand.is_file():
            return _read_session_file_status(cand)
    return _read_session_file_status(candidates[-1])


def _load_accounts_registry() -> list[dict[str, Any]]:
    raw = read_json(_accounts_registry_path(), {"accounts": []})
    items = raw.get("accounts") if isinstance(raw, dict) else []
    return [x for x in (items or []) if isinstance(x, dict) and x.get("id")]


def _save_accounts_registry(items: list[dict[str, Any]]) -> None:
    ph = _resolve_parhelion_home()
    reg_path = _accounts_registry_path()
    ph.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = reg_path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"accounts": items}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, reg_path)


def list_judge_accounts() -> list[dict[str, Any]]:
    """Return all configured Judge team submitter slots (primary + extra teammate sessions)."""
    sdir = _sessions_dir()
    reg = _load_accounts_registry()
    primary_meta = next((x for x in reg if x.get("id") == "primary"), None)
    prim_st = read_judge_status()
    out: list[dict[str, Any]] = [
        {
            "id": "primary",
            "label": (primary_meta or {}).get("label") or "主账号",
            "account_hint": (primary_meta or {}).get("account_hint") or "",
            "enabled": bool((primary_meta or {}).get("enabled", True)),
            "is_primary": True,
            **prim_st,
        }
    ]
    for item in reg:
        aid = str(item.get("id") or "")
        if not aid or aid == "primary":
            continue
        sf = Path(str(item.get("session_file") or (sdir / f"{aid}.json"))).expanduser()
        if not sf.is_file():
            for alt in (sdir / f"{aid}.json", LEGACY_SESSIONS_DIR / f"{aid}.json"):
                if alt.is_file():
                    sf = alt
                    break
        st = _read_session_file_status(sf)
        out.append(
            {
                "id": aid,
                "label": str(item.get("label") or aid),
                "account_hint": str(item.get("account_hint") or ""),
                "enabled": bool(item.get("enabled", True)),
                "is_primary": False,
                **st,
            }
        )
    return out


def set_judge_account_enabled(account_id: str, enabled: bool) -> dict[str, Any]:
    reg = _load_accounts_registry()
    found = False
    for item in reg:
        if item.get("id") == account_id:
            item["enabled"] = bool(enabled)
            found = True
            break
    if not found and account_id == "primary":
        reg.insert(0, {"id": "primary", "label": "主账号", "enabled": bool(enabled), "session_file": str(Path.home() / ".judge" / "session.json")})
        found = True
    if not found:
        return {"ok": False, "error": f"Unknown account slot: {account_id}"}
    _save_accounts_registry(reg)
    return {"ok": True, "accounts": list_judge_accounts()}


def remove_judge_account(account_id: str) -> dict[str, Any]:
    if account_id == "primary":
        return {"ok": False, "error": "Cannot remove primary slot (use logout instead)"}
    reg = _load_accounts_registry()
    kept = []
    removed = None
    for item in reg:
        if item.get("id") == account_id:
            removed = item
        else:
            kept.append(item)
    if removed is None:
        return {"ok": False, "error": f"Unknown account slot: {account_id}"}
    sf = Path(str(removed.get("session_file") or "")).expanduser()
    if sf.is_file() and (_sessions_dir() in sf.parents or LEGACY_SESSIONS_DIR in sf.parents):
        sf.unlink(missing_ok=True)
    _save_accounts_registry(kept)
    return {"ok": True, "accounts": list_judge_accounts()}


def import_judge_session(src_path: str, label: str = "") -> dict[str, Any]:
    src = Path(src_path).expanduser()
    if not src.is_file():
        return {"ok": False, "error": f"Session file not found: {src}"}
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
        uid = data.get("user_id")
        if not uid or not data.get("cookies"):
            return {"ok": False, "error": "Invalid session.json (missing user_id or cookies)"}
    except Exception as exc:
        return {"ok": False, "error": f"Failed to parse session.json: {exc}"}

    reg = _load_accounts_registry()
    existing_ids = {str(x.get("id")) for x in reg} | {"primary"}
    n = 2
    while f"teammate-{n}" in existing_ids:
        n += 1
    aid = f"teammate-{n}"
    sdir = _sessions_dir()
    sdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    dst = sdir / f"{aid}.json"
    dst.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(dst, 0o600)
    reg.append(
        {
            "id": aid,
            "label": label.strip() or f"队员{n}",
            "account_hint": str(uid)[:12],
            "session_file": str(dst),
            "enabled": True,
        }
    )
    _save_accounts_registry(reg)
    return {"ok": True, "account_id": aid, "user_id": uid, "accounts": list_judge_accounts()}


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
    return {
        "judge": read_judge_status(),
        "judge_accounts": list_judge_accounts(),
        "gitcode": read_gitcode_status(use_cache=use_cache),
    }


def fetch_judge_captcha() -> dict[str, Any]:
    judge_dir = _resolve_judge_submit_dir()
    py_exe = resolve_python_exe(discover_default_workspace())
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


def login_judge(
    account: str,
    password: str,
    login_type: str = "auto",
    captcha_id: str = "",
    captcha_code: str = "",
    account_id: str | None = None,
    label: str = "",
) -> dict[str, Any]:
    judge_dir = _resolve_judge_submit_dir()
    py_exe = resolve_python_exe(discover_default_workspace())

    target_slot = (account_id or "primary").strip() or "primary"
    session_file: Path | None = None
    if target_slot != "primary":
        reg = _load_accounts_registry()
        if target_slot == "new":
            existing_ids = {str(x.get("id")) for x in reg} | {"primary"}
            n = 2
            while f"teammate-{n}" in existing_ids:
                n += 1
            target_slot = f"teammate-{n}"
        sdir = _sessions_dir()
        sdir.mkdir(mode=0o700, parents=True, exist_ok=True)
        session_file = sdir / f"{target_slot}.json"

    script = (
        "import json, os, sys\n"
        f"sys.path.insert(0, {str(judge_dir)!r})\n"
        "from judge_cli import JudgeClient\n"
        "pw = sys.stdin.read().rstrip('\\r\\n')\n"
        "sf = os.environ.get('OPSB_CJ_SESSION_FILE') or None\n"
        "c = JudgeClient(session_file=sf)\n"
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
    env = {
        **os.environ,
        "OPSB_CJ_ACCOUNT": account,
        "OPSB_CJ_LTYPE": login_type,
        "OPSB_CJ_CID": captcha_id,
        "OPSB_CJ_CCODE": captcha_code,
        "OPSB_CJ_SESSION_FILE": str(session_file) if session_file else "",
    }
    proc = subprocess.run([py_exe, "-c", script], input=password, env=env, capture_output=True, text=True, timeout=25)
    parsed = json.loads((proc.stdout.strip().splitlines() or ["{}"])[-1])
    if parsed.get("ok") and target_slot != "primary" and session_file is not None:
        try:
            os.chmod(session_file, 0o600)
        except OSError:
            pass
        reg = _load_accounts_registry()
        hint = account.split("@")[0] if "@" in account else account
        updated = False
        for item in reg:
            if item.get("id") == target_slot:
                item["label"] = label.strip() or item.get("label") or target_slot
                item["account_hint"] = hint
                item["session_file"] = str(session_file)
                item["enabled"] = True
                updated = True
                break
        if not updated:
            reg.append(
                {
                    "id": target_slot,
                    "label": label.strip() or target_slot.replace("teammate-", "队员"),
                    "account_hint": hint,
                    "session_file": str(session_file),
                    "enabled": True,
                }
            )
        _save_accounts_registry(reg)
        parsed["account_id"] = target_slot
        parsed["accounts"] = list_judge_accounts()
    return parsed


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
