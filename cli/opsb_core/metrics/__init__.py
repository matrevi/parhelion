"""Token, cache and tool-output metering (4.2 work package A).

- ``usage``: per-model-call records from each driver's own logs (read-only);
- ``shape``: prompt fingerprints and named cache-miss reasons;
- ``store``: incremental cursors under ``<workspace state>/metrics/`` and the hot-mesh monitor;
- ``mcp_meter``: one row per MCP ``tools/call`` and hourly per-tool aggregates.

Session-log replays (sprint metrics, the compaction benchmark, cost efficiency) live in
``opsb_core.replay``.
"""
