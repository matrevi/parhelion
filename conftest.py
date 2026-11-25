"""Test-session defaults shared by every suite.

Hardware/toolchain facts live in ``~/.parhelion/config.json`` (``"device"``) so the
repository never carries vendor detail. A developer machine that has a real device
configured must not leak those values into assertions, otherwise the suite passes only
on unconfigured checkouts. Setting ``OPSB_DEVICE_ISOLATED`` makes
``opsb_core.device_env`` and the standalone runtime loader fall back to their neutral
defaults for the duration of the test session.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPSB_DEVICE_ISOLATED", "1")
