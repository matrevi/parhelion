"""Process environment and read-path isolation for managed workers (4.2 K).

A worker runs in its own worktree and talks to the platform only through MCP, so its
process must not be able to import the platform's modules or open the team's judge
credential stores. Three layers, weakest first:

1. **Environment.** ``worker_env`` keeps the operating system's environment but drops the
   platform-private variables (``PARHELION_*``/``OPSB_*`` other than ``PARHELION_HOME``,
   ``JUDGE_SESSION_FILE``) and every ``PYTHONPATH`` entry inside a platform source
   directory, so ``import opsb_core`` fails in the worker's shell.
2. **Read paths.** ``seatbelt_profile`` is the macOS sandbox profile that denies
   ``~/.parhelion/cj_accounts.json``, ``~/.parhelion/sessions/``, ``~/.judge/``, the
   remote-judge payload cache, ``daemon.json`` and — when ``config.json`` carries a
   credential-shaped key — ``config.json`` itself. ``worker_access.prepare_worker_command``
   applies it to every managed adapter, and ``drivers.base.launch_session`` applies the
   same profile to the detached job runner, whose driver child inherits it.
3. **Detection.** What a driver's own sandbox cannot express (DSH confines writes only,
   Codex permission profiles vary by build) is covered by ``worker_boundary`` alerts.

The exclusion list is a whole-file granularity because sandbox(7) has no notion of a JSON
section: a credential-bearing ``config.json`` is denied entirely and the worker's MCP
server then sees the built-in defaults (``config.read_json`` already tolerates that), which
is why the deny is conditional instead of unconditional.
"""
from __future__ import annotations

import json
import os
import platform
import re
from pathlib import Path
from typing import Any, Iterable

from . import config as _cfg

#: Variables a worker never inherits; ``PARHELION_HOME`` is re-added explicitly.
DROPPED_ENV_PREFIXES = ("PARHELION_", "OPSB_")
DROPPED_ENV_NAMES = ("JUDGE_SESSION_FILE", "PYTHONHOME", "PYTHONSTARTUP")
KEPT_ENV_NAMES = ("PARHELION_HOME",)

#: ``<PARHELION_HOME>/...`` paths a worker's process may not open.
CREDENTIAL_FILES = ("cj_accounts.json", "daemon.json")
CREDENTIAL_DIRS = ("sessions",)
#: Leftovers of the pre-Parhelion judge session store and the legacy per-contest controller.
LEGACY_JUDGE_DIR = ".judge"
#: Board payloads fetched with the team's cookies (``cache/.cache_remote_judge_<key>.json``).
CACHE_BASENAME_PREFIX = ".cache_remote_judge_"

CREDENTIAL_KEY_RE = re.compile(
    r"cookie|token|secret|password|passwd|api[_-]?key|credential|session[_-]?file|\bauth\b", re.I)

SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def platform_source_dirs() -> tuple[Path, ...]:
    """The directories that must never appear on a worker's ``PYTHONPATH``.

    In a checkout that is ``<repo>`` plus ``<repo>/cli``; inside the packaged application it
    is ``Contents/Resources`` plus its ``cli``, ``scheduler`` and ``runtime`` siblings.
    """
    cli = Path(_cfg.CLI_DIR).resolve()
    root = cli.parent
    dirs = [root, cli, root / "scheduler", root / "runtime"]
    if root.name == "Resources":
        dirs += [root / "cli", root / "scheduler", root / "runtime"]
    out: list[Path] = []
    for d in dirs:
        if d not in out:
            out.append(d)
    return tuple(out)


def _under(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _resolved(text: str) -> Path | None:
    try:
        return Path(os.path.expandvars(os.path.expanduser(str(text)))).resolve()
    except (OSError, ValueError):
        return None


def sanitize_pythonpath(value: str, roots: Iterable[Path] | None = None) -> str:
    """``value`` without the entries that make the platform importable (empty when none remain)."""
    roots = tuple(roots if roots is not None else platform_source_dirs())
    kept: list[str] = []
    for entry in str(value or "").split(os.pathsep):
        raw = entry.strip()
        if not raw:
            continue
        resolved = _resolved(raw)
        if resolved is not None and any(_under(resolved, root) for root in roots):
            continue
        kept.append(raw)
    return os.pathsep.join(kept)


def worker_env(base: dict[str, str] | None = None, overrides: dict[str, str] | None = None,
               *, roots: Iterable[Path] | None = None) -> dict[str, str]:
    """The environment of a worker process: OS environment minus platform-private state.

    ``overrides`` are the driver's own variables (``PARHELION_MCP_ROLE``, ``CODEX_HOME``,
    ``DSH_PERMISSION_MODE``, ...); they are applied after the filtering, so an adapter can
    still hand its child the role and identity it needs.
    """
    source = os.environ if base is None else base
    roots = tuple(roots if roots is not None else platform_source_dirs())
    env: dict[str, str] = {}
    for key, value in source.items():
        name = str(key)
        if name in DROPPED_ENV_NAMES:
            continue
        if name.startswith(DROPPED_ENV_PREFIXES) and name not in KEPT_ENV_NAMES:
            continue
        env[name] = value
    if "PYTHONPATH" in env:
        cleaned = sanitize_pythonpath(env["PYTHONPATH"], roots)
        if cleaned:
            env["PYTHONPATH"] = cleaned
        else:
            env.pop("PYTHONPATH", None)
    if overrides:
        for key, value in overrides.items():
            env[str(key)] = str(value)
        if "PYTHONPATH" in overrides:
            cleaned = sanitize_pythonpath(str(overrides["PYTHONPATH"]), roots)
            if cleaned:
                env["PYTHONPATH"] = cleaned
            else:
                env.pop("PYTHONPATH", None)
    return env


def parhelion_home() -> Path:
    override = os.environ.get("PARHELION_HOME") or os.environ.get("OPSB_HOME")
    return Path(override).expanduser().resolve() if override else (Path.home() / ".parhelion")


def config_carries_credentials(path: Path | None = None) -> bool:
    """True when ``config.json`` has a credential-shaped key anywhere in its tree."""
    target = Path(path) if path is not None else parhelion_home() / "config.json"
    try:
        doc = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False

    def walk(node: Any) -> bool:
        if isinstance(node, dict):
            for key, value in node.items():
                if CREDENTIAL_KEY_RE.search(str(key)) and value not in (None, "", [], {}):
                    return True
                if walk(value):
                    return True
        elif isinstance(node, list):
            return any(walk(item) for item in node)
        return False

    return walk(doc)


def read_deny_paths(*, home: Path | None = None, parhelion: Path | None = None) -> dict[str, list[str]]:
    """``{"dirs": [...], "files": [...], "regex": [...]}`` a worker process may not open.

    Only the credential stores are listed: workspace state, bench specs and the test-run
    registry stay readable because the worker's MCP server and its tools legitimately need
    them (the detection layer reports reads of ``state/`` and ``agent_bus.db`` instead).
    """
    ph = Path(parhelion) if parhelion is not None else parhelion_home()
    user_home = Path(home) if home is not None else Path.home()
    # sandbox(7) matches canonical paths: /var and /tmp are symlinks on macOS, so a filter
    # written from an unresolved path silently never fires.
    def _canonical(path: Path) -> str:
        try:
            return str(path.resolve())
        except OSError:
            return str(path)

    dirs = [_canonical(ph / name) for name in CREDENTIAL_DIRS]
    dirs.append(_canonical(user_home / LEGACY_JUDGE_DIR))
    files = [_canonical(ph / name) for name in CREDENTIAL_FILES]
    if config_carries_credentials(ph / "config.json"):
        files.append(_canonical(ph / "config.json"))
    return {
        "dirs": dirs,
        "files": files,
        "regex": [rf"/{re.escape(CACHE_BASENAME_PREFIX)}[^/]*$"],
    }


def seatbelt_profile(*, extra: Iterable[tuple[str, str]] = (), home: Path | None = None,
                     parhelion: Path | None = None) -> list[str]:
    """A ``sandbox-exec -p`` profile: allow everything, then deny the worker's read targets.

    ``extra`` adds ``(kind, path)`` pairs (``"subpath"`` or ``"literal"``) for the caller's own
    exclusions, e.g. the SSH store and the daemon token file.
    """
    deny = read_deny_paths(home=home, parhelion=parhelion)
    lines = ["(version 1)", "(allow default)"]
    for path in deny["dirs"]:
        lines.append(f'(deny file-read* file-write* (subpath {json.dumps(path)}))')
    for path in deny["files"]:
        lines.append(f'(deny file-read* file-write* (literal {json.dumps(path)}))')
    for pattern in deny["regex"]:
        lines.append(f'(deny file-read* (regex #"{pattern}"))')
    for kind, path in extra:
        try:
            path = Path(path).resolve()
        except OSError:
            pass
        lines.append(f'(deny file-read* file-write* ({kind} {json.dumps(str(path))}))')
    return lines


def wrap_command(cmd: list[str], *, extra: Iterable[tuple[str, str]] = (),
                 require_macos: bool = False) -> list[str]:
    """``cmd`` under the worker sandbox profile, or unchanged where the sandbox cannot run.

    ``require_macos`` keeps the managed-adapter contract (a worker is never launched
    unsandboxed on a platform without ``sandbox-exec``); the detached launch passes False so
    a portability gap degrades to detection instead of a failed launch.
    """
    available = platform.system() == "Darwin" and Path(SANDBOX_EXEC).exists()
    if not available:
        if require_macos:
            raise RuntimeError("Managed worker SSH isolation requires macOS")
        return list(cmd)
    return [SANDBOX_EXEC, "-p", "\n".join(seatbelt_profile(extra=extra)), *cmd]


def capabilities() -> dict[str, dict[str, str]]:
    """Which layer protects which driver (the report's per-driver table, from one source)."""
    wrapper = "platform sandbox profile (worker_access.prepare_worker_command)"
    return {
        "deepseek": {
            "sandbox_config": "DSH permission preset: `sandbox` names one mode (read-only / workspace-write / "
                              "danger-full-access) and `sandbox/mode` records the applied value; the mode confines "
                              "writes only, reads stay unconfined, so it cannot exclude a read path",
            "read_deny": wrapper,
            "extra": "detection covers what the mode cannot express",
        },
        "codex": {
            "sandbox_config": "config.toml: `default_permissions` + `[permissions.<name>.filesystem]` deny entries "
                              "(rendered by drivers/codex_appserver.render_codex_config_toml) and a per-turn "
                              "`sandboxPolicy`",
            "read_deny": wrapper,
            "extra": "the config profile is defence in depth; older builds may ignore it",
        },
        "claude": {"sandbox_config": "no config-file read policy; permission mode only", "read_deny": wrapper},
        "antigravity": {"sandbox_config": "ACP session/new has no read policy", "read_deny": wrapper},
        "gemini": {"sandbox_config": "none", "read_deny": wrapper},
        "pi": {"sandbox_config": "none", "read_deny": wrapper},
    }
