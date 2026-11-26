# Context compaction and agent performance: evidence, techniques, and a design for Parhelion

Date of research: 2026-11-26. Read-only research (web fetch, arXiv abs/html pages, `gh api` on public repos). Nothing was posted.

**Verification legend**
- **[V]** the primary page (arXiv abstract/HTML, vendor doc, repo file) was fetched this session and the number appears there. Numbers were extracted through a fetch summarizer, so re-check before quoting externally.
- **[S]** only a secondary write-up was available (blog, aggregator, search snippet).
- **[U]** from background knowledge, not re-verified this session.

---

## 0. Executive summary

1. Long context itself degrades models well before the window is full (RULER, NoLiMa, Chroma). Median peak context of ~349K tokens means Parhelion workers spend hours in the degraded regime before compaction, and the one-shot handoff summary is written by a model reading that same 350K.
2. Lossy compaction demonstrably drops the items that exist nowhere else: rules, constraints, decisions, "why" (2026 papers: Compaction Cliff, ConstraintRot, "Benchmarks Are Surprisingly Compaction-Friendly", cascading-compaction study). Where state is recoverable from disk (code, commits, reports), compaction is cheap and thresholds barely matter. Insights, hypotheses and refuted approaches are exactly the "exists nowhere else" class unless the platform writes them to disk.
3. Repeated summarization compounds loss (summary-of-summary). Three cascading compaction cycles kept 36.8% of facts vs 90.1% for full context (arXiv 2605.24657 [V]); one compaction preserved 53% of safety rules and five preserved 10% (arXiv 2608.22752 [V]).
4. For tool-output noise, simple observation masking matches LLM summarization at about half the cost (arXiv 2508.21433 [V]); LLM condensers add 24-94% token cost with no hypothesis-quality gain in a scientific-coding study (arXiv 2605.18854 [V]).
5. What works in practice (Anthropic memory tool and harness posts, Manus, Cursor, Karpathy's autoresearch loop, mempact, ACE, Factory): move state out of the context into typed files the platform controls, build the facts part of the handoff by code rather than by the model, keep recent turns verbatim, recite the plan at the end of context, retrieve old notes on demand, and validate the handoff against the ledger.
6. No source found measures "agent forgets optimization ideas after compaction" in kernel or performance-engineering work. Kernel-agent papers (AccelOpt, KernelBlaster, KernelClaw, KernelPro) all use persistent optimization memory but none ablate compaction. The Parhelion proposals in section 5 are an inference from adjacent evidence and must be tested with the replay probe in 5.8.

---

## 1. Evidence that long context and compaction hurt agents

### 1a. Long-context degradation (not compaction-specific)

| Source | Finding | Verification |
|---|---|---|
| Liu et al., "Lost in the Middle: How Language Models Use Long Contexts", arXiv 2307.03172 | Accuracy is highest when the relevant item is at the start or end of the input and degrades in the middle, even for long-context models. GPT-3.5-Turbo multi-doc QA, answer first / middle / last: 10 docs 76.8 / 61.2 / 62.4; 20 docs 75.8 / 53.8 / 63.2; 30 docs 73.4 / 50.9 / 63.7. Closed-book was 56.1, so middle-position accuracy fell below no-context in the 20- and 30-doc settings. Old models (2023). | [V] HTML tables |
| Hsieh et al. (NVIDIA), "RULER: What's the Real Context Size of Your Long-Context LMs?", arXiv 2404.06654 | Models near-perfect on plain needle-in-haystack still lose accuracy as length and task complexity grow. Of the models tested, only four (GPT-4, Command-R, Yi-34B, Mixtral) kept satisfactory performance at 32K despite claiming >=32K. | [V] abstract/search |
| Modarressi et al., "NoLiMa: Long-Context Evaluation Beyond Literal Matching", arXiv 2502.05167 (ICML 2025) | When the question has little lexical overlap with the needle, 11 of 13 models (all claiming >=128K) keep only about half of their short-context score at 32K. GPT-4o: 99.3 at short context to 69.7 at 32K. "Effective length" is defined as the longest length keeping >=85% of base score. Reasoning/CoT models also degrade. | [V] abstract |
| Chroma, "Context Rot: How Increasing Input Tokens Impacts LLM Performance" (July 2025), https://www.trychroma.com/research/context-rot | 18 LLMs (Claude Opus 4 / Sonnet 4 / 3.7 / 3.5, GPT-4.1 family, o3, Gemini 2.5, Qwen3). Lower needle-question similarity degrades faster with length; **even one distractor lowers accuracy and four compound it**; LongMemEval: ~113K-token full prompt vs ~300-token focused prompt, all models did significantly better on the focused prompt (Claude models showed the largest gap, via abstention). Surprising: shuffled haystacks beat logically structured ones in all 18 models. The report gives charts, not a headline % drop. Chroma sells a vector DB (pro-retrieval framing). | [V] |
| Laban et al., "LLMs Get Lost In Multi-Turn Conversation", arXiv 2505.06120 | 39% average drop vs single-turn across six tasks, 200K+ simulated conversations; mostly unreliability rather than lost aptitude. Models make early assumptions, answer prematurely, then over-rely on their earlier attempt: "when LLMs take a wrong turn... they get lost and do not recover." | [V] abstract; recap/snowball mitigation not verified |

Implication for Parhelion: distractors hurt even at short length, and a worker's history of 1,766 shell calls and 621 JSON-parsing scripts is mostly semantically-similar distractor text. Peer messages that quote each other add near-duplicate distractors.

### 1b. Measured loss from summarization / compaction (mostly 2026)

| Source | Finding | Verification |
|---|---|---|
| Zerhoudi et al., "The Compaction Cliff in Long-Running AI Agent Memory", arXiv 2608.22752 (CIKM 2026) | On 20 production agent configs, Claude Code's `/compact` prompt on Sonnet 4.6 preserved **53% of safety rules after one round, 10% after five**. Rules and episodic logs are compacted at the same rate though rules need exact wording. Fix "Knowledge Triage": classify each line by type and apply type-specific retention (TypeCompact in place, TypeDecompose, TypeRetrieve pinned). TypeCompact preserves 2-4x more rules than the best single-shot LLM compactor, 96% recall over five rounds. | [V] abstract |
| Dennis et al., arXiv 2605.24657, "Beyond Inference-Only Deployment: Comparing Weight-Based Consolidation Against Cascading Compaction" | Ten software-dev conversations, 1,146 questions: three cascading compaction cycles retained **36.8 +/- 3.0%** of knowledge (floor 11.8% no context, ceiling 90.1% full context). Weight consolidation (nightly LoRA) retained 80.4%. Biggest compaction losses on procedural corrections (36.3%) and episodic project facts (31.5%). | [V] abstract |
| Chen, "Governance Decay" (ConstraintRot), arXiv 2606.22528 | 1,323 episodes, seven model families: violations 0% with the policy in full context, **30% after compaction**, up to 59% for some models. If the constraint survives the summary: 0%; if dropped: 38%. "Constraint Pinning" (keep rules outside lossy compaction) restores 0%. | [V] abstract |
| Zha et al., "Do Agents Still Follow the Rules? ARCBench", arXiv 2611.03223 | Compaction rarely deletes the rule; most violations occur while the rule is still in the summary and the agent treats a later request as permission. Compacted runs that **keep** the rule violate it more than twice as often as full-history runs. A compactor-targeted attack more than doubles violations. Abstract is qualitative (no numbers). | [V] abstract |
| Salle et al., "Benchmarks Are Surprisingly Compaction-Friendly", arXiv 2610.31778 (v2 20 Nov 2026) | OpenCode scaffold, five compaction strategies, three frontier models, PaperBench / ProgramBench / PostTrainBench. For two 1M-window models the compaction threshold had no measurable effect; GPT-5.3-Codex (smallest window) did worse at lower thresholds. **No method beat default self-summarization**; methods that discard more cost some models up to 20 points. But on ProgramBench the **first summary dropped ~1/3 of anti-cheating rules, more with each compaction**; in real SWE-chat sessions instructions about how the agent should work / what to avoid were dropped **~6x more often** than specifications of what to build. Their explanation: benchmarks are compaction-friendly because the goal is fixed and progress sits on disk, so most discarded content is recoverable. The exception is constraints that exist nowhere else. | [V] abstract |
| Factory, "Evaluating Context Compression for AI Agents" (Dec 2025), https://factory.ai/news/evaluating-compression | Probe-based eval (recall, artifact, continuation, decision probes; six judged dimensions) over ~36K production messages. Overall 3.70 (Factory structured, anchored iterative summary) vs 3.44 (Anthropic) vs 3.35 (OpenAI compact); accuracy 4.04 / 3.74 / 3.43. All methods weak on artifact trail (which files were created/modified/read): 2.19-2.45 / 5. Compression ratios 98.6-99.3%. Vendor-run, self-evaluated. | [S] via ZenML summary; Factory page fetch failed twice |
| topmass/mempact (GitHub), README | Replays 36 real Claude Code sessions with a 64K window, up to five compactions, local Qwen model as writer and judge. Fact ledger built by code + checked narrative: ~20% more probe questions answered than the pi harness's compaction (+11 points pooled over four runs, 95% CI 7-15; wins 29/36 sessions); ledger-alone and the Codex prompt did worse (-9 and -19 points vs mempact). Accuracy still decays across rounds (mempact 72% to 49% over five rounds, pi 55% to 47%); lead shrinks to 2 points by round five. Caveats stated by author: one judge, one user's sessions, pi wrote 1/4 as much summary. | [V] README (self-reported, small) |
| Chintalapati et al., "Evaluating Memory Condensation Strategies for Coding Agents in Data-Driven Scientific Discovery", arXiv 2605.18854 | Eight condensers, GPT-4o, 60 DiscoveryBench tasks (480 runs): **no condenser significantly changed hypothesis quality; LLM-based condensers raised token cost 24-94%; masking tool-call outputs saved 8.6%**. Best condenser varies by domain and task length. | [V] abstract |
| Zhang et al., ACE, arXiv 2510.04618 (ICLR 2026) | Names two failure modes of rewriting a context with an LLM: **brevity bias** (collapse toward short generic text, dropping domain heuristics and failure modes) and **context collapse** (AppWorld case: context 18,282 tokens at step 60 with accuracy 66.7, then 122 tokens at the next step with accuracy 57.1, below the 63.7 no-adaptation baseline). | [V] HTML |
| Lindenbauer et al., "The Complexity Trap", arXiv 2508.21433 | LLM-Summary makes trajectories longer: Gemini 2.5 Flash 52 mean turns vs 44 for masking (+15%); Qwen3-Coder 480B +15% vs the raw agent and +13% vs masking. The paper argues summaries mask stop/failure signals and encourage the agent to keep going. | [V] HTML |

### 1c. Counter-evidence and caveats (be honest)

- The compaction-friendly paper (2610.31778) found threshold and method often do not matter when progress lives on disk. Parhelion's code, commits and test reports are on disk; its hypotheses and insights are not. That is the whole case for a ledger.
- ARCBench (2611.03223): keeping text in the summary is not enough; the agent must still act on it. The post-compaction seed must end with an action (select an open hypothesis, run a recall), not only context.
- Reasoning can be safely dropped when state is externalized: ICLR (Interaction-aware Compression for Long-horizon Reasoning), arXiv 2609.29875, removed low-value reasoning blocks (kept actions, tool calls, observations) and got reward 0.699 to 0.718 with input/output/cache-read tokens down 25.5% / 14.4% / 33.3% on 260 WorkBuddyBench tasks; "trajectory amplification" (deleting reasoning can change later behavior nonlinearly) is the risk. [V]
- Dead-end memory can anchor the agent. One kernel-benchmark paper (arXiv 2607.14541) reportedly uses "optimization dropout" to escape stalled search contexts [V abstract mentions dropout only; the fresh-sub-agent claim was not supported by the page]. Store refuted approaches with scope and `revisit_if`, not as blanket bans.

---

## 2. Papers on agent memory and context management

For each: mechanism, what is kept vs dropped, reported numbers.

**Retrieval / paging memories**
- **MemGPT** (Packer et al.), arXiv 2310.08560 [V abstract only]. OS-style virtual context: a bounded main context plus external storage; the agent moves data with function calls; interrupts manage control flow. Evaluated on document analysis beyond the window and multi-session chat. Kept: whatever the agent pages in. Dropped: whatever it evicts (to external store, so recoverable). The mechanism details (memory-pressure warnings, recursive summary) are from background knowledge [U].
- **Generative Agents** (Park et al.), arXiv 2304.03442 [V abstract]. Memory stream of natural-language experiences; retrieval plus synthesised higher-level "reflections"; ablations show observation, planning and reflection each matter (no numbers on the abstract page). Retrieval score = recency + importance + relevance [U].
- **A-MEM**, arXiv 2502.12110 [V abstract]. Zettelkasten-style notes (description, keywords, tags), automatic links between notes, and "memory evolution" (a new note updates attributes of old ones). Gains over baselines on six foundation models; no numbers on the abstract page.
- **Mem0**, arXiv 2504.19413 [V abstract]. Extracts and consolidates salient facts; on LOCOMO 26% relative gain on LLM-as-judge vs the OpenAI memory baseline, 91% lower p95 latency and >90% token-cost savings vs full-context.
- **ReadAgent**, arXiv 2402.09727 [V]. Paginate, compress each page to a "gist", look up the original page when details are needed; effective context up to 20x; beats retrieval and gist-only baselines on QuALITY, NarrativeQA, QMSum. Lesson: gist + pointer to the source, not gist alone.
- **LongMem** (arXiv 2306.07174): not fetched [U]; architectural side-network memory, not relevant to black-box agents.

**Learning from experience (closest to "do not repeat dead ends")**
- **Reflexion**, arXiv 2303.11366 [V]. After a failed attempt the agent writes a verbal reflection into an episodic buffer used on the next trial; 91% pass@1 on HumanEval vs 80% for GPT-4.
- **Voyager**, arXiv 2305.16291 [V]. Skill library of executable, verified code retrieved for new tasks, plus automatic curriculum and iterative self-verification: 3.3x more unique items, 2.3x longer distances, 15.3x faster tech-tree milestones vs prior SOTA.
- **Dynamic Cheatsheet**, arXiv 2504.07952 [V]. Persistent, self-curated memory of strategies/snippets at inference time: GPT-4o Game of 24 from 10% to 99% after discovering and reusing a Python solution; Claude 3.5 Sonnet AIME accuracy more than doubled; +9% GPQA-Diamond, +8% MMLU-Pro (Claude).
- **ReasoningBank** (Google), arXiv 2509.25140 [V abstract]. Distills strategies from both successes **and failures** (self-judged) into memory items, retrieved at test time and written back; beats raw-trajectory memory and success-only routines; MaTTS adds scaling. Numbers not on the abstract page.
- **ACE: Agentic Context Engineering**, arXiv 2510.04618 [V]. Generator / Reflector / Curator. Context is an itemized playbook of bullets with id plus helpful/harmful counters; the Curator emits small **delta** entries merged by non-LLM logic ("grow-and-refine": append, update in place, semantic de-dup, run lazily when the window is exceeded). Explicitly avoids whole-context rewrites to prevent brevity bias and collapse. AppWorld (DeepSeek-V3.1 base 42.4): +17.0 (59.4) with labels, +14.8 without labels, matching the top production agent on average (59.4 vs 60.3); FiNER/Formula +12.8 offline; headline +10.6% agents, +8.6% finance.
- **ACON**, arXiv 2510.00615 [V abstract]. Optimizes natural-language compression guidelines from analysis of **cases where compression broke the agent** (no fine-tuning); 26-54% peak-token reduction on AppWorld, OfficeBench, multi-objective QA while preserving accuracy; compressor distilled into small models; small LMs gain up to 46% from reduced distraction.
- **Sleep-time compute**, arXiv 2504.13171 [V]. Precompute useful inferences about a context offline: ~5x less test-time compute for the same accuracy on Stateful GSM-Symbolic / AIME; up to +13% / +18% by scaling sleep-time compute; 2.5x cost reduction per query when amortized over related queries; gain tracks how predictable the queries are.

**Context folding / trained context managers**
- **Context-Folding** (Sun et al.), arXiv 2510.11967 [V abstract]. Branch into a sub-trajectory for a subtask, then fold it, keeping only a short outcome summary; trained with FoldGRPO; ~10x smaller active context than ReAct with equal or better results on deep-research and SWE tasks; beats summarization-based management.
- **AgentFold**, arXiv 2510.24699 [V abstract]. Per-step folding at two scales: granular condensation of recent steps, deep consolidation of whole sub-tasks. AgentFold-30B-A3B: 36.2% BrowseComp, 47.3% BrowseComp-ZH with plain SFT, beating DeepSeek-V3.1-671B and o4-mini.
- **MEM1**, arXiv 2506.15841 [V]. RL-trained constant-memory agent that rewrites one internal state per turn; 7B model 3.5x performance and 3.7x lower memory than Qwen2.5-14B on a 16-objective multi-hop task.
- **MemAgent**, arXiv 2507.02259 [V]. RL-trained overwrite memory; 8K-trained model extrapolates to 3.5M-token QA with <5% loss; >95% on RULER 512K.
- **CAT / SWE-Compressor** (Context as a Tool), arXiv 2512.22087 [V abstract]. Context maintenance is a callable tool; context split into stable task semantics, condensed long-term memory, detailed short-term interactions; 57.6% on SWE-Bench Verified.
- **RECAP**, arXiv 2611.31258 [V abstract]. RL objective that a summary should preserve the *behavior* of the original history ("behavior distortion"); +4.6% BrowseComp-plus and +4.3% SWE-bench Verified on Qwen3.5-4B over task-reward-only RL.
- These trained approaches are out of reach for black-box DeepSeek/Claude/Codex workers; the transferable ideas are (a) fold finished sub-tasks into one outcome line, (b) judge a summary by whether behavior after it is unchanged (the probe test in 5.8).

**Kernel / accelerator optimisation agents with persistent memory (domain match)**
- **AccelOpt**, arXiv 2511.15915 [V]. AWS Trainium NKI kernels; an **optimization memory** stores slow-to-fast kernel pairs and LLM-written summaries of the insight; peak-throughput share 49% to 61% (Trainium 1) and 45% to 59% (Trainium 2); matches Claude Sonnet 4 improvements with open models at ~26x lower cost.
- **KernelBlaster**, arXiv 2602.14293 [V]. Persistent CUDA knowledge base of lessons from past runs for in-context RL; geomean speedup 1.43x / 2.50x / 1.50x on KernelBench L1/L2/L3.
- **KernelClaw**, arXiv 2603.10085 [V]. Dual memory: long-term expert skills plus **short-term memory that prevents repetitive backtracking**; 100% success and 6.98x / 7.89x / 1.94x over Torch eager on L1-L3.
- **KernelPro**, arXiv 2606.26453 [V abstract]. MCTS with dead-end pruning and "search memory for cross-iteration learning"; MCTS vs greedy +26% geomean (p=0.004); micro-profiling tools turn raw counters into text guidance.
- None of these report how memory survives a context reset. They show that typed memory of slow-to-fast pairs, dead ends and skills is standard in this domain.

**Anthropic**
- **"Effective context engineering for AI agents"** (Sept 2025), https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents [V]. Compaction (keep architectural decisions, unresolved bugs, implementation details; discard redundant tool output; tune by maximizing recall first, then precision; warns aggressive compaction loses subtle context whose importance shows up later); tool-result clearing as "one of the safest, lightest-touch forms of compaction"; structured note-taking (NOTES.md, todo list; Pokémon agent keeps tallies and notes on what worked against which opponents across context resets); sub-agents returning 1,000-2,000-token summaries after tens of thousands of tokens of work; just-in-time retrieval via lightweight identifiers.
- **Context editing + memory tool** announcement, https://claude.com/blog/context-management [V]. Internal agentic-search eval: memory tool + context editing +39%, context editing alone +29%; 100-turn web search: 84% fewer tokens. Context editing clears stale tool calls/results; memory tool is client-side file storage.
- **Context editing API** (platform docs) [V]: `clear_tool_uses_20250919` with `trigger` (default 100K input tokens), `keep` (default last 3 tool uses), `clear_at_least` (so a clear is only applied when worth breaking the prompt cache), `exclude_tools` (never clear these tools' results), optional `clear_tool_inputs`; `clear_thinking_20251015`.
- **Memory tool docs** [V]: auto-injected protocol "ALWAYS VIEW YOUR MEMORY DIRECTORY BEFORE DOING ANYTHING ELSE... ASSUME INTERRUPTION: your context window might be reset at any moment, so you risk losing any progress that is not recorded in your memory directory." Recommends pairing memory with compaction ("compaction keeps the active context small... memory preserves the information that must survive summarization") and a multi-session pattern: initializer session creates progress log + feature checklist; each session starts by reading them and ends by updating them.
- **Compaction API docs** [V]: custom `instructions` (up to 16,384 characters) replace the default prompt entirely ("when the default summary drops something a later turn needs"); "keep recent turns word for word" variant; **"Compact again: the new block summarizes the old summary and everything after it"** (the cascade the 2026 papers measure). Default SDK summary sections: Task Overview, Current State, Important Discoveries, Next Steps, Context to Preserve.
- **"Effective harnesses for long-running agents"**, https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents [V]. Initializer + coding-agent roles; `claude-progress.txt` + git history; **JSON feature list with a `passes` field that workers may only flip** (JSON chosen because models edit JSON less carelessly than Markdown); startup routine: pwd, read git log and progress file, read feature list and pick the highest-priority incomplete item, run a smoke test; failure modes fixed (declaring done early, undocumented progress, marking features done without testing).

---

## 3. Community and product approaches

**Claude Code** (official docs, https://code.claude.com/docs/en/context-window and /memory) [V]
- Auto-compaction replaces the conversation with a structured summary. After compaction: CLAUDE.md, auto memory (`MEMORY.md`, first 200 lines / 25KB), unscoped rules, plan-mode plan are **re-injected from disk**; up to **five most recently modified files are re-read** (a file over 5,000 tokens comes back as a path reference); invoked skill bodies re-injected, capped at 5,000 tokens per skill and 25,000 total; SessionStart hooks matching `compact` can add output to the compacted context; **context that hooks added earlier is summarized away with the rest**; path-scoped rules are lost until re-triggered.
- `/compact <focus instructions>` makes "the summary keep what you choose instead of what the automatic pass guesses is important"; `/autocompact <tokens>` sets how full before compacting; `/rewind` "Summarize from here / up to here"; subagents keep large reads out of the main context (example: 9,890 tokens spent in the subagent, 420-token summary returned); example session 22,225 to 17,095 tokens after `/compact`, with the conversation summary only 1,725 tokens.
- Community PreCompact-hook tools write the transcript or a ledger before compaction (e.g., posts and issue #43733 requesting reliable session-state persistence) [S]. Their existence is evidence of the same complaint.

**Codex CLI** (source, https://github.com/openai/codex, `codex-rs/prompts/templates/compact/prompt.md`, `codex-rs/core/src/compact.rs`) [V]
- Local compaction prompt: "You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task. Include: current progress and key decisions made; important context, constraints, or user preferences; what remains to be done (clear next steps); any critical data, examples, or references needed to continue. Be concise, structured..." The next model sees a prefix saying another model started the problem and "avoid duplicating work."
- The rebuilt history keeps **recent user messages verbatim up to `COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000`** plus the summary. In the part of the source read, assistant text and tool output are not carried beyond the summary (a `compact_retained_tool_outputs.rs` exists and was not read). Remote compaction (`compact_remote_v2*.rs`) is used when the provider supports it, otherwise the local path above [V from `tasks/compact.rs` capability switch; which path Parhelion's Codex workers hit was not verified].
- Consequence: in Codex threads the platform's own turns and multi-KB peer notes (user-role messages) survive compaction, while the worker's own insights (assistant-role text) are only as good as the summary.

**OpenHands condensers** (https://github.com/OpenHands/software-agent-sdk, `openhands-sdk/openhands/sdk/context/condenser/`) [V]
- `LLMSummarizingCondenser`: keeps the first `keep_first` events (default 2; head and system prompt protected), summarizes a forgotten middle range into a rolling `Condensation` event, keeps a tail sized by the strictest of event-count, request-count or token constraints (about max_size//2), refuses condensations that forget <10% (`minimum_progress`), retries with shrinking event-string length on failure. Defaults seen: `max_size` 240 (`default_condenser` 80). Other condensers in the family include observation masking, recent-events, amortized forgetting, structured-summary and pipeline chaining [S; the docs page 404ed, listing is from search results].
- OpenHands' April 2025 blog reports ~54% vs ~53% solved with condensation vs baseline and a large per-turn cost drop [S snippet; blog not fetched].
- **JetBrains / "The Complexity Trap"** (arXiv 2508.21433 [V]): SWE-agent on SWE-bench Verified (n=500), rolling observation window M=10 vs LLM summary (summarize N=21 turns, keep last 10). Qwen3-Coder 480B: raw 53.4% / $1.29; masking 54.8% / $0.61 (-52.7% cost); summary 53.8% / $0.64. Gemini 2.5 Flash: raw 32.8%; masking 35.6%; summary 36.0%. Gemini 2.5 Flash (thinking): raw 40.4%; masking 36.4%; summary **31.4%** (summary worse). Summary calls cost 0.65-7.2% of total; trajectory elongation +13-15% with summary. A hybrid of masking and summarization (mechanics not described on the page; tuned N=43, M=10, tested on 50 instances) was another -7% cost vs masking and -11% vs summary; the untuned N=21 setting was worse. Masking is not free: for thinking Gemini it lost 4 points vs raw.

**Manus, "Context Engineering for AI Agents: Lessons from Building Manus"** (https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus) [V]
- KV-cache hit rate is "the single most important metric": cached input $0.30/MTok vs $3/MTok uncached for Claude Sonnet; average input:output ratio ~100:1; keep the prefix stable, append-only context, deterministic JSON serialization.
- Mask tools via logit constraints instead of removing them mid-task.
- File system as context with **restorable compression**: drop a page body but keep its URL, drop a document but keep its sandbox path.
- **Recitation**: a `todo.md` rewritten as the task progresses (about 50 tool calls per task) pushes the plan to the end of context against lost-in-the-middle and goal drift.
- **Keep failures in context**: leaving failed actions and stack traces lets the model update its beliefs and avoid repeating them.
- Beware few-shot ruts: repetitive histories make the agent imitate its own pattern; add controlled variation.

**Cursor, "Dynamic context discovery"** (https://cursor.com/blog/dynamic-context-discovery) [V]
- Long tool outputs are written to files and the agent reads the tail on demand instead of truncating; when the window fills, the agent gets a **reference to the stored chat history** because the summary is "a lossy compression", and it can search the file to recover details; MCP tool descriptions are synced to folders and looked up on demand (A/B: -46.9% total tokens in runs that called an MCP tool); terminal sessions synced to files for grep.

**Cline Memory Bank** (https://docs.cline.bot/features/memory-bank) [V]. Convention of six Markdown files in `memory-bank/`: `projectbrief.md`, `productContext.md`, `activeContext.md` (current focus, recent changes, next steps, active decisions, learnings; changes most), `systemPatterns.md`, `techContext.md`, `progress.md` (what works, what is left, known issues, how decisions evolved). Custom instructions make the agent read all files at the start of every task; update on milestones or "update memory bank". No measured results given. Community forks (e.g., `alioshr/memory-bank-mcp`, 926 stars, MCP server variant) [V from `gh search`].

**Aider repo map** (https://aider.chat/docs/repomap.html) [V]. Graph-ranked symbol map fitted to a token budget (`--map-tokens`, default 1K, expands dynamically when no files are in the chat): a cheap, regenerable "map" that points to files, instead of carrying file bodies. Same idea as pointer-plus-regeneration.

**Karpathy `autoresearch`** (https://github.com/karpathy/autoresearch, `program.md`) [V]. Closest structural match to Parhelion's loop: fixed 5-minute experiment, agent edits one file, **commits, runs with all output redirected to `run.log` ("do NOT use tee or let output flood your context"), greps only the metric**, appends one row to an untracked `results.tsv` (`commit, val_bpb, memory_gb, status keep|discard|crash, description`), keeps the commit if improved else `git reset`, loops forever; crash handling "tail -n 50"; the agent reads recent `results.tsv` before proposing a hypothesis (per secondary write-ups [S]). The ledger is one line per experiment and the log lives outside the context.

**Letta / LangMem**
- LangMem concept guide (https://langchain-ai.github.io/langmem/concepts/conceptual_guide/) [V]: memory types semantic (facts), episodic (past experiences, few-shot), procedural (how the agent behaves); stored as a profile (single current-state document, schema-driven) or a collection (many documents, harder to keep consistent); formed either on the hot path (immediate, adds latency, the agent decides) or in the background (reflect after the conversation, no latency, delayed).
- Letta docs page returned was about a Letta Code "dreaming" feature (background sub-agents review recent conversations and update memory after N steps or on compaction) [V, but the page did not match the older "sleep-time agent" blog; original Letta sleep-time agent and MemGPT block docs not verified].

**Cognition (Devin), "Don't Build Multi-Agents"** [S]. Argues for a single agent with context compression because actions carry implicit decisions that parallel agents cannot share; mentions fine-tuning a small model as the compressor. Direct quote not verified. Relevant to peer messaging: sharing *decisions* matters, sharing transcripts does not.

**Not researched in depth:** Windsurf rules/memories, Cursor rules files, Roo Code memory bank variants, LangGraph checkpointers. Rules-file mechanisms (CLAUDE.md, `.cursorrules`) are static instruction pinning, equivalent to Constraint Pinning; they do not hold run-time insights.

---

## 4. Techniques for preserving insight across compaction (synthesis)

Each technique lists the source that supports it and what failure it addresses.

1. **Externalize to typed files the platform owns; inject by pointer plus short digest.** Anthropic memory tool and harness posts [V]; Manus file system; Cursor history file; ACE itemized bullets; mempact `.mempact/memory.md` + artifacts. Cure for: forgotten insights, repeated dead ends.
2. **Build facts by code, narrative by model.** mempact ledger from original messages "never from an earlier summary, so it cannot decay"; Compaction Cliff TypeCompact; ConstraintRot Constraint Pinning (0% violations vs up to 59%). Cure for: measured per-case numbers, SHAs, scores being misremembered.
3. **Never summarize a summary.** Build handoff N+1 from the ledger and raw tail, not from handoff N. Evidence: 36.8% after three cascades (2605.24657), 53% to 10% over five rounds (2608.22752), Anthropic "compact again" semantics. Cure for: slow erosion across repeated compactions (each Parhelion worker runs for hours).
4. **Typed retention policy.** Constraints and measurements pinned exact; hypotheses kept verbatim until resolved; failures kept as one compressed line with why/scope; narrative summarized freely (Compaction Cliff; LangMem semantic/episodic/procedural; ReasoningBank success + failure; Voyager verified skills only).
5. **Delta updates, not rewrites.** ACE (non-LLM merge of small deltas, helpful/harmful counters, de-dup) avoids brevity bias and collapse (18,282 to 122 tokens in the AppWorld case study).
6. **Write at event time, not at compaction time.** The handoff is produced when context is largest and attention is worst (NoLiMa, Chroma). Memory-tool protocol "assume interruption"; Reflexion writes after each failure; autoresearch logs after each run; hot-path vs background in LangMem.
7. **Keep recent turns verbatim.** Codex (20K tokens of user messages), Anthropic keep-recent-turns API, Claude Code re-reads the last five files, mempact raw tail plus rehydrated files.
8. **Recitation at the end of context.** Manus `todo.md`; Claude Code todo list; Anthropic harness progress file read at session start. Cure for goal drift and "lost in the middle" of the open-hypothesis list.
9. **Observation masking for old, regenerable tool output; summarize only reasoning that is not otherwise stored.** Complexity Trap (-50% cost, same solve rate), 2605.18854 (+8.6% saving vs +24-94% cost), Anthropic `clear_tool_uses` with `exclude_tools` and `clear_at_least`, ICLR 2609.29875 (reasoning replaceable once state is externalized), mempact stubs old tool outputs past 63% of the window and cuts outputs over 12,000 bytes saving them whole as artifacts. Always leave a pointer ("restorable compression", Manus).
10. **Redirect large outputs to files and read a slice.** autoresearch (`> run.log`, grep the metric), Cursor (write output to file, `tail`), mempact artifacts. Cure for: shell-call and JSON-parsing noise.
11. **Keep errors and failures visible** (Manus; Reflexion; KernelClaw short-term memory against repetitive backtracking) but scoped, with a way to retry when conditions change.
12. **Retrieve on demand** (ReadAgent gist + page lookup, MemGPT paging, A-MEM links, mempact `recall` over the full pre-compaction history, Cursor searchable history file). Cure for: dropped details that become relevant later.
13. **Verify the handoff.** (a) Deterministic schema checks: all headings present, next step names a real file/command (mempact `summarySchema.ts`), referenced SHAs exist; (b) carry-forward: items in the previous narrative or ledger that the new narrative dropped are appended back (mempact `carry.ts`); (c) behavioral probes: after compaction ask canned questions and grade against ground truth (Factory probe types: recall, artifact, continuation, decision; mempact replay benchmark; RECAP's "behavior distortion" idea); (d) ACON-style: collect cases where compression caused a mistake and refine the compression instructions from them.
14. **Schema-constrained, hard-to-corrupt state file.** Anthropic harness: JSON, not Markdown, and workers may only flip a `passes` field. Cure for: the model rewriting history or marking things done.
15. **Isolate noisy exploration in sub-contexts and return a short outcome.** Anthropic sub-agents (1-2K token returns), Context-Folding, Claude Code subagent example. Parhelion workers cannot spawn arbitrary sub-agents under the global rules, but the platform can offer a "run this analysis, return a table" tool, which has the same effect.

---

## 5. What this means for Parhelion

### 5.1 Where the current flow loses information (from the code)

Files: `cli/opsb_core/agents/adapters.py` (ACP `compact`, ~line 230; Codex `compact`, ~line 356), `cli/opsb_core/agents/manager.py` (`compact_threshold` = 30% of window clamped to 120K-400K, `COMPACT_PERCENT = 70`, `CHECKPOINT_MAX_CHARS = 4000`, `SQUAD_MAX_CHARS = 4000` for peer notes, `_should_compact`, `_checkpoint`, `_compact`, `CHECKPOINT_TAIL`), `cli/opsb_core/services/state_card.py` (<=2000 bytes: best score, head SHA, last three receipts, public per-case gap for five cases).

| Observation | Why it loses ideas / repeats dead ends | Evidence |
|---|---|---|
| ACP handoff = one prompt, "Write a short handoff" to the same model at ~350K context | "Short" invites brevity bias; the summarizer reads a context where mid-context notes are least reliably used; no list of hypotheses or refuted approaches is forced into existence | ACE brevity bias; NoLiMa/Chroma; Compaction Cliff |
| Seed = model summary + checkpoint capped at `CHECKPOINT_MAX_CHARS = 4000` (last stage result + state card + "Do not re-read files you already summarized"). `_checkpoint` gives the stage result only the room left after the card (about 2,000 chars) and keeps its **first** `room` characters (`stage[:room]`), so the end of a long stage result, where conclusions usually sit, is cut first | State card holds scores and receipts only. No insights, no failed approaches with reasons, no per-case measurement history beyond the last five public gaps. "Do not re-read" suppresses the recovery path | Benchmarks-compaction-friendly: constraints (and here, insights) that exist nowhere else are what is lost |
| Next compaction summarizes (previous summary + new work) | Cascading erosion | 2605.24657, 2608.22752 |
| Codex native compaction keeps recent user-role messages up to 20K tokens, drops assistant reasoning (local path) | Platform turns and peer notes persist; worker's own insights do not | `compact.rs` [V] |
| No mechanism writes an insight at the time it is found | Insights live only in assistant text at arbitrary depth in a 350K context | Memory-tool "assume interruption"; Reflexion |
| Tool noise: 1,766 `cd && sed/grep/git/cat`, 621 ad-hoc JSON-parsing scripts | Fills the window with regenerable, near-duplicate text; each script re-derives the same per-case view the platform already has | Chroma distractors; Complexity Trap; autoresearch redirect pattern |
| Peer messages of several KB quoting each other | Near-duplicate distractors; they survive Codex compaction as user messages while worker insight does not | Chroma; `compact.rs`; Manus few-shot rut |

### 5.2 Ranked designs (benefit vs effort)

| # | Design | Fixes | Benefit | Effort | Main evidence |
|---|---|---|---|---|---|
| 1 | **Per-worker experiment/insight ledger** (append-only JSONL under the problem workspace, typed entries, section 5.3) written through one MCP tool, with platform auto-filled measurement and experiment rows; digest injected into every seed | forgotten ideas, repeated dead ends | Very high | M (one file format, `parhelion_note` + `parhelion_recall`, digest builder; reuse the receipts and test-run records you already store) | Anthropic memory/harness, ACE, Compaction Cliff, autoresearch, kernel-agent memories |
| 2 | **Hook ledger writes to existing workflow events.** `parhelion_test` result prompts a one-line takeaway; `parhelion_submit_gate` requires a `hypothesis_id` and `predicted_effect`; the judge receipt callback auto-resolves the hypothesis (confirmed/refuted/inconclusive) with per-case deltas | forgotten ideas, repeated dead ends; makes ledger a by-product of work, not a chore | Very high | S-M | autoresearch (one row per experiment), Reflexion, Anthropic harness (progress file updated at each step) |
| 3 | **Handoff validator with carry-forward.** Handoff must reference every OPEN ledger id by status; platform diffs the handoff against the ledger, appends back anything dropped, checks SHAs exist, asks once more if the schema is unfilled. Seed built from ledger + raw tail, never from the previous handoff | forgotten ideas, cascading erosion | High | S | mempact `summarySchema` + `carry`, Compaction Cliff, 2605.24657 |
| 4 | **Replace ad-hoc parsing with platform views and cap outputs.** Cluster the 621 scripts to find the 5-8 views workers keep rebuilding (per-case table, diff between SHAs, bucket summary, noise band, best-so-far) and expose them as `parhelion_report(view=...)`; cap tool output (e.g., 4-6 KB), spill the rest to a file and return a path plus head/tail | noisy tool output | High | M | autoresearch, Cursor, mempact artifacts, Manus restorable compression |
| 5 | **Peer-message protocol.** Structured note: `{refs:[ledger ids], claim<=280 chars, evidence: path#anchor, ask?}`; reject or truncate >1,200 chars; strip quoted prior messages by overlap check; write the note into the recipient's ledger as `source=peer, confidence=unverified`; deliver only id plus claim into context | peer bloat, plus gives teammates' insights the same durable home | Medium-high | S | Chroma distractors, Cognition (share decisions, not traces), Codex retains user messages |
| 6 | **Recitation footer.** `parhelion_test` / receipt tool results append a <=600-char digest: open hypotheses by priority, last refuted, next planned experiment | goal drift, forgotten open hypotheses between compactions | Medium | S | Manus todo.md |
| 7 | **Episode-boundary compaction.** Prefer compacting at judge-receipt or stage boundaries (no open tool call, hypothesis just resolved), and reseed from the ledger sooner (for example at 60-70% of the current threshold when a boundary appears) instead of waiting for 349K. Keep the threshold as a ceiling | cleaner cuts; less time in the degraded regime | Medium | S (policy) | NoLiMa, 2610.31778 (threshold matters only for the smallest-window model), 2609.29875 |
| 8 | **Observation masking where the harness allows it** (old shell output, JSON dumps, compile logs; keep ledger/receipt/test-run results; keep last M=10 turns; batch masking with `clear_at_least` to protect the prompt cache) | noise, cost, distraction | Medium | Depends on the ACP harness and Codex app-server hooks (not verified) | Complexity Trap, Anthropic context editing |
| 9 | **`parhelion_recall(query, case, tag)`** over ledger, archived transcripts of previous sessions and handoffs (JIT retrieval; replace "do not re-read" with "call recall before re-deriving") | late-surfacing dropped details | Medium | M | ReadAgent, mempact `recall`, Cursor history file |
| 10 | **Offline replay probe** for handoff quality, before and after changes (section 5.8) | measurement; prevents shipping a prompt that sounds good | Enabling | M | Factory, mempact, RECAP |
| 11 | Background curator ("sleep-time" agent) that merges ledger entries, de-dups, and proposes promotion to the five-element KB after cross-problem validation | long-run hygiene, new-worker takeover after six judged submissions | Later | M-L | ACE, sleep-time compute, AccelOpt |
| 12 | Train or fine-tune a compressor | all | Skip | L | Cognition, ACON, RECAP |

Order of work: 3 and 2 first (cheap, immediately stop dropping open hypotheses), then 1 and 6, 5 and 4 in parallel (they address the noise complaints), then 7 and 9, with 10 built early enough to compare variants.

### 5.3 Proposed ledger schema

File: `results/ledger/<problem>/<worker-id>.jsonl` (append-only; edits are new rows with `supersedes`). Entries are small (<=1 KB). Platform-owned fields are filled by code and are not model-editable. Reuse existing ids: `test_run_id`, `receipt_id`/`submission_id`, SHA.

Common fields (all types):

| Field | Meaning |
|---|---|
| `id` | `L-<seq>` unique per worker (ids stay stable across compactions and are what handoffs and peer notes reference) |
| `ts`, `worker`, `problem`, `source` (`platform` / `worker` / `peer`) | provenance |
| `type` | `measurement` / `hypothesis` / `experiment` / `insight` / `failure` / `decision` / `constraint` |
| `claim` | one sentence, <=240 chars, assertive |
| `why` | mechanism or reasoning, <=300 chars |
| `scope` | `{cases:[...], bucket, shape/dtype, hw_gen}` where the claim applies |
| `base_sha`, `sha` | code state the entry is about |
| `evidence` | list of refs: `test_run_id`, `receipt_id`, `path#anchor`, `peer_note_id` |
| `confidence` | `measured` / `inferred` / `speculative` (platform sets `measured` only when backed by a run record) |
| `links` / `supersedes` | related ids |
| `revisit_if` | free text or predicate: conditions under which a closed item should be re-opened (new SHA touching X, noise band tightened, new hardware gen) |

Type-specific fields:

- `measurement` (platform only, immutable): `key` (e.g. `case05.p50_us`), `value`, `unit`, `n_repeats`, `noise_band`, `env_id`, `test_level` (smoke/standard/final).
- `hypothesis`: `status` (`open` / `queued` / `testing` / `confirmed` / `refuted` / `inconclusive` / `abandoned`), `predicted_effect` (`{case05: "-8..-12%"}`), `test_plan`, `priority` (1-5), `resolved_by` (experiment id), `resolution_note`.
- `experiment`: `hypothesis_id`, `change_summary` (<=200 chars), `diff_hash` (platform), `test_run_id`, `per_case_us` (platform from the report), `baseline_sha`, `delta_vs_baseline`, `outcome` (`better` / `worse` / `neutral` / `noise` / `WA` / `RE` / `TLE` / `compile_fail`), `lesson` (one line), `receipt_id` (when judged), `submitted` (bool).
- `insight`: `actionable_next`, `est_gain`, `cases`, `exploited_by` (experiment id or null; platform marks an insight "unexploited" until an experiment cites it).
- `failure` (dead end): `approach`, `why_failed` (mechanism, not only "slower"), `cost_spent` (runs, hours), `retry_conditions`.
- `decision`: `chosen`, `rejected`, `reason`.
- `constraint`: `text` verbatim, `source` (contest rule, admin instruction, platform rule); pinned in every seed.

Example rows (illustrative):

```json
{"id":"L-0412","type":"insight","source":"worker","claim":"case 05 spends most time in the tail tile; reordering the load loop looks free","why":"per-case report shows case05 2.4x slower than tbest while cases 01-04 are within 5%","scope":{"cases":["05"]},"sha":"a1b2c3d","evidence":["test_run:tr_8841","report:results/tr_8841.json#case05"],"confidence":"measured","actionable_next":"try tile-order swap on case05 only","est_gain":{"case05":"-15%"},"exploited_by":null}
{"id":"L-0419","type":"failure","source":"worker","approach":"double-buffer the A tile","claim":"double-buffering A did not help","why_failed":"kernel is DMA-bound on B; extra buffer adds sync cost","scope":{"cases":["03","05"],"shape":"M>=2048"},"evidence":["test_run:tr_8863"],"confidence":"measured","retry_conditions":"if B moves to L1-resident layout"}
```

Tools (MCP, small): `parhelion_note(type, claim, why, scope, evidence, ...)` returns the new id; `parhelion_ledger(view=open|insights_unexploited|failures|case:05|since:<id>)`; `parhelion_recall(query)`; hypothesis status updates go through `parhelion_note(type="hypothesis", id=..., status=...)` so history is preserved by `supersedes`.

Gate integration: `parhelion_submit_gate` refuses a candidate with no `hypothesis_id` or `experiment` row (a one-line note, auto-created from the commit message if the worker supplies none); receipts resolve it automatically.

### 5.4 What goes in the handoff vs stays on disk

Handoff / seed (target 6-10K chars, about 2-3K tokens; still ~99% compression of 350K, comparable to Factory's 98.6-99.3% ratios; the current 4,000-char cap is probably too small for what is lost). Built in this order, the end being what the model reads last:

1. **Pinned, code-built, verbatim:** contest rules, admin instructions, platform rules (the existing `RULES_LINE`), goal.
2. **State card (existing):** best score and SHA, head SHA, last receipts, public per-case gap. Extend to all cases, tbest gap per case.
3. **Ledger digest (code-built from the ledger, not LLM-written):**
   - OPEN and queued hypotheses: all, by priority, one line each with id and predicted effect.
   - Unexploited insights: all, one line each with id and cases.
   - Failures: last 15-20, one line each (id, approach, why_failed, retry_conditions); older ones summarized as "N earlier failures; call `parhelion_ledger(view=failures)`".
   - Latest per-case measurements for the cases under work, with noise band.
4. **Model-authored narrative <= 1,500 chars, schema-checked** (headings: where I am, what I was in the middle of, in-flight uncommitted changes and file paths, the next experiment as a ledger id, anything I believe that is not yet in the ledger). Anything the validator finds unmentioned (open hypothesis id missing) is appended back automatically.
5. **Raw tail:** last stage result and the last few turns or tool results verbatim (up to about 8-10K tokens), plus `git status` / `git diff --stat` of the working tree and the paths of in-flight files (mempact "rehydrate paths").
6. **Closing action line (recitation, replaces "Do not re-read files you already summarized"):** "Pick the top OPEN hypothesis or unexploited insight, state its id, call `parhelion_recall` before re-deriving anything, then run the test. If you re-read a file, say why." This addresses ARCBench's point that retention without action does not change behavior.

Stays on disk (retrievable by id or recall): full ledger, raw per-case reports and test-run JSON, every old handoff, transcripts of previous sessions, diffs, peer-message archive, compile logs, anything over the output cap.

Cache note: in-session, keep the seed prefix stable and append-only (Manus). Changing the prefix by masking invalidates cache; batch it (`clear_at_least`).

### 5.5 When to mask observations vs summarize with an LLM

Use **observation masking** (replace the result with `[elided <tool> <bytes> sha256:… saved at <path>]`) when all hold:
- the output is machine-regenerable (shell `cat/sed/grep/git log`, JSON report dumps, compile or runtime logs) or already distilled into a ledger row or receipt;
- it is older than about 10 turns (the Complexity Trap setting) and not the result of the current hypothesis test;
- it is not an error under active investigation (Manus: keep failures visible) and not a ledger, receipt, judge or admin message.

Use an **LLM summary** only for:
- the *rationale narrative* of a finished episode, whose product is then written into the ledger as `insight` / `decision` / `failure` (this is where hypotheses and reasoning live and masking cannot help);
- digesting a long peer thread or external document into a ledger row with an evidence pointer.

Prefer a **fresh, ledger-seeded session** (reset) over mid-episode summarization when a stage boundary is reached and the ledger is current: it avoids summary elongation of trajectories (+13-15%), the 24-94% extra token cost of LLM condensers, and cascading loss.

Never mask or summarize: constraints and rules (pin them), per-case measurements you intend to compare against (they are rows, not prose), unresolved hypotheses.

### 5.6 Noise fixes beyond masking

- **Shell and JSON noise.** Log every shell command and Python snippet shape over a sprint; the top clusters tell you which platform views are missing. Replace with typed tools that return a compact table (`case, ours µs, tbest µs, delta %, noise band`) and spill long raw detail to a file with a pointer. If the harness supports it, run a PostToolUse-style truncation: cap results at a fixed size, keep head and tail, store the whole (mempact cuts outputs over 12,000 bytes in the middle and saves them whole; autoresearch redirects to a log).
- **Peer messages.** See design 5 in 5.2. Also, since a Codex thread retains user-role messages up to 20K tokens through compaction, never deliver multi-KB peer text as a user turn; deliver a ledger id and a claim.

### 5.7 Verification that a handoff did not drop key items

Cheap and deterministic, run on every compaction:
1. Every `open`/`queued` hypothesis id and every `unexploited` insight id appears in the new session's seed (by construction, since the digest is code-built; also check the narrative references the next experiment id).
2. Every SHA, file path and test-run id in the narrative resolves (git, results folder).
3. Carry-forward: diff the previous narrative and the ledger against the new narrative; append dropped decisions and failures.
4. Schema check on narrative headings; one repair round if missing; fall back to ledger-only seed on a second failure.

Behavioral (offline, then sampled in production): immediately after seeding, ask the new session 5-8 canned probes drawn from the ledger ("What is the top unexploited insight for case 05?", "Which approaches were refuted and why?", "What was the last measured p50 for case 03?", "What is the next experiment?") and grade by id and number match; a score below a threshold triggers one re-seed with the full ledger dump. This mirrors Factory's recall/artifact/continuation/decision probes and mempact's replay benchmark.

### 5.8 Evaluating the redesign on your own data

- **Replay benchmark.** Take the sprint's worker transcripts (you have them), cut at each compaction, and compare four seeds: (a) current (model handoff + 4K checkpoint), (b) ledger digest + current handoff, (c) ledger digest + validated narrative + raw tail (proposed), (d) full history as an oracle. Score with probes generated from the later part of the same transcript: "which of the ideas that this worker eventually tried had been mentioned before the cut", "which dead ends were re-tried after the cut". This reuses mempact's method; bootstrap CI over workers.
- **Live metrics.** Duplicate-experiment rate (same `diff_hash` or same `approach` within a worker or across workers), time and tokens from seed to first new experiment, number of file re-reads of files already touched pre-compaction, shell calls per test cycle, bytes of peer message per worker per hour, share of unexploited insights older than N hours, per-problem score gain per judged submission.
- A/B across problems with equal budgets only after the replay test passes.

### 5.9 Risks and open questions

- **Anchoring on dead ends.** Failures must carry scope and `revisit_if`; allow the orchestrator or a fresh worker to re-open (the six-submission handoff rule in the platform design already sends a new worker in an orthogonal direction; give it the failure ledger and let it override entries with evidence).
- **Wrong or noisy insights.** Confidence field and evidence refs; platform only marks `measured` for run-backed entries; noise band on measurements to separate noise from regressions (platform goal 2 and 4).
- **Model discipline varies** (DeepSeek, Claude, Codex): enforce at the gate and via tool results, do not rely on prompts.
- **Ledger growth.** Cap digest size by type; age out low-priority entries to "see recall"; the ACE grow-and-refine de-dup (non-LLM) or a background curator handles hygiene.
- **Harness limits.** Whether ACP workers' harnesses (and the Codex app-server in use) allow tool-result clearing or hooks was not verified; if not, items 4, 5, 6, 7 still work because they sit in platform-controlled MCP tools and turn-level policy.
- **Platform fit.** The ledger is the structured form of the "题目实验记录" (per-problem experiment record) already described in the platform design; promotion into the five-element knowledge base only after cross-problem validation, as the design already says.
- **Thresholds.** Do not lower the compaction threshold without data: 2610.31778 found it irrelevant for 1M-window models when state is on disk and harmful only for the smallest-window model. Moving state to disk first makes the threshold a cost question rather than an insight-loss question.

---

## 6. Gaps: what could not be found or verified

- No paper or benchmark found that measures forgetting of *optimization hypotheses* after compaction in kernel/performance engineering, and none on DeepSeek-model behavior after compaction specifically.
- Chroma's report gives charts, not headline numbers, for degradation; I have no exact percentages from it.
- Factory's evaluation: the primary page failed twice (socket closed); numbers are from a ZenML summary [S] and are vendor-run.
- OpenHands blog (cost numbers) and condenser docs page: not retrieved; only SDK source file and search snippets.
- Cognition post: only secondary summaries.
- Windsurf/Cursor rules, Roo memory bank, LangGraph: not researched beyond what is listed.
- Letta: the page fetched did not match the older sleep-time-agent blog; MemGPT internals beyond the abstract not verified; Generative Agents retrieval formula and LongMem, ExpeL (arXiv 2308.10144), Agent Workflow Memory (arXiv 2409.07429) were not fetched.
- 2026 arXiv ids (2605.18854, 2605.24657, 2606.22528, 2608.22752, 2609.29875, 2610.31778, 2611.03223, 2611.31258) were read from their arXiv abstract pages this session; they are recent preprints, some not peer reviewed, and the compaction-specific ones (cascading, Cliff, ConstraintRot) were found via the mempact README and a search; verify claims before relying on them externally.
- Whether Parhelion's Codex app-server uses local or remote (`compact_remote_v2`) compaction for each worker model, and what the ACP harness exposes for context editing, was not determined.

---

## 7. Source index

Papers (arXiv): 2307.03172 (Lost in the Middle), 2404.06654 (RULER), 2502.05167 (NoLiMa), 2505.06120 (Lost in Multi-Turn), 2508.21433 (Complexity Trap), 2510.04618 (ACE), 2510.00615 (ACON), 2510.11967 (Context-Folding), 2510.24699 (AgentFold), 2506.15841 (MEM1), 2507.02259 (MemAgent), 2512.22087 (CAT), 2611.31258 (RECAP), 2609.29875 (ICLR compression), 2605.18854 (Memory condensation for coding agents), 2605.24657 (cascading compaction), 2608.22752 (Compaction Cliff), 2606.22528 (Governance Decay / ConstraintRot), 2611.03223 (ARCBench), 2610.31778 (Benchmarks Are Compaction-Friendly), 2310.08560 (MemGPT), 2304.03442 (Generative Agents), 2303.11366 (Reflexion), 2305.16291 (Voyager), 2502.12110 (A-MEM), 2504.19413 (Mem0), 2402.09727 (ReadAgent), 2504.07952 (Dynamic Cheatsheet), 2509.25140 (ReasoningBank), 2504.13171 (Sleep-time compute), 2511.15915 (AccelOpt), 2602.14293 (KernelBlaster), 2603.10085 (KernelClaw), 2606.26453 (KernelPro), 2607.14541 (Atrex-Bench).

Vendor and community: Chroma Context Rot (trychroma.com/research/context-rot); Anthropic engineering posts (effective context engineering; effective harnesses for long-running agents); Anthropic context-management announcement (claude.com/blog/context-management); platform docs for memory tool, context editing, compaction (platform.claude.com); Claude Code docs (code.claude.com/docs/en/context-window, /memory); Manus blog; Cursor dynamic context discovery; Cline memory bank docs; Aider repomap docs; Factory evaluating-compression; JetBrains Research blog (403, not read); GitHub: openai/codex (`codex-rs/prompts/templates/compact/prompt.md`, `codex-rs/core/src/compact.rs`, `tasks/compact.rs`), OpenHands/software-agent-sdk (`llm_summarizing_condenser.py`), topmass/mempact (README), karpathy/autoresearch (`program.md`), Aider-AI/aider (`aider/repomap.py`), alioshr/memory-bank-mcp.

Parhelion files referenced: `cli/opsb_core/agents/adapters.py`, `cli/opsb_core/agents/manager.py`, `cli/opsb_core/services/state_card.py`.
