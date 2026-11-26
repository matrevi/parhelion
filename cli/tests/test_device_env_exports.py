"""Unit tests for forwarding deployment facts to the remote NPU probes.

Regression cover for a live outage: the probes are deployed as standalone copies
that cannot read this machine's ``config.json``. When the device settings stopped
travelling with the ssh command, ``driver_root`` and the ``driver_libs`` preload
map were empty on the device host, ``libdcmi`` failed to load, and the probe
silently degraded to ``npu-smi`` with an unknown SoC ("NPU unreachable").
"""
from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core import device_env  # noqa: E402
from opsb_core.probe import npu_stream_probe as sp  # noqa: E402

DEVICE = {
    "driver_root": "/opt/vendor",
    "dcmi_lib": "libdcmi.so",
    "driver_libs": {"sec": "libc_sec.so", "hal": "libhal.so"},
    "driver_lib_dirs": ["a", "b"],
    "profiler_bin": "profiler",
    "judge_base": "",
    "toolchain_root": "",
    "soc": "soc-a",
}


class DeviceEnvExportsTest(unittest.TestCase):
    def _exports(self, settings: dict) -> dict[str, str]:
        with mock.patch.object(device_env, "device_settings", return_value=dict(settings)):
            prefix = device_env.device_env_exports()
        self.assertTrue(prefix.startswith("export "), prefix)
        self.assertTrue(prefix.endswith("; "), prefix)
        body = prefix[len("export ") : -len("; ")]
        return {k: v for k, v in (part.split("=", 1) for part in shlex.split(body))}

    def test_scalars_are_forwarded_and_quoted(self) -> None:
        env = self._exports(DEVICE)
        self.assertEqual(env["OPSB_DEVICE_DRIVER_ROOT"], "/opt/vendor")
        self.assertEqual(env["OPSB_DEVICE_DCMI_LIB"], "libdcmi.so")
        self.assertEqual(env["OPSB_DEVICE_PROFILER_BIN"], "profiler")

    def test_structured_settings_travel_as_json(self) -> None:
        env = self._exports(DEVICE)
        self.assertEqual(json.loads(env["OPSB_DEVICE_DRIVER_LIBS"]), DEVICE["driver_libs"])
        self.assertEqual(json.loads(env["OPSB_DEVICE_DRIVER_LIB_DIRS"]), ["a", "b"])

    def test_empty_and_structured_empty_are_skipped(self) -> None:
        env = self._exports(DEVICE)
        self.assertNotIn("OPSB_DEVICE_JUDGE_BASE", env)
        self.assertNotIn("OPSB_DEVICE_TOOLCHAIN_ROOT", env)

    def test_unconfigured_device_forwards_nothing(self) -> None:
        with mock.patch.object(device_env, "device_settings", return_value={}):
            self.assertEqual(device_env.device_env_exports(), "")


class ProbeEnvOverrideTest(unittest.TestCase):
    """The deployed copy must accept structured settings from the environment."""

    def _resolve(self, **env: str) -> dict:
        # `OPSB_DEVICE_ISOLATED=1` (set for the whole test session) short-circuits to
        # the neutral defaults, so it must be cleared for these override assertions.
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(
                os.environ, {**env, "OPSB_DEVICE_ISOLATED": "0"}, clear=False
            ), mock.patch.object(sp.pathlib.Path, "home", return_value=Path(home)):
                return sp._device_settings()

    def test_json_driver_libs_override_the_default_map(self) -> None:
        libs = {"sec": "libc_sec.so", "hal": "libhal.so"}
        resolved = self._resolve(OPSB_DEVICE_DRIVER_LIBS=json.dumps(libs))
        self.assertEqual(resolved["driver_libs"], libs)

    def test_json_dir_list_override_the_default_list(self) -> None:
        resolved = self._resolve(OPSB_DEVICE_DRIVER_LIB_DIRS='["one","two"]')
        self.assertEqual(resolved["driver_lib_dirs"], ["one", "two"])

    def test_scalar_override_still_wins(self) -> None:
        resolved = self._resolve(OPSB_DEVICE_DRIVER_ROOT="/opt/vendor")
        self.assertEqual(resolved["driver_root"], "/opt/vendor")

    def test_malformed_json_is_ignored_not_crashed(self) -> None:
        resolved = self._resolve(OPSB_DEVICE_DRIVER_LIBS="{not json")
        self.assertEqual(resolved["driver_libs"], sp._DEVICE_DEFAULTS["driver_libs"])


if __name__ == "__main__":
    unittest.main()
