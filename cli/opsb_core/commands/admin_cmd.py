"""CLI commands: `opsb workspace`, `opsb collab`, `opsb auth`, `opsb ssh`, `opsb skills`."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..config import DEFAULT_BENCH_ROOT, ConfigRepository
from ..services.auth_service import (
    batch_invite_collaborator,
    fetch_judge_captcha,
    get_auth_status,
    import_judge_session,
    list_judge_accounts,
    login_judge,
    login_gitcode_with_token,
    remove_judge_account,
    set_judge_account_enabled,
)
from ..services.contest_service import resolve_workspace_meta
from ..services.contest_scrape_service import ContestScrapeError, fetch_contest_problems
from ..services.problem_repo_service import bootstrap_workspace_repos
from ..services.scaffold_service import scaffold_workspace, write_agent_guides
from ..services.skills_service import get_skills_status, install_skills
from ..services.ssh_service import parse_ssh_hosts, upsert_ssh_config_block


def _parse_problem_spec(spec: str) -> list[dict]:
    """Parse `--problems "OpA:3, OpB"` into scaffold-shaped problem dicts."""
    out: list[dict] = []
    for item in (x.strip() for x in (spec or "").split(",") if x.strip()):
        if ":" in item:
            t_name, d_val = item.split(":", 1)
            out.append({"title": t_name.strip(), "difficulty": int(d_val) if d_val.strip().isdigit() else 2})
        else:
            out.append({"title": item, "difficulty": 2})
    return out


def run_workspace(args: argparse.Namespace) -> int:
    cfg = ConfigRepository.load()
    if args.ws_cmd == "list":
        items = []
        for p_str in cfg.get("workspaces", []):
            p = Path(p_str)
            m = resolve_workspace_meta(p) if p.is_dir() else {"workspace": p_str, "title": p.name, "layout": "missing", "problems": []}
            items.append({**m, "active": str(p) == cfg.get("active_workspace")})
        if args.json:
            print(json.dumps({"active_workspace": cfg.get("active_workspace"), "workspaces": items}, ensure_ascii=False))
        else:
            for it in items:
                print(f"{'*' if it['active'] else ' '} {it['workspace']}  ({it['title']} · {len(it.get('problems', []))} problems · {it['layout']})")
        return 0

    if args.ws_cmd == "select":
        target = str(Path(args.path).expanduser().resolve())
        cfg["active_workspace"] = target
        if target not in cfg["workspaces"]:
            cfg["workspaces"].append(target)
        ConfigRepository.save(cfg)
        print(json.dumps({"ok": True, "active_workspace": target}) if args.json else f"Selected workspace: {target}")
        return 0

    if args.ws_cmd == "init":
        target = Path(args.target_dir).expanduser().resolve()
        probs = _parse_problem_spec(args.problems)
        contest_url = (getattr(args, "contest_url", "") or "").strip()
        contest_id = (args.contest_id or "").strip()
        title = (args.title or "").strip()
        scrape_ref = contest_url or contest_id
        scraped: dict = {}
        if not probs:
            # A contest link/id must yield its real problem list: silently scaffolding a
            # placeholder operator is what made a new contest workspace look "wrong".
            if not scrape_ref:
                raise SystemExit(
                    "workspace init requires --problems, or --contest-url/--contest-id so the "
                    "problem list can be scraped from Judge"
                )
            try:
                scraped = fetch_contest_problems(scrape_ref)
            except ContestScrapeError as exc:
                raise SystemExit(f"sdkot scrape contest {scrape_ref!r}: {exc}")
            probs = scraped["problems"]
            title = title or str(scraped["contest"].get("title") or "")
            contest_id = contest_id or str(scraped["contest"].get("name") or scraped["contest"].get("id") or "")
        # A folder re-initialised for a different contest must not inherit the previous
        # contest's score snapshot, board summary, queue or scheduler cache.
        from ..services.contest_state_service import existing_contest_id, reset_contest_state

        prev_contest = existing_contest_id(target)
        state_reset: dict = {}
        if prev_contest and contest_id and prev_contest != contest_id:
            state_reset = reset_contest_state(target, contest_id)
        res = scaffold_workspace(
            target_dir=target,
            contest_title=title or "KernelC Operator Contest",
            contest_id=contest_id,
            contest_url=contest_url,
            git_owner=args.git_owner,
            repo_prefix=args.repo_prefix,
            npu_host=args.npu_host or cfg.get("npu_ssh_host", "npu"),
            problems=probs or None,
        )
        if scraped:
            res["scraped"] = {"contest": scraped["contest"], "problem_count": len(probs)}
        if state_reset:
            res["state_reset"] = state_reset
        if not getattr(args, "no_repos", False):
            manifest = json.loads((target / "opsb.workspace.json").read_text(encoding="utf-8"))
            res["repos"] = bootstrap_workspace_repos(
                target,
                manifest.get("problems") or [],
                git_owner=args.git_owner,
            )
        if not args.no_register:
            t_str = str(target)
            if t_str not in cfg["workspaces"]:
                cfg["workspaces"].append(t_str)
            if args.activate:
                cfg["active_workspace"] = t_str
            ConfigRepository.save(cfg)
        print(json.dumps({"ok": True, **res}, ensure_ascii=False) if args.json else f"Initialized workspace at {res['workspace']}")
        return 0

    if args.ws_cmd == "sync-problems":
        target = Path(args.target_dir or args.workspace or cfg.get("active_workspace") or "").expanduser()
        if not target.is_dir():
            raise SystemExit(f"workspace not found: {target}")
        meta = resolve_workspace_meta(target)
        ref = (args.contest_url or args.contest_id or meta.get("contest_url") or meta.get("contest_id") or "").strip()
        if not ref:
            raise SystemExit("no contest link/id on this workspace; pass --contest-url or --contest-id")
        try:
            scraped = fetch_contest_problems(ref)
        except ContestScrapeError as exc:
            raise SystemExit(f"sdkot scrape contest {ref!r}: {exc}")
        existing = {str(p.get("title")): p for p in (meta.get("problems") or []) if p.get("title")}
        merged, added = [], []
        for sp in scraped["problems"]:
            prev = existing.get(sp["title"], {})
            row = {
                **sp,
                "slug": prev.get("slug") or sp.get("slug") or "",
                "repo_slug": prev.get("repo_slug") or "",
                "path": prev.get("path") or sp["title"],
            }
            if not prev:
                added.append(sp["title"])
            merged.append(row)
        removed = [t for t in existing if t not in {p["title"] for p in merged}]
        res = scaffold_workspace(
            target_dir=target,
            contest_title=meta.get("title") or scraped["contest"].get("title") or "",
            contest_id=args.contest_id or meta.get("contest_id") or scraped["contest"].get("name") or "",
            contest_url=args.contest_url or meta.get("contest_url") or "",
            git_owner=meta.get("git_owner") or "winbeau",
            repo_prefix=meta.get("repo_prefix") or "accel-ops",
            npu_host=meta.get("npu_ssh_host") or cfg.get("npu_ssh_host", "npu"),
            problems=merged,
        )
        if not getattr(args, "no_repos", False):
            manifest = json.loads((target / "opsb.workspace.json").read_text(encoding="utf-8"))
            res["repos"] = bootstrap_workspace_repos(
                target,
                manifest.get("problems") or [],
                git_owner=meta.get("git_owner") or "winbeau",
            )
        payload = {
            "ok": True,
            "workspace": str(target),
            "contest": scraped["contest"],
            "problem_count": len(merged),
            "added": added,
            "removed": removed,
            "problem_dirs": res["problem_dirs"],
            "repos": res.get("repos"),
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(f"Synced {len(merged)} problems from {scraped['contest'].get('name') or ref}")
            if added:
                print(f"  added: {', '.join(added)}")
            if removed:
                print(f"  stale (dirs kept): {', '.join(removed)}")
        return 0

    if args.ws_cmd == "gen-agents":
        target = Path(args.target_dir).expanduser().resolve()
        if DEFAULT_BENCH_ROOT is not None and target == DEFAULT_BENCH_ROOT:
            raise SystemExit("Refused on legacy ops-bench root; clean gen-agents targets opsb clean workspaces.")
        meta = json.loads((target / "opsb.workspace.json").read_text())
        write_agent_guides(target, meta)
        res = {"workspace": str(target), "problem_count": len(meta["problems"]), "agents_md": str(target / "AGENTS.md")}
        print(json.dumps({"ok": True, **res}, ensure_ascii=False) if args.json else f"Regenerated AGENTS.md across {res['problem_count']} problems")
        return 0
    return 1


def run_collab(args: argparse.Namespace) -> int:
    cfg = ConfigRepository.load()
    ws = ConfigRepository.resolve_workspace(args.workspace, cfg)
    meta = resolve_workspace_meta(ws)
    owner = args.owner or meta.get("git_owner") or "winbeau"
    out = batch_invite_collaborator(meta["problems"], owner, args.username, args.permission, args.dry_run)
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        for r in out["results"]:
            print(f"  [{r['status'].upper():<8}] {r['repo']:<42} {r['detail']}")
    return 0 if out["ok"] else 1


def run_auth(args: argparse.Namespace) -> int:
    if args.auth_cmd == "judge-captain":
        from ..services.ranking_service import set_captain
        try:
            set_captain(Path(args.workspace).expanduser().resolve(), args.account_id)
        except (OSError, ValueError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 1
        print(json.dumps({"ok": True}))
        return 0
    if args.auth_cmd == "status":
        out = get_auth_status(use_cache=False)
        print(json.dumps(out, ensure_ascii=False) if args.json else json.dumps(out, indent=2))
        return 0
    if args.auth_cmd == "judge-captcha":
        out = fetch_judge_captcha()
        print(json.dumps(out, ensure_ascii=False))
        return 0 if out.get("ok") else 1
    if args.auth_cmd == "judge-login":
        pw = sys.stdin.read().rstrip("\r\n")
        if not pw:
            print(json.dumps({"ok": False, "error": "Password required on stdin"}))
            return 1
        out = login_judge(
            args.account,
            pw,
            args.login_type or "auto",
            args.captcha_id or "",
            args.captcha_code or "",
            account_id=getattr(args, "account_id", "primary"),
            label=getattr(args, "label", "") or "",
        )
        print(json.dumps(out, ensure_ascii=False))
        return 0 if out.get("ok") else 1
    if args.auth_cmd == "judge-accounts":
        print(json.dumps({"ok": True, "accounts": list_judge_accounts()}, ensure_ascii=False))
        return 0
    if args.auth_cmd == "judge-import":
        out = import_judge_session(args.session_file, label=args.label or "")
        print(json.dumps(out, ensure_ascii=False))
        return 0 if out.get("ok") else 1
    if args.auth_cmd == "judge-toggle":
        en = str(args.enabled).strip().lower() in ("1", "true", "on", "yes")
        out = set_judge_account_enabled(args.account_id, en)
        print(json.dumps(out, ensure_ascii=False))
        return 0 if out.get("ok") else 1
    if args.auth_cmd == "judge-remove":
        out = remove_judge_account(args.account_id)
        print(json.dumps(out, ensure_ascii=False))
        return 0 if out.get("ok") else 1
    if args.auth_cmd == "judge-logout":
        (Path.home() / ".judge" / "session.json").unlink(missing_ok=True)
        print(json.dumps({"ok": True}))
        return 0
    if args.auth_cmd == "gitcode-login":
        tok = sys.stdin.read().strip()
        if not tok:
            print(json.dumps({"ok": False, "error": "Token required on stdin"}))
            return 1
        out = login_gitcode_with_token(tok)
        print(json.dumps(out, ensure_ascii=False))
        return 0 if out.get("ok") else 1
    return 1


def run_ssh(args: argparse.Namespace) -> int:
    cfg = ConfigRepository.load()
    if args.ssh_cmd == "list":
        print(json.dumps({"active_host": cfg.get("npu_ssh_host", "npu"), "hosts": parse_ssh_hosts(cfg)}, ensure_ascii=False))
        return 0
    if args.ssh_cmd == "select":
        cfg["npu_ssh_host"] = args.alias
        ConfigRepository.save(cfg)
        print(json.dumps({"ok": True, "active_host": args.alias}))
        return 0
    if args.ssh_cmd == "save":
        entry = {
            "alias": args.alias.strip(),
            "hostname": args.hostname.strip(),
            "port": int(args.port),
            "user": args.user.strip(),
            "identity_file": (args.identity_file or "").strip(),
        }
        cfg["custom_ssh_hosts"] = [h for h in (cfg.get("custom_ssh_hosts") or []) if h.get("alias") != entry["alias"]] + [entry]
        if args.activate:
            cfg["npu_ssh_host"] = entry["alias"]
        ConfigRepository.save(cfg)
        if args.write_ssh_config:
            upsert_ssh_config_block(entry)
        print(json.dumps({"ok": True, "saved": entry, "hosts": parse_ssh_hosts(cfg)}, ensure_ascii=False))
        return 0
    return 1


def run_skills(args: argparse.Namespace) -> int:
    if args.skills_cmd == "status":
        print(json.dumps(get_skills_status(), ensure_ascii=False))
        return 0
    if args.skills_cmd == "install":
        try:
            res = install_skills(args.targets)
            if args.json:
                print(json.dumps(res, ensure_ascii=False))
            else:
                for rec in res["installed"]:
                    print(f"Installed [{rec['target']}] fallback skill -> {rec['path']}")
            return 0
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}) if args.json else str(exc), file=sys.stderr if not args.json else sys.stdout)
            return 1
    return 1
