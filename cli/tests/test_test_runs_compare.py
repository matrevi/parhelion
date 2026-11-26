"""`parhelion_test(compare=...)` reaches `opsb test --compare` and keeps paired requests apart."""
from __future__ import annotations

import sys
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.mcp.schemas import TOOL_SCHEMAS  # noqa: E402
from opsb_core.services import test_runs  # noqa: E402


def test_compare_travels_to_the_test_command(tmp_path):
    cmd = test_runs.build_command(tmp_path, "Op", "a" * 40, "standard", [], False, False, "b" * 40)
    assert cmd[cmd.index("--compare") + 1] == "b" * 40
    assert "--compare" not in test_runs.build_command(tmp_path, "Op", "a" * 40, "standard", [], False, False)


def test_paired_and_plain_requests_do_not_join_each_other():
    plain = {"problem": "Op", "sha": "a" * 40, "level": "standard", "cases": [], "build_only": False}
    paired = dict(plain, compare="b" * 40)
    assert test_runs._dedup_key(plain) != test_runs._dedup_key(paired)
    assert test_runs._dedup_key(plain) == test_runs._dedup_key(dict(plain, compare=None))


def test_schema_offers_compare_to_workers_and_orchestrators():
    schema = next(t for t in TOOL_SCHEMAS if t["name"] == "parhelion_test")
    assert schema["inputSchema"]["properties"]["compare"]["type"] == "string"
