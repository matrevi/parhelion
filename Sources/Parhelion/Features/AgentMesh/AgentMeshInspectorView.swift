import SwiftUI

struct AgentMeshInspectorView: View {
    let mesh: AgentMeshHotState
    let lang: AppLanguage
    let selection: AgentMeshSelection
    let messages: [AgentBusMessage]
    let diffStat: String
    let isSending: Bool
    let onSendWorkerMessage: (String, String, String) -> Void
    let onSendAdminDirective: (String, String, Bool) -> Void
    let onStopWorker: (String, Bool) -> Void
    let onJumpOrchestrator: () -> Void
    let onJumpWorker: (AgentWorkerHot) -> Void
    let onSelect: (AgentMeshSelection) -> Void

    @UIState private var draftMessage: String = ""
    @UIState private var broadcastDirective: Bool = true

    private var selectedWorker: AgentWorkerHot? {
        guard case .worker(let wid) = selection else { return nil }
        return mesh.workers.first { $0.agentId == wid }
    }

    private var selectedSquad: AgentSquadHot? {
        guard case .squad(let prob) = selection else { return nil }
        return mesh.squads.first { $0.problem == prob }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let worker = selectedWorker {
                workerHeader(worker)
            } else if let squad = selectedSquad {
                squadHeader(squad)
            } else {
                orchestratorHeader
            }

            Divider()

            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 8) {
                        if messages.isEmpty {
                            Text(lang.tr("自治静默运行中 · 暂无总线消息", "Autonomous quiet · no bus messages"))
                                .font(.system(size: 11))
                                .foregroundStyle(.tertiary)
                                .frame(maxWidth: .infinity, alignment: .center)
                                .padding(.vertical, 36)
                        } else {
                            ForEach(messages) { msg in
                                messageBubble(msg, showAgentTag: selectedWorker == nil)
                                    .id(msg.seq)
                            }
                        }
                    }
                    .padding(12)
                }
                .onChange(of: messages.last?.seq) { _, lastSeq in
                    if let lastSeq {
                        withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo(lastSeq, anchor: .bottom) }
                    }
                }
            }

            Divider()
            if let worker = selectedWorker, worker.isActive {
                workerComposer(worker)
            } else {
                adminDirectiveComposer(targetProblem: selectedSquad?.problem ?? "")
            }
        }
        .background(Color(nsColor: .controlBackgroundColor).opacity(0.55))
    }

    private var orchestratorHeader: some View {
        HStack(spacing: 8) {
            HarnessLogoView(driver: mesh.orchestrator.driver, appPath: mesh.orchestrator.guiAppPath, size: 24)
            VStack(alignment: .leading, spacing: 2) {
                Text("\(mesh.orchestrator.displayName) · ORCH")
                    .font(.system(size: 12.5, weight: .bold))
                Text("\(mesh.squads.count) squads · \(mesh.orchestrator.activeWorkers) workers · #\(mesh.latestSeq)")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                if let cache = mesh.orchestrator.cache {
                    cacheLine(cache)
                }
            }
            Spacer()
            Button(action: onJumpOrchestrator) {
                Label(lang.tr("打开对话", "Open"), systemImage: "arrow.up.forward.app.fill")
                    .font(.system(size: 10.5, weight: .semibold))
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.mini)
            .help(lang.tr("跳转至 \(mesh.orchestrator.displayName) 主控对话上下文", "Open \(mesh.orchestrator.displayName) conversation"))
        }
        .padding(12)
    }

    private func cacheLine(_ cache: AgentCacheUsage) -> some View {
        let miss = cache.lastMissReason.isEmpty ? "-" : cache.lastMissReason
        return Text(lang.tr(
            "缓存命中 \(cache.hitLabel) · 命中 \(AgentCacheUsage.compactTokens(cache.cacheReadTokens)) · 未命中 \(AgentCacheUsage.compactTokens(cache.inputMissTokens)) · 最近未命中 \(miss)",
            "Cache hit \(cache.hitLabel) · read \(AgentCacheUsage.compactTokens(cache.cacheReadTokens)) · missed \(AgentCacheUsage.compactTokens(cache.inputMissTokens)) · last miss \(miss)"
        ))
        .font(.system(size: 9.5, design: .monospaced))
        .foregroundStyle(cache.hitPct >= 80 ? Color.secondary : Color.orange)
        .lineLimit(1)
    }

    private func squadHeader(_ squad: AgentSquadHot) -> some View {
        let members = mesh.workers.filter { squad.workerIds.contains($0.agentId) }
        return VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 6) {
                DifficultyBadge(difficulty: squad.difficulty)
                Text(squad.problem)
                    .font(.system(size: 13, weight: .bold))
                Text(squad.collabMode.uppercased())
                    .font(.system(size: 8.5, weight: .heavy, design: .monospaced))
                    .foregroundStyle(.cyan)
                    .padding(.horizontal, 5)
                    .padding(.vertical, 1.5)
                    .background(Capsule().fill(Color.cyan.opacity(0.14)))
                Spacer()
                if squad.currentScore > 0 {
                    Text(String(format: "%.2f", squad.currentScore))
                        .font(.system(size: 11.5, weight: .bold, design: .monospaced))
                }
            }

            // Cross-Harness member comparison strip
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    ForEach(members) { m in
                        let style = AgentDriverStyle.forDriver(m.driver)
                        Button { onSelect(.worker(m.agentId)) } label: {
                            HStack(spacing: 4) {
                                HarnessLogoView(driver: m.driver, appPath: m.guiAppPath, size: 14)
                                Text(m.axis.isEmpty ? m.roleLabel : m.axis)
                                    .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                                if !m.lastCommitSha.isEmpty {
                                    Text(m.lastCommitSha)
                                        .font(.system(size: 9, design: .monospaced))
                                        .foregroundStyle(.secondary)
                                }
                            }
                            .padding(.horizontal, 7)
                            .padding(.vertical, 4)
                            .background(Capsule().fill(style.accent.opacity(0.11)))
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        }
        .padding(12)
    }

    private func workerHeader(_ w: AgentWorkerHot) -> some View {
        let style = AgentDriverStyle.forDriver(w.driver)
        return VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 7) {
                HarnessLogoView(driver: w.driver, appPath: w.guiAppPath, size: 22)
                VStack(alignment: .leading, spacing: 1) {
                    HStack(spacing: 5) {
                        Text(w.roleLabel)
                            .font(.system(size: 12.5, weight: .bold))
                        if !w.axis.isEmpty {
                            Text(w.axis)
                                .font(.system(size: 9, weight: .semibold, design: .monospaced))
                                .foregroundStyle(style.accent)
                                .padding(.horizontal, 5)
                                .padding(.vertical, 1)
                                .background(Capsule().fill(style.accent.opacity(0.14)))
                        }
                        SemanticPill(text: w.status.uppercased(), compact: true)
                    }
                    HStack(spacing: 6) {
                        Button(w.problem) { onSelect(.squad(w.problem)) }
                            .buttonStyle(.plain)
                            .font(.system(size: 10, weight: .semibold, design: .monospaced))
                            .foregroundStyle(.secondary)
                        if !w.lastCommitSha.isEmpty {
                            Text(w.lastCommitSha)
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(style.accent)
                        }
                        Text("streak \(w.noGainStreak)/6")
                            .font(.system(size: 9.5, design: .monospaced))
                            .foregroundStyle(w.isStagnated ? .orange : .secondary)
                        if let percent = w.contextPercent {
                            Text(lang.tr("上下文 \(percent)%", "Context \(percent)%"))
                                .font(.caption2.monospacedDigit())
                        }
                    }
                    if let cache = w.cache {
                        cacheLine(cache)
                    }
                }
                Spacer()
                Button { onJumpWorker(w) } label: {
                    Image(systemName: "arrow.up.forward.app.fill")
                        .font(.system(size: 10.5, weight: .semibold))
                }
                .buttonStyle(.borderedProminent)
                .tint(style.accent)
                .controlSize(.mini)
                .help(lang.tr("跳转至 \(style.displayName) 对话上下文", "Open \(style.displayName) conversation"))
                if !w.worktree.isEmpty {
                    Button { SystemNavigator.revealInFinder(w.worktree) } label: {
                        Image(systemName: "folder").font(.system(size: 10.5))
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.mini)
                }
                if w.isActive {
                    Button { onStopWorker(w.agentId, false) } label: {
                        Image(systemName: "stop.fill").font(.system(size: 9.5)).foregroundStyle(.red)
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.mini)
                }
            }

            if w.status == "error" && !w.error.isEmpty {
                Text(w.error)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .textSelection(.enabled)
            }
            if !diffStat.isEmpty {
                Text(diffStat)
                    .font(.system(size: 9.5, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .padding(6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 6).fill(Color.primary.opacity(0.04)))
            }
        }
        .padding(12)
    }

    private func messageBubble(_ msg: AgentBusMessage, showAgentTag: Bool) -> some View {
        let isOut = msg.direction == "orch_to_worker"
        let isSys = msg.direction == "system"
        let isSquad = msg.direction == "squad_note" || msg.direction == "squad_to_worker"
        let tint: Color = {
            switch msg.kind {
            case "breakthrough": return Color(red: 0.92, green: 0.68, blue: 0.05)
            case "stagnation", "question": return .orange
            case "escape_zero", "error": return .red
            case "directive": return .cyan
            case "result": return .green
            default: return isOut ? .cyan : (isSys ? .secondary : .teal)
            }
        }()

        return VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 5) {
                Text(isSquad ? lang.tr("题组", "SQUAD") : msg.kind == "directive" ? "ADMIN 📣" : (isSys ? "SYS" : (isOut ? "ORCH →" : "← WORKER")))
                    .font(.system(size: 9.5, weight: .bold, design: .monospaced))
                    .foregroundStyle(tint)
                if showAgentTag && !msg.agentId.isEmpty && msg.agentId != "admin" {
                    Button(msg.agentId) { onSelect(.worker(msg.agentId)) }
                        .buttonStyle(.plain)
                        .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
                Text(msg.kind.uppercased())
                    .font(.system(size: 8.5, weight: .heavy, design: .monospaced))
                    .foregroundStyle(tint)
                    .padding(.horizontal, 4.5)
                    .padding(.vertical, 1)
                    .background(Capsule().fill(tint.opacity(0.15)))
                if !msg.commitSha.isEmpty {
                    Text(msg.commitSha).font(.system(size: 9, design: .monospaced)).foregroundStyle(.secondary)
                }
                Spacer()
                Text("#\(msg.seq)").font(.system(size: 9, design: .monospaced)).foregroundStyle(.tertiary)
            }

            Text(msg.body)
                .font(.system(size: 11.5))
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(9)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(tint.opacity(msg.priority == "strategic" ? 0.13 : 0.07)))
        .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(tint.opacity(msg.priority == "strategic" ? 0.45 : 0.20), lineWidth: msg.priority == "strategic" ? 1.1 : 0.8))
    }

    private func workerComposer(_ worker: AgentWorkerHot) -> some View {
        VStack(spacing: 6) {
            TextField(lang.tr("发指令给 \(worker.roleLabel)…", "Message \(worker.roleLabel)…"), text: $draftMessage, axis: .vertical)
                .textFieldStyle(.plain)
                .font(.system(size: 12))
                .lineLimit(1...4)
                .padding(8)
                .background(RoundedRectangle(cornerRadius: 7).fill(Color.primary.opacity(0.05)))

            HStack(spacing: 6) {
                Button {
                    let txt = draftMessage.trimmingCharacters(in: .whitespacesAndNewlines)
                    guard !txt.isEmpty else { return }
                    draftMessage = ""
                    onSendWorkerMessage(worker.agentId, txt, "interrupt")
                } label: {
                    Label(lang.tr("打断纠偏", "Interrupt"), systemImage: "bolt.fill").font(.system(size: 10.5, weight: .semibold))
                }
                .buttonStyle(.bordered)
                .tint(.orange)
                .controlSize(.small)
                .disabled(isSending || draftMessage.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)

                Spacer()

                Button {
                    let txt = draftMessage.trimmingCharacters(in: .whitespacesAndNewlines)
                    guard !txt.isEmpty else { return }
                    draftMessage = ""
                    onSendWorkerMessage(worker.agentId, txt, "queue")
                } label: {
                    Label(lang.tr("发送", "Send"), systemImage: "paperplane.fill").font(.system(size: 10.5, weight: .semibold))
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                .disabled(isSending || draftMessage.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(10)
    }

    private func adminDirectiveComposer(targetProblem: String) -> some View {
        VStack(spacing: 6) {
            TextField(
                targetProblem.isEmpty
                    ? lang.tr("输入管理员情报 / 全局策略分发…", "Admin directive / hardware insight…")
                    : lang.tr("向 \(targetProblem) Squad 分发策略…", "Directive for \(targetProblem) squad…"),
                text: $draftMessage,
                axis: .vertical
            )
            .textFieldStyle(.plain)
            .font(.system(size: 12))
            .lineLimit(1...3)
            .padding(8)
            .background(RoundedRectangle(cornerRadius: 7).fill(Color.primary.opacity(0.05)))

            HStack(spacing: 6) {
                Toggle(lang.tr("同步广播给活跃 Worker", "Broadcast workers"), isOn: $broadcastDirective)
                    .toggleStyle(.checkbox)
                    .font(.system(size: 10.5))
                Spacer()
                Button {
                    let txt = draftMessage.trimmingCharacters(in: .whitespacesAndNewlines)
                    guard !txt.isEmpty else { return }
                    draftMessage = ""
                    onSendAdminDirective(txt, targetProblem, broadcastDirective)
                } label: {
                    Label(lang.tr("下发指令", "Dispatch"), systemImage: "megaphone.fill").font(.system(size: 10.5, weight: .semibold))
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                .disabled(isSending || draftMessage.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(10)
    }
}
