#!/usr/bin/env python3
# OPSB_STREAM_PROBE_V5
"""Persistent, strictly read-only DCMI ctypes + /proc NPU telemetry stream probe.

Deployed to `~/.opsb/npu_stream_probe.py` on the remote Accel NPU host.
IMPORTANT:
- Never acquires, opens for write, or calls flock/fcntl on any `.lock` file.
- Connects directly to `<driver-root>/driver/lib64/driver/<dcmi-lib>` via ctypes.
- Falls back to 3s-cached `npu-smi` automatically if DCMI fails or mismatches.
- Keeps `/proc/stat` delta, `telemetry_ring` history, and `die_ledger.jsonl` cache in memory.
- Emits single-line compact NDJSON frames in `--stream` mode (`< 5 ms` per frame).
"""
from __future__ import annotations

import argparse
import ctypes
import datetime
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Any

if __package__:
    from .npu_probe import estimate_waits, host_case_tasks, unregistered_holder
else:
    from npu_probe import estimate_waits, host_case_tasks, unregistered_holder

# --------------------------------------------------------------------------- #
# Deployment facts (SoC, toolchain, driver and remote work roots)
# --------------------------------------------------------------------------- #
# Neutral fallbacks; the real deployment facts live in ~/.parhelion/config.json
# ("device").  The resolver is deliberately self-contained and stdlib-only: this
# file is deployed to the compute host as one script, where the CLI package is
# not importable, so it must never import `opsb_core`.
_DEVICE_DEFAULTS: dict[str, Any] = {
    "soc": "soc-a",
    "soc_legacy": "soc-b",
    "build_target": "soc-a",
    "build_target_legacy": "soc-b",
    "arch": "arch-v1",
    "toolchain_env": "",
    "toolchain_root": "",
    "toolchain_dir": "",
    "sdk_glob": "sdk-*",
    "driver_root": "",
    "driver_lib_dirs": [],
    "driver_libs": {},
    "dcmi_lib": "",
    "driver_version_info": "",
    "toolkit_info": "toolkit_install.info",
    "toolkit_name": "toolkit",
    "lib_runtime": "libruntime.so",
    "visible_devices_env": "VISIBLE_DEVICES",
    "extra_ops_env": "EXTRA_OPS_PATH",
    "toolchain_home_env": "TOOLCHAIN_HOME",
    "tpl_env": "TPL_SEL",
    "tpl_prefix": "TPL_",
    "profiler_bin": "profiler",
    "profiler_acl_flag": "--acl",
    "judge_base": "https://judge.invalid",
    "judge_bot": "judge-bot",
    "work_root": "/srv/work",
    "work_prefix": "bench",
    "repo_url_template": "https://git.invalid/{prefix}-{slug}.git",
    "contest_org": "",
    "contest_track": "",
}


def _device_settings() -> dict[str, Any]:
    """Deployment facts for this host (config file < `OPSB_DEVICE_<KEY>` env override)."""
    if os.environ.get("OPSB_DEVICE_ISOLATED") == "1":
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in _DEVICE_DEFAULTS.items()}
    merged: dict[str, Any] = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _DEVICE_DEFAULTS.items()}
    try:
        raw = json.loads((pathlib.Path.home() / ".parhelion" / "config.json").read_text(encoding="utf-8"))
        sect = raw.get("device") if isinstance(raw, dict) else None
        if isinstance(sect, dict):
            for key, value in sect.items():
                if key in merged and value not in (None, ""):
                    merged[key] = value
    except Exception:
        pass
    for key in list(merged):
        raw_env = os.environ.get("OPSB_DEVICE_" + key.upper())
        if not raw_env:
            continue
        default = merged.get(key)
        if isinstance(default, (dict, list)):
            # Structured settings (driver library maps, directory lists) travel as JSON
            # so a remote standalone copy can be configured without a local config file.
            try:
                parsed = json.loads(raw_env)
            except Exception:
                continue
            if isinstance(parsed, type(default)):
                merged[key] = parsed
            continue
        merged[key] = raw_env
    return merged


DEVICE = _device_settings()


def device_text(key: str) -> str:
    """One deployment fact as text (empty string when unset or not textual)."""
    value = DEVICE.get(key, "")
    return value if isinstance(value, str) else ""


KNOWN_PROBLEMS = [
    "ConcatD",
    "ConfusionTransposeD",
    "CTCLossV2",
    "LayerNormV3",
    "MultilabelMarginLoss",
    "PadV3Grad",
    "PReluGradUpdate",
    "QuantUpdateScatter",
    "StridedSliceGrad",
    "SyncBatchNormGatherStats",
]

VENDOR_TO_PROBLEM = {
    "concat_d": "ConcatD",
    "confusion_transpose": "ConfusionTransposeD",
    "ctc_loss_v2": "CTCLossV2",
    "layer_norm_v3": "LayerNormV3",
    "multilabel_margin_loss": "MultilabelMarginLoss",
    "pad_v3_grad": "PadV3Grad",
    "prelu_grad_update": "PReluGradUpdate",
    "quant_update_scatter": "QuantUpdateScatter",
    "strided_slice_grad": "StridedSliceGrad",
    "sync_batch_norm_gather_stats": "SyncBatchNormGatherStats",
}

SHA_RE = re.compile(r"\b([0-9a-f]{40})\b")

# DCMI utilization_rate input_type constants (from dcmi_interface_api.h)
DCMI_UTILIZATION_RATE_AICORE = 2
DCMI_UTILIZATION_RATE_CTRLCPU = 4
DCMI_UTILIZATION_RATE_HBM_BANDWIDTH = 10
DCMI_UTILIZATION_RATE_VECTORCORE = 12

#: Driver library file names, configured per deployment (empty entries are skipped, never faked).
_DRIVER_LIBS: dict[str, str] = DEVICE.get("driver_libs") if isinstance(DEVICE.get("driver_libs"), dict) else {}


def _driver_lib_path(subdir: str, key: str) -> str:
    """`<driver-root>/driver/lib64/<subdir>/<lib>` for one configured library, else `""`."""
    name = _DRIVER_LIBS.get(key) or ""
    if not name:
        return ""
    return f"{device_text('driver_root')}/driver/lib64/{subdir}/{name}"


# Loaded RTLD_GLOBAL in this order: the base HAL must come before the HAL that layers on it.
DCMI_PRELOAD_LIBS = [
    lib
    for lib in (
        _driver_lib_path("common", "sec"),
        _driver_lib_path("common", "mpa"),
        _driver_lib_path("driver", "base_hal"),
        _driver_lib_path("driver", "hal"),
        _driver_lib_path("driver", "dsmi"),
    )
    if lib
]
# The DCMI library itself: an explicit `dcmi_lib` wins, else the `dcmi` driver-lib entry.
_DCMI_LIB_NAME = device_text("dcmi_lib") or str(_DRIVER_LIBS.get("dcmi") or "")
DCMI_LIB_PATH = f"{device_text('driver_root')}/driver/lib64/driver/{_DCMI_LIB_NAME}" if _DCMI_LIB_NAME else ""
DRIVER_VERSION_INFO = pathlib.Path(f"{device_text('driver_root')}/driver/version.info")


class _DcmiChipInfo(ctypes.Structure):
    _fields_ = [
        ("chip_type", ctypes.c_char * 32),
        ("chip_name", ctypes.c_char * 32),
        ("chip_ver", ctypes.c_char * 32),
        ("aicore_cnt", ctypes.c_uint),
    ]


class _DcmiChipInfoV2(ctypes.Structure):
    _fields_ = [
        ("chip_type", ctypes.c_char * 32),
        ("chip_name", ctypes.c_char * 32),
        ("chip_ver", ctypes.c_char * 32),
        ("aicore_cnt", ctypes.c_uint),
        ("npu_name", ctypes.c_char * 32),
    ]


class _DcmiHbmInfo(ctypes.Structure):
    _fields_ = [
        ("memory_size", ctypes.c_ulonglong),
        ("freq", ctypes.c_uint),
        ("memory_usage", ctypes.c_ulonglong),
        ("temp", ctypes.c_int),
        ("bandwith_util_rate", ctypes.c_uint),
    ]


class _DcmiPcieInfoAll(ctypes.Structure):
    _fields_ = [
        ("venderid", ctypes.c_uint),
        ("subvenderid", ctypes.c_uint),
        ("deviceid", ctypes.c_uint),
        ("subdeviceid", ctypes.c_uint),
        ("domain", ctypes.c_int),
        ("bdf_busid", ctypes.c_uint),
        ("bdf_deviceid", ctypes.c_uint),
        ("bdf_funcid", ctypes.c_uint),
        ("reserve", ctypes.c_ubyte * 32),
    ]


class _DcmiProcMemInfo(ctypes.Structure):
    _fields_ = [
        ("proc_id", ctypes.c_int),
        ("proc_mem_usage", ctypes.c_ulong),
    ]


def _read_driver_version() -> str:
    try:
        if DRIVER_VERSION_INFO.is_file():
            for line in DRIVER_VERSION_INFO.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("Version="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def _read_proc_comm(pid: int) -> str:
    try:
        comm = pathlib.Path(f"/proc/{pid}/comm").read_text(encoding="utf-8", errors="replace").strip()
        if comm:
            return comm
    except Exception:
        pass
    try:
        raw = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
        arg0 = raw.split(b"\x00", 1)[0].decode("utf-8", "replace")
        if arg0:
            return os.path.basename(arg0)
    except Exception:
        pass
    return "process"


class DcmiProbe:
    """In-process ctypes DCMI telemetry reader with static startup metadata cache."""

    def __init__(self) -> None:
        self.available = False
        self.error: str | None = None
        self.lib: Any = None
        self.smi_version: str = _read_driver_version()
        self.targets: list[dict[str, Any]] = []
        self._slow_cache: dict[tuple[int, int], dict[str, Any]] = {}
        self._slow_lock = threading.Lock()
        self._consec_failures = 0

    def init(self) -> bool:
        try:
            for dep in DCMI_PRELOAD_LIBS:
                if os.path.isfile(dep):
                    try:
                        ctypes.CDLL(dep, mode=ctypes.RTLD_GLOBAL)
                    except OSError:
                        pass
            if not os.path.isfile(DCMI_LIB_PATH):
                self.error = f"missing {DCMI_LIB_PATH}"
                return False
            lib = ctypes.CDLL(DCMI_LIB_PATH)
            self._configure_signatures(lib)
            rc = lib.dcmi_init()
            if rc != 0:
                self.error = f"dcmi_init failed rc={rc}"
                return False

            card_num = ctypes.c_int(0)
            card_list = (ctypes.c_int * 64)()
            rc_c = lib.dcmi_get_card_list(ctypes.byref(card_num), card_list, 64)
            if rc_c != 0 or card_num.value <= 0:
                self.error = f"dcmi_get_card_list failed rc={rc_c} n={card_num.value}"
                return False

            if not self.smi_version:
                ver_buf = ctypes.create_string_buffer(256)
                if lib.dcmi_get_driver_version(ver_buf, 256) == 0:
                    self.smi_version = ver_buf.value.decode("utf-8", "replace").strip()

            targets: list[dict[str, Any]] = []
            for idx in range(min(64, card_num.value)):
                cid = int(card_list[idx])
                dmax, mcu, cpu = ctypes.c_int(1), ctypes.c_int(0), ctypes.c_int(0)
                rc_d = lib.dcmi_get_device_id_in_card(cid, ctypes.byref(dmax), ctypes.byref(mcu), ctypes.byref(cpu))
                dev_count = max(1, int(dmax.value)) if rc_d == 0 else 1
                for did in range(dev_count):
                    soc_name = self._read_soc_name(lib, cid, did)
                    bus_id = self._read_bus_id(lib, cid, did)
                    targets.append({"card_id": cid, "device_id": did, "soc_name": soc_name, "bus_id": bus_id})

            if not targets:
                self.error = "no dcmi devices discovered"
                return False

            self.lib = lib
            self.targets = targets
            self.available = True
            self.error = None
            return True
        except Exception as exc:
            self.error = str(exc)[:240]
            self.available = False
            return False

    @staticmethod
    def _configure_signatures(lib: Any) -> None:
        lib.dcmi_init.argtypes = []
        lib.dcmi_init.restype = ctypes.c_int
        lib.dcmi_get_card_list.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.c_int]
        lib.dcmi_get_card_list.restype = ctypes.c_int
        lib.dcmi_get_device_id_in_card.argtypes = [
            ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
        ]
        lib.dcmi_get_device_id_in_card.restype = ctypes.c_int
        lib.dcmi_get_device_chip_info.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(_DcmiChipInfo)]
        lib.dcmi_get_device_chip_info.restype = ctypes.c_int
        lib.dcmi_get_device_chip_info_v2.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(_DcmiChipInfoV2)]
        lib.dcmi_get_device_chip_info_v2.restype = ctypes.c_int
        lib.dcmi_get_device_pcie_info_v2.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(_DcmiPcieInfoAll)]
        lib.dcmi_get_device_pcie_info_v2.restype = ctypes.c_int
        lib.dcmi_get_device_power_info.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        lib.dcmi_get_device_power_info.restype = ctypes.c_int
        lib.dcmi_get_device_temperature.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        lib.dcmi_get_device_temperature.restype = ctypes.c_int
        lib.dcmi_get_device_health.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]
        lib.dcmi_get_device_health.restype = ctypes.c_int
        lib.dcmi_get_device_hbm_info.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(_DcmiHbmInfo)]
        lib.dcmi_get_device_hbm_info.restype = ctypes.c_int
        lib.dcmi_get_device_utilization_rate.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_uint),
        ]
        lib.dcmi_get_device_utilization_rate.restype = ctypes.c_int
        lib.dcmi_get_device_resource_info.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.POINTER(_DcmiProcMemInfo), ctypes.POINTER(ctypes.c_int),
        ]
        lib.dcmi_get_device_resource_info.restype = ctypes.c_int
        lib.dcmi_get_driver_version.argtypes = [ctypes.c_char_p, ctypes.c_uint]
        lib.dcmi_get_driver_version.restype = ctypes.c_int

    @staticmethod
    def _read_soc_name(lib: Any, cid: int, did: int) -> str:
        ci2 = _DcmiChipInfoV2()
        if lib.dcmi_get_device_chip_info_v2(cid, did, ctypes.byref(ci2)) == 0:
            ct = ci2.chip_type.decode("utf-8", "replace").strip("\x00 ").strip()
            npu_n = ci2.npu_name.decode("utf-8", "replace").strip("\x00 ").strip()
            cn = ci2.chip_name.decode("utf-8", "replace").strip("\x00 ").strip()
            raw = npu_n or cn
            if raw:
                return raw if raw.lower().startswith("accel") else f"{ct or 'Accel'}{raw}"
        ci = _DcmiChipInfo()
        if lib.dcmi_get_device_chip_info(cid, did, ctypes.byref(ci)) == 0:
            ct = ci.chip_type.decode("utf-8", "replace").strip("\x00 ").strip()
            cn = ci.chip_name.decode("utf-8", "replace").strip("\x00 ").strip()
            if cn:
                return cn if cn.lower().startswith("accel") else f"{ct or 'Accel'}{cn}"
        return device_text("soc")

    @staticmethod
    def _read_bus_id(lib: Any, cid: int, did: int) -> str:
        pcie = _DcmiPcieInfoAll()
        if lib.dcmi_get_device_pcie_info_v2(cid, did, ctypes.byref(pcie)) == 0:
            return f"{pcie.domain:04X}:{pcie.bdf_busid:02X}:{pcie.bdf_deviceid:02X}.{pcie.bdf_funcid:X}"
        return ""

    def refresh_slow_pmu(self) -> bool:
        """Query kernel PMU-window utilization (aicore/aivector/ctrlcpu) and HBM info.

        Called synchronously in `--once` mode and on a dedicated background thread in `--stream` mode
        because Accel driver utilization ioctls block ~58ms each for PMU hardware sampling.
        """
        if not self.available or self.lib is None:
            return False
        updates: dict[tuple[int, int], dict[str, Any]] = {}
        ok_any = False
        for t in self.targets:
            cid, did = t["card_id"], t["device_id"]
            entry: dict[str, Any] = {}
            hbm = _DcmiHbmInfo()
            if self.lib.dcmi_get_device_hbm_info(cid, did, ctypes.byref(hbm)) == 0:
                entry["hbm_total_mib"] = int(hbm.memory_size) or 65536
                entry["hbm_used_mib"] = int(hbm.memory_usage)
                entry["hbm_bw_pct"] = int(hbm.bandwith_util_rate)
                entry["hbm_temp_c"] = int(hbm.temp)
                ok_any = True

            temp_raw = ctypes.c_int(0)
            if self.lib.dcmi_get_device_temperature(cid, did, ctypes.byref(temp_raw)) == 0:
                entry["temp_c"] = int(temp_raw.value)
                ok_any = True

            for key, code in (
                ("aicore_pct", DCMI_UTILIZATION_RATE_AICORE),
                ("aivector_pct", DCMI_UTILIZATION_RATE_VECTORCORE),
                ("ctrlcpu_pct", DCMI_UTILIZATION_RATE_CTRLCPU),
            ):
                u = ctypes.c_uint(0)
                if self.lib.dcmi_get_device_utilization_rate(cid, did, code, ctypes.byref(u)) == 0:
                    entry[key] = int(u.value)
                    ok_any = True

            if entry:
                updates[(cid, did)] = entry

        if updates:
            with self._slow_lock:
                for key, val in updates.items():
                    prev = self._slow_cache.get(key, {})
                    merged = {**prev, **val}
                    self._slow_cache[key] = merged
        return ok_any

    def sample_fast(self, *, sync_slow: bool = False) -> dict[str, Any] | None:
        """Read fast DCMI counters (power, temp, health, HBM BW, proc memory) in < 3.5 ms."""
        if not self.available or self.lib is None:
            return None
        if sync_slow:
            if not self.refresh_slow_pmu():
                self._consec_failures += 1
                return None

        with self._slow_lock:
            slow_snapshot = {k: dict(v) for k, v in self._slow_cache.items()}

        devices: list[dict[str, Any]] = []
        npu_processes: list[dict[str, Any]] = []
        health_map = {0: "OK", 1: "Warning", 2: "Error", 3: "Alarm"}
        core_ok = 0

        for t in self.targets:
            cid, did = t["card_id"], t["device_id"]
            slow = slow_snapshot.get((cid, did), {})

            pwr_raw = ctypes.c_int(0)
            rc_pwr = self.lib.dcmi_get_device_power_info(cid, did, ctypes.byref(pwr_raw))
            power_w = round(pwr_raw.value / 10.0, 1) if rc_pwr == 0 and pwr_raw.value >= 0 else None
            if rc_pwr == 0:
                core_ok += 1

            temp_c = slow.get("temp_c") or slow.get("hbm_temp_c")
            if temp_c is None:
                temp_raw = ctypes.c_int(0)
                rc_tmp = self.lib.dcmi_get_device_temperature(cid, did, ctypes.byref(temp_raw))
                if rc_tmp == 0:
                    temp_c = int(temp_raw.value)
                    core_ok += 1

            hlth_raw = ctypes.c_uint(0)
            rc_hlth = self.lib.dcmi_get_device_health(cid, did, ctypes.byref(hlth_raw))
            health = health_map.get(int(hlth_raw.value), "Unknown") if rc_hlth == 0 else "Unknown"
            if rc_hlth == 0:
                core_ok += 1

            bw_raw = ctypes.c_uint(0)
            # Reuse cached hbm_bw_pct from refresh_slow_pmu (hbm_info.bandwith_util_rate) to keep sample_fast < 2.5ms
            hbm_bw_pct = int(slow.get("hbm_bw_pct") or 0)
            if "hbm_bw_pct" not in slow:
                if (
                    self.lib.dcmi_get_device_utilization_rate(
                        cid, did, DCMI_UTILIZATION_RATE_HBM_BANDWIDTH, ctypes.byref(bw_raw)
                    )
                    == 0
                ):
                    hbm_bw_pct = int(bw_raw.value)

            hbm_used = int(slow.get("hbm_used_mib") or 0)
            hbm_total = int(slow.get("hbm_total_mib") or 65536)
            aicore_pct = int(slow.get("aicore_pct") or 0)
            aivector_pct = int(slow.get("aivector_pct") or 0)
            ctrlcpu_pct = int(slow.get("ctrlcpu_pct") or 0)

            devices.append(
                {
                    "npu_id": cid,
                    "chip_id": did,
                    "soc_name": t["soc_name"],
                    "health": health,
                    "bus_id": t["bus_id"],
                    "power_w": power_w,
                    "temp_c": temp_c,
                    "aicore_pct": aicore_pct,
                    "aivector_pct": aivector_pct,
                    "hbm_bw_pct": hbm_bw_pct,
                    "ctrlcpu_pct": ctrlcpu_pct,
                    "hbm_used_mib": hbm_used,
                    "hbm_total_mib": hbm_total,
                }
            )

            proc_buf = (_DcmiProcMemInfo * 64)()
            proc_num = ctypes.c_int(64)
            if self.lib.dcmi_get_device_resource_info(cid, did, proc_buf, ctypes.byref(proc_num)) == 0:
                cnt = min(64, max(0, int(proc_num.value)))
                for idx in range(cnt):
                    pid = int(proc_buf[idx].proc_id)
                    mem_mib = int(proc_buf[idx].proc_mem_usage // (1024 * 1024))
                    npu_processes.append(
                        {
                            "npu_chip": f"{cid} {did}",
                            "pid": pid,
                            "name": _read_proc_comm(pid),
                            "memory_mib": mem_mib,
                        }
                    )

        if core_ok == 0 or not devices:
            self._consec_failures += 1
            return None

        self._consec_failures = 0
        return {
            "available": True,
            "smi_version": self.smi_version,
            "devices": devices,
            "npu_processes": npu_processes,
            "error": None,
        }


def _parse_proc_stat() -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    try:
        with open("/proc/stat", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.startswith("cpu"):
                    if out:
                        break
                    continue
                parts = line.split()
                name = parts[0]
                nums = [int(x) for x in parts[1:] if x.isdigit()]
                if len(nums) >= 4:
                    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
                    out[name] = (idle, sum(nums))
    except Exception:
        pass
    return out


def read_memory() -> dict[str, Any]:
    info: dict[str, int] = {}
    wanted = ("MemTotal", "MemFree", "MemAvailable", "Buffers", "Cached", "SReclaimable")
    try:
        with open("/proc/meminfo", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                k = k.strip()
                if k in wanted:
                    num = v.strip().split()[0]
                    if num.isdigit():
                        info[k] = int(num)
                    if len(info) == len(wanted):
                        break
    except Exception:
        pass
    total_kib = info.get("MemTotal", 0)
    avail_kib = info.get("MemAvailable", info.get("MemFree", 0))
    cached_kib = info.get("Buffers", 0) + info.get("Cached", 0) + info.get("SReclaimable", 0)
    used_kib = max(0, total_kib - avail_kib)
    return {
        "total_mib": round(total_kib / 1024),
        "used_mib": round(used_kib / 1024),
        "available_mib": round(avail_kib / 1024),
        "total_gib": round(total_kib / (1024 * 1024), 1),
        "used_gib": round(used_kib / (1024 * 1024), 1),
        "cached_gib": round(cached_kib / (1024 * 1024), 1),
    }


def read_sdk_version() -> str:
    toolchain_dir = device_text("toolchain_dir")
    candidates = [
        pathlib.Path(f"{toolchain_dir}/latest/aarch64-linux/{device_text('toolkit_info')}"),
        pathlib.Path(f"{device_text('driver_root')}/{device_text('toolkit_name')}/latest/aarch64-linux/{device_text('toolkit_info')}"),
    ]
    for p in candidates:
        try:
            if p.is_file():
                for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("version="):
                        return line.split("=", 1)[1].strip()
        except Exception:
            continue
    return "9.1.0"


def read_npu_smi_fallback() -> dict[str, Any]:
    env = os.environ.copy()
    driver_root = device_text("driver_root")
    lib_paths = [
        f"{driver_root}/driver/lib64",
        f"{driver_root}/driver/lib64/common",
        f"{driver_root}/driver/lib64/driver",
        f"{device_text('toolchain_dir')}/latest/lib64",
    ]
    existing = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = ":".join(lib_paths + ([existing] if existing else []))

    result: dict[str, Any] = {
        "available": False,
        "smi_version": "",
        "devices": [],
        "npu_processes": [],
        "error": None,
    }
    id_cache = pathlib.Path.home() / ".opsb" / "npu_id_hint"
    hint_id = id_cache.read_text().strip() if id_cache.is_file() else "5"
    try:
        p_info = subprocess.Popen(["npu-smi", "info"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        p_usage = subprocess.Popen(
            ["npu-smi", "info", "-t", "usages", "-i", hint_id],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out, err = p_info.communicate(timeout=2.5)
        u_out, _ = p_usage.communicate(timeout=1.8)
        u_rc = p_usage.returncode
        if p_info.returncode != 0:
            result["error"] = (err or out).strip()[:240]
            return result
    except Exception as exc:
        result["error"] = str(exc)[:240]
        return result

    result["available"] = True
    lines = out.splitlines()
    for line in lines:
        m_ver = re.search(r"npu-smi\s+([\d.]+)", line)
        if m_ver:
            result["smi_version"] = m_ver.group(1)
            break

    i = 0
    while i < len(lines) - 1:
        l1 = lines[i].strip()
        l2 = lines[i + 1].strip()
        if l1.startswith("|") and l2.startswith("|") and "/" in l1 and "/" in l2:
            cols1 = [c.strip() for c in l1.strip("|").split("|")]
            cols2 = [c.strip() for c in l2.strip("|").split("|")]
            if len(cols1) >= 3 and len(cols2) >= 3:
                p1 = cols1[0].split()
                health = cols1[1]
                m1 = cols1[2].split()
                p2 = cols2[0].split()
                bus_id = cols2[1]
                m2 = cols2[2].split()
                if len(p1) >= 2 and p1[0].isdigit() and health in ("OK", "Warning", "Error", "Alarm"):
                    npu_id = int(p1[0])
                    raw_name = p1[1]
                    soc_name = raw_name if raw_name.lower().startswith("accel") else f"Accel{raw_name}"
                    power_w = float(m1[0]) if m1 and re.match(r"^[\d.]+$", m1[0]) else None
                    temp_c = int(float(m1[1])) if len(m1) > 1 and re.match(r"^[\d.]+$", m1[1]) else None
                    chip_id = int(p2[0]) if p2 and p2[0].isdigit() else 0
                    aicore_pct = int(float(m2[0])) if m2 and re.match(r"^[\d.]+$", m2[0]) else 0
                    slashes = re.findall(r"(\d+)\s*/\s*(\d+)", cols2[2])
                    hbm_used, hbm_total = (int(slashes[-1][0]), int(slashes[-1][1])) if slashes else (0, 65536)
                    result["devices"].append(
                        {
                            "npu_id": npu_id,
                            "chip_id": chip_id,
                            "soc_name": soc_name,
                            "health": health,
                            "bus_id": bus_id,
                            "power_w": power_w,
                            "temp_c": temp_c,
                            "aicore_pct": aicore_pct,
                            "aivector_pct": 0,
                            "hbm_bw_pct": 0,
                            "ctrlcpu_pct": 0,
                            "hbm_used_mib": hbm_used,
                            "hbm_total_mib": hbm_total,
                        }
                    )
                    i += 2
                    continue
        i += 1

    for dev in result["devices"]:
        try:
            dev_id_str = str(dev["npu_id"])
            if dev_id_str != hint_id:
                u_proc = subprocess.run(
                    ["npu-smi", "info", "-t", "usages", "-i", dev_id_str],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=1.5,
                )
                cur_u_out = u_proc.stdout if u_proc.returncode == 0 else ""
            else:
                cur_u_out = u_out if u_rc == 0 else ""
            for uline in cur_u_out.splitlines():
                if ":" not in uline:
                    continue
                k, v = [s.strip() for s in uline.split(":", 1)]
                if not v.isdigit():
                    continue
                ival = int(v)
                kl = k.lower()
                if "aivector usage rate" in kl:
                    dev["aivector_pct"] = ival
                elif "aicore usage rate" in kl:
                    dev["aicore_pct"] = max(dev["aicore_pct"], ival)
                elif "hbm bandwidth usage rate" in kl:
                    dev["hbm_bw_pct"] = ival
                elif "ctrlcpu usage rate" in kl:
                    dev["ctrlcpu_pct"] = ival
        except Exception:
            pass

    in_proc_table = False
    for line in lines:
        if "Process id" in line and "Process name" in line:
            in_proc_table = True
            continue
        if in_proc_table and line.strip().startswith("|"):
            if "No running processes" in line:
                break
            cols = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cols) >= 4 and cols[1].isdigit():
                result["npu_processes"].append(
                    {
                        "npu_chip": cols[0],
                        "pid": int(cols[1]),
                        "name": cols[2],
                        "memory_mib": int(cols[3]) if cols[3].isdigit() else 0,
                    }
                )
    return result


def _read_single_proc(pid: int, uptime_s: float, clk_tck: int) -> dict[str, Any] | None:
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as fh:
            st = fh.read()
        rp = st.rfind(")")
        if rp < 0:
            return None
        rest = st[rp + 2 :].split()
        if len(rest) <= 19:
            return None
        ppid = int(rest[1])
        starttime = int(rest[19])
        elapsed = max(0, int(uptime_s - starttime / max(1, clk_tck)))
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmd = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        if not cmd:
            return None
        return {"pid": pid, "ppid": ppid, "elapsed_sec": elapsed, "cmd": cmd}
    except Exception:
        return None


def read_processes_procfs() -> dict[int, dict[str, Any]]:
    """Scan `/proc/[pid]/{stat,cmdline}` directly without spawning `ps`."""
    procs: dict[int, dict[str, Any]] = {}
    try:
        uptime_s = float(pathlib.Path("/proc/uptime").read_text().split()[0])
        clk_tck = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            rec = _read_single_proc(pid, uptime_s, clk_tck)
            if rec is not None:
                procs[pid] = rec
    except Exception:
        pass
    return procs


_LOCK_INODE_CACHE: tuple[float, dict[tuple[int, int], str], dict[int, str]] | None = None


def _known_lock_inodes() -> tuple[dict[tuple[int, int], str], dict[int, str]]:
    global _LOCK_INODE_CACHE
    now = time.monotonic()
    if _LOCK_INODE_CACHE is not None and (now - _LOCK_INODE_CACHE[0]) < 30.0 and _LOCK_INODE_CACHE[2]:
        return _LOCK_INODE_CACHE[1], _LOCK_INODE_CACHE[2]
    by_dev_ino: dict[tuple[int, int], str] = {}
    by_ino: dict[int, str] = {}
    work_root = pathlib.Path(device_text("work_root"))
    work_prefix = device_text("work_prefix")
    candidates = [
        work_root / f"{work_prefix}-device0.lock",
        work_root / f"{work_prefix}-device1.lock",
        work_root / f"{work_prefix}-validation.lock",
    ]
    for p in candidates:
        try:
            st = os.stat(p)
            p_str = str(p)
            by_dev_ino[(st.st_dev, st.st_ino)] = p_str
            by_ino[st.st_ino] = p_str
        except OSError:
            continue
    _LOCK_INODE_CACHE = (now, by_dev_ino, by_ino)
    return by_dev_ino, by_ino


def read_locks_procfs() -> list[dict[str, Any]]:
    """Read `/proc/locks` strictly read-only (< 0.1ms, never calls flock/lslocks)."""
    locks: list[dict[str, Any]] = []
    by_dev_ino, by_ino = _known_lock_inodes()
    if not by_ino:
        return locks
    try:
        with open("/proc/locks", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                tokens = line.split()
                if len(tokens) < 7:
                    continue
                is_waiting = tokens[1] == "->"
                off = 1 if is_waiting else 0
                if len(tokens) < 7 + off:
                    continue
                lock_type = tokens[1 + off]
                mode = tokens[3 + off]
                pid_s = tokens[4 + off]
                dev_ino_s = tokens[5 + off]
                if not pid_s.isdigit():
                    continue
                parts = dev_ino_s.split(":")
                if len(parts) != 3 or not parts[2].isdigit():
                    continue
                ino = int(parts[2])
                matched_path: str | None = None
                try:
                    dev = os.makedev(int(parts[0], 16), int(parts[1], 16))
                    matched_path = by_dev_ino.get((dev, ino))
                except ValueError:
                    continue
                if matched_path is None:
                    continue
                locks.append(
                    {
                        "pid": int(pid_s),
                        "command": "",
                        "type": lock_type,
                        "mode": f"{mode}{'*' if is_waiting else ''}",
                        "waiting": is_waiting,
                        "path": matched_path,
                    }
                )
    except Exception:
        pass
    return locks


def get_descendants(root_pid: int, procs: dict[int, dict[str, Any]]) -> list[int]:
    children_map: dict[int, list[int]] = {}
    for pid, p in procs.items():
        children_map.setdefault(p["ppid"], []).append(pid)
    result: list[int] = []
    stack = [root_pid]
    seen = set()
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        result.append(cur)
        stack.extend(children_map.get(cur, []))
    return result


def _is_driver_wrapper(cmd: str) -> bool:
    return "python3 -c" in cmd and (
        re.search(r"[\w.-]+-repo-[\w.-]+\.lock", cmd) is not None
        or "PHASE A:" in cmd
        or "CONCATD_FAST" in cmd
        or "BACKFILL_RESULT " in cmd
        or ".opsb_backfill_build.json" in cmd
    )


def _collect_op_ancestors(root_pid: int, procs: dict[int, dict[str, Any]], max_depth: int = 8) -> set[int]:
    """Walk up driver/repo ancestors without climbing into generic login shells or sshd."""
    ancestors: set[int] = set()
    cur = procs.get(root_pid, {}).get("ppid", 0)
    for _ in range(max_depth):
        if cur <= 1 or cur not in procs or cur in ancestors:
            break
        cmd = procs[cur].get("cmd", "")
        low = cmd.lower()
        if not (
            _is_driver_wrapper(cmd)
            or any(tok in low for tok in (device_text("work_prefix"), "npu-test", "validation", "build.sh", "backfill", "arena", "flock", "opsb"))
        ):
            break
        ancestors.add(cur)
        cur = procs[cur].get("ppid", 0)
    return ancestors


def _collect_tree_and_driver_peers(root_pid: int, procs: dict[int, dict[str, Any]]) -> set[int]:
    """Collect descendant tree plus owning driver wrapper subtree to avoid sibling PID duplicates."""
    related = set(get_descendants(root_pid, procs))
    ancestors = _collect_op_ancestors(root_pid, procs)
    related.update(ancestors)
    for anc in ancestors:
        if _is_driver_wrapper(procs.get(anc, {}).get("cmd", "")):
            related.update(get_descendants(anc, procs))
    return related


def infer_problem_and_sha(pids: list[int], procs: dict[int, dict[str, Any]]) -> tuple[str | None, str | None]:
    problem: str | None = None
    sha: str | None = None
    results_re = re.compile(rf"(?:{re.escape(device_text('work_prefix'))}|[\w.-]+)-results/[^/]+/([0-9a-f]{{7,40}})")
    for pid in pids:
        raw_cmd = procs.get(pid, {}).get("cmd", "")
        cmd = "" if _is_driver_wrapper(raw_cmd) else raw_cmd
        if cmd and not problem:
            for p_name in KNOWN_PROBLEMS:
                if p_name in cmd or p_name.lower() in cmd.lower():
                    problem = p_name
                    break
        if cmd and not problem:
            for vendor_key, p_name in VENDOR_TO_PROBLEM.items():
                if vendor_key in cmd.lower():
                    problem = p_name
                    break
        if cmd and not sha:
            m40 = SHA_RE.search(cmd)
            if m40:
                sha = m40.group(1)
            else:
                m_res = results_re.search(cmd)
                if m_res:
                    sha = m_res.group(1)
        if not _is_driver_wrapper(raw_cmd) and (not problem or not sha):
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
                if not problem:
                    for p_name in KNOWN_PROBLEMS:
                        if f"/{p_name}" in cwd:
                            problem = p_name
                            break
                if problem and not sha:
                    head_file = pathlib.Path(f"{device_text('work_root')}/{device_text('work_prefix')}/{problem}/.git/HEAD")
                    if head_file.is_file():
                        htxt = head_file.read_text().strip()
                        if re.match(r"^[0-9a-f]{40}$", htxt):
                            sha = htxt
            except Exception:
                pass
    return problem, sha


def classify_stage(
    pids: list[int],
    procs: dict[int, dict[str, Any]],
    holds_die: bool,
    substage_hint: str | None = None,
) -> tuple[str, str]:
    if not holds_die:
        return "WAITING-DIE", "Waiting for exclusive NPU die lock"
    non_driver = [
        procs.get(pid, {}).get("cmd", "")
        for pid in pids
        if not _is_driver_wrapper(procs.get(pid, {}).get("cmd", ""))
    ]
    joined = "\n".join(non_driver)
    low = joined.lower()
    if "profiler" in low or "kperf.py" in low or substage_hint == "profiler":
        m_case = re.search(r"--(?:ref-)?worker\s+(\S+)", joined)
        detail = f"profiler ({m_case.group(1)})" if m_case else "Hardware kernel profiling (profiler)"
        return "PROFILER", detail
    if any(k in low for k in ("build.sh", "cmake", "/ccec", "compiler", "kernelc", "ninja", "g++")):
        return "BUILD", "Compiling KernelC host/kernel package"
    if "--fast" in low or "fast-lane" in low or "fast_lane" in low:
        return "FAST-LANE", "Fast-lane smoke & public cases"
    if "stress" in low or "determinism" in low:
        return "STRESS", "Multi-run determinism & stress gate"
    if "extended.py" in low or "verify" in low or "run-npu.sh" in low or "pytest" in low:
        return "PRECISION", "Extended precision & boundary suite"
    return "PRECISION", "Active NPU validation task"


class StreamProbeSession:
    """Persistent telemetry session maintaining in-memory CPU deltas, ring buffer, and caches."""

    def __init__(self, stream_mode: bool = False, interval: float = 1.0) -> None:
        self.stream_mode = stream_mode
        self.interval = max(0.25, float(interval))
        self.opsb_home = pathlib.Path.home() / ".opsb"
        self.sdk_version = read_sdk_version()
        self.hostname = os.uname().nodename
        self.arch = os.uname().machine
        self.cores = os.cpu_count() or 1

        self.dcmi = DcmiProbe()
        self.dcmi_ok = self.dcmi.init()

        self._prev_cpu_stat: dict[str, tuple[int, int]] | None = _parse_proc_stat()
        self._history: list[dict[str, Any]] = self._seed_ring_history()

        # 3s cached npu-smi fallback
        self._smi_lock = threading.Lock()
        self._smi_cache: dict[str, Any] | None = None
        self._smi_cache_ts: float = 0.0

        # Background proc & lock table cache (refreshed post-tick in stream mode so main tick stays < 5ms)
        self._proc_lock = threading.Lock()
        self._procs_cache: dict[int, dict[str, Any]] = {}
        self._procs_ts: float = 0.0

        # Incremental die_ledger.jsonl mtime/size cache
        self._ledger_sig: tuple[int, int] | None = None
        self._ledger_rows: list[dict[str, Any]] = []

        self._stop_event = threading.Event()
        self._tick_done_event = threading.Event()
        self._bg_threads: list[threading.Thread] = []

    def _seed_ring_history(self) -> list[dict[str, Any]]:
        ring_file = self.opsb_home / "telemetry_ring.json"
        now_ts = time.time()
        try:
            if ring_file.is_file():
                loaded = json.loads(ring_file.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    return [p for p in loaded if now_ts - float(p.get("ts", 0)) <= 1800][-90:]
        except Exception:
            pass
        return []

    def start_background_collectors(self) -> None:
        if not self.stream_mode:
            return

        # Seed initial baseline synchronously before first emitted frame so frame #1 has real values.
        self._prev_cpu_stat = _parse_proc_stat()
        with self._proc_lock:
            self._procs_cache = read_processes_procfs()
            self._procs_ts = time.monotonic()

        if self.dcmi_ok:
            if not self.dcmi.refresh_slow_pmu():
                self.dcmi_ok = False
        else:
            time.sleep(0.05)

        if not self.dcmi_ok:
            self._refresh_smi_if_needed(force=True)

        def _slow_loop() -> None:
            while not self._stop_event.is_set():
                # Wait until the emitter finishes a frame so slow PMU ioctls & /proc/locks run in the idle gap
                # and never contend with `sample_fast()` on libdcmi's internal mutex or kernel lock lists.
                triggered = self._tick_done_event.wait(timeout=max(0.5, self.interval * 1.5))
                if self._stop_event.is_set():
                    break
                if triggered:
                    self._tick_done_event.clear()

                if self.dcmi_ok:
                    ok = self.dcmi.refresh_slow_pmu()
                    if not ok and self.dcmi._consec_failures >= 3:
                        self.dcmi_ok = False
                elif self.dcmi.init():
                    self.dcmi_ok = self.dcmi.refresh_slow_pmu()

                if not self.dcmi_ok:
                    self._refresh_smi_if_needed(force=False)

                if (time.monotonic() - self._procs_ts) >= 2.0:
                    procs = read_processes_procfs()
                    with self._proc_lock:
                        self._procs_cache = procs
                        self._procs_ts = time.monotonic()

        th = threading.Thread(target=_slow_loop, daemon=True, name="dcmi-slow-sampler")
        th.start()
        self._bg_threads.append(th)

    def notify_tick_done(self) -> None:
        self._tick_done_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._tick_done_event.set()
        for th in self._bg_threads:
            th.join(timeout=0.5)

    def _refresh_smi_if_needed(self, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        with self._smi_lock:
            if not force and self._smi_cache is not None and (now - self._smi_cache_ts) < 3.0:
                return self._smi_cache
        fresh = read_npu_smi_fallback()
        with self._smi_lock:
            self._smi_cache = fresh
            self._smi_cache_ts = time.monotonic()
            return fresh

    def _read_cpu_and_load_mem(self) -> dict[str, Any]:
        load_1, load_5, load_15 = 0.0, 0.0, 0.0
        running_procs, total_procs = 0, 0
        try:
            with open("/proc/loadavg", "r", encoding="utf-8", errors="replace") as fh:
                parts = fh.read().split()
            load_1, load_5, load_15 = float(parts[0]), float(parts[1]), float(parts[2])
            if "/" in parts[3]:
                r_s, t_s = parts[3].split("/", 1)
                running_procs, total_procs = int(r_s), int(t_s)
        except Exception:
            pass

        cur_stat = _parse_proc_stat()
        if self._prev_cpu_stat is None:
            self._prev_cpu_stat = cur_stat
            time.sleep(0.05)
            cur_stat = _parse_proc_stat()
        prev_stat = self._prev_cpu_stat
        self._prev_cpu_stat = cur_stat

        def calc_pct(key: str) -> float:
            if key not in cur_stat or key not in prev_stat:
                return 0.0
            c_idle, c_tot = cur_stat[key]
            p_idle, p_tot = prev_stat[key]
            dt = c_tot - p_tot
            if dt <= 0:
                return 0.0
            return round(max(0.0, min(100.0, (1.0 - (c_idle - p_idle) / dt) * 100.0)), 1)

        return {
            "cores": self.cores,
            "load_1m": round(load_1, 2),
            "load_5m": round(load_5, 2),
            "load_15m": round(load_15, 2),
            "running_threads": running_procs,
            "total_threads": total_procs,
            "usage_pct": calc_pct("cpu"),
            "per_core_pct": [calc_pct(f"cpu{i}") for i in range(self.cores)],
        }

    def _get_procs_snapshot(self, hint_pids: set[int] | None = None) -> dict[int, dict[str, Any]]:
        if not self.stream_mode:
            return read_processes_procfs()
        with self._proc_lock:
            live_procs = dict(self._procs_cache)
        if hint_pids:
            # Verify liveness for active lock/ticket/build PIDs and supplement any newly spawned ones
            missing: list[int] = []
            for pid in hint_pids:
                if pid <= 0:
                    continue
                if not os.path.exists(f"/proc/{pid}"):
                    live_procs.pop(pid, None)
                elif pid not in live_procs:
                    missing.append(pid)
            if missing:
                try:
                    uptime_s = float(pathlib.Path("/proc/uptime").read_text().split()[0])
                    clk_tck = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
                    for pid in missing:
                        rec = _read_single_proc(pid, uptime_s, clk_tck)
                        if rec is not None:
                            live_procs[pid] = rec
                except Exception:
                    pass
        return live_procs

    def _read_ledger_rows_cached(self, now_ts: float, window_sec: float = 600.0) -> list[dict[str, Any]]:
        ledger_file = self.opsb_home / "die_ledger.jsonl"
        try:
            st = os.stat(ledger_file)
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            self._ledger_sig = None
            self._ledger_rows = []
            return []

        if sig != self._ledger_sig:
            rows: list[dict[str, Any]] = []
            try:
                for line in ledger_file.read_text(encoding="utf-8", errors="replace").splitlines()[-120:]:
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        rows.append(json.loads(s))
                    except Exception:
                        continue
            except Exception:
                rows = []
            self._ledger_rows = rows
            self._ledger_sig = sig

        out: list[dict[str, Any]] = []
        for row in self._ledger_rows:
            end_ep = float(row.get("ended_epoch") or row.get("ts") or 0)
            if end_ep > 0 and (now_ts - end_ep) <= window_sec:
                out.append(row)
        return out

    def sample_frame(self) -> dict[str, Any]:
        t0 = time.monotonic()
        now_epoch = time.time()
        # Die-lock files are siblings of the work root, named after the work prefix.
        work_root = device_text("work_root")
        work_prefix = device_text("work_prefix")

        # 1. NPU hardware telemetry via DCMI (or 3s-cached npu-smi fallback)
        probe_backend = "dcmi"
        npu: dict[str, Any] | None = None
        if self.dcmi_ok:
            npu = self.dcmi.sample_fast(sync_slow=not self.stream_mode)
            if npu is None:
                self.dcmi_ok = False
        if npu is None:
            probe_backend = "npu-smi"
            npu = self._refresh_smi_if_needed(force=not self.stream_mode)

        # 2. Host CPU / Memory (in-memory delta, 0 disk writes)
        cpu = self._read_cpu_and_load_mem()
        mem = read_memory()

        die_session: dict[str, Any] = {}
        sess_file = self.opsb_home / "die_session.json"
        if sess_file.is_file():
            try:
                die_session = json.loads(sess_file.read_text(encoding="utf-8")) or {}
            except Exception:
                die_session = {}

        raw_cpu_builds: list[dict[str, Any]] = []
        cpu_builds_dir = self.opsb_home / "cpu_builds"
        if cpu_builds_dir.is_dir():
            for bf in sorted(cpu_builds_dir.glob("*.json")):
                try:
                    raw_cpu_builds.append(json.loads(bf.read_text(encoding="utf-8")))
                except Exception:
                    continue

        raw_tickets: list[dict[str, Any]] = []
        ticket_dir = pathlib.Path("/tmp/opsb-npu-tickets")
        if ticket_dir.is_dir():
            for tf in ticket_dir.glob("*.json"):
                try:
                    raw_tickets.append(json.loads(tf.read_text(encoding="utf-8")))
                except Exception:
                    continue

        locks = read_locks_procfs()
        hint_pids: set[int] = {int(lk["pid"]) for lk in locks}
        if die_session.get("pid"):
            hint_pids.add(int(die_session["pid"]))
        for bd in raw_cpu_builds:
            if bd.get("pid"):
                hint_pids.add(int(bd["pid"]))
        for td in raw_tickets:
            if td.get("pid"):
                hint_pids.add(int(td["pid"]))

        procs = self._get_procs_snapshot(hint_pids=hint_pids)

        active_cpu_build_pids: set[int] = set()
        cpu_builds_by_pid: dict[int, dict[str, Any]] = {}
        for bd in raw_cpu_builds:
            b_pid = int(bd.get("pid") or 0)
            if bd.get("finished_at") or (b_pid and b_pid not in procs):
                continue
            if b_pid:
                active_cpu_build_pids.add(b_pid)
                cpu_builds_by_pid[b_pid] = bd

        die_holders: dict[int, int] = {}
        waiting_roots: set[int] = set()
        for lk in locks:
            path = lk["path"]
            mode = lk["mode"].upper()
            pid = lk["pid"]
            is_waiting = bool(lk.get("waiting"))
            m_die = re.search(r"device(\d+)\.lock$", path)
            if m_die:
                die_idx = int(m_die.group(1))
                if "WRITE" in mode and not is_waiting:
                    prev = die_holders.get(die_idx)
                    if prev is None or procs.get(pid, {}).get("elapsed_sec", 0) > procs.get(prev, {}).get("elapsed_sec", 0):
                        die_holders[die_idx] = pid
                else:
                    waiting_roots.add(pid)
            elif path.endswith("validation.lock"):
                waiting_roots.add(pid)

        for pid, p in procs.items():
            cmd = p["cmd"]
            if _is_driver_wrapper(cmd):
                continue
            if re.search(r"[\w.-]+-device\d*\.lock", cmd) is not None and "flock" in cmd:
                if pid not in die_holders.values():
                    waiting_roots.add(pid)

        holder_trees: dict[int, list[int]] = {}
        all_holder_pids: set[int] = set()
        for die_idx, h_pid in die_holders.items():
            tree = get_descendants(h_pid, procs)
            holder_trees[die_idx] = tree
            all_holder_pids.update(tree)
            all_holder_pids.update(_collect_tree_and_driver_peers(h_pid, procs))
            ppid = procs.get(h_pid, {}).get("ppid", 0)
            if ppid:
                all_holder_pids.add(ppid)

        waiting_roots = {
            pid for pid in waiting_roots
            if pid not in all_holder_pids
            and pid not in active_cpu_build_pids
            and not (_collect_tree_and_driver_peers(pid, procs) & all_holder_pids)
        }

        tasks: list[dict[str, Any]] = []
        die_locks_info: list[dict[str, Any]] = []
        max_dies = max(1, len(npu.get("devices", [])))
        for die_idx in range(max_dies):
            h_pid = die_holders.get(die_idx)
            if h_pid is not None and h_pid in procs:
                tree = holder_trees.get(die_idx, [h_pid])
                prob, sha = infer_problem_and_sha(tree, procs)
                sess_matches = bool(
                    die_session
                    and int(die_session.get("die", 0)) == die_idx
                    and any(int(die_session.get(k) or 0) in tree or int(die_session.get(k) or 0) == h_pid
                         for k in ("pid", "holder_pid"))
                )
                substage_hint = str(die_session.get("substage") or "") if sess_matches else None
                sess_mode = str(die_session.get("mode") or "").upper() if sess_matches else ""
                if sess_matches:
                    prob = die_session.get("title") or prob
                    sha = die_session.get("sha") or sha
                is_backfill = (
                    sess_mode == "B0"
                    or substage_hint == "backfill_perf"
                    or any("/.backfill/" in procs.get(cpid, {}).get("cmd", "") for cpid in tree)
                )
                if is_backfill:
                    stage, detail = "BACKFILL", "Baseline perf-bucket backfill (B0)"
                else:
                    stage, detail = classify_stage(tree, procs, holds_die=True, substage_hint=substage_hint)
                start = float(die_session.get("started_epoch") or 0) if sess_matches else 0
                elapsed = int(max(0, now_epoch - start)) if start > 0 else None
                if not sess_matches and not is_backfill:
                    label, elapsed = unregistered_holder(h_pid, tree, procs)
                    stage, detail, prob, sha = "UNREGISTERED", "Device lock without a platform session", label or prob, None
                lock_entry: dict[str, Any] = {
                    "die": die_idx,
                    "lock_path": f"{work_root}/{work_prefix}-device{die_idx}.lock",
                    "state": "HELD",
                    "holder_pid": h_pid,
                    "problem": prob or "Unknown",
                    "sha": sha,
                    "stage": stage,
                    "elapsed_sec": elapsed,
                }
                if substage_hint:
                    lock_entry["substage"] = substage_hint
                die_locks_info.append(lock_entry)
                tasks.append(
                    {
                        "pid": h_pid,
                        "run_id": die_session.get("run_id") if sess_matches else None,
                        "case_progress": die_session.get("case_progress") if sess_matches else None,
                        "die": die_idx,
                        "state": "ACTIVE",
                        "stage": stage,
                        "lane": "B0" if is_backfill else (sess_mode or "Q1"),
                        "is_backfill": is_backfill,
                        "problem": prob,
                        "sha": sha,
                        "elapsed_sec": elapsed,
                        "detail": detail,
                        "subprocesses": len(tree),
                    }
                )
            else:
                die_locks_info.append(
                    {
                        "die": die_idx,
                        "lock_path": f"{work_root}/{work_prefix}-device{die_idx}.lock",
                        "state": "FREE",
                        "holder_pid": None,
                        "problem": None,
                        "sha": None,
                        "stage": None,
                        "elapsed_sec": 0,
                    }
                )

        tickets_by_pid: dict[int, dict[str, Any]] = {}
        for td in raw_tickets:
            tpid = int(td.get("pid") or 0)
            if tpid in procs and tpid not in active_cpu_build_pids:
                tickets_by_pid[tpid] = td
                if tpid not in all_holder_pids:
                    waiting_roots.add(tpid)

        waiting_tasks_list: list[dict[str, Any]] = []
        all_waiting_pids: set[int] = set()
        sorted_waiters = sorted(
            waiting_roots,
            key=lambda x: (
                1 if x in tickets_by_pid else 0,
                int(tickets_by_pid.get(x, {}).get("tier") or 1),
                float(tickets_by_pid.get(x, {}).get("prio") or 0.0),
                -float(tickets_by_pid.get(x, {}).get("created") or now_epoch),
            ),
            reverse=True,
        )
        for w_pid in sorted_waiters:
            if w_pid not in procs or w_pid in active_cpu_build_pids or w_pid in all_waiting_pids:
                continue
            rel_pids = _collect_tree_and_driver_peers(w_pid, procs)
            if rel_pids & (all_holder_pids | all_waiting_pids):
                continue
            tree = get_descendants(w_pid, procs)
            all_waiting_pids.update(rel_pids)
            tk = tickets_by_pid.get(w_pid, {})
            prob, sha = infer_problem_and_sha(tree, procs)
            prob = tk.get("title") or prob
            sha = tk.get("sha") or sha
            if not prob:
                continue
            is_backfill = (
                str(tk.get("lane") or "").upper() == "B0"
                or str(tk.get("kind") or "").upper() == "BACKFILL"
                or ("tier" in tk and int(tk.get("tier") or 0) == 0)
                or any("/.backfill/" in procs.get(cpid, {}).get("cmd", "") for cpid in tree)
            )
            if is_backfill:
                stage, detail = "BACKFILL-WAIT", "Waiting idle Die 0 for perf-bucket backfill (B0 · tier 0)"
                is_boosted = False
                lane_str = "B0"
            else:
                stage, detail = classify_stage(tree, procs, holds_die=False)
                is_boosted = int(tk.get("tier") or 0) >= 50
                if is_boosted:
                    detail = "Priority boosted by Parhelion"
                lane_str = "Q0" if tk.get("fast") else "Q1"
            tk_created = float(tk.get("created") or 0)
            wait_elapsed = int(max(0, round(now_epoch - tk_created))) if tk_created > 0 else procs[w_pid]["elapsed_sec"]
            wt_item = {
                "pid": w_pid,
                "run_id": tk.get("run_id"),
                "case_progress": tk.get("case_progress"),
                "die": None,
                "state": "WAITING",
                "stage": stage,
                "lane": lane_str,
                "is_backfill": is_backfill,
                "problem": prob,
                "sha": sha,
                "elapsed_sec": wait_elapsed,
                "detail": detail,
                "boosted": is_boosted,
                "subprocesses": len(tree),
            }
            waiting_tasks_list.append(wt_item)
            tasks.append(wt_item)

        parallel_cpu_builds: list[dict[str, Any]] = []
        seen_build_keys: set[tuple[str, str, str]] = set()
        cpu_build_tree_pids: set[int] = set()
        for b_pid, bd in cpu_builds_by_pid.items():
            rel_b = _collect_tree_and_driver_peers(b_pid, procs) if b_pid in procs else {b_pid}
            if rel_b & (all_holder_pids | all_waiting_pids):
                continue
            b_title = bd.get("title") or bd.get("problem") or "Operator"
            b_sha = str(bd.get("sha") or "")
            st_ep = float(bd.get("started_epoch") or 0)
            el_s = int(now_epoch - st_ep) if st_ep > 0 else procs.get(b_pid, {}).get("elapsed_sec", 0)
            b_is_bf = str(bd.get("lane") or "").upper() == "B0" or str(bd.get("kind") or "").upper() == "BACKFILL"
            lane_str = "B0" if b_is_bf else "Q1"
            b_key = (str(b_title).strip().lower(), lane_str, bd.get("run_id") or b_sha[:7])
            cpu_build_tree_pids.update(rel_b)
            if b_key not in seen_build_keys:
                seen_build_keys.add(b_key)
                parallel_cpu_builds.append(
                    {
                        "pid": b_pid,
                        "run_id": bd.get("run_id"),
                        "jobs": bd.get("jobs"),
                        "title": b_title,
                        "sha": b_sha,
                        "short_sha": b_sha[:7] or "—",
                        "elapsed_sec": max(0, el_s),
                        "lane": lane_str,
                        "is_backfill": b_is_bf,
                    }
                )

        occupied_pids = all_holder_pids | all_waiting_pids | cpu_build_tree_pids
        for pid, p in procs.items():
            if pid in occupied_pids or _is_driver_wrapper(p["cmd"]):
                continue
            cmd_l = p["cmd"].lower()
            if any(k in cmd_l for k in ("build.sh", "/ccec", "compiler")) and "grep" not in cmd_l:
                rel_c = _collect_tree_and_driver_peers(pid, procs)
                if rel_c & occupied_pids:
                    continue
                prob, sha = infer_problem_and_sha([pid], procs)
                b_is_bf = "/.backfill/" in p["cmd"]
                if not b_is_bf:
                    try:
                        if "/.backfill/" in os.readlink(f"/proc/{pid}/cwd"):
                            b_is_bf = True
                    except Exception:
                        pass
                if prob:
                    lane_str = "B0" if b_is_bf else "Q1"
                    b_key = (prob.strip().lower(), lane_str, (sha or "")[:7])
                    cpu_build_tree_pids.update(rel_c)
                    occupied_pids.update(rel_c)
                    if b_key not in seen_build_keys:
                        seen_build_keys.add(b_key)
                        parallel_cpu_builds.append(
                            {
                                "pid": pid,
                                "title": prob,
                                "sha": sha or "",
                                "short_sha": (sha or "")[:7] or "—",
                                "elapsed_sec": p["elapsed_sec"],
                                "lane": lane_str,
                                "is_backfill": b_is_bf,
                            }
                        )

        for cb in parallel_cpu_builds:
            cb_bf = bool(cb.get("is_backfill"))
            tasks.append(
                {
                    "pid": cb["pid"],
                    "run_id": cb.get("run_id"),
                    "die": None,
                    "state": "CPU_BUILD",
                    "stage": "BACKFILL-BUILD" if cb_bf else "CPU-BUILD",
                    "lane": "B0" if cb_bf else "Q1",
                    "is_backfill": cb_bf,
                    "problem": cb["title"],
                    "sha": cb["sha"] or None,
                    "elapsed_sec": cb["elapsed_sec"],
                    "detail": (
                        "Phase A baseline worktree build for perf-bucket backfill (B0)"
                        if cb_bf
                        else f"CMake parallel limit: -j{cb['jobs']}" if cb.get("jobs") else "Phase A lock-free CPU build"
                    ),
                    "subprocesses": len(get_descendants(cb["pid"], procs)) - 1,
                }
            )

        # Preserve independent runs while suppressing multiple observations of the same job.
        state_rank = {"ACTIVE": 3, "CPU_BUILD": 2, "WAITING": 1}
        winners = {}
        for t in tasks:
            p_raw = str(t.get("problem") or "").strip()
            p_key = p_raw.lower() if p_raw else f"pid:{t.get('pid')}"
            l_key = str(t.get("lane") or ("B0" if t.get("is_backfill") else "Q1")).strip().upper()
            k = (p_key, l_key, t.get("run_id") or str(t.get("sha") or "")[:7])
            prev = winners.get(k)
            cand_score = (state_rank.get(str(t.get("state") or ""), 0), int(t.get("subprocesses") or 0), 1 if t.get("sha") else 0)
            prev_score = (state_rank.get(str(prev.get("state") or ""), 0), int(prev.get("subprocesses") or 0), 1 if prev.get("sha") else 0) if prev else (-1, -1, -1)
            if prev is None or cand_score > prev_score:
                winners[k] = t
        selected = {id(t) for t in winners.values()}
        tasks = [t for t in tasks if id(t) in selected]
        build_pids = {t["pid"] for t in tasks if t["state"] == "CPU_BUILD"}
        parallel_cpu_builds = [cb for cb in parallel_cpu_builds if cb["pid"] in build_pids]
        waiting_tasks_list = [t for t in tasks if t.get("state") == "WAITING"]
        host_case_tasks(self.opsb_home, tasks, now_epoch)

        primary_dev = (
            npu["devices"][0]
            if npu.get("devices")
            else {
                "npu_id": 0,
                "chip_id": 0,
                "soc_name": device_text("soc"),
                "health": "Unknown",
                "bus_id": "",
                "power_w": None,
                "temp_c": None,
                "aicore_pct": 0,
                "aivector_pct": 0,
                "hbm_bw_pct": 0,
                "ctrlcpu_pct": 0,
                "hbm_used_mib": 0,
                "hbm_total_mib": 65536,
            }
        )

        now_ts = round(now_epoch, 1)
        hbm_pct = round((primary_dev.get("hbm_used_mib", 0) / max(1, primary_dev.get("hbm_total_mib", 65536))) * 100.0, 2)
        ram_pct = round((mem.get("used_mib", 0) / max(1, mem.get("total_mib", 1))) * 100.0, 2)
        sample_pt = {
            "ts": now_ts,
            "aicore_pct": float(primary_dev.get("aicore_pct") or 0.0),
            "aivector_pct": float(primary_dev.get("aivector_pct") or 0.0),
            "hbm_bw_pct": float(primary_dev.get("hbm_bw_pct") or 0.0),
            "power_w": float(primary_dev.get("power_w") or 0.0),
            "temp_c": int(primary_dev.get("temp_c") or 0),
            "hbm_pct": hbm_pct,
            "cpu_pct": float(cpu.get("usage_pct") or 0.0),
            "ram_pct": ram_pct,
            "die_held": 1 if (die_locks_info and die_locks_info[0]["state"] == "HELD") else 0,
            "die_stage": die_locks_info[0].get("stage") if die_locks_info else None,
        }

        min_gap = min(1.5, max(0.4, self.interval * 0.8))
        if not self._history or (now_ts - float(self._history[-1].get("ts", 0))) >= min_gap:
            self._history.append(sample_pt)
            self._history = [p for p in self._history if (now_ts - float(p.get("ts", 0))) <= 1800][-90:]

        window_sec = 600.0
        ledger_rows = self._read_ledger_rows_cached(now_ts, window_sec=window_sec)
        holds = [float(row["hold_sec"]) for row in ledger_rows if row.get("hold_sec") is not None]
        if ledger_rows and all("acl_host_sec" in row for row in ledger_rows):
            npu_s = sum(float(r.get("npu_exec_sec") or 0.0) for r in ledger_rows)
            acl_s = sum(float(r.get("acl_host_sec") or 0.0) for r in ledger_rows)
            waste_s = sum(float(r.get("lock_waste_sec") or 0.0) for r in ledger_rows)
            tot_hold = sum(holds) if len(holds) == len(ledger_rows) else npu_s + acl_s + waste_s
            denom = max(window_sec, tot_hold, 1.0)
            free_s = max(0.0, denom - (npu_s + acl_s + waste_s))
            die_duty_10m = {
                "npu_exec_pct": round(100.0 * npu_s / denom, 2),
                "acl_host_pct": round(100.0 * acl_s / denom, 2),
                "lock_waste_pct": round(100.0 * waste_s / denom, 2),
                "free_pct": round(100.0 * free_s / denom, 2),
            }
        else:
            win_pts = [p for p in self._history if (now_ts - float(p.get("ts", 0))) <= window_sec] or [sample_pt]
            n_pts = max(1, len(win_pts))
            held_pts = [
                p for p in win_pts
                if p.get("die_held", (1 if die_locks_info and die_locks_info[0]["state"] == "HELD" else 0))
            ]
            held_frac = len(held_pts) / n_pts
            build_in_lock = sum(1 for p in held_pts if p.get("die_stage") == "BUILD") / n_pts
            mean_vec = (
                sum(max(float(p.get("aivector_pct") or 0.0), float(p.get("aicore_pct") or 0.0)) for p in win_pts) / n_pts
            )
            npu_exec_p = round(min(held_frac * 100.0, max(mean_vec, 0.4 if held_frac > 0 else 0.0)), 2)
            waste_p = round(build_in_lock * 100.0, 2)
            acl_p = round(max(0.0, held_frac * 100.0 - npu_exec_p - waste_p), 2)
            free_p = round(max(0.0, 100.0 - npu_exec_p - acl_p - waste_p), 2)
            die_duty_10m = {
                "npu_exec_pct": npu_exec_p,
                "acl_host_pct": acl_p,
                "lock_waste_pct": waste_p,
                "free_pct": free_p,
            }
        avg_hold_sec = round(sum(holds) / len(holds), 2) if holds else None
        slots_per_hr = round(3600.0 / avg_hold_sec, 1) if avg_hold_sec else None

        estimate_waits(waiting_tasks_list, die_locks_info, avg_hold_sec)

        sample_ms = round((time.monotonic() - t0) * 1000, 2)
        return {
            "sampled_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "probe_backend": probe_backend,
            "sample_ms": sample_ms,
            "probe_latency_ms": sample_ms,
            "hostname": self.hostname,
            "arch": self.arch,
            "sdk_version": self.sdk_version,
            "smi_version": npu.get("smi_version", ""),
            "primary_npu": primary_dev,
            "npu_devices": npu.get("devices", []),
            "npu_processes": npu.get("npu_processes", []),
            "cpu": cpu,
            "memory": mem,
            "history": list(self._history),
            "die_duty_10m": die_duty_10m,
            "slots_per_hr": slots_per_hr,
            "avg_die_hold_sec": avg_hold_sec,
            "parallel_cpu_builds": parallel_cpu_builds,
            "die_locks": die_locks_info,
            "tasks": tasks,
        }


def _emit_line(payload: dict[str, Any]) -> bool:
    try:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        return True
    except BrokenPipeError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parhelion persistent NPU DCMI stream probe")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Sample once and exit (default)")
    mode.add_argument("--stream", action="store_true", help="Continuously emit NDJSON telemetry lines")
    parser.add_argument("--interval", type=float, default=1.0, help="Stream interval in seconds (default 1.0)")
    parser.add_argument("--count", type=int, default=0, help="Optional max frames to emit in --stream mode")
    args = parser.parse_args(argv)

    stop_requested = threading.Event()

    def _on_signal(_sig: int, _frame: Any) -> None:
        stop_requested.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _on_signal)

    session = StreamProbeSession(stream_mode=bool(args.stream), interval=args.interval)
    if not args.stream:
        _emit_line(session.sample_frame())
        return 0

    session.start_background_collectors()
    emitted = 0
    try:
        while not stop_requested.is_set():
            tick_start = time.monotonic()
            frame = session.sample_frame()
            if not _emit_line(frame):
                break
            emitted += 1
            if args.count > 0 and emitted >= args.count:
                break
            session.notify_tick_done()
            sleep_s = max(0.05, session.interval - (time.monotonic() - tick_start))
            stop_requested.wait(sleep_s)
    finally:
        session.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
