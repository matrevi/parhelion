import AppKit
import SwiftUI

/// 「经验转交 / Bridge」 — per-agent delivery + receipt console.
///
/// Renders exactly the three states the bridge protocol promises:
/// 待总结 (`pending`) · 已取 (`pulled`) · 已回执 (`acked`).
struct BridgePanelView: View {
    @EnvironmentObject var store: ContestStore
    @UIState private var filter: String = "all"
    @UIState private var expandedGroups: Set<String> = []

    private var lang: AppLanguage { store.lang }
    private var status: BridgeStatusResponse? { store.bridgeStatus }

    private var activeAgents: [BridgeAgent] {
        (status?.agents ?? []).filter { agent in
            if filter == "all" { return true }
            return agent.items.contains { $0.state == filter }
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            topBar
            Divider()
            if store.bridgeError != nil && status == nil {
                degradedState(store.bridgeError ?? "unknown")
            } else if let status {
                if status.agents.isEmpty {
                    emptyRegistry
                } else {
                    ScrollView {
                        LazyVStack(spacing: 10) {
                            ForEach(activeAgents) { agent in
                                agentCard(agent)
                            }
                        }
                        .padding(12)
                    }
                }
            } else {
                loadingState
            }
        }
        .onAppear { Task { await store.reloadBridge(force: true) } }
    }

    // MARK: - Top bar

    private var topBar: some View {
        HStack(spacing: 10) {
            Label(lang.tr("经验转交 · Bridge", "Experience Bridge"), systemImage: "arrow.triangle.branch")
                .font(.system(size: 13.5, weight: .bold))

            if let totals = status?.totals {
                Divider().frame(height: 18)
                SemanticPill(text: lang.tr("待总结 \(totals.pending)", "TO SUMMARIZE \(totals.pending)"), icon: "tray.full.fill", compact: true)
                SemanticPill(text: lang.tr("已取 \(totals.pulled)", "CLAIMED \(totals.pulled)"), icon: "hand.raised.fill", compact: true)
                SemanticPill(text: lang.tr("已回执 \(totals.acked)", "RECEIPTS \(totals.acked)"), icon: "checkmark.seal.fill", compact: true)
                Text(lang.tr("index 回执 \(status?.summariesCount ?? 0)", "index receipts \(status?.summariesCount ?? 0)"))
                    .font(.system(size: 10.5, design: .monospaced))
                    .foregroundStyle(.secondary)
            }

            Spacer()

            Picker("", selection: $filter) {
                Text(lang.tr("全部", "All")).tag("all")
                Text(lang.tr("待总结", "Pending")).tag("pending")
                Text(lang.tr("已取", "Claimed")).tag("pulled")
                Text(lang.tr("已回执", "Acked")).tag("acked")
            }
            .pickerStyle(.segmented)
            .controlSize(.small)
            .frame(width: 232)

            Button {
                Task { await store.syncBridge() }
            } label: {
                Label(lang.tr("同步投递", "Deliver"), systemImage: "arrow.down.to.line")
                    .font(.system(size: 11.5))
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .help(lang.tr("把新突破按 scope 路由进 inbox（幂等）", "Route new breakthroughs into inbox/ (idempotent)"))

            Button {
                Task { await store.collectBridge() }
            } label: {
                Label(lang.tr("收集回执", "Collect"), systemImage: "tray.and.arrow.down")
                    .font(.system(size: 11.5))
            }
            .buttonStyle(.bordered)
            .controlSize(.small)

            Menu {
                if let path = status?.agentsPath, !path.isEmpty {
                    Button(lang.tr("打开 agents.json", "Open agents.json")) {
                        NSWorkspace.shared.open(URL(fileURLWithPath: path))
                    }
                }
                if let dir = status?.bridgeDir, !dir.isEmpty {
                    Button(lang.tr("打开桥目录", "Open bridge folder")) {
                        NSWorkspace.shared.open(URL(fileURLWithPath: dir))
                    }
                    Button(lang.tr("打开回执区", "Open outbox")) {
                        NSWorkspace.shared.open(URL(fileURLWithPath: dir + "/outbox"))
                    }
                    Button(lang.tr("打开投递区", "Open inbox")) {
                        NSWorkspace.shared.open(URL(fileURLWithPath: dir + "/inbox"))
                    }
                }
                Divider()
                Button(lang.tr("复制订阅者模板", "Copy subscriber template")) {
                    copyToPasteboard(Self.subscriberTemplate)
                }
                Button(lang.tr("复制协议文档路径", "Copy protocol path")) {
                    copyToPasteboard("ops-supervisor/bridge/README.md")
                }
            } label: {
                Image(systemName: "ellipsis.circle")
            }
            .menuStyle(.borderlessButton)
            .frame(width: 28)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
        .background(Color.primary.opacity(0.025))
    }

    // MARK: - Agent card

    private func agentCard(_ agent: BridgeAgent) -> some View {
        SurfaceCard(padding: 12) {
            VStack(alignment: .leading, spacing: 9) {
                HStack(spacing: 8) {
                    Image(systemName: kindSymbol(agent.kind))
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(kindColor(agent.kind))
                        .frame(width: 18)
                    Text(agent.display)
                        .font(.system(size: 13, weight: .bold))
                    Text(agent.agentId)
                        .font(.system(size: 10.5, design: .monospaced))
                        .foregroundStyle(.secondary)
                    SemanticPill(text: agent.transport.mode.uppercased(), icon: "arrow.left.arrow.right", compact: true)
                    SemanticPill(text: agent.kind.uppercased(), compact: true)
                    if !agent.enabled {
                        SemanticPill(text: lang.tr("已停用", "DISABLED"), compact: true)
                    }
                    if agent.minDelta > 0 {
                        Text(String(format: "Δ ≥ %.1f", agent.minDelta))
                            .font(.system(size: 9.5, design: .monospaced))
                            .foregroundStyle(.tertiary)
                    }
                    Spacer()
                    Text(lang.tr(
                        "待总结 \(agent.counts.pending) · 已取 \(agent.counts.pulled) · 已回执 \(agent.counts.acked)",
                        "\(agent.counts.pending) pending · \(agent.counts.pulled) claimed · \(agent.counts.acked) receipts"
                    ))
                    .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                    .foregroundStyle(.secondary)
                }

                scopeRow(agent)

                if agent.items.isEmpty {
                    Text(lang.tr("暂无投递 — 命中该订阅者 scope 的突破会自动投递到这里",
                                 "No deliveries yet — breakthroughs matching this scope land here automatically"))
                        .font(.system(size: 10.5))
                        .foregroundStyle(.tertiary)
                } else {
                    VStack(spacing: 6) {
                        ForEach(["pending", "pulled", "acked"], id: \.self) { group in
                            let items = agent.items.filter { $0.state == group }
                            if !items.isEmpty {
                                stateGroup(agent: agent, group: group, items: items)
                            }
                        }
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func scopeRow(_ agent: BridgeAgent) -> some View {
        HStack(spacing: 5) {
            Text(lang.tr("范围", "Scope"))
                .font(.system(size: 9.5, weight: .semibold))
                .foregroundStyle(.tertiary)
            ForEach(agent.scope.paths.prefix(3), id: \.self) { path in
                chip(path, tint: .accentColor)
            }
            ForEach(agent.scope.tags.prefix(4), id: \.self) { tag in
                chip("#" + tag, tint: Color(red: 0.88, green: 0.46, blue: 0.08))
            }
            if agent.scope.paths.isEmpty && agent.scope.tags.isEmpty {
                Text(lang.tr("未声明（不会隐式收到任何条目）", "none declared (receives nothing implicitly)"))
                    .font(.system(size: 9.5))
                    .foregroundStyle(.tertiary)
            }
            Spacer()
            if agent.transport.mode == "exec" {
                Text(agent.transport.exec.joined(separator: " "))
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            } else if agent.transport.mode == "http" {
                Text(agent.transport.url)
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
        }
    }

    private func stateGroup(agent: BridgeAgent, group: String, items: [BridgeItem]) -> some View {
        let key = "\(agent.agentId)#\(group)"
        let isOpen = expandedGroups.contains(key) || group == "pending"
        return VStack(alignment: .leading, spacing: 4) {
            Button {
                if expandedGroups.contains(key) { expandedGroups.remove(key) } else { expandedGroups.insert(key) }
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: isOpen ? "chevron.down" : "chevron.right")
                        .font(.system(size: 8.5, weight: .bold))
                        .foregroundStyle(.secondary)
                    Text(groupTitle(group))
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(groupColor(group))
                    Text("\(items.count)")
                        .font(.system(size: 9.5, weight: .bold, design: .monospaced))
                        .foregroundStyle(.secondary)
                    Spacer()
                }
            }
            .buttonStyle(.plain)

            if isOpen {
                VStack(spacing: 3) {
                    ForEach(items) { item in
                        itemRow(agent: agent, item: item)
                    }
                }
            }
        }
        .padding(.top, 2)
    }

    private func itemRow(agent: BridgeAgent, item: BridgeItem) -> some View {
        HStack(spacing: 7) {
            Circle()
                .fill(groupColor(item.state))
                .frame(width: 5.5, height: 5.5)
            Text(item.entryId)
                .font(.system(size: 10.5, weight: .medium, design: .monospaced))
                .lineLimit(1)
                .truncationMode(.middle)
                .frame(maxWidth: 320, alignment: .leading)
            if !item.problem.isEmpty {
                Text(item.problem)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
            }
            if let delta = item.deltaRaw {
                Text(String(format: "%+.2f", delta))
                    .font(.system(size: 10, weight: .bold, design: .monospaced))
                    .foregroundStyle(Color(red: 0.10, green: 0.72, blue: 0.42))
            }
            if item.selfInitiated {
                Text(lang.tr("主动回执", "volunteered"))
                    .font(.system(size: 9))
                    .foregroundStyle(.tertiary)
            }
            if item.outboxExists {
                Text(lang.tr("回执 \(item.outboxLines) 行", "receipt \(item.outboxLines) lines"))
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundStyle(.tertiary)
            }
            if let pushError = item.lastError, !pushError.isEmpty {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.system(size: 9))
                    .foregroundStyle(.orange)
                    .help(pushError)
            }
            Spacer(minLength: 6)

            Button {
                copyToPasteboard(commandBlock(agent: agent, item: item))
                store.bannerMessage = lang.tr("已复制 \(item.entryId) 的执行指令",
                                              "Copied instruction for \(item.entryId)")
            } label: {
                Label(lang.tr("复制指令", "Copy cmd"), systemImage: "doc.on.doc")
                    .font(.system(size: 9.5))
            }
            .buttonStyle(.bordered)
            .controlSize(.mini)

            Menu {
                if item.inboxExists {
                    Button(lang.tr("在访达中显示投递文件", "Reveal delivery note")) {
                        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: item.inbox)])
                    }
                    Button(lang.tr("复制投递文件路径", "Copy delivery path")) {
                        copyToPasteboard(item.inbox)
                    }
                }
                if item.outboxExists {
                    Button(lang.tr("打开回执", "Open receipt")) {
                        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: item.outbox)])
                    }
                }
                if item.state == "pending" {
                    Button(lang.tr("标记为已取", "Mark claimed")) {
                        Task { await store.ackBridgeItem(agentId: agent.agentId, entryId: item.entryId) }
                    }
                }
                if agent.transport.mode != "file" && item.state != "acked" {
                    Button(lang.tr("唤起 \(agent.transport.mode) 推送", "Invoke \(agent.transport.mode) push")) {
                        Task { await store.pushBridgeItem(agentId: agent.agentId, entryId: item.entryId) }
                    }
                }
                Button(lang.tr("复制条目查看命令", "Copy breakthrough show")) {
                    copyToPasteboard("opsb breakthrough show \(item.entryId)")
                }
            } label: {
                Image(systemName: "ellipsis")
                    .font(.system(size: 9.5))
            }
            .menuStyle(.borderlessButton)
            .frame(width: 22)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.primary.opacity(0.028)))
    }

    // MARK: - States

    private var emptyRegistry: some View {
        VStack(spacing: 10) {
            Image(systemName: "person.2.fill")
                .font(.system(size: 26))
                .foregroundStyle(.secondary)
            Text(lang.tr("尚未注册任何订阅 Agent", "No subscriber registered yet"))
                .font(.system(size: 13, weight: .semibold))
            Text(lang.tr("在 agents.json 里追加一个订阅者（scope.paths / scope.tags 决定它能收到哪些经验）",
                         "Append a subscriber to agents.json — scope.paths / scope.tags decide what it receives"))
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 460)
            HStack(spacing: 8) {
                Button(lang.tr("复制订阅者模板", "Copy subscriber template")) {
                    copyToPasteboard(Self.subscriberTemplate)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                if let path = status?.agentsPath, !path.isEmpty {
                    Button(lang.tr("打开 agents.json", "Open agents.json")) {
                        NSWorkspace.shared.open(URL(fileURLWithPath: path))
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func degradedState(_ message: String) -> some View {
        VStack(spacing: 10) {
            Image(systemName: "bolt.horizontal.circle")
                .font(.system(size: 26))
                .foregroundStyle(.orange)
            Text(lang.tr("Bridge 状态暂不可用", "Bridge status unavailable"))
                .font(.system(size: 13, weight: .semibold))
            Text(message)
                .font(.system(size: 10.5, design: .monospaced))
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 520)
            HStack(spacing: 8) {
                Button(lang.tr("重试", "Retry")) {
                    Task { await store.reloadBridge(force: true) }
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                Button(lang.tr("复制诊断命令", "Copy diagnostic")) {
                    copyToPasteboard("opsb bridge status --json")
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var loadingState: some View {
        VStack(spacing: 8) {
            ProgressView()
            Text(lang.tr("正在读取 bridge 台账…", "Reading bridge ledger…"))
                .font(.system(size: 11.5))
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: - Helpers

    private func commandBlock(agent: BridgeAgent, item: BridgeItem) -> String {
        var lines = [
            "# \(item.entryId)  →  \(agent.agentId)",
        ]
        if item.inboxExists {
            lines.append("cat \"\(item.inbox)\"")
        } else {
            lines.append("opsb bridge pull \(agent.agentId) --ack --entry \(item.entryId)")
        }
        lines.append("opsb bridge publish \(agent.agentId) \(item.entryId) --file -")
        return lines.joined(separator: "\n")
    }

    private func groupTitle(_ group: String) -> String {
        switch group {
        case "pending": return lang.tr("待总结", "To summarize")
        case "pulled": return lang.tr("已取", "Claimed")
        case "acked": return lang.tr("已回执", "Receipts")
        default: return group
        }
    }

    private func groupColor(_ group: String) -> Color {
        switch group {
        case "pending": return Color(red: 0.88, green: 0.46, blue: 0.08)
        case "pulled": return Color(red: 0.20, green: 0.56, blue: 0.96)
        case "acked": return Color(red: 0.10, green: 0.72, blue: 0.42)
        default: return .secondary
        }
    }

    private func kindSymbol(_ kind: String) -> String {
        switch kind {
        case "dsh": return "sparkles"
        case "codex": return "chevron.left.forwardslash.chevron.right"
        case "antigravity": return "antenna.radiowaves.left.and.right"
        default: return "terminal"
        }
    }

    private func kindColor(_ kind: String) -> Color {
        switch kind {
        case "dsh": return Color(red: 0.58, green: 0.38, blue: 0.95)
        case "codex": return Color(red: 0.12, green: 0.68, blue: 0.62)
        case "antigravity": return Color(red: 0.20, green: 0.56, blue: 0.96)
        default: return .secondary
        }
    }

    private func chip(_ text: String, tint: Color) -> some View {
        Text(text)
            .font(.system(size: 9, weight: .medium, design: .monospaced))
            .foregroundStyle(tint)
            .padding(.horizontal, 5)
            .padding(.vertical, 1.5)
            .background(Capsule().fill(tint.opacity(0.12)))
    }

    private func copyToPasteboard(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    private static let subscriberTemplate = """
    {
      "agent_id": "my-agent",
      "display": "My Agent",
      "kind": "cli",
      "scope": { "paths": ["bench/ConcatD"], "tags": ["多模态分核切分"] },
      "transport": { "mode": "file" }
    }
    """
}
