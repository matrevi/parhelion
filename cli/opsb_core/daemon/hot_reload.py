"""Reload the daemon's stateless scheduling modules without restarting it.

A restart interrupts every running worker, so edits to card text (`ipc_bridge`) or to the
dispatch decision (`daemon.dispatch`) used to wait for the next one. The daemon now reloads a
whitelist of pure-logic modules between dispatch ticks when their source changes, and on demand
through `opsb daemon reload`.

Rules:
* Only whitelisted modules are reloaded. They hold no live objects: the mesh manager, worker
  runners, SSE streams and caches live in modules this file refuses to touch.
* A module reloads in place (`importlib.reload`), so `module.attr` lookups see the new code at
  once. Functions other package modules imported by name are re-pointed to the new versions.
* Failure keeps the old module: a syntax error never reaches the module, and an error while
  executing restores the previous namespace. The failure is reported and retried only after the
  file changes again (or on a manual reload).
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import threading
import time
import traceback
import types
from pathlib import Path
from typing import Any, Iterable

_PACKAGE = __name__.rsplit(".daemon.", 1)[0]

#: Reloadable modules, relative to the `opsb_core` package. Never add one that owns live state.
HOT_MODULES: tuple[str, ...] = ("ipc_bridge", "daemon.dispatch")

#: Modules holding live objects; the reloader refuses them even if a whitelist names them.
STATEFUL_MODULES: tuple[str, ...] = (
    "agents.manager", "agents.bus_store", "agents.adapters", "daemon.server", "daemon.state_cache",
    "daemon.npu_stream", "daemon.agent_routes", "daemon.hot_reload", "mcp.handlers_agents", "config",
)


def _qualified(names: Iterable[str], package: str) -> tuple[str, ...]:
    return tuple(f"{package}.{name}" if package else name for name in names)


def _stamp(path: str | None) -> tuple[int, int] | None:
    try:
        st = os.stat(path) if path else None
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size) if st else None


def _rebind(old: dict[str, Any], module: types.ModuleType) -> int:
    """Re-point `from <module> import f` bindings in the package's loaded modules; return the count."""
    mapping: dict[int, Any] = {}
    for key, value in old.items():
        new = module.__dict__.get(key)
        if (isinstance(value, types.FunctionType) and value.__module__ == module.__name__
                and isinstance(new, types.FunctionType) and new is not value):
            mapping[id(value)] = new
    if not mapping:
        return 0
    root = module.__name__.split(".")[0]
    count = 0
    for name, other in list(sys.modules.items()):
        if other is None or other is module or not (name == root or name.startswith(root + ".")):
            continue
        namespace = getattr(other, "__dict__", None)
        if not isinstance(namespace, dict):
            continue
        for attr, value in list(namespace.items()):
            new = mapping.get(id(value))
            if new is not None and namespace.get(attr) is value:
                namespace[attr] = new
                count += 1
    return count


class ModuleReloader:
    """Watch whitelisted modules by mtime and size; reload changed ones on `poll()`."""

    def __init__(self, names: Iterable[str], *, protected: Iterable[str] = ()) -> None:
        self.names = tuple(names)
        refused = sorted(set(self.names) & set(protected))
        if refused:
            raise ValueError(f"refusing to hot-reload stateful modules: {', '.join(refused)}")
        self._lock = threading.Lock()
        self._stamps: dict[str, tuple[int, int] | None] = {}
        self.errors: dict[str, str] = {}
        self.reloaded_at: dict[str, float] = {}
        for name in self.names:
            module = sys.modules.get(name)
            self._stamps[name] = _stamp(getattr(module, "__file__", None)) if module else None

    def changed(self) -> list[str]:
        out = []
        for name in self.names:
            module = sys.modules.get(name)
            if module is None:
                continue  # never imported: nothing to replace, first import is already fresh
            if _stamp(getattr(module, "__file__", None)) != self._stamps.get(name):
                out.append(name)
        return out

    def poll(self) -> dict[str, Any] | None:
        """Reload modules whose source changed since the last attempt; None when none did."""
        names = self.changed()
        return self.reload(names) if names else None

    def reload(self, names: Iterable[str] | None = None) -> dict[str, Any]:
        """Reload `names` (default: the whole whitelist); a failing module keeps its old code."""
        targets = list(names) if names is not None else list(self.names)
        with self._lock:
            results = [self._reload_one(name) for name in targets if name in self.names]
        return {"ok": all(r["ok"] for r in results), "modules": results}

    def status(self) -> dict[str, Any]:
        return {"modules": list(self.names), "errors": dict(self.errors),
                "reloaded_at": dict(self.reloaded_at), "pending": self.changed()}

    def _reload_one(self, name: str) -> dict[str, Any]:
        module = sys.modules.get(name) or importlib.import_module(name)
        path = getattr(module, "__file__", None)
        stamp = _stamp(path)
        self._stamps[name] = stamp  # one attempt per file version; a manual reload retries
        try:
            source = Path(path).read_bytes() if path else b""
            compile(source, path or name, "exec")  # syntax errors never reach the live module
        except (OSError, SyntaxError, ValueError) as exc:
            return self._failed(name, exc)
        try:
            # The bytecode cache validates by whole seconds and size; drop it so a quick
            # same-size edit is not served stale.
            os.unlink(importlib.util.cache_from_source(path))
        except (OSError, NotImplementedError, ValueError, TypeError):
            pass
        snapshot = dict(module.__dict__)
        try:
            importlib.reload(module)
        except BaseException as exc:  # noqa: BLE001 - module code may raise anything
            # Restore in place (never clear) so concurrent readers always find a binding.
            module.__dict__.update(snapshot)
            for key in [k for k in module.__dict__ if k not in snapshot]:
                module.__dict__.pop(key, None)
            sys.modules[name] = module
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return self._failed(name, exc)
        rebound = _rebind(snapshot, module)
        self.errors.pop(name, None)
        self.reloaded_at[name] = round(time.time(), 3)
        return {"module": name, "ok": True, "rebound": rebound}

    def _failed(self, name: str, exc: BaseException) -> dict[str, Any]:
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()[-600:]
        self.errors[name] = detail
        return {"module": name, "ok": False, "error": detail, "kept": "previous module"}


def daemon_reloader() -> ModuleReloader:
    """The daemon's reloader over `HOT_MODULES`, importing each so its version is recorded."""
    names = _qualified(HOT_MODULES, _PACKAGE)
    for name in names:
        importlib.import_module(name)
    return ModuleReloader(names, protected=_qualified(STATEFUL_MODULES, _PACKAGE))
