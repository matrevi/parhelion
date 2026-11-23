"""Transparent PersistentArenaRunner post-import hook for KernelC acl_runner.

Supports two acceleration modes:
  1. `OPSB_ARENA_PHASE=record` (Phase A, 0.0s NPU lock):
     Intercepts `Runner.run(...)` without calling `aclInit`, serializing
     `(inputs, attrs, out_specs)` and `(expected, extra)` separately into
     `OPSB_ARENA_CACHE_DIR` so Phase B replays pure NPU kernels and Phase C
     runs lock-free golden verification on 32 CPU cores.
  2. Default `OPSB_ARENA_ACCEL=1` (in-process Pointer Keepalive Ring):
     Eliminates the 4.15s/case subprocess overhead while preventing
     `NnopbaseMatchArgs` executor-cache pointer-reuse collisions.
"""
from __future__ import annotations

import atexit
import ctypes as C
import importlib.abc
import io
import json
import os
import pathlib
import pickle
import sys
import time
import traceback
from typing import Any

MAX_KEEPALIVE_CASES = int(os.environ.get("OPSB_ARENA_RING_CASES", "96"))
MAX_KEEPALIVE_BYTES = int(os.environ.get("OPSB_ARENA_RING_BYTES", str(2 * 1024 * 1024 * 1024)))
GUARD_PAD_BYTES = 64

_ACTIVE_ARENAS: dict[tuple[str, str, int], "_ArenaSession"] = {}
_STATS = {"cases_inproc": 0, "cases_fallback": 0, "flushes": 0, "acl_wall_sec": 0.0}
_RECORD_STATE: dict[str, Any] = {"count": 0, "vendor": "", "soft_mode": False, "poison": False}
_ATEXIT_REGISTERED = False


class _TeeStdout:
    def __init__(self, real_out: Any, buf: io.StringIO) -> None:
        self._real = real_out
        self._buf = buf

    def write(self, s: str) -> int:
        self._buf.write(s)
        return self._real.write(s)

    def flush(self) -> None:
        self._buf.flush()
        self._real.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _ArenaSession:
    """Wraps a single InlineRunner with a deferred Pointer Keepalive Ring."""

    def __init__(self, inline_cls: type, root: str, vendor: str) -> None:
        self.owner_pid = os.getpid()
        self.root = str(root)
        self.vendor = str(vendor)
        self.runner = inline_cls(self.root, self.vendor)
        self._alloc_sizes: dict[int, int] = {}
        self._ring_lists: list[Any] = []
        self._ring_intarrays: list[Any] = []
        self._ring_tensors: list[Any] = []
        self._ring_mem: list[Any] = []
        self._ring_cases = 0
        self._ring_bytes = 0
        self._install_wrappers()

    def _install_wrappers(self) -> None:
        acl = self.runner.acl
        base = self.runner.base

        self._orig_malloc = acl.aclrtMalloc
        self._orig_free = acl.aclrtFree
        self._orig_destroy_tensor = base.aclDestroyTensor
        self._orig_destroy_list = base.aclDestroyTensorList
        self._orig_destroy_intarray = base.aclDestroyIntArray

        def _wrapped_malloc(ptr_ref: Any, size: Any, policy: Any) -> int:
            req = int(getattr(size, "value", size))
            padded = max(req, 32) + GUARD_PAD_BYTES
            rc = self._orig_malloc(ptr_ref, C.c_uint64(padded), policy)
            if rc == 0:
                try:
                    addr = C.cast(ptr_ref, C.POINTER(C.c_void_p)).contents.value
                    if addr:
                        self._alloc_sizes[int(addr)] = padded
                except Exception:
                    pass
            return rc

        def _wrapped_free(ptr: Any) -> int:
            addr = getattr(ptr, "value", ptr)
            sz = self._alloc_sizes.pop(int(addr), 64) if addr else 0
            self._ring_mem.append(ptr)
            self._ring_bytes += sz
            return 0

        def _wrapped_destroy_tensor(t: Any) -> int:
            self._ring_tensors.append(t)
            return 0

        def _wrapped_destroy_list(p: Any) -> int:
            self._ring_lists.append(p)
            return 0

        def _wrapped_destroy_intarray(p: Any) -> int:
            self._ring_intarrays.append(p)
            return 0

        acl.aclrtMalloc = _wrapped_malloc
        acl.aclrtFree = _wrapped_free
        base.aclDestroyTensor = _wrapped_destroy_tensor
        base.aclDestroyTensorList = _wrapped_destroy_list
        base.aclDestroyIntArray = _wrapped_destroy_intarray

    def maybe_flush(self, force: bool = False) -> None:
        if os.getpid() != self.owner_pid:
            return
        if not force and self._ring_cases < MAX_KEEPALIVE_CASES and self._ring_bytes < MAX_KEEPALIVE_BYTES:
            return
        if not (self._ring_lists or self._ring_intarrays or self._ring_tensors or self._ring_mem):
            self._ring_cases = 0
            self._ring_bytes = 0
            return
        try:
            self.runner.acl.aclrtSynchronizeStream(self.runner.stream)
        except Exception:
            pass
        for p in self._ring_lists:
            try:
                self._orig_destroy_list(p)
            except Exception:
                pass
        for p in self._ring_intarrays:
            try:
                self._orig_destroy_intarray(p)
            except Exception:
                pass
        for t in self._ring_tensors:
            try:
                self._orig_destroy_tensor(t)
            except Exception:
                pass
        for m in self._ring_mem:
            try:
                self._orig_free(m)
            except Exception:
                pass
        self._ring_lists.clear()
        self._ring_intarrays.clear()
        self._ring_tensors.clear()
        self._ring_mem.clear()
        self._ring_cases = 0
        self._ring_bytes = 0
        _STATS["flushes"] += 1

    def run_case(self, *args: Any, **kwargs: Any) -> Any:
        t0 = time.monotonic()
        try:
            return self.runner.run(*args, **kwargs)
        finally:
            _STATS["acl_wall_sec"] += max(0.0, time.monotonic() - t0)
            self._ring_cases += 1
            self.maybe_flush(force=False)


def _flush_all_at_exit() -> None:
    if os.environ.get("OPSB_ARENA_PHASE") == "record":
        cache_dir_s = os.environ.get("OPSB_ARENA_CACHE_DIR", "")
        if cache_dir_s and _RECORD_STATE["count"] > 0:
            cdir = pathlib.Path(cache_dir_s)
            cdir.mkdir(parents=True, exist_ok=True)
            # For LayerNormV3, 4 det groups * 3 runs = 12 runs correspond to 4 reported cases (total - 8)
            cnt = int(_RECORD_STATE["count"])
            det_runs = int(_RECORD_STATE.get("det_runs", 0))
            reported = cnt - (det_runs * 2 // 3) if det_runs and det_runs % 3 == 0 else cnt
            tmp = cdir / "DONE.tmp"
            tmp.write_text(
                json.dumps(
                    {
                        "count": cnt,
                        "reported_cases": reported,
                        "vendor": _RECORD_STATE["vendor"],
                        "soft_mode": bool(_RECORD_STATE["soft_mode"]),
                        "poison": bool(_RECORD_STATE["poison"]),
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, cdir / "DONE")
        return

    for sess in list(_ACTIVE_ARENAS.values()):
        try:
            sess.maybe_flush(force=True)
        except Exception:
            pass
    stats_file = os.environ.get("OPSB_ARENA_STATS_FILE")
    if stats_file and (_STATS["cases_inproc"] > 0 or _STATS["cases_fallback"] > 0):
        try:
            with open(stats_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"pid": os.getpid(), **_STATS}) + "\n")
        except Exception:
            pass


def _is_precision_failure(exc: BaseException) -> bool:
    if isinstance(exc, AssertionError):
        return True
    msg = str(exc)
    return "outside the platform tolerance" in msg or "Not equal to tolerance" in msg


def patch_acl_runner_module(module: Any) -> None:
    global _ATEXIT_REGISTERED
    inline_cls = getattr(module, "InlineRunner", None)
    runner_cls = getattr(module, "Runner", None)
    if inline_cls is None or runner_cls is None:
        return
    if getattr(runner_cls, "_opsb_arena_patched", False):
        return

    orig_run = runner_cls.run

    if not _ATEXIT_REGISTERED:
        atexit.register(_flush_all_at_exit)
        _ATEXIT_REGISTERED = True

    # Detect if this repo's InlineRunner poisons output buffers (StridedSliceGrad)
    mod_file = getattr(module, "__file__", "") or ""
    uses_poison = False
    if mod_file and os.path.isfile(mod_file):
        try:
            uses_poison = "def poison(" in pathlib.Path(mod_file).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass

    def _record_run(self: Any, name: str, inputs: Any, attrs: Any, expected: Any, *extra_pos: Any, **extra_kw: Any) -> Any:
        cache_dir = pathlib.Path(os.environ["OPSB_ARENA_CACHE_DIR"])
        cache_dir.mkdir(parents=True, exist_ok=True)
        extra = extra_kw.get("extra", extra_pos[0] if len(extra_pos) >= 1 else None)
        soft = extra_kw.get("soft", extra_pos[1] if len(extra_pos) >= 2 else hasattr(self, "failures"))
        tag = extra_kw.get("tag", extra_pos[2] if len(extra_pos) >= 3 else "")

        idx = int(_RECORD_STATE["count"])
        _RECORD_STATE["vendor"] = str(self.vendor)
        _RECORD_STATE["soft_mode"] = bool(hasattr(self, "failures"))
        _RECORD_STATE["poison"] = bool(uses_poison)
        if isinstance(tag, str) and tag.startswith("det-"):
            _RECORD_STATE["det_runs"] = int(_RECORD_STATE.get("det_runs", 0)) + 1

        out_specs = [(tuple(a.shape), a.dtype) for a in expected]
        in_payload = {
            "name": name,
            "inputs": inputs,
            "attrs": attrs,
            "out_specs": out_specs,
            "tag": tag,
            "soft": bool(soft),
        }
        exp_payload = {
            "name": name,
            "tag": tag,
            "expected": expected,
            "extra": extra,
        }
        (cache_dir / f"case_{idx:04d}_in.pkl").write_bytes(pickle.dumps(in_payload, protocol=pickle.HIGHEST_PROTOCOL))
        (cache_dir / f"case_{idx:04d}_exp.pkl").write_bytes(pickle.dumps(exp_payload, protocol=pickle.HIGHEST_PROTOCOL))
        _RECORD_STATE["count"] = idx + 1
        if hasattr(self, "last_output"):
            self.last_output = "  sha256 out0=record_stub\n"
        return expected

    def _accelerated_run(self: Any, name: str, inputs: Any, attrs: Any, expected: Any, *extra_pos: Any, **extra_kw: Any) -> Any:
        if os.environ.get("OPSB_ARENA_PHASE") == "record" and os.environ.get("OPSB_ARENA_CACHE_DIR"):
            return _record_run(self, name, inputs, attrs, expected, *extra_pos, **extra_kw)

        has_soft_protocol = hasattr(self, "failures") or ("soft" in extra_kw) or ("extra" in extra_kw) or len(extra_pos) > 0
        extra = extra_kw.get("extra", extra_pos[0] if len(extra_pos) >= 1 else None)
        soft = extra_kw.get("soft", extra_pos[1] if len(extra_pos) >= 2 else True)
        tag = extra_kw.get("tag", extra_pos[2] if len(extra_pos) >= 3 else "")

        key = (str(self.root), str(self.vendor), os.getpid())
        buf = io.StringIO()
        prev_stdout = sys.stdout
        try:
            sess = _ACTIVE_ARENAS.get(key)
            if sess is None or sess.owner_pid != os.getpid():
                sess = _ArenaSession(inline_cls, str(self.root), str(self.vendor))
                _ACTIVE_ARENAS[key] = sess

            sys.stdout = _TeeStdout(prev_stdout, buf)
            try:
                if has_soft_protocol:
                    out = sess.run_case(name, inputs, attrs, expected, extra)
                else:
                    out = sess.run_case(name, inputs, attrs, expected)
            finally:
                sys.stdout = prev_stdout

            if hasattr(self, "last_output"):
                self.last_output = buf.getvalue()
            _STATS["cases_inproc"] += 1
            return out

        except Exception as exc:
            sys.stdout = prev_stdout
            captured = buf.getvalue()
            if hasattr(self, "last_output"):
                self.last_output = captured

            if _is_precision_failure(exc):
                _STATS["cases_inproc"] += 1
                if hasattr(self, "failures"):
                    self.failures.append((tag or name, 1))
                    if not soft:
                        raise RuntimeError(f"{name}: isolated case failed (1)") from exc
                    return None
                if not captured:
                    traceback.print_exc(file=sys.stdout)
                raise RuntimeError(f"{name}: isolated case failed (1)") from exc

            old = _ACTIVE_ARENAS.pop(key, None)
            if old is not None:
                try:
                    old.maybe_flush(force=True)
                except Exception:
                    pass
            _STATS["cases_fallback"] += 1
            return orig_run(self, name, inputs, attrs, expected, *extra_pos, **extra_kw)

    runner_cls._opsb_orig_run = orig_run
    runner_cls.run = _accelerated_run
    runner_cls._opsb_arena_patched = True


class _AclRunnerLoaderWrapper(importlib.abc.Loader):
    def __init__(self, orig_loader: Any) -> None:
        self._orig = orig_loader

    def create_module(self, spec: Any) -> Any:
        if hasattr(self._orig, "create_module"):
            return self._orig.create_module(spec)
        return None

    def exec_module(self, module: Any) -> None:
        self._orig.exec_module(module)
        patch_acl_runner_module(module)


class _AclRunnerMetaFinder(importlib.abc.MetaPathFinder):
    def __init__(self) -> None:
        self._in_find = False

    def find_spec(self, fullname: str, path: Any, target: Any = None) -> Any:
        if self._in_find:
            return None
        if fullname != "acl_runner" and not fullname.endswith(".acl_runner"):
            return None
        self._in_find = True
        try:
            for finder in sys.meta_path:
                if finder is self:
                    continue
                find_spec = getattr(finder, "find_spec", None)
                if find_spec is None:
                    continue
                spec = find_spec(fullname, path, target)
                if spec is not None and spec.loader is not None:
                    if not isinstance(spec.loader, _AclRunnerLoaderWrapper):
                        spec.loader = _AclRunnerLoaderWrapper(spec.loader)
                    return spec
            return None
        finally:
            self._in_find = False


def install_hook() -> None:
    if os.environ.get("OPSB_ARENA_ACCEL") != "1" and os.environ.get("OPSB_ARENA_PHASE") != "record":
        return
    if "--case" in sys.argv or "--child" in sys.argv:
        return
    if any(isinstance(f, _AclRunnerMetaFinder) for f in sys.meta_path):
        return
    sys.meta_path.insert(0, _AclRunnerMetaFinder())
    for mname, mod in list(sys.modules.items()):
        if mod is not None and (mname == "acl_runner" or mname.endswith(".acl_runner")):
            patch_acl_runner_module(mod)
