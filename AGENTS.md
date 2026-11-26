# Parhelion 平台设计目标

题组 worker 通过平台 MCP 请求 NPU 编译、逐例执行和测速。外部诊断脚本由主控调度。正常的一批用例和重复测量连续执行，单例设备超时要回报 TLE、释放锁并为余下用例重新排票。

修改调度、工具、界面或比赛提示词时，以从 worker 交付候选到收到官方反馈的完整流程作为验收单位。以下约定也适用于新建比赛的英文主控提示词。

1. **平台管工具，worker 管题目。** Worker 自主选择优化方向、修改实现、提交验证任务，达到阶段目标时通过 MCP 把指定 SHA 交给平台。编译、调试、逐例验证、预测、入队、上传和回执查询由平台提供统一入口。Worker 可通过同一套接口增补用例、分桶和消融配置。用例数从本题模板或判题证据发现，桶是同一批用例上的分析维度。保存运行配置、环境、原始测量及来源，支持复放与方案对比。
2. **五元知识库积累可迁移的经验。** 突破、代码积木、硬件规律、社区方案和 Bug 档案都要说明适用条件与可复查证据。逐发成绩及尚待验证的解释留在题目实验记录。重复回执按身份去重，区分可复现退化、测量噪声和榜单自然衰减；题内拟合参数经跨题验证后才推广。
3. **平台统一分配提交额度。** 单纯运行本地测试只产生验证证据。收到明确提交请求后，尽快返回预测分、相对同代基线的增益、证据质量、是否可以自动提交及理由，让 worker 继续迭代。缺少可靠测量时标记未知。符合条件的请求按加权预期收益和耗时排序，结合账号槽、冷却、额度和在途请求逐次派发；其余进入暂缓并保留来源。新比赛每题前三个不同的官方受理回执享受开局豁免，仍遵守账号资源约束。重试、轮询及本地拒绝不重复扣发。最终判题异步送回提交该 SHA 的 worker。
4. **跨时点比较使用同代成绩。** 保留最近一发原始成绩、当前在榜分和观测时间，按本场计分规则展示。估分、突破和停滞比较以同一时点的 tbest 或有历史支撑的衰减估计为基线，记录依据及误差。优先使用可配对的官方逐例时间；经过校准的本地性能测量才参与升分判断。
5. **单台 NPU 按整题验证任务调度。** Worker 直接入队。编译、输入及期望输出准备和结果比对在 CPU 完成。准备好的整批用例只排一次设备队，同一运行连续完成所选用例与重复测量，再释放设备供下一题使用。独占期间复用 ACL 会话，逐次重建输入输出，逐次校验；可关闭会话复用以复查冷启动差异。三车道按运行 ID 展示一张卡片，并显示实际用例与重复进度。分别记录排队、持锁与端到端反馈时长，以整题完成速度验证调度效果。设备主机上由常驻代理按 WSPT 顺序串行执行设备作业并独占设备锁；worker 只能经 `parhelion_test` 选择 smoke、standard 或 final 级别，套件由平台按版本维护。
6. **主控负责例外与方向。** 日常验证和提交由 worker 直接入队。管理员下达新指令时，主控将其交给相关题目负责人。本地放行但线上 WA、RE 或 TLE 时，平台通知原 worker 复盘，主控另派 worker 将可泛化的防错经验写入 Bug 档案。官方同代增益超过配置阈值时，主控另派 worker 复核并提炼突破。每题累计六次实际消耗额度的官方判题后，将这六发中的最高同代分与窗口开始前的历史最高同代分比较；增益未超过阈值时，收集原 worker 的失败总结，由新的 worker 从正交方向接手。同一批回执重放不得重复触发交接。冲刺默认每题一个 worker，双轨与校准席位需显式开启；供应商余额或配额故障时暂停该驱动的全部派发，只告警一次。

7. **主控跨 Harness 协作。** 主控可通过平台发现可用驱动，按题目、职责和工作树派发外部 worker，并发送后续指令。协作页展示主控、Harness、题目归属、工作状态，以及平台总线上每次任务、回复、指令、判题反馈的有序历史。用户浏览与主控消费消息各自独立。原生子代理的可见范围以驱动实际公开的信息为准。
8. **让刷分方法论进入实际工作目录。** 仓库、比赛和单题指南应能指导 worker 完成一次可复查的迭代：查本场规则和历史失败，选择同代基线，从分桶证据提出一个假设，改变一个变量，按相同输入和环境复测，将候选 SHA 交给平台，最后用官方回执检验假设、调整下一轮实验。记录负结果与不确定性。同步说明必须保留既有比赛策略、冻结的 oracle 和实测历史。
9. **为 worker 提供统一的 MCP 接口。** 发现实验、验证、门禁预览、候选入队、诊断、知识查询与收件箱使用平台统一契约。明确区分预览与执行，返回与该题及 SHA 关联的结果、依据和下一步。角色权限、账号选择、冷却、设备竞争和底层日志由平台处理，日常进度留在协作页，需决策的例外才唤醒主控。

收到整场启动指令后，主控调用 `parhelion_sprint_start`，核对每题的算法、数据通路与 bench-author 三个席位。官方回执与提交前同 SHA 的逐例报告直接进入本题校准者收件箱。队友通过 parhelion_squad 定向分享复现、实验配置与对照结果。日常校准留在题组时间线，仅显式求助和战略事件通知主控。每个方向有独立的工作树、分支、worker 身份和回执收件箱，重试启动会复用仍在运行的分工。工作树按明确 SHA 交付候选。自动提交沿用管理员设定的开关及账号资源策略。

<!-- aoci:begin -->
## AOCI Repository Cognition

AOCI maintains a stable, versioned, incrementally updatable repository-level cognition layer so models can reuse their understanding of this system across tasks.

`aoci.txt` is a structured cognition index for models. It assigns one independent Entry to every managed file, database table, or other managed object. Symbolic tags and F/R/A/S semantics describe the object's core responsibility, important relationships, external contracts, and non-obvious constraints or design decisions needed to understand or modify the system.

The Header, directory sections, and all Entries form the complete repository index. They can cover frontend, backend, configuration, database structures, and other managed content. When managed content changes, normally only the affected cognition Entries need maintenance; the complete index does not need to be regenerated.

AOCI provides a high-density view of system architecture, object responsibilities, important relationships, external contracts, and key constraints.

### How it works

AOCI uses a model-generated, model-read cognition loop.

Header, Entry, and Curation semantics follow only the current machine-issued Plan and live Guide. The Host model independently authors them from the current bound evidence.

Entry semantics must come from the model's understanding of actual evidence. Never derive, prefill, assemble, or rewrite index semantics solely from paths, filenames, extensions, an AST, symbol lists, dependency scans, regular expressions, fixed templates, or rule engines.

For a Fresh Bootstrap, follow only the current machine-issued Plan and live Guide. When they require authoring, the Host model authors Root, Meta, tags, and F/R/A/S, supplies its authoring-run declaration, and binds it to the Plan, Evidence, and complete Candidate. Never ask AOCI to set `origin=host_model`, manufacture a receipt, or turn a generated framework into semantics. Do not reconstruct the Onboarding progression here. Internal batches are not user decisions; stop only at an existing approval boundary or a real safety, drift, CAS, or Recovery condition.

### Minimal entry points

- `aoci_rules`: obtain the session-level runtime contract for the current AOCI version.
- `aoci_overview`: establish or restore complete cognition for this repository.
- `aoci_maintain`: after managed objects reach their final stable state, check whether cognition needs maintenance.
- `aoci_update_entry`: submit a complete semantic update batch bound to current evidence and source digests.
- `aoci_report`: when the current layout and tool state support it, record follow-up work if evidence is insufficient to generate semantics reliably; do not guess.

For other MCP tools, CLI commands, parameters, and specialized workflows, follow current tool descriptions, Guide, and `--help` output. This file does not duplicate the full manual.

This managed block defines only repository integration, cognition use, and task-closing principles. `aoci_rules` carries the current session contract. Live Guide output carries the execution order and stop conditions of the current Plan. Tool Schema, Spec, and Validator carry machine structures and criteria. Prompt, Description, README, and static documentation cannot override those machine facts.

### Establishing, generating, and restoring cognition

1. At the beginning of every new Agent Run, first determine:

   - whether this repository already has a usable complete AOCI index; and
   - whether current context already contains complete repository cognition that matches this repository root, current index version, and current AOCI service, and that the model can still use reliably.

2. When the repository has a usable complete index but the current Run lacks reliable complete cognition, call `aoci_rules` first and then `aoci_overview`.

   Reuse complete cognition directly while it remains reliable. Local uncertainty does not by itself require mechanically rereading the system-wide view.

   A Run that resumes from a known Host context compaction, including a Host-injected compaction summary, must treat prior model cognition as unreliable. The compacted handoff must not retain or summarize the formal Whole-Index or any Overview Header, Entry, Chunk, Challenge, or Attestation body; it may retain only receipt identity, unfinished write or Recovery state needed for safe continuation, and an instruction to reload immediately. Whole-Index semantics or a receipt copied into that handoff cannot prove that the resumed model's current cognition is reliable. If the runtime contract is no longer reliably present, call `aoci_rules` first. Before continuing the business task, make an ordinary complete Whole-Index `aoci_overview` request (`check_only` absent or false) with `refresh_reasons=["context_compaction"]` and a fresh `refresh_event_id`; do not use `check_only` or a cognition probe. Follow every exact `next_cursor` through `completed=true`, confirm delivery, and submit one Attestation based only on the newly delivered body. After that fresh complete transport, a partial or failed Attestation consumes the generation and permits the existing source-bound continuation without another automatic Overview.

   AOCI can report checkpoint and cognition-status facts for `context_compaction`, the machine `semantic_threshold` under the project `cognition_refresh_threshold`, or a major `phase_transition`. Use `check_only=true` when only those compact facts are needed. They advise the Agent but do not decide whether the model needs the system-wide view.

   When the Agent explicitly calls ordinary `aoci_overview` (`check_only` absent or false), AOCI must deliver the complete requested scope whenever a coherent CognitionSet can be formed. It must not suppress that body because a receipt already exists, a threshold was not reached, or no refresh reason is pending. Dirty or stale formal cognition is still delivered but is marked unreliable. Pending recovery or an incoherent snapshot fails closed without a mixed body.

   When an ordinary Overview reports `continuation_required=true`, submit its exact `next_cursor` automatically until `completed=true`. Do not ask the user to continue, begin the business task, or state a partial system conclusion. Stop the cognition chain on Host truncation, a missing, duplicate, or reordered Chunk, cursor failure, Index change, or `chunk_tokens` change. Until Attestation completes, never use Memory, source, Spec, `aoci.txt`, historical sessions, scope, search, or Entry reads to repair or supplement Whole-Index cognition. A challenge ordinal is the 1-based position in the formal Entry sequence; Header content, comments, blank lines, Section/Overview/Chunk markers, receipts, and Metadata are excluded, and Chunk Receipt ordinals use that same sequence. The Attestation must echo the Challenge's exact current `index_sha256`, `entry_sequence_sha256`, and `entry_count`; a prior Index, Entry sequence, count, or Attestation is invalid. After the complete chain, submit the existing model cognition Attestation once. One same-response JSON Schema or field-format error may be corrected once without changing semantic answers; an object, Tag, or F mismatch means failure and uncertain assimilation, with no semantic retry or information bypass. During initial cognition it also blocks Root/Meta, Migration, layout-wide, or other unbound system decisions. During a context-compaction refresh with complete transport, unchanged cognition identity, aligned governance, and no Recovery or third-party conflict, the attempt consumes that refresh generation even when Attestation is partial or failed; continue the existing task without another automatic Overview. `system_mastery_percent` self-assesses only the system framework—architecture, responsibilities, strong relationships, stable external contracts, and high-entropy safety and maintenance constraints—not complete implementation or runtime knowledge. Keep machine Index coverage separate, and normally give the user only the prescribed single success or failure sentence derived from actual coverage, Challenge, Chunk, token, and mastery results. If the Host truncates a Chunk, ask the user to set `overview_delivery.chunk_tokens` to a smaller valid value and restart; do not change it automatically.

   Interpret the additive cognition level independently from strict proof fields. `delivery_verified` means the Index was loaded and Host delivery was confirmed while complete cognition verification is still unfinished; describe that state as loaded and delivery-verified, never as no cognition or failure to understand the system. `cognition_verified` requires a passing Attestation (at least 80 percent of Challenge ordinals fully correct with at most one object identity miss), and `cognition_governed` additionally requires governance alignment. A generic complete-read failure sentence is reserved for an actual delivery fault.

   When an Overview response contains the optional `cognition-state/v2` projection, use its dimensions independently. Its Level ends at `model_cognition_usable`; `strict_attestation_verified`, `governance_aligned`, and `current_system_cognition_reliable` are independent states and never participate in that Level. An ordinal, object identity, Tag, or core F mismatch can make strict Attestation fail while model cognition remains usable; do not report that mismatch alone as proof that the model did not understand the system. Only `current_system_cognition_reliable=true` permits an unqualified current complete-system cognition claim. When the projection is absent, keep using the legacy interpretation above.

   An ordinary read-only audit, analysis, or check, a request not to modify code, or a request not to commit or push does not automatically mean strictly zero writes and does not alter the cognition-validity decision above. Codex Memory and historical Skills may only help recover experience, user preferences, and investigation directions. They cannot replace a current cognition receipt matching the repository root, index digest, AOCI service identity, and cognition scope. Project AGENTS and current AOCI identity take precedence over historical Memory for AOCI state.

   Treat a task as strictly zero-write only when the user explicitly prohibits Ledger, metadata, `.aoci` runtime assets, and every filesystem write. If necessary cognition establishment conflicts with that boundary, report the conflict and ask the user to decide or recommend an isolated copy. Never silently substitute Memory for current repository cognition.

3. If the repository has no usable complete index, or has only a minimal skeleton, an incomplete Header, unfinished Entries, or undecided required Curation, obtain `aoci_rules` and enter the current AOCI Guide when a formal complete AOCI index is required. Let Guide choose the next phase from actual repository state and complete the required safety steps.

   `aoci_maintain` does not replace the index-establishment workflow.

   Do not reconstruct or hard-code the full-index generation state machine in this file.

4. During a long-running task, the model is responsible for preserving the current cognition receipt and using the refresh gate correctly:

   - when the Host reports context compaction or the model knows the system-wide view was lost, follow the mandatory `context_compaction` reload rule above; AOCI cannot infer the Host event;
   - when entering a genuinely major phase, declare `phase_transition`, not a function, test run, or small step;
   - at a plausible stable checkpoint, use `check_only=true` to obtain the machine semantic count when that fact is useful;
   - except for the mandatory known-compaction reload, decide whether the current task needs another explicit scoped or complete Overview; and
   - keep the Dirty or Stale reliability state reported by AOCI until maintenance and alignment complete.

### Task closing and cognition maintenance

5. A purely read-only question, analysis, version check, or task that changes no AOCI-managed object does not require a maintenance-tool call. The AOCI version in use is `cognition_receipt.mcp_service_version` in any `aoci_overview` check_only or `aoci_maintain` response; the binary path is the `command` in the project's `.mcp.json`, and the CLI need not be on PATH.

6. When AOCI-managed objects change, call `aoci_maintain` once after they reach the task's final stable state. Do not maintain files individually after each intermediate edit.

7. If maintenance returns actual semantic candidates, the Host model must independently author the complete tag and F/R/A/S updates from each candidate's bound object and necessary evidence. Submit the complete candidate set for that current machine-issued batch in one `aoci_update_entry` call while preserving each `source_sha256`, `candidate_id`, and domain batch identity. `max_entries` limits one request and atomic transaction, not the logical plan, Whole-Index, or Managed Scope. When `remaining` is nonzero, call Maintain again after the successful Apply and continue from the new preimage; never shrink Index coverage or slice a returned batch to satisfy transport limits.

   When evidence is insufficient and the current layout supports `aoci_report`, use it instead of guessing, applying a template, or generating unsupported cognition merely to eliminate follow-up work.

8. Obey structured tool states and safety boundaries:

   - `repair_required`: repair only the explicitly identified candidates, then resubmit the complete current machine-issued batch;
   - `stopped`: end that write attempt and inspect `failed_step`, error, formal-write evidence, and Recovery. In auto mode, a proven zero-write closure is followed by a fresh Plan; a complete Intent with provable postimage is resumed; a policy-selected Rollback with exact preimage is completed and replanned. Stop the user task only when proof is unavailable, third-party bytes conflict, approval or external action is required, or another real safety boundary applies;
   - never ignore conflicts, approvals, human decisions, permissions, or safety signals; and
   - after alignment, do not repeat maintenance or writes; `refresh_ready_for_overview` is a checkpoint fact, and the Agent decides whether to request an ordinary complete Overview for its next phase.

   If any managed object changes after maintenance completes, the previous result is invalid. Complete closing again from the new final stable state.

9. When the user limits only business-file scope and does not explicitly forbid repository-managed assets, AOCI-managed assets may be updated during closing to preserve cognition consistency. Distinguish them from business files in audits and commits.

   When the user explicitly forbids changes to `aoci.txt`, `.aoci`, metadata, or any additional file, obey that restriction, do not write, and report any remaining inconsistency accurately.

### Specialized workflows

Initialization, complete-index generation, Header generation, Entries generation, database-structure indexing, Curation, human review, and failure recovery must follow only the instructions, commands, and safety stops returned by the current AOCI Guide or tool at the corresponding stage.

Do not preload, guess, or reconstruct these specialized workflows. The relevant Guide, tool descriptions, model Prompt, and CLI help provide platform invocation, request format, batch limits, approval rules, index-format details, and recovery steps as needed.
<!-- aoci:end -->
