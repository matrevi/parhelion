# Token-economy research for Parhelion

Research date: 2026-11-26. Read-only: GitHub via `gh api`, public docs via fetch. Everything is marked **verified** (I read the source/doc text), **self-reported** (a project's own claim, not independently reproduced) or **unverified**.

Our measured waste, used as the yardstick throughout: orchestrator 136K tokens in ~10 min with 51 shell `cd`/`cat` calls; workers 150-193K in 10 min and 21-44K at start from `read`/`cd`/`ls`; per hour ~250 `agent_wait`, ~200 `test_wait`, ~200 `agent_send`, ~110 `status`; multi-KB peer tables re-quoted; ~2.36 B cumulative re-read tokens; compaction at 30% of window (120k-400k).

---

## 1. Reasonix

### 1.1 What it is (verified)

- Repo: <https://github.com/esengine/DeepSeek-Reasonix> (MIT, ~36k stars). "An open-source coding agent for your terminal, desktop, browser and editor"; DeepSeek preset built in, any OpenAI-compatible endpoint configurable. Speaks ACP (`reasonix acp`) and can run as a subagent inside DeepSeek Harness (`docs/DSH.md`, plugin index under `integrations/dsh-plugin`).
- **Status: no longer maintained as of 2026-11-12; final release v2.61.0** (README banner, announcement issue 15109). Forks welcome. So treat it as a reference implementation, not a dependency.
- Two lines: **2.x** = Go rewrite on branch `studio` (default branch; the code cited below), **1.x** = `main-v2` (and the TypeScript 0.x ancestors, e.g. tag `v0.30.0`, which carry the original "cache-first loop" write-up in `docs/ARCHITECTURE.md`).
- Name collisions to ignore: `Plocr/Reasonix-computer-use`, `christopherarter/superpowers-reasonix` (skills port), `thetrebor/pi-reasonix` (third-party port of its DeepSeek tricks to the Pi agent). Adjacent and possibly relevant to Parhelion's multi-agent shape: `rixzkiye/codex-reasonix-mcp` ("Codex-supervised Reasonix ACP workers"), `OwlCodeTech/reasonix-bridge` (Codex to DeepSeek MCP bridge) - I did not read these.

Design north star, stated in the 0.30 architecture doc: "coding agent that stays cheap enough to leave on", built around DeepSeek's byte-exact prefix cache.

### 1.2 Techniques, with file references

Paths are relative to the repo root on branch `studio` unless noted.

**A. Append-only prompt, compaction as a rare "cache-reset point".**
- `internal/runtime/agent/compact.go:18-27`: "the prompt grows append-only until a maintenance boundary is crossed, then one content-driven summary checkpoint is installed (stable prefix + one structured digest + recent tail)". Constants: `defaultCompactRatio = 0.90`, `checkpointCeilingRatio = 0.50`, `refoldMinTokensRatio = 0.10`, `minRefoldTokens = 32*1024`, `maxRefoldTokens = 96*1024`.
- `compact.go:99-100` (`refoldMinTokens`): "every fold costs the whole prefix cache, so it must free more than it costs" - a second fold waits for 10% of the window (32k-96k tokens) of new closed history.
- `compact.go:254` `closedPrefixEnd`: a fold may only end where every tool call has its result. This is how it keeps tool-call correctness. `keepIndexes` (`:273`) additionally pins the active turn's request and any tool-call group still executing.
- 0.x design (tag `v0.30.0`, `docs/ARCHITECTURE.md` "Pillar 1"): context split into IMMUTABLE PREFIX (system + tool specs + few-shots, hashed and pinned for the session), APPEND-ONLY LOG ("no rewrites"), VOLATILE SCRATCH ("never sent upstream").

**B. Cache-miss attribution (observability for the prefix).**
- `internal/runtime/agent/cache_shape.go:38-58` `CaptureShape` hashes system prompt, normalized tool schemas, and the pair; `:60-72` `NormalizeToolSchemas` sorts a copy by name/description/params so ordering noise does not raise false alarms (the registry itself keeps registration order plus a canonicalized schema per tool, `internal/contract/tool/replace_prefix.go:46`); `:80-97` `BodyChain` is a cumulative per-message hash; `:118-150` `CompareShape` reports reasons `system`, `tools`, declared rewrite reasons (`compact_auto`, `snip`, `rewind_truncate`) or `body_unreported` (a rewrite nobody declared). So every miss gets a named cause.
- `internal/contract/config/cache_policy.go:21-36`: per-vendor cache TTL (Anthropic 5 min, DashScope 5 min, DeepSeek/unknown 24 h) used by "cold-resume prune" to decide whether it is worth rewriting history when the provider cache is already cold. Comment records "measured ~4x miss cost" for pruning a still-warm DeepSeek cache.

**C. Lean, stable tool surface + capability proxy.**
- `docs/TOOL_CONTRACT.md:146-172`: every execution mode starts with the same small core (`bash`, `bash_output`, `edit_file`, `kill_shell`, `read_file`, `wait`, `write_file`, `compress`, `recall`, `context_budget`, `use_capability`, plus todo/ask tools). Optional tools (`glob`, `grep`, `ls`, `web_fetch`, MCP, skills, subagents, memory, history...) "stay registered for host dispatch but are not expanded into the top-level provider schema; the model reaches them through `use_capability` without cache-breaking schema churn" (line 140). The fixed proxy's name/description/schema/order do not change when MCP inventory changes. Unconditional builtin schema budget is documented as 7,185 tokens (line 26). A golden test (`TestBootToolContractMatchesProviderVisibleSurface`) guards the surface; adding four peer tools is documented as causing "one cold prefix on upgrade".

**D. Tool-output gate: spill to file, window, or head+tail snip.**
- `internal/runtime/agent/tool_output_spill.go:230-258` `boundToolOutput` (comment at `:225`: "the single gate every tool result passes before it becomes context"), three outcomes: (1) **spilled**: oversize body written beside the session, context keeps a fixed-price pointer `[bash output kept out of context: N lines, X KB] Full output: <path> Read a window of it with read_file (offset/limit), or search it with grep` plus a preview no larger than the instructions (`spillPointer` `:178`, `spillInstructions` `:190-192`); (2) **windowed** for tools that are paged (never snipped, because the model can read on; `windowMarker` `:260`); (3) **truncated** head+tail snip. Spilling only happens when it "pays for itself" (`spillPays`, `:53`), and reading back a spilled file never spills again (`readsSpilledOutput`, `:97`).
- `internal/runtime/agent/prune.go:13-14`: snip geometry per tool class: read-only `head 80 lines/10,000 chars, tail 12 lines/2,000 chars`; side-effecting `40/40 lines, 8,000/8,000 chars`; `forFailure` (`:40-46`) reweights toward the tail because "a failed call's answer is at its end". Tools can supply their own `SnipHint`.
- Tool descriptions push the same discipline: `grep` capped at 200 matches and "ignored paths searched only when nothing else matched"; `read_file` returns a pagination trailer; the `bash` description tells the model not to `cat`/`sed`/`head` files because "paging a file through the shell costs one round trip per chunk and yields no line anchors" (`docs/TOOL_CONTRACT.md:41-63`). Exact `MaxToolOutputBytes` value: unverified (constant not located).
- 0.x equivalent (`v0.30.0` architecture doc, pillar 4.2): every tool result over 3,000 tokens is shrunk at turn end; "one extra `read_file` call is vastly cheaper than dragging 12KB through every future prompt"; a proactive 40% context-ratio shrink inside long turns, 80% emergency. Note this rewrites old log entries, i.e. it is a deliberate cache-invalidating edit.

**E. Folded-work index and bounded recall.**
- `compact_recall.go:17-27, 89-93`: after a fold the summary carries a "Folded work index" of `#n` positions; the `recall` tool reads positions or searches the folded region, with a **per-generation budget of 10% of the window (min 2,000 tokens)** and "refused whole rather than truncated: a half-recalled span reads as the whole of what was there". The tool text also says: prefer re-reading a file over recalling it.
- `compact_projection.go:16-25` (`maxCompressAnchorBytes = 512`, `CompressContext`): model-triggered `compress` installs a projection only; the canonical transcript is untouched (anchor must be a unique excerpt of a user turn, `before`/`after`).
- `context_budget` tool (`internal/contract/tool/context_budget.go:9-21`): the model can ask how many tokens remain **to the compaction trigger**, not to the physical window; tool text says to check it before a big read/search/log.

**F. Compaction request shares the main prefix.**
- `benchmarks/compaction/cache-prefix/README.md`: summary request reuses the previously sent system/tools/history so it hits the provider cache. Measured on 2026-10-17 against DeepSeek flash, 3 trials per shape: prompt cache-hit fraction 82.7% (text) / 81.3% (rich) reused vs 63.1% / 57.3% independent. **The README itself says this small fixture does not establish a cost saving** (text total cost +6.8%, rich -12.5% mostly from shorter output). Honest take: it improves hit fraction and removes cold summarizer prefixes; savings at large history are unproven.

**G. Memory/instructions kept out of the prefix, recall as a bounded suffix.**
- `docs/SESSION_MEMORY_RETRIEVAL.md:29, 117, 129-163`: instruction files are "part of the cache-stable prompt prefix, so every extra paragraph is carried by every turn" - keep short, store discoverable facts as memory instead. Pinned memory capped at 1,500 chars. Automatic recall (BM25, CJK bigrams) runs before each real user turn and appends **at most 4 facts / 2,400 chars as a low-authority suffix on that user turn**, "they do not mutate the system prompt or tool schema". Generic turns like "continue" trigger no recall. Subject keys prevent two contradictory facts both being active.

**H. Subagents: bodies never enter the parent; results by reference.**
- `docs/SUBAGENT_PROGRESS.md:66-85`: child reasoning/text/notice bodies "never enter the parent transcript or provider context"; progress previews coalesce for 250 ms, at most 32 non-terminal events/s per group (round-robin so one hot child cannot starve others), 8 KiB per-child pending buffer, truncation always flagged.
- `docs/TOOL_CONTRACT.md:132-136`: `parallel_tasks`/`fleet` keep the combined result under the single-tool limit by returning "a fair preview and a stable `Subagent reference` for every persisted child"; `read_subagent_result` pages one referenced final answer **by UTF-8 byte offset**; `list_subagents` recovers references if the aggregate never arrived.
- `internal/state/peermail/doc.go` and `internal/state/sessioninbox/doc.go`: peer messages are an immutable on-disk spool; "the sender never writes into the receiver's transcript, queue or prefix"; the receiver holds messages for approval; messages "carry text, a sender identity and a hop count, and nothing that approves, elevates or widens anything". Inbox manifest is "metadata only" with per-item blobs, and "this package never enters provider prompts".

**I. Mid-turn guidance queue instead of extra turns.** `internal/runtime/agent/steer_inbox.go`: guidance is admitted only while a Run is executing and drained between steps, `DropSteer` can retract unread items; nothing wakes the model separately per message.

**J. Anti-waste guards.**
- `perseveration_guard.go`: detects degenerate repetition in the stream, trims it to the phrase + one repeat, optionally aborts and never executes the calls from the aborted stream.
- `todo_echo.go`: a `todo_write` that leaves the task list unchanged is "recorded but not redrawn".
- Reasonix 0.x tool-call repair "storm breaker": identical tool+args back-to-back are suppressed (as described in `thetrebor/pi-reasonix` README; the original TS code was not read -> second-hand).
- Parallel dispatch of consecutive read-only tools (`parallelSafe`, default chunk 3, hard cap 16; 0.30 architecture doc).

**K. Cost routing (0.x).** All auxiliary calls (forced summary, subagent exploration, truncation-repair retries) hard-coded to the cheap tier at high effort; frontier tier only on explicit one-turn arming or after 3 "struggling" signals (search/replace failures, repair events). The doc says pro is ~12x flash; current DeepSeek list prices imply 4.4x (miss) to 7.3x (hit), so that multiple is stale (see section 3A).

### 1.3 Measured numbers Reasonix reports (all **self-reported**, not reproduced)
- README at tag `v0.20.0`: "94% live prefix-cache hit", "~30x cheaper per task vs Claude Code", hit rates "~33% (baseline)" for other tools; "the difference between 50% and 94% hit is roughly 2.5x on input cost alone" (at their then-quoted pricing).
- README at tag `v0.10.0`: sample `/stats` table showing 93.8-95.1% cache hit across today/week/month.
- `thetrebor/pi-reasonix` README (third party): real-world hit rate "below 20%" without prefix discipline vs "94%+" with it; one OpenCode Go run `input_tokens 168,112 / cached_tokens 164,736` = 97.99%; tool-result cap default 3,000 tokens head+tail. Also self-reported.
- Only the compaction-prefix benchmark above has a documented method; it is small and says so.

---

## 2. Google Antigravity "skills"

### 2.1 Repos found (verified to exist; I could not verify which one you mean or their open-source dates)

I found **no single official "Antigravity skills pack" repo**. Official Google-org repos that ship skills or skill tooling:

| Repo | What it is |
|---|---|
| <https://github.com/google-antigravity/antigravity-sdk-python> | Official Antigravity SDK. Its `skills/google-antigravity-sdk/` is an Antigravity-authored skill (SKILL.md + `references/` + `examples/`); installable with `npx skills add Google-Antigravity/antigravity-sdk-python --skill google-antigravity-sdk`. Also the best source for context/budget/compaction mechanisms. |
| <https://github.com/google-antigravity/antigravity-cli> | Antigravity CLI (`agy`): README, changelog, statusline/title examples only; no skills. |
| <https://github.com/google/skills> | "Agent Skills for Google products and technologies": ~hundreds of `SKILL.md` skills (cloud, ads, analytics, health, home); `npx skills add google/skills`; has `index.json` catalog (~75 KB) and a "skill finder" skill. Not accepting external PRs. |
| <https://github.com/google/skill-reach> | Eval suite for skill routing, overlap and description optimization (Apache-2.0). Supports the Antigravity SDK/CLI as runtimes. |
| <https://github.com/google-labs-code/stitch-skills>, `jules-skills` | Google Labs skill sets for Stitch/Jules, Agent Skills standard. |
| <https://github.com/google-gemini/gemini-skills> | Skills for Gemini API/SDK (not read). |
| <https://github.com/google-deepmind/robotics-antigravity-plugin> | Robotics skills plugin (not read). |
| community: <https://github.com/rominirani/antigravity-skills> | Linked from Google's codelab; individual author, not Google. |

Docs: <https://antigravity.google/docs/skills>; codelab <https://codelabs.developers.google.com/getting-started-with-antigravity-skills>. The page <https://antigravity.google/docs/knowledge> returned 404, so Antigravity's planning artifacts (task list / plan / walkthrough) and knowledge/summary features are **unverified** here.

### 2.2 Context-economy mechanisms extracted

1. **Three-level progressive disclosure** (docs/skills and the Agent Skills spec at <https://agentskills.io/specification>): discovery shows only `name` + `description` every turn; activation reads full `SKILL.md`; execution loads `scripts/`, `references/`, `assets/` on demand. Spec numbers: metadata ~100 tokens per skill; body "< 5000 tokens recommended"; "keep your main SKILL.md under 500 lines"; description <= 1024 chars, name <= 64; references kept **one level deep**; "smaller files mean less use of context".
2. **Run scripts with `--help` first instead of reading their source** (Antigravity skills doc) - explicit guidance to keep source out of context.
3. **Routing table inside SKILL.md** (`google-antigravity-sdk/SKILL.md`): a short "if the user needs X, read `references/x.md`" table; the skill body itself stays an index.
4. **Catalog-on-demand finder** (`google/skills: skills/developers/finding-google-skills/SKILL.md`): "the catalog lives outside this file and is fetched on demand, so loading this skill costs almost nothing until a lookup actually happens". It tells the agent to narrow the ~75 KB `index.json` with `jq`/`grep` before reading, shortlist at most three, fetch only matches, and write large fetches to a local file because "re-reading a local file costs nothing".
5. **Built-in token guardrails as a reference doc** (`google/skills: skills/cloud/google-cloud-filestore-nfs-browser/references/token-safety-guardrails.md`): directory listing default `depth=2`, max 100 entries with `truncated=true` and a warning, read log tails (`--tail=50`), line slices (`--lines=1:100`), metadata before content, binary files return metadata only ("raw content suppressed to protect LLM context"). The same skill's SKILL.md says answer conceptual questions without tool calls.
6. **Budgets and compaction in the SDK** (`skills/google-antigravity-sdk/examples/getting_started/budget_limits.md`, `compaction.md`): `BudgetConfig` with `max_model_calls`, `max_tool_calls` ("proactively intercepts and aborts repeated or looping tool calls"), `max_input_tokens` evaluated before dispatch on **net uncached tokens (`prompt_tokens - cached_tokens`)**, `max_output_tokens`, `max_total_tokens`, scope LIFETIME or FORWARD_LOOKING; distinct `StopReason`s. `CompactionConfig(token_threshold=...)`: older turns evicted and replaced with a cumulative summary, recent turns verbatim.
7. **Server-side cursors** (`google/antigravity/hooks/README.md`, "Two complementary context systems"): a pagination tool keeps `next_page_token` in `ToolContext` so "the model [does not need] to remember it". **Decide hooks can return `modified_args`** to sanitize a tool call before execution; **transform hooks** (`OnToolErrorHook`) rewrite errors.
8. **Subagent scoping**: `max_subagent_depth`, `allowed_subagents`, per-subagent `enabled_tools` (`subagents.md`).
9. **Skill hygiene tooling** (`google/skill-reach` README): offline `lint` (description 20-1024 chars, kebab-case, name collisions with built-ins), `overlap` (BM25 competition between skill descriptions), `eval`/`sweep` for routing decay as the catalog grows, trajectory metrics including `--max-redundancy`.

Measured savings: none published for these in the repos (verified by reading; no benchmarks in the files I read).

---

## 3. Broader state of the art

### 3A. Provider prefix caching

**Anthropic** (<https://platform.claude.com/docs/en/build-with-claude/prompt-caching>, verified):
- Prefix order is tools, then system, then messages; cache covers everything up to the `cache_control` block. Invalidation table: changing tool definitions invalidates all three levels; system changes invalidate system+messages; tool_choice and images only messages.
- TTL 5 min (default, refreshed free on every hit) or 1 h; write multiplier 1.25x (5 m) / 2x (1 h); read 0.1x (0.05x for Opus 5.5 / Sonnet 5.5). Min cacheable 512-4,096 tokens depending on model. Lookback 20 positions per breakpoint, max 4 breakpoints.
- Rules: put the breakpoint on the last byte-identical block; per-request data (timestamp, incoming message) after it; keep key order stable in `tool_use` JSON (some languages randomize it); parallel requests only hit after the first response has started; pre-warm with `max_tokens: 0`.
- Claude Code's own account (<https://code.claude.com/docs/en/prompt-caching>, verified): order is system+tool definitions, then project context, then conversation; plan mode and skill loading **append** their instructions as messages so the prefix stays intact; "deferred" MCP tools keep the first request's tool list for the whole session so servers connecting later do not disturb the cache; compaction replaces history (cache rebuilt) but the summarization request itself repeats the system prompt/tools/history so it reads from a warm cache; switching model, effort (some models), fast mode, adding/removing a tool definition all invalidate; `/rewind` hits an already-cached earlier prefix; fan-outs of same-prefix agents hold all but the first up to 5 s so they hit the first agent's cache; `/usage` prints "Prompt cache (main): N requests, 91% from cache, 2 misses, 1 expected rebuild (compaction or tool-result clearing)" with a likely cause.

**DeepSeek** (<https://api-docs.deepseek.com/guides/kv_cache>, <https://api-docs.deepseek.com/quick_start/pricing/>, verified):
- Disk cache on by default, prefix match, units persisted at request boundaries, on common-prefix detection and at fixed token intervals; usage fields `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`; no TTL promise, "usually within a few hours to a few days"; best-effort, "does not guarantee a 100% cache hit rate".
- Prices per M tokens (off-peak / peak = 2x): deepseek-flash hit $0.003, miss $0.15, output $0.6; deepseek-v4-pro hit $0.022, miss $0.66, output $1.98; both 1M context. **A cache hit costs 2% (flash) / 3.3% (pro) of a miss.** Peak hours 01-04 and 06-10 UTC Mon-Fri.
- Illustrative arithmetic (mine, assumes the whole 2.36 B re-read tokens are one price class): flash $7 if all hits vs $354 if all misses; pro $52 vs $1,558. So hit rate, not raw token count, dominates cost; but a fat context still costs latency and quality.

**OpenAI** (<https://developers.openai.com/api/docs/guides/prompt-caching>, verified): automatic prefix caching, min 1,024 tokens (GPT-5.6+); stable instructions first, dynamic content last, append rather than rewrite; changes to tools (names, descriptions, schemas, ordering), `parallel_tool_calls`, `reasoning.effort`, or enabling compaction can change the prefix; reads 0.1x (0.05x on newest); `prompt_cache_key` for routing (~15 req/min per prefix before overflow routing).

What breaks caches (consistent across all three): timestamps or volatile state near the top, reordered/changed tool schemas, rewriting earlier turns (snip/prune/compaction), model/effort switches, unstable JSON key order.

### 3B. Tool-result hygiene

- Anthropic, "Writing tools for agents" (<https://www.anthropic.com/engineering/writing-tools-for-agents>, verified): paginate/filter/truncate with sane defaults; Claude Code caps tool responses at 25,000 tokens; a `response_format` enum (concise vs detailed) - Slack example 72 vs 206 tokens ("~1/3"); return high-signal names, not opaque IDs, but keep IDs available in the detailed mode; make truncation and error text steer the agent toward better calls.
- Claude Code (<https://code.claude.com/docs/en/tools-reference>, verified): Bash output inline up to ~30,000 chars, beyond that **saved to a session file with a 2,000-char preview and the path**; Read capped at 25,000 tokens per call and refuses files over 256 KB without paging.
- Anthropic "Code execution with MCP" (<https://www.anthropic.com/engineering/code-execution-with-mcp>, verified): filter in the execution environment before anything reaches the model; progressive tool discovery via a filesystem of tool files or a `search_tools` call with detail levels; example 150,000 to 2,000 tokens (98.7%), a single worked example, not a benchmark. Loops/polling run in code with `sleep`, not as model turns.
- Anthropic "Advanced tool use" (<https://www.anthropic.com/engineering/advanced-tool-use>, verified): tool search with `defer_loading` 77K to 8.7K tokens (-85%) and keeps system prompt/core tools cacheable; programmatic tool calling 43,588 to 27,297 tokens (-37%) on research tasks, ~200 KB of raw data reduced to ~1 KB.
- Delta/ETag style reads that already exist: Reasonix `bash_output` "Returns the output produced since the last bash_output call for that job ... Does not block" (`docs/TOOL_CONTRACT.md:42`); Reasonix `GET /history/page` has cursor bound to a session generation, append-preserving, plus **ETag** checked before body projection (`docs/HISTORY_PAGING.md`); MCP resource subscriptions send `notifications/resources/updated` carrying only the URI, the client then `resources/read`s (<https://modelcontextprotocol.io/specification/2025-06-18/server/resources>, verified); Antigravity `ToolContext` stores the next-page cursor server-side. Applied to LLM status tools specifically, I found no published measurement (unverified as a benchmarked technique).

### 3C. Polling replaced by blocking waits, events, coalescing

- Reasonix: `wait` blocks until background jobs finish and returns status + final output; `bash_output` is the non-blocking delta read; sub-agent progress is coalesced (250 ms merge, 32 events/s/group) and never enters the parent context (section 1.2 H).
- Claude Code `Monitor` tool: runs a command (or WebSocket) in the background and "feeds each output line back to Claude" so it reacts to events mid-conversation instead of poll loops (tools reference, verified from tool table text; exact throttling not read). Claude Code also caps idle goal check-ins at three per goal and offers `crossSessionInbound: hold` to stop cross-session messages from each starting a new full-context turn (<https://code.claude.com/docs/en/costs>, verified).
- MCP Tasks (SEP-1686, <https://modelcontextprotocol.io/seps/1686-tasks>): long-running call returns a task handle; client polls and "SHOULD respect the pollInterval"; status `input_required` for mid-run questions; draft spec moves tasks into an extension (verified via search summary, not full spec).
- Anthropic code-execution article: put the poll loop in code, not in the model loop.
- Published token savings for "blocking wait vs polling" in agent loops: none found (unverified).

### 3D. Compaction and memory strategies

| Strategy | Mechanism | Preserves tool-call correctness? | Numbers |
|---|---|---|---|
| Tool-result clearing (Anthropic `clear_tool_uses_20250919`) | clears oldest tool results past `trigger` (default 100,000 input tokens), `keep` last 3, `clear_at_least`, `exclude_tools`, optional `clear_tool_inputs`; placeholder text | Yes: tool_use blocks stay, results replaced by placeholders | 84% fewer tokens in a 100-turn web-search eval; with memory tool +39% task performance, editing alone +29% (<https://claude.com/blog/context-management>); docs warn clearing invalidates the cached prefix so use `clear_at_least` to make it worth it (<https://platform.claude.com/docs/en/build-with-claude/context-editing>) |
| Server-side compaction (`compact_20260112`) | provider summarizes; docs call it "the primary strategy for most use cases" | provider-managed | parameters not read (unverified) |
| Observation masking (JetBrains, arXiv 2508.21433, <https://arxiv.org/abs/2508.21433>) | omit older observations outside a window M | N/A (pairs kept, bodies masked) | roughly halves cost vs raw agent at equal solve rate vs LLM summarization on SWE-bench Verified; hybrid saves a further 7% vs masking, 11% vs summarization; window M is a tuned hyperparameter |
| OpenHands condensers (<https://docs.openhands.dev/sdk/arch/condenser>) | `LLMSummarizingCondenser`: when events > `max_size` (120), keep first `keep_first` (4), summarize the middle, target `max_size//2`; also NoOp, Pipeline | emits a `Condensation` event listing forgotten ids | no savings figures in the doc; masking `attention_window` default 5 is from a secondary source (unverified) |
| Claude Code auto-compact (<https://code.claude.com/docs/en/prompt-caching>) | summary via a request that repeats the prefix (warm-cache read); idle compaction; custom focus instructions | n/a | "a mid-session /compact costs a fraction of what the context size suggests" while warm; most expensive after cache expiry |
| Codex CLI | provider `POST /responses/compact` returns an opaque encrypted blob, local LLM summary for other providers; threshold `model_auto_compact_token_limit` | provider-managed | third-party write-ups only; verify against the repo (unverified) |
| Roo Code / Cline | condense at a threshold (Roo default 100%, forced at 75% after an overflow error, sliding-window fallback); Cline auto-compact summarizes near the limit | not documented | none |
| Reasonix | section 1.2 A, E | Yes (`closedPrefixEnd`, kept groups) | self-reported only |
| Letta/MemGPT (<https://docs.letta.com/guides/agents/architectures/memgpt>) | in-context labeled "core memory" blocks with char limits, archival/recall stores, sleep-time agent edits memory in the background | n/a | none (search-summary level, unverified details) |
| LangGraph/LangMem | not researched | | |

Key design lesson across sources: do cheap, deterministic reduction first (mask or clear old tool results), do LLM summarization rarely, make every cache-invalidating rewrite an explicit, logged, amortized event, and keep an escape hatch (folded-work index + bounded recall, or re-read the file).

### 3E. Repository maps and code indexes

- Aider repo map (<https://aider.chat/docs/repomap.html>, <https://aider.chat/2023/10/22/repomap.html>, verified): concise map of the git repo with key symbols and call signatures, graph-ranked by how often identifiers are referenced by other files; budget `--map-tokens` default 1k; resized dynamically and expanded when no files are in the chat. (The blog states tree-sitter is used for symbol extraction.)
- Reasonix `code_index` plus `lsp_*` tools; its `bash` description steers away from `cat` (section 1.2 D).
- Claude Code (<https://code.claude.com/docs/en/costs>): code-intelligence plugins so "a single go to definition call replaces a grep followed by reading multiple candidate files"; a project "codebase-overview" skill gives structure "immediately instead of spending tokens reading multiple files"; keep CLAUDE.md under ~200 lines, move workflow-specific text to skills; hooks can pre-filter test output (e.g. last 100 lines) before the model sees it.
- Already in this repo: `aoci.txt` is exactly a model-generated, incrementally maintained repo index (see its AGENTS.md); the measured 21-44K worker start-up context suggests workers still explore by `read`/`cd`/`ls` instead of being handed it.

### 3F. Multi-agent message economy

- Anthropic multi-agent research system (<https://www.anthropic.com/engineering/multi-agent-research-system>, verified): agents use ~4x, multi-agent ~15x the tokens of chat; token usage alone explained 80% of BrowseComp variance; recommended pattern: subagents "store their work in external systems, then pass lightweight references back to the coordinator" to avoid copying large outputs through conversation history; lead agent saves its plan to memory because context truncates beyond 200k; effort-scaling rules embedded in the prompt (1 agent/3-10 calls for simple, 2-4 agents/10-15 calls for comparisons, 10+ for complex) because early versions over-spawned (50 subagents for simple queries).
- Claude Code (<https://code.claude.com/docs/en/costs>, <https://code.claude.com/docs/en/sub-agents>): agent teams use ~7x tokens in plan mode; keep spawn prompts focused; each subagent loads CLAUDE.md/skills at start (`omitClaudeMd` opt-out; Explore/Plan agents skip CLAUDE.md and the git status snapshot); subagent descriptions over 15,000 tokens trigger a startup warning; cross-session messages are delivered as a new full-context turn when the session is idle, with `crossSessionInbound: hold` to batch.
- Reasonix: section 1.2 H (references + byte-offset paging, immutable spool, metadata-only manifests) and I (steer inbox).
- Shared artifacts on disk with pointers: both Anthropic's pattern and Reasonix's spill/subagent-reference pattern; Claude Code's Bash overflow file with preview is the same idea for tool output.

---

## 4. Candidate techniques for Parhelion

Ranked by expected saving relative to effort. "Saving" is qualitative unless a source gives a number; none of these were measured on Parhelion. Mapping uses our own measured waste.

### 1. One change-driven wait with a cursor, replacing polling (agent_wait, test_wait, status)
- **Idea:** a single `parhelion_wait(since=<rev>, until=any of {inbox, test, agent, judge}, timeout)` that blocks server-side and returns either `{changed:false, rev}` (a few dozen tokens) or only the deltas since `rev`; `status(if_rev=...)` answers `not_modified`. Coalesce bursts server-side (Reasonix: 250 ms merge, 32 events/s cap, `docs/SUBAGENT_PROGRESS.md:66-85`). When the same wait returns unchanged repeatedly, grow the timeout and tell the model what it is waiting for, so it stops issuing turns.
- **Sources:** Reasonix `wait`/`bash_output` (`docs/TOOL_CONTRACT.md:42,69`), MCP resources subscribe + read, MCP Tasks pollInterval, Claude Code Monitor, Anthropic code-execution (poll loop in code).
- **Maps to:** ~560 pure polling calls/hour (250 `agent_wait` + 200 `test_wait` + 110 `status`), plus 200 `agent_send` that batched delivery can also reduce; each is a full-context model call (at 136K context, one poll re-reads ~136K tokens, cheap per token on a cache hit but still latency and a turn of output). Highest expected saving; effort low-medium (server side only).

### 2. Make cache-hit rate a first-class metric, then fix prefix volatility
- **Idea:** record `prompt_cache_hit_tokens/miss` per call and per agent; port Reasonix's `CaptureShape`/`CompareShape` (system hash, tools hash, body chain, declared rewrite reasons, `cache_shape.go:38-150`) so every miss is attributed; keep the state card out of the prefix (append it as a trailing message, like Reasonix's recall suffix); keep MCP tool schemas byte-stable and registration order fixed; no timestamps in the system prompt; log every compaction/snip as an expected rebuild.
- **Why it matters here:** on DeepSeek a miss costs 30-50x a hit, so an unmeasured miss rate can swamp every other lever (arithmetic in 3A). Third-party claims of <20% vs 94% hit rates exist but are unverified for our harness.
- **Maps to:** the 2.36 B re-read tokens, compaction cache resets. Effort: low for telemetry, medium for fixes. Do this first or alongside #1, because it tells you which other items are worth doing.

### 3. Peer messages by reference, with a size gate and batched delivery
- **Idea:** `parhelion_squad` carries a structured envelope (`kind`, `subject`, <= ~10-line summary, key numbers, `artifact_id`); the multi-KB table lives on the platform as an artifact the recipient pulls with `parhelion_artifact(id, offset, limit)` (UTF-8 byte paging, like `read_subagent_result`). Platform rejects or auto-converts bodies above a threshold and rejects re-quotes of an existing artifact id. Deliver inbox items at the next natural turn boundary or as one digest instead of waking an idle agent per message (Reasonix steer inbox; Claude Code `crossSessionInbound: hold`).
- **Sources:** Anthropic multi-agent system (references instead of copies), Reasonix `read_subagent_result` and peermail/sessioninbox (metadata manifest vs blobs).
- **Maps to:** peer-message bloat, ~200 `agent_send`, orchestrator re-reading worker tables. Effort medium.

### 4. Worker boot pack: orientation delivered, exploration discouraged
- **Idea:** first message contains task contract, worktree path, a <=1-2K-token map from `aoci.txt`/symbol index (Aider-style, `--map-tokens` 1k), the five files that matter as paths with line ranges; tools take an explicit `workdir`/absolute paths so no `cd && cat` chains; instruct "use grep/ranged read, not cat of whole files" (Reasonix tool text). For the orchestrator, give bounded platform equivalents for what it currently `cat`s (worker file, log tail, diff stat) with paging and tails (Filestore guardrails: tail 50, ranges, default depth 2, 100-entry cap). Skip loading irrelevant instruction files for workers (Claude Code `omitClaudeMd` idea).
- **Maps to:** 21-44K worker start-up, 51 `cd`/`cat` calls on the orchestrator. Effort low-medium.

### 5. A single output gate for every platform tool result (spill, window, head+tail, tail-weighted on failure)
- **Idea:** one `bound_output()` in the MCP server: small result inline; oversized result stored under the run and replaced by a fixed-price pointer plus a small preview; paged tools windowed not snipped; failures keep the tail; never spill a read-back of a spill. Concise vs detailed `response_format` on `status`, `kb_query`, `test` digests (72 vs 206 tokens in Anthropic's example).
- **Sources:** Reasonix `tool_output_spill.go:228-258`, `prune.go:13-46`; Claude Code Bash overflow (30,000 chars inline, 2,000-char preview); Anthropic writing-tools post.
- **Maps to:** test digests (already 2 KB), `agent_wait` payloads, KB queries, any future long tool output. Effort medium.

### 6. Tiered compaction: mask old tool results first, summarize rarely, keep a recall path
- **Idea:** tier 1 observation masking/clearing in batches (keep last N results verbatim, placeholders for older ones, keep call/result pairs, `clear_at_least` so the cache rebuild pays for itself); tier 2 LLM summary only at a high watermark or an explicit phase boundary, request reusing the main prefix; summary includes a folded-work index and a budgeted `recall`; never cut inside an open tool-call group (`closedPrefixEnd`). Re-evaluate the 30% trigger: Reasonix folds at 0.90 and refuses a refold until 10% of the window of new closed history exists; Anthropic's clearing default triggers at 100,000 input tokens.
- **Sources:** arXiv 2508.21433 (about -50% cost, equal solve rate), Anthropic context editing (-84% tokens in a 100-turn eval, +29% performance), Reasonix `compact.go`, `compact_recall.go`.
- **Maps to:** workers peaking at 150-220K, repeated compaction cache resets. Effort medium-high (correctness risk), high payoff; run as an A/B against current compaction.

### 7. Budgets, guards and cheap models for non-reasoning turns
- **Idea:** per-agent `max_tool_calls`/`max_model_calls` and a net-uncached-input cap (Antigravity `BudgetConfig`); a `context_budget`-style tool or state-card field showing distance to compaction (Reasonix); identical-call storm suppression and "no-op echo" suppression (Reasonix `todo_echo.go`, perseveration guard); effort-scaling rules in the orchestrator prompt (Anthropic: size effort to task complexity); route summaries, wait/poll bookkeeping and exploration to the cheap tier at low effort (Reasonix 0.x), noting that model/effort switches fragment caches on some providers (Anthropic/OpenAI docs; DeepSeek behavior unverified).
- **Maps to:** runaway wait/send loops, call storms, aux-call cost. Effort low.

### 8. Progressive disclosure for contest prompts, KB and rarely used tools
- **Idea:** keep the orchestrator/worker system prompt short; contest-specific playbooks become on-demand docs with <=1024-char descriptions and one-level-deep references (Agent Skills spec: ~100 tokens metadata, <5000-token body, <500 lines); `kb_query` returns ids + one-line summaries, `kb_get(id)` returns bodies; measure the token size of our MCP tool schemas and, if large, expose rarely used tools behind a fixed proxy or tool search (Reasonix `use_capability`: lean 7,185-token core; Anthropic tool search -85% in its example, and it keeps the prefix cacheable). Lint descriptions for overlap (google/skill-reach).
- **Maps to:** 21-44K start-up, KB result size. Effort low-medium; saving depends on measured schema/prompt size (not yet measured).

### Not recommended / low confidence
- LLM summaries as the only compaction tier (the masking paper found no consistent advantage over simple masking).
- Rewriting old tool results aggressively without cache accounting (Reasonix 0.x turn-end shrink and Anthropic clearing both invalidate the prefix).
- Treating Reasonix/pi-reasonix hit-rate headlines (94%, 97.99%, "30x cheaper") as expected results for us; self-reported, different workload.

### Suggested sequencing
1. Telemetry for cache hit/miss and per-tool token cost (item 2) plus the delta/blocking wait (item 1); both are small and tell you the real distribution.
2. Boot pack and output gate (4, 5).
3. Peer-message references (3).
4. Compaction redesign as an A/B (6), then budgets/guards (7) and disclosure work (8) as measurements justify.

---

## Appendix: what I could not verify
- Which Google repo, if any, is the "recently open-sourced Antigravity skills" you meant; open-source dates (only `updatedAt` seen). Antigravity docs page on knowledge/artifacts was 404.
- Reasonix `MaxToolOutputBytes` value; 1.x TypeScript storm-breaker and tool-repair code (only second-hand via `pi-reasonix`); Reasonix performance numbers (self-reported).
- Codex compaction internals (third-party write-ups only), MCP Tasks final spec status, Anthropic server-side compaction parameters, LangGraph/LangMem, Cline exact thresholds.
- Any measured token saving for blocking waits vs polling or for delta-status tools in LLM agents.
- Cache behavior on DeepSeek when the tool list, model or reasoning effort changes mid-session.
