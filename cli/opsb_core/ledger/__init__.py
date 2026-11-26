"""Per-worker experiment and insight ledger (4.2 work package C).

``model`` entry schema, ``store`` append-only JSONL, ``view`` current views and queries,
``summary`` the code-built ledger digest, ``rows`` pure builders of platform entries,
``auto`` platform auto-writes (tests, gate requests, receipts), ``checkpoint`` the seed budget,
``handoff`` / ``seed_min`` handoff validation and the ledger-only fallback seed.
"""
