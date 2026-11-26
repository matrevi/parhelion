"""Code-built repository map for the worker start-up pack (4.2 G, research R11).

The map has two sources. A worktree that carries ``aoci.txt`` is described by the compressed ``F``
(core responsibility) field of every entry, which is the densest stable summary of the managed files;
a worktree without one is scanned for its managed source files and described as one line each with the
path, the line count and the main symbols. R11 keeps the map **off by default**: the study behind it
measured a 20-23% cost increase from generated overviews with no measurable gain, so the pack enables
it only when a replay shows it pays for itself (``startup_pack.config()``'s ``repo_map`` flag).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .brief import _truncate_utf8

MAP_F_MAX = 140  # bytes of one compressed F field
MAP_FILES_MAX = 60
MAP_MIN_BYTES = 400
MAP_MAX_BYTES = 7000
SKIP_DIRS = frozenset({".git", ".worktrees", "__pycache__", "node_modules", "results", "bench-runs",
                       ".pytest_cache", ".venv", "build", "cmake-build-debug"})
SOURCE_EXTS = {".py", ".asc", ".h", ".hpp", ".cpp", ".cc", ".c", ".sh", ".cmake", ".txt", ".json", ".md"}
ENTRY_RE = re.compile(r"^(\S+?)\[([^\]]*)\]:\s*F:(.*?)(?:\|\s*R:|$)")
PY_DEF_RE = re.compile(r"^(?:async\s+)?def\s+([A-Za-z_]\w*)|^class\s+([A-Za-z_]\w*)", re.M)
C_DEF_RE = re.compile(r"^(?:template\s*<[^>\n]*>\s*)?(?:__\w+__[ \t]+)*(?:inline[ \t]+|static[ \t]+|extern[ \t]+)*"
                      r"[A-Za-z_][\w:<>,*& \t]*?[\s*&]([A-Za-z_]\w*)[ \t]*\([^;\n]*$", re.M)
NOT_SYMBOLS = frozenset({"if", "for", "while", "switch", "return"})


def aoci_entries(worktree: Path) -> list[tuple[str, str]]:
    """``(path, F)`` pairs from ``aoci.txt`` and any volume files its manifest names."""
    root = worktree / "aoci.txt"
    try:
        text = root.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    sources = [text]
    for rel in re.findall(r"#Volume:.*?\bpath=(\S+)", text):
        if rel and "/" not in rel and ".." not in rel:
            try:
                sources.append((worktree / rel).read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for body in sources:
        for line in body.splitlines():
            m = ENTRY_RE.match(line.strip())
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            out.append((m.group(1), " ".join(m.group(3).split())))
    return out


def symbols(path: Path) -> list[str]:
    """The main definitions of one file: ``def``/``class``, C-like signatures, or Markdown headings."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: list[str] = []
    if path.suffix == ".py":
        found = [a or b for a, b in PY_DEF_RE.findall(text)]
    elif path.suffix in (".asc", ".h", ".hpp", ".cpp", ".cc", ".c"):
        found = C_DEF_RE.findall(text)
    elif path.suffix == ".md":
        found = re.findall(r"^#{1,3}\s+(.+?)\s*$", text, re.M)
    out: list[str] = []
    for name in found:
        name = name.strip()
        if name and name not in out and name not in NOT_SYMBOLS:
            out.append(name)
    return out[:8]


def code_entries(worktree: Path) -> list[tuple[str, str]]:
    """One ``(relative path, "N lines: symbols")`` row per managed source file."""
    base = Path(worktree).expanduser().resolve()
    rows: list[tuple[int, str, str]] = []
    for path in sorted(base.rglob("*")):
        try:
            rel = path.relative_to(base)
        except ValueError:
            continue
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            continue
        if not path.is_file() or path.suffix.lower() not in SOURCE_EXTS:
            continue
        try:
            lines = sum(1 for _ in path.open("rb"))
        except OSError:
            continue
        named = ", ".join(symbols(path))
        rows.append((lines, rel.as_posix(), f"{lines} lines" + (f": {named}" if named else "")))
    rows.sort(key=lambda r: (-r[0], r[1]))
    return [(rel, body) for _n, rel, body in rows[:MAP_FILES_MAX]]


def repo_map(worktree: Path | str, max_bytes: int = MAP_MAX_BYTES) -> dict[str, Any]:
    """``{text, source, entries}``: ``aoci`` F fields first, else a code-built map, else nothing."""
    base = Path(worktree).expanduser().resolve()
    entries, source = aoci_entries(base), "aoci"
    if not entries:
        entries, source = code_entries(base), "code"
    if not entries:
        return {"text": "", "source": "none", "entries": 0}
    kind = "F fields of aoci.txt" if source == "aoci" else "path, length and main symbols"
    lines = [f"Managed files ({len(entries)}, one line each; {kind})."]
    for path, body in entries:
        if source == "aoci" and len(body.encode("utf-8")) > MAP_F_MAX:
            body = body.encode("utf-8")[:MAP_F_MAX].decode("utf-8", "ignore").rstrip() + "…"
        lines.append(f"- {path}: {body}")
    return {"text": _truncate_utf8("\n".join(lines), max_bytes), "source": source,
            "entries": len(entries)}


__all__ = [
    "MAP_F_MAX",
    "MAP_FILES_MAX",
    "MAP_MAX_BYTES",
    "MAP_MIN_BYTES",
    "aoci_entries",
    "code_entries",
    "repo_map",
    "symbols",
]
