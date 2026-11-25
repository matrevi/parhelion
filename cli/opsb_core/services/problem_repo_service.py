"""Bootstrap every problem directory with the official Judge template and a private GitCode repo.

Two external systems are involved, both optional and both idempotent:

* ``GET /api/problems/<problem_id>/template`` (public, no login) returns the official
  starter files for an operator — CMakeLists.txt, ``kernel.asc``, ``main.asc``,
  ``reference.py``, ``sample.py``, ``run.sh`` …
* ``gc repo create <slug> --private`` creates the per-problem private repository that
  ``runtime/npu-test.py`` already expects as ``origin`` before server validation.

The bootstrap never overwrites files a user may have edited, never force-pushes an
existing repository, and reports per-problem status so a partially failed init is
visible instead of silently producing half-empty problem directories.
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..device_env import device_text

DEFAULT_JUDGE_BASE = device_text("judge_base")
DEFAULT_GITCODE_HOST = "https://gitcode.com"
_USER_AGENT = "parhelion/1.0 (problem bootstrap)"

# Parhelion owns these names; an official template must never replace them.
PROTECTED_NAMES = {"AGENTS.md", "PROBLEM.md", "DESIGN.md", "SCORE_HISTORY.md"}
PROBLEM_GITIGNORE = "build-npu/\nbuild-cpu/\n__pycache__/\n*.pyc\n.parhelion/\n.DS_Store\n"


class ProblemBootstrapError(RuntimeError):
    """Raised when a problem directory sdkot be bootstrapped."""


# --------------------------------------------------------------------------- template


def fetch_problem_template(
    problem_id: str,
    *,
    base_url: str = DEFAULT_JUDGE_BASE,
    timeout: float = 30.0,
) -> dict[str, str]:
    """Return ``{relative_path: content}`` for the official template of ``problem_id``."""
    pid = str(problem_id or "").strip()
    if not pid:
        raise ProblemBootstrapError("problem_id is required to fetch the official template")
    url = f"{base_url.rstrip('/')}/api/problems/{pid}/template"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raise ProblemBootstrapError(f"GET {url} -> HTTP {exc.code}") from exc
    except Exception as exc:  # noqa: BLE001 - network/JSON failures surface the same way
        raise ProblemBootstrapError(f"GET {url} failed: {exc}") from exc

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ProblemBootstrapError(f"{url} returned no template data")
    files: dict[str, str] = {}
    for row in data.get("files") or []:
        if not isinstance(row, dict):
            continue
        path = str(row.get("path") or "").strip().lstrip("./")
        content = row.get("content")
        if path and isinstance(content, str):
            files[path] = content
    # Older payloads also carry these as separate fields instead of files[].
    for field, name in (("tiling_h", "tiling.h"), ("tiling_key_h", "tiling_key.h"),
                        ("host_cpp", "host.cpp"), ("kernel_cpp", "kernel.cpp")):
        extra = data.get(field)
        if isinstance(extra, str) and extra.strip() and name not in files:
            files[name] = extra
    if not files:
        raise ProblemBootstrapError(f"template for problem {pid} contains no files")
    return files


def materialize_template(problem_dir: Path, files: dict[str, str], *, overwrite: bool = False) -> dict[str, Any]:
    """Write template files into ``problem_dir`` without clobbering user work.

    Existing files are skipped unless ``overwrite`` is set; Parhelion-managed names are
    always skipped. Returns the written/skipped paths for reporting.
    """
    root = problem_dir.expanduser().resolve()
    written: list[str] = []
    skipped: list[str] = []
    for rel, content in sorted(files.items()):
        name = Path(rel).name
        if name in PROTECTED_NAMES:
            skipped.append(rel)
            continue
        target = (root / rel).resolve()
        if root not in target.parents and target != root:
            skipped.append(rel)  # never escape the problem directory
            continue
        if target.exists() and not overwrite:
            skipped.append(rel)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(rel)
    ignore = root / ".gitignore"
    if not ignore.exists():
        ignore.write_text(PROBLEM_GITIGNORE, encoding="utf-8")
        written.append(".gitignore")
    return {"problem_dir": str(root), "written": written, "skipped": skipped}


# ------------------------------------------------------------------------------- repo


def _run(cmd: list[str], *, cwd: Path | None = None, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout)


def ensure_private_repo(owner: str, repo_slug: str, *, description: str = "", gc_bin: str = "gc") -> dict[str, Any]:
    """Create ``owner/repo_slug`` as a private GitCode repo; an existing repo is success."""
    slug = str(repo_slug or "").strip()
    if not slug:
        raise ProblemBootstrapError("repo_slug is required to create the private repository")
    cmd = [gc_bin, "repo", "create", slug, "--private", "--no-interactive", "--json"]
    if description:
        cmd += ["--description", description[:160]]
    try:
        proc = _run(cmd)
    except FileNotFoundError as exc:
        raise ProblemBootstrapError(f"gitcode CLI not found ({gc_bin}); install `gc` and run `gc auth login`") from exc
    out, err = proc.stdout.strip(), proc.stderr.strip()
    if proc.returncode == 0:
        data: dict[str, Any] = {}
        try:
            data = json.loads(out) if out else {}
        except ValueError:
            data = {}
        return {"ok": True, "created": True, "full_name": data.get("full_name") or f"{owner}/{slug}",
                "repo_slug": slug, "detail": ""}
    if "already exists" in (err + out).lower():
        return {"ok": True, "created": False, "full_name": f"{owner}/{slug}", "repo_slug": slug,
                "detail": "repository already exists"}
    raise ProblemBootstrapError(f"gc repo create {slug} failed: {(err or out)[:300]}")


def git_init_commit(problem_dir: Path, *, message: str = "chore(scaffold): parhelion problem bootstrap") -> dict[str, Any]:
    """Initialise the problem repo (if needed) and commit the scaffolding."""
    root = problem_dir.expanduser().resolve()
    if not (root / ".git").is_dir():
        proc = _run(["git", "init", "-b", "main"], cwd=root)
        if proc.returncode != 0:
            raise ProblemBootstrapError(f"git init failed in {root}: {proc.stderr.strip()[:200]}")
    _run(["git", "add", "-A"], cwd=root)
    status = _run(["git", "status", "--porcelain"], cwd=root)
    committed = False
    if status.stdout.strip():
        proc = _run(["git", "commit", "-m", message], cwd=root)
        if proc.returncode != 0:
            raise ProblemBootstrapError(f"git commit failed in {root}: {proc.stderr.strip()[:200]}")
        committed = True
    sha = _run(["git", "rev-parse", "HEAD"], cwd=root).stdout.strip()
    return {"ok": True, "committed": committed, "sha": sha, "problem_dir": str(root)}


def push_private_repo(
    problem_dir: Path,
    owner: str,
    repo_slug: str,
    *,
    created: bool,
    gitcode_host: str = DEFAULT_GITCODE_HOST,
) -> dict[str, Any]:
    """Push HEAD to the private repo.

    A repository created by this run can still hold GitCode's auto-generated README, so
    the first push is forced; an existing repository is only ever fast-forwarded, and a
    rejection is reported instead of overwritten.
    """
    root = problem_dir.expanduser().resolve()
    url = f"{gitcode_host.rstrip('/')}/{owner}/{repo_slug}.git"
    existing = _run(["git", "remote", "get-url", "origin"], cwd=root)
    if existing.returncode == 0:
        if existing.stdout.strip() != url:
            _run(["git", "remote", "set-url", "origin", url], cwd=root)
    else:
        _run(["git", "remote", "add", "origin", url], cwd=root)

    cmd = ["git", "push", "-u", "origin", "HEAD:main"]
    if created:
        cmd.insert(2, "-f")
    proc = _run(cmd, cwd=root, timeout=300.0)
    if proc.returncode != 0:
        raise ProblemBootstrapError(
            f"git push to {url} failed: {(proc.stderr or proc.stdout).strip().splitlines()[-1][:300]}"
        )
    return {"ok": True, "url": url, "forced": created, "problem_dir": str(root)}


# -------------------------------------------------------------------------- bootstrap


def bootstrap_problem(
    problem_dir: Path,
    problem: dict[str, Any],
    *,
    owner: str,
    repo_slug: str,
    fetch_template: bool = True,
    create_repo: bool = True,
    push: bool = True,
    gc_bin: str = "gc",
    can_judge_base: str = DEFAULT_JUDGE_BASE,
) -> dict[str, Any]:
    """One problem: official template → git init/commit → private repo → push."""
    root = Path(problem_dir).expanduser().resolve()
    result: dict[str, Any] = {"title": str(problem.get("title") or root.name), "problem_dir": str(root),
                              "repo_slug": repo_slug, "template": None, "commit": None, "repo": None, "push": None,
                              "error": ""}
    try:
        if fetch_template:
            problem_id = str(problem.get("problem_id") or "").strip()
            if problem_id:
                files = fetch_problem_template(problem_id, base_url=can_judge_base)
                result["template"] = materialize_template(root, files)
            else:
                result["template"] = {"problem_dir": str(root), "written": [], "skipped": [],
                                      "detail": "no problem_id; template skipped"}
        result["commit"] = git_init_commit(root)
        created = False
        if create_repo:
            repo = ensure_private_repo(owner, repo_slug, description=f"Parhelion {result['title']}", gc_bin=gc_bin)
            result["repo"] = repo
            created = bool(repo.get("created"))
        if push:
            result["push"] = push_private_repo(root, owner, repo_slug, created=created)
    except ProblemBootstrapError as exc:
        result["error"] = str(exc)
    return result


def seed_template_bench(workspace: Path, problem: dict[str, Any]) -> None:
    """Expose the executable official sample as the first public benchmark."""
    from ..config import workspace_bench_specs_dir
    title = problem["title"]
    repo = workspace / (problem.get("path") or title)
    if not all((repo / name).is_file() for name in ("reference.py", "sample.py", "kernel.asc")):
        return
    directory = workspace_bench_specs_dir(workspace) / title
    directory.mkdir(parents=True, exist_ok=True)
    anchor = {"id": "official_sample", "stratum": "PUBLIC", "generator": "sample.py generate",
              "verifier": "sample.py verify", "reference": "reference.py"}
    for filename, spec in (
        ("cases_perf.json", {"problem": title, "public_anchors": [anchor], "hidden_buckets": []}),
        ("cases_correctness.json", {"problem": title, "cases": [anchor]}),
    ):
        try:
            with (directory / filename).open("x", encoding="utf-8") as stream:
                json.dump(spec, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
        except FileExistsError:
            continue


def bootstrap_workspace_repos(
    target_dir: Path,
    problems: list[dict[str, Any]],
    *,
    git_owner: str,
    fetch_template: bool = True,
    create_repo: bool = True,
    push: bool = True,
    gc_bin: str = "gc",
    can_judge_base: str = DEFAULT_JUDGE_BASE,
) -> dict[str, Any]:
    """Run the per-problem bootstrap for a freshly scaffolded workspace."""
    root = Path(target_dir).expanduser().resolve()
    results: list[dict[str, Any]] = []
    for p in problems:
        title = str(p.get("title") or "").strip()
        if not title:
            continue
        repo_slug = str(p.get("repo_slug") or "").strip()
        if not repo_slug:
            results.append({"title": title, "error": "missing repo_slug", "problem_dir": str(root / title)})
            continue
        results.append(
            bootstrap_problem(
                root / str(p.get("path") or title),
                p,
                owner=git_owner,
                repo_slug=repo_slug,
                fetch_template=fetch_template,
                create_repo=create_repo,
                push=push,
                gc_bin=gc_bin,
                can_judge_base=can_judge_base,
            )
        )
        seed_template_bench(root, p)
    ok = [r for r in results if not r.get("error")]
    return {
        "ok": len(ok) == len(results),
        "owner": git_owner,
        "total": len(results),
        "succeeded": len(ok),
        "failed": [{"title": r["title"], "error": r["error"]} for r in results if r.get("error")],
        "problems": results,
    }
