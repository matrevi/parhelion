"""Hardware/toolchain settings resolved from `~/.parhelion/config.json`.

The repository must stay free of hardware-vendor detail: SoC names, ISA archs,
toolchain environment scripts, driver library directories, profiler binaries and
the judge endpoint are *deployment* facts, not source facts. They live in the
local configuration file (mode 0600, never committed) under a `"device"` object;
this module gives the rest of the code base neutral, typed access with harmless
neutral defaults so unit tests and fresh checkouts still run.

Resolution order for every key:
  1. ``OPSB_DEVICE_<KEY>`` environment override (upper-case key)
  2. ``~/.parhelion/config.json`` -> ``"device"`` -> ``<key>``
  3. the neutral default below
"""
from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any

# Neutral placeholders: never a real product name, only a shape that keeps the
# code paths exercised (fixtures, dry runs, CI) without a configured device.
DEFAULTS: dict[str, Any] = {
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
    # Suite levels whose timed executions run under the profiler (comma list, or "off").
    "profiler_levels": "standard,final",
    "judge_base": "https://judge.invalid",
    "judge_bot": "judge-bot",
    "work_root": "/srv/work",
    "work_prefix": "bench",
    "repo_url_template": "https://git.invalid/{prefix}-{slug}.git",
    "contest_org": "",
    "contest_track": "",
    # Global byte budget of the prepared-case cache on the device host (int or digit string);
    # empty keeps the runtime default (24 GiB, see runtime/npu_runtime/case_cache.py).
    "case_cache_bytes": "",
    # Toolchain the judge compiles with, for the compile-only check before the gate
    # (scheduler/judge_compile.py). Empty root means the check is skipped with a hint.
    "judge_toolchain_root": "",
    "judge_toolchain_env": "",
    "judge_compile_flags": [],
}


def parhelion_home() -> Path:
    override = os.environ.get("PARHELION_HOME") or os.environ.get("OPSB_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".parhelion").resolve()


def _config_document() -> dict[str, Any]:
    path = parhelion_home() / "config.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    device = raw.get("device")
    return device if isinstance(device, dict) else {}


_CACHE: dict[str, Any] = {"stamp": None, "value": None}


def _config_stamp(path: Path) -> tuple:
    try:
        st = path.stat()
        file_part: tuple = (st.st_mtime_ns, st.st_size)
    except OSError:
        file_part = (0, 0)
    env_part = tuple(os.environ.get("OPSB_DEVICE_" + k.upper(), "") for k in sorted(DEFAULTS))
    return (file_part, env_part)


def device_settings() -> dict[str, Any]:
    """Full merged settings map (defaults < config file < env override).

    Cached on the config file's (mtime, size) plus the environment overrides, because
    the scheduler probes call this from polling loops.
    """
    if os.environ.get("OPSB_DEVICE_ISOLATED") == "1":
        return {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
                for k, v in DEFAULTS.items()}
    path = parhelion_home() / "config.json"
    stamp = _config_stamp(path)
    if _CACHE["stamp"] == stamp and _CACHE["value"] is not None:
        return dict(_CACHE["value"])
    merged: dict[str, Any] = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    for key, value in _config_document().items():
        if key in merged and value not in (None, ""):
            merged[key] = value
    for key in list(merged):
        env_name = "OPSB_DEVICE_" + key.upper()
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        default = merged[key]
        if isinstance(default, dict):
            try:
                parsed = json.loads(raw)
            except Exception:
                continue
            if isinstance(parsed, dict):
                merged[key] = parsed
        elif isinstance(default, list):
            parsed: Any = None
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                merged[key] = [str(part) for part in parsed]
            else:
                merged[key] = [part for part in raw.split(os.pathsep) if part]
        else:
            merged[key] = raw
    _CACHE["stamp"] = stamp
    _CACHE["value"] = dict(merged)
    return dict(merged)


def device(key: str) -> Any:
    """One setting; unknown keys raise so typos fail loudly instead of silently."""
    if key not in DEFAULTS:
        raise KeyError(f"unknown device setting: {key!r}")
    return device_settings()[key]


def device_text(key: str) -> str:
    value = device(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)  # numeric settings (byte budgets) may be written as JSON numbers
    return value if isinstance(value, str) else ""


def device_list(key: str) -> list[str]:
    value = device(key)
    return [str(item) for item in value] if isinstance(value, list) else []


#: Settings the standalone NPU probes read (``device_text``/``DEVICE.get`` in
#: ``probe/npu_probe.py`` and ``probe/npu_stream_probe.py``). Only these travel to the
#: shared device host, where a remote command line is visible to every user via ``ps``.
PROBE_DEVICE_KEYS: tuple[str, ...] = (
    "dcmi_lib", "driver_libs", "driver_root", "soc", "toolchain_dir",
    "toolkit_info", "toolkit_name", "work_prefix", "work_root",
)


def device_env_exports(keys: tuple[str, ...] = PROBE_DEVICE_KEYS) -> str:
    """Shell prefix forwarding the probe's device settings to a remote host.

    The NPU probes are deployed as standalone copies, so they cannot read this
    machine's ``config.json``; without the values they fall back to neutral
    defaults, lose the driver library path, and silently degrade to ``npu-smi``
    with an unknown SoC. Forwarding the settings as ``OPSB_DEVICE_<KEY>`` keeps
    deployment facts out of the repository while still giving the remote probe
    the real values. Returns ``""`` when nothing is configured, so an
    unconfigured host behaves exactly as before.
    """
    assignments: list[str] = []
    settings = device_settings()
    for key, value in sorted((k, settings[k]) for k in keys if k in settings):
        if value == DEFAULTS.get(key):
            continue  # the probe already falls back to the same neutral default
        if isinstance(value, bool):
            continue
        if isinstance(value, (dict, list)):
            if not value:
                continue
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        elif isinstance(value, (str, int, float)):
            text = str(value).strip()
            if not text:
                continue
        else:
            continue
        assignments.append(f"OPSB_DEVICE_{key.upper()}={shlex.quote(text)}")
    return f"export {' '.join(assignments)}; " if assignments else ""
