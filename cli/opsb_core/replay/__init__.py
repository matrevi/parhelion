"""Session-log replay (4.2 work package A): sprint metrics, compaction benchmark, cost efficiency.

Everything here reads logs and platform records only; nothing is written to the sources.

- ``loader``: mesh ACP log -> turns -> tool calls -> results, cut at compaction points;
- ``actions``: shared classification of shell commands, reads and tests;
- ``sprint_metrics``: the section 1/3 table of docs/v4.2/00-PLAN.md;
- ``probes`` / ``seeds`` / ``score`` / ``bench``: the section 5.1 replay benchmark;
- ``cost``: tokens and device-seconds per kept improvement and per official gain point;
- ``sources``: read-only orchestrator sessions, bus, test registry, gate log, receipts.
"""
