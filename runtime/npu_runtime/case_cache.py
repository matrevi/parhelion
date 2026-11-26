"""Prepared reference cases cached on the device host: entries, LRU budgets and hit statistics.

Inputs and goldens depend only on the oracle side of the checkout, the generated I/O schema
and the case item, never on the kernel. Each candidate of a problem therefore pays for
reference generation once. Layout: ``<top>/<title>/<key>/`` per entry (read-only files plus
``.complete`` with the byte count) and ``<top>/.stats.jsonl`` with one line per preparation.

Stdlib only. The case-bench bootstrap ships this file next to ``case_batch.py``, and
``opsb doctor --cases --remote`` runs the same source on the device host with a short footer,
so the budget the runs enforce and the numbers the doctor shows come from one definition.
"""
import fcntl
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

CACHE_FORMAT = 1
KERNEL_SUFFIXES = {".asc", ".h", ".hpp", ".hh", ".c", ".cc", ".cpp", ".cxx", ".cu", ".cmake"}
CACHE_BYTES_PER_PROBLEM = int(os.environ.get("OPSB_CASE_CACHE_BYTES") or 8 << 30)
#: Default budget across every problem; the platform sends `device.case_cache_bytes` in the run config.
CACHE_BYTES_TOTAL = 24 << 30
CACHE_MAX_AGE_SEC = 7 * 86400
STATS_NAME = ".stats.jsonl"
STATS_KEEP = 1000
STATS_TRIM_BYTES = 512 << 10


def _positive(value, default):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def total_budget(cfg):
    """Host override, else the configured device setting, else 24 GiB."""
    configured = _positive(cfg.get("case_cache_bytes"), CACHE_BYTES_TOTAL)
    return _positive(os.environ.get("OPSB_CASE_CACHE_TOTAL_BYTES"), configured)


def find_root(configured=None):
    """The cache directory a doctor run on the host inspects.

    ``configured`` is the platform's ``device.case_cache_dir`` (none when unset). The host's
    ``OPSB_CASE_CACHE_DIR`` override wins; an explicit configured directory is authoritative
    even when it does not exist yet, so the doctor reports the place the runs will use.
    """
    if configured:
        return Path(os.environ.get("OPSB_CASE_CACHE_DIR") or configured)
    candidates = [Path(tempfile.gettempdir()) / "opsb-case-cache", Path("/tmp/opsb-case-cache")]
    if os.environ.get("OPSB_CASE_CACHE_DIR"):
        candidates.insert(0, Path(os.environ["OPSB_CASE_CACHE_DIR"]))
    return next((path for path in candidates if path.is_dir()), candidates[0])


def same_filesystem(left, right):
    """True when both paths resolve to one filesystem, so cached files can be hard-linked."""
    def device(path):
        probe = Path(path)
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            return probe.stat().st_dev
        except OSError:
            return None
    a, b = device(left), device(right)
    return a is not None and a == b


def _link_or_copy(src, dst):
    """Hard-link (the cache sits on the run directory's filesystem); copy across filesystems."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _oracle_digest(repo):
    """Digest every checkout file that case generation can read; kernel sources are left out."""
    h = hashlib.sha256()
    files = []
    for top, dirs, names in os.walk(repo):
        dirs[:] = [d for d in dirs if not (d.startswith("build") or d in ("__pycache__", ".git"))]
        files.extend(Path(top) / name for name in names)
    for path in sorted(files):
        if path.suffix.lower() in KERNEL_SUFFIXES or path.name == "CMakeLists.txt" or path.suffix == ".pyc":
            continue
        h.update(str(path.relative_to(repo)).encode() + b"\\0" + path.read_bytes() + b"\\0")
    return h.hexdigest()


def entries(top):
    """Every complete entry under ``top``, as dicts with path, title, bytes and last use (mtime)."""
    rows = []
    if not Path(top).is_dir():
        return rows
    for problem in sorted(Path(top).iterdir()):
        if problem.name.startswith(".") or not problem.is_dir():
            continue
        for entry in problem.iterdir():
            if entry.name.startswith(".") or not entry.is_dir():
                continue
            try:
                size = int(json.loads((entry / ".complete").read_text())["bytes"])
                used = entry.stat().st_mtime
            except (OSError, ValueError, KeyError, TypeError):
                continue
            rows.append({"path": entry, "title": problem.name, "bytes": size, "used": used})
    return rows


def prune_global(top, budget):
    """Evict least recently used whole entries across problems until the cache fits ``budget``.

    Runs that restored an entry hold their own hard links, so evicting it never breaks them.
    """
    kept = evicted = 0
    for row in sorted(entries(top), key=lambda r: r["used"], reverse=True):
        kept += row["bytes"]
        if kept > budget:
            shutil.rmtree(row["path"], ignore_errors=True)
            evicted += 1
    return evicted


def record(top, **fields):
    """Append one preparation's hit/miss line; the file keeps the latest STATS_KEEP lines."""
    path = Path(top) / STATS_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.write(json.dumps(dict(at=round(time.time(), 3), **fields), sort_keys=True) + "\n")
            fh.flush()
            if fh.tell() > STATS_TRIM_BYTES:
                temp = path.with_name(f".{STATS_NAME}.{os.getpid()}.tmp")
                temp.write_text("".join(path.read_text().splitlines(True)[-STATS_KEEP:]))
                temp.replace(path)
    except OSError:
        pass


def recent_stats(top, limit):
    try:
        lines = (Path(top) / STATS_NAME).read_text().splitlines()
    except OSError:
        return []
    rows = []
    for line in lines[-max(0, int(limit)):] if limit else []:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def report(top, budget, recent=20):
    """Per-problem and total use, the budgets, the oldest entry and the hit rate of recent runs."""
    rows = entries(top)
    problems = {}
    for row in rows:
        item = problems.setdefault(row["title"], {"title": row["title"], "entries": 0, "bytes": 0,
                                                   "oldest_used": row["used"], "newest_used": row["used"]})
        item["entries"] += 1
        item["bytes"] += row["bytes"]
        item["oldest_used"] = min(item["oldest_used"], row["used"])
        item["newest_used"] = max(item["newest_used"], row["used"])
    runs = recent_stats(top, recent)
    hits = sum(int(r.get("hits") or 0) for r in runs)
    misses = sum(int(r.get("misses") or 0) for r in runs)
    return {
        "root": str(top), "exists": Path(top).is_dir(), "now": round(time.time(), 3),
        "budget_bytes": int(budget), "per_problem_budget_bytes": CACHE_BYTES_PER_PROBLEM,
        "max_age_sec": CACHE_MAX_AGE_SEC, "total_bytes": sum(r["bytes"] for r in rows), "entries": len(rows),
        "oldest_used": min((r["used"] for r in rows), default=None),
        "problems": sorted(problems.values(), key=lambda p: -p["bytes"]),
        "recent": {"runs": len(runs), "hits": hits, "misses": misses,
                   "hit_rate": round(hits / (hits + misses), 4) if hits + misses else None,
                   "evicted": sum(int(r.get("evicted") or 0) for r in runs)},
    }


def purge(top):
    """Remove every entry (complete or not); statistics stay. Return (entries, bytes) removed."""
    removed = freed = 0
    if not Path(top).is_dir():
        return removed, freed
    sizes = {str(r["path"]): r["bytes"] for r in entries(top)}
    for problem in Path(top).iterdir():
        if not problem.is_dir() or problem.is_symlink():
            continue
        for entry in problem.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
                freed += sizes.get(str(entry), 0)
    return removed, freed


class CaseCache:
    def __init__(self, repo, cfg):
        from importlib import metadata
        self.enabled = cfg.get("case_cache", True) and os.environ.get("OPSB_CASE_CACHE", "1") != "0"
        self.title = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(cfg.get("title") or "problem"))
        # `device.case_cache_dir` when the platform sets one, else next to the run's scratch
        # directory (`device.case_tmp_dir` moves both), so cached files can be hard-linked.
        self.top = Path(os.environ.get("OPSB_CASE_CACHE_DIR") or cfg.get("case_cache_dir")
                        or repo.parent.parent / "opsb-case-cache")
        self.root = self.top / self.title
        self.budget = total_budget(cfg)
        try:
            numpy_version = metadata.version("numpy")
        except metadata.PackageNotFoundError:
            numpy_version = "none"
        engine = (repo.parent / "reference_cases.py").read_bytes()
        schema = (repo.parent / "schema.json").read_bytes()
        base = [CACHE_FORMAT, sys.version.split()[0], numpy_version, _oracle_digest(repo),
                hashlib.sha256(engine).hexdigest(), hashlib.sha256(schema).hexdigest()]
        self.base = json.dumps(base)

    def key(self, item):
        return hashlib.sha256((self.base + json.dumps(item, sort_keys=True)).encode()).hexdigest()[:40]

    def restore(self, item, case):
        """Copy a cached case into ``case``; return its parts with absolute paths, or None."""
        if not self.enabled:
            return None
        entry = self.root / self.key(item)
        if not (entry / ".complete").is_file():
            return None
        try:
            shutil.copytree(entry, case, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".complete"),
                            copy_function=_link_or_copy)
            parts = json.loads((case / "parts.json").read_text())
            for part in parts:
                part["path"] = str(case / part["path"])
            os.utime(entry)  # last use, for both LRU budgets
            return parts
        except (OSError, ValueError, KeyError):
            return None

    def store(self, item, case, parts):
        """Publish a generated case; return True when this call created the entry."""
        if not self.enabled:
            return False
        entry = self.root / self.key(item)
        if entry.exists():
            return False
        temp = self.root / f".{entry.name}.{os.getpid()}.tmp"
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(case, temp, copy_function=_link_or_copy)
            relative = [dict(part, path=str(Path(part["path"]).relative_to(case))) for part in parts]
            (temp / "parts.json").unlink()
            (temp / "parts.json").write_text(json.dumps(relative))
            size = 0
            for top, _, names in os.walk(temp):
                for name in names:
                    path = Path(top) / name
                    # Shared by every run that restores it: inputs and goldens are read-only.
                    path.chmod(0o444)
                    size += path.stat().st_size
            (temp / ".complete").write_text(json.dumps({"bytes": size}))
            temp.rename(entry)
            return True
        except (OSError, ValueError):
            shutil.rmtree(temp, ignore_errors=True)
            return False

    def prune(self):
        """Apply this problem's byte budget and age limit, then the global budget; return evictions."""
        if not self.enabled or not self.root.is_dir():
            return 0
        now, kept, evicted = time.time(), 0, 0
        entries_ = sorted((p for p in self.root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
        for entry in entries_:
            age = now - entry.stat().st_mtime
            if entry.name.startswith("."):
                if age > 3600:
                    shutil.rmtree(entry, ignore_errors=True)
                continue
            try:
                kept += int(json.loads((entry / ".complete").read_text())["bytes"])
            except (OSError, ValueError, KeyError, TypeError):
                kept += 0 if age < 3600 else CACHE_BYTES_PER_PROBLEM
            if kept > CACHE_BYTES_PER_PROBLEM or age > CACHE_MAX_AGE_SEC:
                shutil.rmtree(entry, ignore_errors=True)
                evicted += 1
        return evicted + prune_global(self.top, self.budget)

    def finish(self, cfg, hits, misses, stored):
        """End of one preparation: enforce the budgets, then log hits and misses."""
        if not self.enabled:
            return
        try:
            evicted = self.prune()
        except OSError:
            evicted = 0
        record(self.top, title=self.title, sha=str(cfg.get("sha") or "")[:12], run_id=cfg.get("run_id"),
               hits=int(hits), misses=int(misses), stored=int(stored), evicted=int(evicted))
