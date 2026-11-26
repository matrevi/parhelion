"""Deterministic classification of logged tool calls: shell programs, inline scripts, reads, tests.

Shared by the sprint metrics and the replay probes so both count the same events.
"""
from __future__ import annotations

import os
import re
import shlex
from typing import Any

from .loader import ToolCall

SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\||\n")
CD_GROUPS = ("sed", "git", "python3", "grep", "ls", "cat")
PROGRAM_ALIASES = {"python": "python3", "rg": "grep", "egrep": "grep", "fgrep": "grep"}
INLINE_PY = re.compile(r"\bpython3?\b[^|;&\n]*?(\s-c\s|\s-\s*<<|<<)")
CLI_BYPASS = {
    "opsb queue list": re.compile(r"\bopsb\s+queue\s+list\b"),
    "opsb history": re.compile(r"\bopsb\s+history\b"),
    "opsb bench verify-escape": re.compile(r"\bopsb\s+bench\s+verify-escape\b"),
}
READ_PROGRAMS = frozenset({"cat", "sed", "head", "tail", "nl", "less", "more", "grep"})
#: Programs whose first operand is a script or pattern rather than a file.
SCRIPT_FIRST = frozenset({"sed", "grep"})
SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")


def shell_command(call: ToolCall) -> str:
    return str(call.args.get("command") or "") if call.name == "bash" else ""


def _program(segment: str) -> str:
    words = segment.strip().split()
    while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
        words = words[1:]  # leading VAR=value assignments
    if words and words[0] in ("timeout", "env", "time", "nice"):
        words = [w for w in words[1:] if not re.match(r"^-|^\d+[smh]?$", w)] or words
    prog = words[0].rsplit("/", 1)[-1] if words else ""
    prog = prog.rstrip(";")
    if re.match(r"^python3(\.\d+)?$", prog):
        prog = "python3"
    return PROGRAM_ALIASES.get(prog, prog)


def cd_program(command: str) -> str | None:
    """Program run after a leading ``cd X &&`` chain; None when the command does not start with cd."""
    text = command.lstrip()
    if not text.startswith("cd "):
        return None
    segments = [s for s in SEGMENT_SPLIT.split(text) if s.strip()]
    for seg in segments:
        if not seg.strip().startswith("cd "):
            return _program(seg) or ""
    return ""


def cd_group(command: str) -> str | None:
    prog = cd_program(command)
    if prog is None:
        return None
    return prog if prog in CD_GROUPS else "other"


def inline_python(command: str) -> dict[str, bool] | None:
    """``{reads_json, parses_report}`` for an inline (``-c`` / heredoc) Python script, else None."""
    if not INLINE_PY.search(command):
        return None
    return {"reads_json": "json" in command, "parses_report": "report.json" in command}


def cli_bypass(command: str) -> list[str]:
    return [name for name, pat in CLI_BYPASS.items() if pat.search(command)]


def _cwd_of(call: ToolCall, command: str, default: str) -> str:
    wd = str(call.args.get("workdir") or "")
    m = re.match(r"\s*cd\s+(\"[^\"]+\"|'[^']+'|\S+)", command)
    if m:
        target = m.group(1).strip("'\"")
        return os.path.normpath(os.path.join(wd or default, os.path.expanduser(target)))
    return wd or default


def _abs(path: str, cwd: str) -> str:
    path = os.path.expanduser(path.strip("'\""))
    return os.path.normpath(path if os.path.isabs(path) or not cwd else os.path.join(cwd, path))


def read_paths(call: ToolCall, cwd: str = "") -> list[str]:
    """Files a call reads: the ``read`` tool, or ``cat``/``sed``/``head``/``tail``/``grep`` segments."""
    if call.name == "read":
        p = str(call.args.get("file_path") or call.args.get("path") or "")
        return [_abs(p, cwd)] if p else []
    command = shell_command(call)
    if not command:
        return []
    base = _cwd_of(call, command, cwd)
    out: list[str] = []
    for seg in SEGMENT_SPLIT.split(command):
        if _program(seg) not in READ_PROGRAMS or ">" in seg or "<<" in seg:
            continue
        try:
            words = shlex.split(seg)
        except ValueError:
            continue
        while words and "=" in words[0] and not words[0].startswith(("/", ".")):
            words = words[1:]
        prog, args = (_program(words[0]), words[1:]) if words else ("", [])
        files = [a for a in args if not a.startswith("-") and not re.match(r"^\d+$", a)]
        if prog in SCRIPT_FIRST and files and not any(a in ("-e", "-f") for a in args):
            files = files[1:]  # the first operand is the sed script or grep pattern
        out.extend(_abs(f, base) for f in files if re.search(r"[./]", f) and not re.search(r"[{}$*?]", f))
    return out


def test_key(call: ToolCall) -> tuple[str, str] | None:
    """``(sha7, level)`` of a ``parhelion_test`` request; the result's sha7 wins over the argument."""
    if call.name != "parhelion_test":
        return None
    level = str(call.args.get("level") or "standard").lower()
    m = re.search(r'"sha7"\s*:\s*"([0-9a-f]{7})"', call.result)
    sha = m.group(1) if m else str(call.args.get("sha") or "").lower()[:7]
    if not SHA_RE.fullmatch(sha or ""):
        return None
    return sha, level


def commit_messages(call: ToolCall) -> list[str]:
    command = shell_command(call)
    if "git" not in command or "commit" not in command:
        return []
    return [m.group(2) for m in re.finditer(r"(?:^|\s)(?:-[A-Za-z]*m|--message)(?:\s+|=)([\"'])(.+?)(?<!\\)\1",
                                            command, re.S)]


def args_text(call: ToolCall, *keys: str) -> str:
    return " ".join(str(call.args.get(k) or "") for k in keys if isinstance(call.args.get(k), (str, int, float)))


def platform_result(call: ToolCall) -> dict[str, Any]:
    """Best-effort JSON body of a platform tool result (MCP text content)."""
    import json
    text = call.result.strip()
    if not text.startswith("{"):
        return {}
    try:
        obj = json.loads(text)
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}
