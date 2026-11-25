#!/usr/bin/env python3
"""Phase-A/B/C split extended-suite replay & lock-free golden verification.

Phase A (0.0s NPU lock): `extended.py` runs with `OPSB_ARENA_PHASE=record`,
  which intercepts `Runner.run(...)` in `arena_accel.py` and saves inputs/attrs
  and expected goldens separately into `/tmp/opsb-cases/<Title>/<spec_hash>/`.
  Subsequent commits with identical `extended.py` + `reference.py` hit cache
  in 0.00s!

Phase B (inside device0.lock, ~1.8-3.0s): `arena_cache.py replay` loads ONLY
  the cached inputs/attrs, runs ONE `aclInit` with a Pointer Keepalive Ring
  (preventing `NnopbaseMatchArgs` executor-cache collisions), executes all
  kernels (`H2D -> aclnn -> D2H`), and dumps raw outputs to `--out`. ZERO CPU
  RNG, ZERO `reference.impl`, ZERO float64/tolerance diffing inside lock!

Phase C (0.0s NPU lock): `arena_cache.py verify` loads raw outputs + cached
  goldens on the 32-core CPU, runs exact platform tolerances & determinism
  checks, and prints the exact verdict lines expected by `gate.py`.
"""
from __future__ import annotations

import argparse
import ctypes as C
import hashlib
import json
import os
import pathlib
import pickle
import shutil
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np

P = C.c_void_p
I64 = C.c_int64
U64 = C.c_uint64

ACL_TYPES = {
    "float32": 0,
    "float16": 1,
    "int8": 2,
    "int32": 3,
    "uint8": 4,
    "int16": 6,
    "uint16": 7,
    "uint32": 8,
    "int64": 9,
    "uint64": 10,
    "float64": 11,
    "bool": 12,
    "bfloat16": 27,
}

MAX_KEEPALIVE_CASES = int(os.environ.get("OPSB_ARENA_RING_CASES", "96"))
MAX_KEEPALIVE_BYTES = int(os.environ.get("OPSB_ARENA_RING_BYTES", str(2 * 1024 * 1024 * 1024)))
GUARD_PAD_BYTES = 64


def _poison_array(shape: tuple[int, ...], dtype: Any) -> np.ndarray:
    try:
        return np.full(shape, np.nan, dtype=np.float32, order="C").astype(dtype, order="C")
    except (TypeError, ValueError):
        return np.full(shape, np.iinfo(dtype).min, dtype=dtype, order="C")


def _zero_array(shape: tuple[int, ...], dtype: Any) -> np.ndarray:
    return np.zeros(shape, dtype=dtype, order="C")


class _FastNpuReplayer:
    """Single-process ACL replayer with deferred Pointer Keepalive Ring (zero CPU verification)."""

    def __init__(self, repo: pathlib.Path, vendor: str, poison_outputs: bool = False) -> None:
        self.repo = repo
        self.vendor = vendor
        self.poison_outputs = poison_outputs
        pkg = repo / "build/packages/vendors" / vendor
        os.environ[os.environ.get("OPSB_EXTRA_OPS_ENV") or "EXTRA_OPS_PATH"] = str(pkg)
        self.acl = C.CDLL(os.environ.get("OPSB_RUNTIME_LIB") or "libruntime.so", mode=C.RTLD_GLOBAL)
        self.base = C.CDLL("libnnopbase.so", mode=C.RTLD_GLOBAL)
        self.lib = C.CDLL(str(pkg / "op_api/lib/libcust_opapi.so"), mode=C.RTLD_GLOBAL)
        self.acl.aclrtMalloc.argtypes = [C.POINTER(P), U64, C.c_int]
        self.acl.aclrtMemcpy.argtypes = [P, U64, P, U64, C.c_int]
        self.acl.aclrtFree.argtypes = [P]
        self.acl.aclrtCreateStream.argtypes = [C.POINTER(P)]
        self.acl.aclrtSynchronizeStream.argtypes = [P]
        self.acl.aclrtDestroyStream.argtypes = [P]
        self.base.aclCreateTensor.argtypes = [
            C.POINTER(I64), U64, C.c_int, C.POINTER(I64), I64, C.c_int, C.POINTER(I64), U64, P,
        ]
        self.base.aclCreateTensor.restype = P
        self.base.aclDestroyTensor.argtypes = [P]
        self.base.aclCreateTensorList.argtypes = [C.POINTER(P), U64]
        self.base.aclCreateTensorList.restype = P
        self.base.aclDestroyTensorList.argtypes = [P]
        self.base.aclCreateIntArray.argtypes = [C.POINTER(I64), U64]
        self.base.aclCreateIntArray.restype = P
        self.base.aclDestroyIntArray.argtypes = [P]
        self.check(self.acl.aclInit(None))
        self.check(self.acl.aclrtSetDevice(0))
        self.stream = P()
        self.check(self.acl.aclrtCreateStream(C.byref(self.stream)))
        self._ring_lists: list[Any] = []
        self._ring_intarrays: list[Any] = []
        self._ring_tensors: list[Any] = []
        self._ring_mem: list[Any] = []
        self._ring_cases = 0
        self._ring_bytes = 0

    @staticmethod
    def check(code: int) -> None:
        if code:
            raise RuntimeError(f"ACL status {code}")

    def _flush_ring(self, force: bool = False) -> None:
        if not force and self._ring_cases < MAX_KEEPALIVE_CASES and self._ring_bytes < MAX_KEEPALIVE_BYTES:
            return
        if not (self._ring_lists or self._ring_intarrays or self._ring_tensors or self._ring_mem):
            self._ring_cases = 0
            self._ring_bytes = 0
            return
        try:
            self.acl.aclrtSynchronizeStream(self.stream)
        except Exception:
            pass
        for p in self._ring_lists:
            self.base.aclDestroyTensorList(p)
        for p in self._ring_intarrays:
            self.base.aclDestroyIntArray(p)
        for t in self._ring_tensors:
            self.base.aclDestroyTensor(t)
        for m in self._ring_mem:
            self.acl.aclrtFree(m)
        self._ring_lists.clear()
        self._ring_intarrays.clear()
        self._ring_tensors.clear()
        self._ring_mem.clear()
        self._ring_cases = 0
        self._ring_bytes = 0

    def run_one(self, name: str, inputs: list[Any], attrs: list[Any], out_specs: list[tuple[tuple[int, ...], Any]]) -> list[np.ndarray]:
        def mk_tensor(a: Any) -> tuple[P, P | None]:
            if a is None:
                return P(), None
            a = np.ascontiguousarray(a)
            req = max(a.nbytes, 32)
            padded = req + GUARD_PAD_BYTES
            ptr = P()
            self.check(self.acl.aclrtMalloc(C.byref(ptr), padded, 0))
            self._ring_mem.append(ptr)
            self._ring_bytes += padded
            if a.nbytes:
                self.check(self.acl.aclrtMemcpy(ptr, req, P(a.ctypes.data), a.nbytes, 1))
            dims = (I64 * a.ndim)(*a.shape)
            strides = (I64 * a.ndim)(*[s // a.itemsize for s in a.strides])
            t = self.base.aclCreateTensor(dims, a.ndim, ACL_TYPES[a.dtype.name], strides, 0, 2, dims, a.ndim, ptr)
            if not t:
                raise RuntimeError("aclCreateTensor failed")
            self._ring_tensors.append(t)
            return P(t), ptr

        def mk_arg(value: Any) -> P:
            if isinstance(value, list):
                ts = [mk_tensor(a)[0] for a in value]
                arr = (P * len(ts))(*ts)
                p = self.base.aclCreateTensorList(arr, len(ts))
                self._ring_lists.append(p)
                return P(p)
            return mk_tensor(value)[0]

        ins = [mk_arg(v) for v in inputs]
        at: list[Any] = []
        for a in attrs:
            if isinstance(a, bool):
                at.append(C.c_bool(a))
            elif isinstance(a, int):
                at.append(I64(a))
            elif isinstance(a, float):
                at.append(C.c_double(a))
            elif isinstance(a, str):
                at.append(C.c_char_p(a.encode()))
            else:
                arr = (I64 * len(a))(*a)
                p = self.base.aclCreateIntArray(arr, len(a))
                self._ring_intarrays.append(p)
                at.append(P(p))

        mk_out = _poison_array if self.poison_outputs else _zero_array
        outarrays = [mk_out(shape, dtype) for shape, dtype in out_specs]
        outs: list[P] = []
        outmem: list[P] = []
        for a in outarrays:
            t, ptr = mk_tensor(a)
            outs.append(t)
            assert ptr is not None
            outmem.append(ptr)

        ws_size = U64()
        executor = P()
        fn = getattr(self.lib, "aclnn" + name + "GetWorkspaceSize")
        args = ins + at + outs + [C.byref(ws_size), C.byref(executor)]
        fn.argtypes = [type(x) for x in ins + at + outs] + [C.POINTER(U64), C.POINTER(P)]
        self.check(fn(*args))

        workspace = P()
        if ws_size.value:
            ws_pad = int(ws_size.value) + GUARD_PAD_BYTES
            self.check(self.acl.aclrtMalloc(C.byref(workspace), ws_pad, 0))
            self._ring_mem.append(workspace)
            self._ring_bytes += ws_pad

        execute = getattr(self.lib, "aclnn" + name)
        execute.argtypes = [P, U64, P, P]
        self.check(execute(workspace, ws_size, executor, self.stream))
        self.check(self.acl.aclrtSynchronizeStream(self.stream))

        for a, ptr in zip(outarrays, outmem):
            if a.nbytes:
                self.check(self.acl.aclrtMemcpy(P(a.ctypes.data), a.nbytes, ptr, a.nbytes, 2))

        self._ring_cases += 1
        self._flush_ring(force=False)
        return outarrays

    def close(self) -> None:
        self._flush_ring(force=True)
        self.acl.aclrtDestroyStream(self.stream)
        self.acl.aclrtResetDevice(0)
        self.acl.aclFinalize()


def cmd_replay(args: argparse.Namespace) -> int:
    cache_dir = pathlib.Path(args.cache)
    out_dir = pathlib.Path(args.out)
    repo = pathlib.Path(args.repo)
    meta = json.loads((cache_dir / "DONE").read_text(encoding="utf-8"))
    count = int(meta["count"])
    vendor = str(meta["vendor"])
    poison = bool(args.poison or meta.get("poison", False))

    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    replayer = _FastNpuReplayer(repo, vendor, poison_outputs=poison)
    run_errors: list[dict[str, Any]] = []
    try:
        for idx in range(count):
            print(f"OPSB_REPLAY_CASE {idx + 1}/{count}", flush=True)
            rec = pickle.loads((cache_dir / f"case_{idx:04d}_in.pkl").read_bytes())
            try:
                outs = replayer.run_one(rec["name"], rec["inputs"], rec["attrs"], rec["out_specs"])
                (out_dir / f"out_{idx:04d}.pkl").write_bytes(pickle.dumps(outs, protocol=pickle.HIGHEST_PROTOCOL))
            except Exception as exc:
                run_errors.append({"idx": idx, "tag": rec.get("tag") or rec["name"], "error": str(exc)})
                (out_dir / f"out_{idx:04d}.err").write_text(traceback.format_exc(), encoding="utf-8")
                if not rec.get("soft", False):
                    break
    finally:
        replayer.close()
        print("OPSB_REPLAY_DONE", flush=True)

    elapsed = round(time.monotonic() - t0, 3)
    (out_dir / "replay_meta.json").write_text(
        json.dumps({"count": count, "errors": run_errors, "replay_sec": elapsed}),
        encoding="utf-8",
    )
    stats_file = os.environ.get("OPSB_ARENA_STATS_FILE")
    if stats_file:
        try:
            with open(stats_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"pid": os.getpid(), "cases_inproc": count, "cases_fallback": 0, "acl_wall_sec": elapsed}) + "\n")
        except Exception:
            pass
    print(f"[PHASE-B] Replayed {count} cached extended cases on NPU in {elapsed:.3f}s (verify deferred to Phase C)", flush=True)
    if run_errors and not meta.get("soft_mode", False):
        for e in run_errors[:5]:
            print(f"EXTENDED_FAILED runtime idx={e['idx']} tag={e['tag']} err={e['error']}", flush=True)
        return 1
    return 0


def _measure_ln(actual: np.ndarray, want: np.ndarray, tol: float) -> tuple[bool, int, int, float]:
    a = actual.astype(np.float64)
    b = want.astype(np.float64)
    d = np.abs(a - b)
    bad = int(np.count_nonzero(d > tol * (1.0 + np.abs(b))))
    return bad <= tol * b.size, bad, b.size, float(d.max()) if d.size else 0.0


def _tol_for(dtype_name: str) -> float:
    if dtype_name == "float32":
        return 1e-4
    if dtype_name == "float16":
        return 1e-3
    if dtype_name == "bfloat16":
        return 1e-2
    return 0.0


def _verify_one_standard(cache_dir: pathlib.Path, out_dir: pathlib.Path, idx: int) -> tuple[int, bool, str]:
    err_f = out_dir / f"out_{idx:04d}.err"
    if err_f.is_file():
        return idx, False, err_f.read_text(encoding="utf-8", errors="replace").splitlines()[-1]
    out_f = out_dir / f"out_{idx:04d}.pkl"
    if not out_f.is_file():
        return idx, False, "missing output file (replay aborted earlier)"
    actuals = pickle.loads(out_f.read_bytes())
    exp_rec = pickle.loads((cache_dir / f"case_{idx:04d}_exp.pkl").read_bytes())
    expected = exp_rec["expected"]
    name = exp_rec.get("name", "op")
    for i, (actual, want) in enumerate(zip(actuals, expected)):
        tol = _tol_for(want.dtype.name)
        a = actual.astype(np.float64)
        w = want.astype(np.float64)
        if not np.allclose(a, w, atol=tol, rtol=tol, equal_nan=True):
            bad_mask = ~np.isclose(a, w, atol=tol, rtol=tol, equal_nan=True)
            nbad = int(np.count_nonzero(bad_mask))
            max_err = float(np.max(np.abs(a - w))) if a.size else 0.0
            return idx, False, f"{name} case#{idx} out{i} shape={want.shape} dtype={want.dtype.name} bad={nbad}/{want.size} max={max_err:.3e} tol={tol}"
    return idx, True, ""


def _verify_one_layernorm(cache_dir: pathlib.Path, out_dir: pathlib.Path, idx: int) -> dict[str, Any]:
    exp_rec = pickle.loads((cache_dir / f"case_{idx:04d}_exp.pkl").read_bytes())
    tag = exp_rec.get("tag") or f"case_{idx}"
    err_f = out_dir / f"out_{idx:04d}.err"
    if err_f.is_file():
        return {"idx": idx, "tag": tag, "ok": False, "digests": (), "msg": err_f.read_text(errors="replace")}
    actuals = pickle.loads((out_dir / f"out_{idx:04d}.pkl").read_bytes())
    expected = exp_rec["expected"]
    ok_all = True
    lines: list[str] = []
    for i, (actual, want) in enumerate(zip(actuals, expected)):
        tol = _tol_for(want.dtype.name)
        ok, bad, size, far = _measure_ln(actual, want, tol)
        if not ok:
            ok_all = False
            lines.append(f"  FAIL {tag} out{i} bad@tol={bad}/{size} allowed={tol*size:.1f} max={far:.3e}")
    digests = tuple(hashlib.sha256(np.ascontiguousarray(a).tobytes()).hexdigest() for a in actuals)
    return {"idx": idx, "tag": tag, "ok": ok_all, "digests": digests, "msg": "\n".join(lines)}


def cmd_verify(args: argparse.Namespace) -> int:
    cache_dir = pathlib.Path(args.cache)
    out_dir = pathlib.Path(args.out)
    title = args.title
    meta = json.loads((cache_dir / "DONE").read_text(encoding="utf-8"))
    count = int(meta["count"])
    reported_cases = int(meta.get("reported_cases") or count)

    try:
        t0 = time.monotonic()
        if title == "LayerNormV3":
            with ThreadPoolExecutor(max_workers=8) as ex:
                results = list(ex.map(lambda i: _verify_one_layernorm(cache_dir, out_dir, i), range(count)))
            failures = [r for r in results if not r["ok"]]
            # Determinism check over `det-<dtype>-<shape>-<0|1|2>` groups
            det_groups: dict[str, list[tuple[str, ...]]] = {}
            for r in results:
                tag = r["tag"]
                if tag.startswith("det-") and tag.rsplit("-", 1)[-1].isdigit():
                    grp = tag.rsplit("-", 1)[0]
                    det_groups.setdefault(grp, []).append(r["digests"])
            det_ok = True
            for grp, digs in det_groups.items():
                if len(set(digs)) != 1:
                    det_ok = False
                    print(f"  NONDET {grp} {digs}", flush=True)
            print("DETERMINISTIC" if det_ok else "NONDETERMINISTIC", flush=True)
            print(f"EXTENDED_CASES={reported_cases} FAILED={len(failures)}", flush=True)
            for r in failures[:15]:
                print(r["msg"] or f"  FAILED_CASE {r['tag']}", flush=True)
            bad = bool(failures) or not det_ok
            dt = time.monotonic() - t0
            if not bad:
                print(f"EXTENDED_PASSED {reported_cases} cases (Phase-C verify {dt:.2f}s)", flush=True)
                print("EXTENDED_RC=0", flush=True)
                return 0
            print(f"EXTENDED_FAILED {len(failures)}/{reported_cases} cases", flush=True)
            print("EXTENDED_RC=1", flush=True)
            return 1

        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(lambda i: _verify_one_standard(cache_dir, out_dir, i), range(count)))
        failures = [r for r in results if not r[1]]
        dt = time.monotonic() - t0
        if not failures:
            print(f"EXTENDED_PASSED {reported_cases} cases (Phase-C verify {dt:.2f}s)", flush=True)
            print("EXTENDED_RC=0", flush=True)
            return 0
        for _, _, msg in failures[:15]:
            print(f"  FAIL: {msg}", flush=True)
        print(f"EXTENDED_FAILED {len(failures)}/{reported_cases} cases", flush=True)
        print("EXTENDED_RC=1", flush=True)
        return 1
    finally:
        if not args.keep:
            shutil.rmtree(out_dir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_rep = sub.add_parser("replay")
    p_rep.add_argument("--repo", required=True)
    p_rep.add_argument("--cache", required=True)
    p_rep.add_argument("--out", required=True)
    p_rep.add_argument("--poison", action="store_true")
    p_rep.set_defaults(func=cmd_replay)

    p_ver = sub.add_parser("verify")
    p_ver.add_argument("--cache", required=True)
    p_ver.add_argument("--out", required=True)
    p_ver.add_argument("--title", required=True)
    p_ver.add_argument("--keep", action="store_true")
    p_ver.set_defaults(func=cmd_verify)

    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
