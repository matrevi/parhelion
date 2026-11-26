"""Test-session defaults shared by every suite.

Hardware/toolchain facts live in ``~/.parhelion/config.json`` (``"device"``) so the
repository never carries vendor detail. A developer machine that has a real device
configured must not leak those values into assertions, otherwise the suite passes only
on unconfigured checkouts. Setting ``OPSB_DEVICE_ISOLATED`` makes
``opsb_core.device_env`` and the standalone runtime loader fall back to their neutral
defaults for the duration of the test session.

State isolation: tests used to write ``ws-*`` / ``bus.db-*`` directories into the real
``~/.parhelion/workspaces``. Pytest loads this root conftest before any test module, so
pointing ``PARHELION_HOME`` / ``OPSB_HOME`` at a session-scoped temporary directory here
also fixes the modules that freeze the home path at import time
(``opsb_core.config.PARHELION_HOME`` and its derived constants). Child processes spawned
by tests inherit the same environment. Set ``PARHELION_TEST_KEEP_HOME`` to opt out and
keep whatever home the environment already names.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile

os.environ.setdefault("OPSB_DEVICE_ISOLATED", "1")

if not os.environ.get("PARHELION_TEST_KEEP_HOME"):
    _SESSION_HOME = os.path.realpath(tempfile.mkdtemp(prefix="parhelion-test-home-"))
    os.environ["PARHELION_HOME"] = _SESSION_HOME
    os.environ["OPSB_HOME"] = _SESSION_HOME
    os.environ["PARHELION_TEST_SESSION_HOME"] = _SESSION_HOME
    atexit.register(shutil.rmtree, _SESSION_HOME, True)
