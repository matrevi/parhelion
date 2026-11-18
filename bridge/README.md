# Parhelion · Breakthrough Hand-off Bridge (`bridge/`)

A **transport-agnostic hand-off channel** for structured breakthroughs. Whenever a verified
high-Δ entry lands in `breakthrough/`, the bridge routes it to every subscribed agent as a
self-contained work note, and collects each agent's ≤5-line receipt back into
`breakthrough/index.json#summaries`.

It is deliberately dumb: no SDK, no daemon requirement, no coupling to DSH / Codex /
Antigravity. Any process that can read a directory, run `opsb bridge pull`, or receive an
`exec`/`http` hook can participate.

```text
bridge/
├── README.md            # this file — the protocol
├── agents.json          # subscriber registry (the only file you edit by hand)
├── inbox/<agent_id>/    # delivery area: one immutable .md per (entry content sha, agent)
├── outbox/<agent_id>/   # receipt area: agents drop their summary here
└── ledger.jsonl         # append-only idempotency ledger (runtime state, git-ignored)
```

`inbox/`, `outbox/` and `ledger.jsonl` are **runtime state** (git-ignored). `agents.json` and
this README are versioned so every clone of the repo knows who the subscribers are.

---

## 1. Protocol in one screen

| Step | Who | Command | Effect |
| --- | --- | --- | --- |
| route | any sync path | `opsb bridge sync` | new `(entry sha, agent)` → `inbox/<agent>/<sha8>-<entry-id>.md` |
| fetch | agent | `opsb bridge pull <agent_id> --ack` | prints the notes; `--ack` marks them 已取 |
| answer | agent | `opsb bridge publish <agent_id> <entry-id> --file -` | writes `outbox/<agent>/<entry-id>.md` |
| fold | supervisor | `opsb bridge collect` | upserts receipts into `breakthrough/index.json#summaries` |
| escalate | headless host | `opsb bridge push` / `opsb bridge watch` | turns `file` deliveries into `exec` / `http` pushes |

**Idempotency contract:** delivery is keyed by the *SHA-256 of the entry document bytes* plus
`agent_id`. The same pair is never delivered twice, so re-running `sync` — or the macOS app's
「同步」/`opsb breakthrough sync` — is always safe and produces no new files. If an entry
document is edited, its content hash changes and it is delivered once more (that is a new
revision of the experience, not a duplicate).

`inbox` files are **immutable**: the bridge never rewrites or removes a delivery.

### `opsb bridge sync` is automatic

You rarely call it by hand — it is already wired into every path that writes a breakthrough:

* `opsb breakthrough sync` (also the macOS app's 「同步经验库」/`Sync New` button)
* `opsb breakthrough add`
* the auto-archiver inside `opsb queue drain` / the app's consumer engine (terminal `Pass`)

---

## 2. `agents.json` schema

```json
{
  "schema_version": 1,
  "updated_at": "2026-11-18T09:00:00+00:00",
  "agents": [
    {
      "agent_id": "concatd-optimizer",
      "display": "ConcatD 算子优化",
      "kind": "dsh",
      "enabled": true,
      "min_delta": 0.0,
      "scope": {
        "paths": ["bench/ConcatD"],
        "tags": ["多模态分核切分"]
      },
      "transport": {
        "mode": "file",
        "exec": [],
        "url": "",
        "headers": {}
      }
    }
  ]
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `agent_id` | ✅ | Stable id; also the `inbox/` + `outbox/` folder name. No `/`. |
| `display` | – | Human label in the app / CLI (defaults to `agent_id`). |
| `kind` | – | `dsh` \| `codex` \| `antigravity` \| `cli` \| `script` (cosmetic grouping). |
| `enabled` | – | `false` mutes the subscriber without deleting its history. |
| `min_delta` | – | Only route entries with `delta_raw >= min_delta` (implicit matches only). |
| `scope.paths` | – | **Prefix** match against `bench/<Operator>` (absolute workspace paths also work). `"*"` = everything. |
| `scope.tags` | – | **Set intersection** with the entry's `tags`, `category`, its 中/EN category label, and the operator name. `"*"` = everything. |
| `transport.mode` | – | `file` (default) \| `exec` \| `http`. |
| `transport.exec` | for `exec` | argv array, e.g. `["/usr/local/bin/my-agent", "--notify"]`. `.format` placeholders: `{entry_id}` `{entry_sha}` `{sha8}` `{agent_id}` `{inbox}` `{workspace}`. The full note is also piped to **stdin** and exposed as `$OPSB_BRIDGE_*` env vars. |
| `transport.url` | for `http` | `POST` target; body is `{"agent_id","entry_id","entry_sha","note"}` as JSON. |
| `transport.headers` | – | Extra HTTP headers (auth tokens **from env**, never committed). |

Routing is the union of three independent triggers:

1. the entry itself says so — `notify: [agent_id, …]` (in `index.json` or the markdown
   front-matter; `"*"` = every subscriber, and this overrides `min_delta`/`scope`);
2. `scope.paths` prefix hit;
3. `scope.tags` intersection hit.

---

## 3. The note an agent receives

`inbox/<agent_id>/<sha8>-<entry-id>.md` is self-contained — an agent that has never seen this
repo can act on it:

```bash
sed -n '1,40p' "ops-supervisor/bridge/inbox/concatd-optimizer/8d3f2a1c-BT-20261118-ConcatD-7919dcb.md"
```

It always contains: the entry **path**, the **operator**, the **before → after µs** (from
`results/<Title>/calibration.json` buckets, the entry's own `timings_us`, or the documented
`n/a`), the **weighted gain**, the **one-line mechanism** + transferable rule, the **tags**,
the **source commit / baseline**, and a fixed closing instruction:

> 请用 ≤5 行总结：① 可迁移机制 ② 适用条件/失效条件 ③ 对你自己当前题目的下一步；写入
> `ops-supervisor/bridge/outbox/<agent_id>/<entry-id>.md`

---

## 4. Three ways to plug an agent in

### (A) Directory watching — any agent, any editor, one `fswatch` line

```bash
# macOS, no daemon of our own: fswatch hands each new note to whatever you like
fswatch -0 --event Created "ops-supervisor/bridge/inbox/concatd-optimizer" \
  | xargs -0 -n1 -I{} sh -c 'echo "== new experience: {}"; cat "{}"'
```

```bash
# portable polling variant (works on any box with bash, no fswatch)
while true; do
  for f in ops-supervisor/bridge/inbox/concatd-optimizer/*.md; do
    [ -f "$f" ] || continue
    grep -q "^seen:" "$f" 2>/dev/null || { cat "$f"; echo "seen: $(date -u +%FT%TZ)" >> "$f"; }
  done
  sleep 30
done
```

Point an editor/IDE watcher task, a `launchd` `WatchPaths` job, or an agent's own file-watcher
at the same directory — the inbox file *is* the message. When the agent is done, it writes the
receipt file directly (same as `publish`):

```bash
cat > ops-supervisor/bridge/outbox/concatd-optimizer/BT-20261118-ConcatD-7919dcb.md <<'EOF'
① 可迁移机制: 动态 colTile/copyStep 让搬运粒度跟随形状，而不是固定 tile
② 适用条件: 输出行宽差异大的 concat；失效条件: 单一大块连续写、对齐完美时收益消失
③ 下一步: 在 ConcatD 的 rows_big_c8 桶上试 c128→c256 自适应
EOF
opsb bridge collect        # fold it into breakthrough/index.json#summaries
```

### (B) `opsb bridge pull --json` — agents with a shell (DSH, Codex, scripts)

```bash
# 1. see what's pending (no state change)
opsb bridge pull concatd-optimizer --json | jq -r '.items[] | "\(.entry_id)\t\(.sha8)"'

# 2. claim it and get the full note text in one round-trip
opsb bridge pull concatd-optimizer --ack --json > /tmp/bridge-todo.json
jq -r '.items[0].note' /tmp/bridge-todo.json      # the complete instruction

# 3. answer (stdin keeps the ≤5 lines verbatim; no quoting hell)
printf '%s\n' \
  '① 可迁移机制: ...' \
  '② 适用条件/失效条件: ...' \
  '③ 下一步: ...' \
  | opsb bridge publish concatd-optimizer BT-20261118-ConcatD-7919dcb --file -

# 4. supervisor folds every outbox receipt
opsb bridge collect --json
```

A prompt an agent can be given verbatim:

> Run `opsb bridge pull <your-agent-id> --ack --json`. For every item, follow the `note` field
> and publish the ≤5-line summary with `opsb bridge publish <your-agent-id> <entry_id> --file -`.

### (C) `exec` / `http` push hooks — no polling at all

```json
{
  "agent_id": "codex-review",
  "display": "Codex 交叉评审",
  "kind": "codex",
  "scope": { "paths": ["*"], "tags": ["async-pipeline", "deterministic-sync"] },
  "transport": {
    "mode": "exec",
    "exec": ["/bin/sh", "-c", "mkdir -p /tmp/agent-inbox && cat > /tmp/agent-inbox/{entry_id}.md"]
  }
}
```

```json
{
  "agent_id": "antigravity-lab",
  "kind": "antigravity",
  "scope": { "tags": ["simd-transpose"] },
  "transport": {
    "mode": "http",
    "url": "http://127.0.0.1:8787/bridge",
    "headers": { "Authorization": "Bearer ${BRIDGE_TOKEN}" }
  }
}
```

Deliveries are still written to `inbox/` first (the ledger stays the source of truth); the push
is an *escalation* and is recorded as `push_ok` / `push_failed`:

```bash
opsb bridge push --dry-run          # show what would run
opsb bridge push codex-review       # push one agent's pending notes
opsb bridge watch --interval 20     # foreground loop (headless hosts only)
opsb bridge watch --once            # single tick, exits — safe to call from cron/launchd
```

> **One consumer only.** The macOS app already owns a consumer engine for submissions. Run
> `opsb bridge watch` **only** on headless machines, and never as a second long-lived process
> next to the app. `--once` is the safe primitive for cron / `launchd StartInterval`.

---

## 5. Command reference

```bash
opsb bridge status [<agent_id>] [--json]     # per-agent 待总结 / 已取 / 已回执 + index summaries
opsb bridge sync [--min-delta D] [--agent ID] [--dry-run] [--json]
opsb bridge pull <agent_id> [--ack] [--json]
opsb bridge publish <agent_id> <entry_id> [--file <path>|-] [--json]
opsb bridge collect [--agent ID] [--json]
opsb bridge push [<agent_id>] [--entry <entry_id>] [--dry-run] [--json]
opsb bridge watch [--interval N] [--once] [--no-escalate] [--json]
```

All verbs accept `--workspace/-w` and `--bridge-dir` (plus `$OPSB_BRIDGE_DIR`) so a scratch
workspace can be exercised without touching the live registry.

### Ledger states

`ledger.jsonl` is append-only, one JSON object per line:

```json
{"ts":"…","state":"delivered","entry_id":"BT-…","entry_sha":"…","sha8":"…","agent_id":"…","inbox":"inbox/…","mode":"file"}
{"ts":"…","state":"pulled",   "entry_id":"BT-…","entry_sha":"…","agent_id":"…"}
{"ts":"…","state":"published","entry_id":"BT-…","entry_sha":"…","agent_id":"…","outbox":"outbox/…","lines":5}
{"ts":"…","state":"collected","entry_id":"BT-…","entry_sha":"…","agent_id":"…","outbox":"bridge/outbox/…"}
{"ts":"…","state":"push_ok"|"push_failed", "entry_id":"BT-…","agent_id":"…","mode":"exec","error":""}
```

The last record per `(entry_sha, agent_id)` wins; the panel groups by the derived state
`pending → pulled → acked`. Deleting `ledger.jsonl` is safe (it re-heals from the inbox files on
the next `sync`), deleting an `inbox/*.md` file is **not** (that is the delivery itself).

---

## 6. `breakthrough/index.json#summaries`

`opsb bridge collect` appends an additive top-level field — it never rewrites `entries` or any
other key:

```json
"summaries": [
  {
    "entry_id": "BT-20261118-ConcatD-7919dcb",
    "agent_id": "concatd-optimizer",
    "agent_display": "ConcatD 算子优化",
    "agent_kind": "dsh",
    "problem": "ConcatD",
    "entry_sha": "8d3f2a1c…",
    "sha8": "8d3f2a1c",
    "summary": "① …\n② …\n③ …",
    "lines": 3,
    "summary_path": "bridge/outbox/concatd-optimizer/BT-20261118-ConcatD-7919dcb.md",
    "collected_at": "2026-11-18T09:12:00+00:00"
  }
]
```

Collection is idempotent: re-running `collect` with unchanged receipts reports
`0 collected / N unchanged`. Editing a receipt updates the record in place and keeps the
original `collected_at`.

Consumers: `opsb bridge status --json`, `opsb breakthrough list --json` (`summaries` +
`summary_count`), and the macOS 「经验转交」panel.

---

## 7. Adding a subscriber in 30 seconds

```bash
python3 - <<'PY'
import json, pathlib
p = pathlib.Path("ops-supervisor/bridge/agents.json")
doc = json.loads(p.read_text())
doc["agents"].append({
    "agent_id": "my-agent",
    "display": "My Agent",
    "kind": "cli",
    "scope": {"paths": ["bench/LayerNormV3"], "tags": ["simd-transpose"]},
    "transport": {"mode": "file"},
})
p.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
PY
opsb bridge sync --dry-run      # see what would be delivered
opsb bridge sync                # deliver, once
opsb bridge pull my-agent --json
```

## 8. Safety notes

* `agents.json` is trusted local config — an `exec` transport runs **your** argv with the note
  on stdin, `cwd = workspace`. Never commit credentials; use `headers` from env at runtime.
* `agent_id` / `entry_id` are validated against `/` and `..` so a hand-written receipt sdkot
  escape `inbox/` or `outbox/`.
* A broken bridge never blocks the submission pipeline: all automatic hooks run inside
  `try/except` and report `{"ok": false, "error": …}` instead of raising.
