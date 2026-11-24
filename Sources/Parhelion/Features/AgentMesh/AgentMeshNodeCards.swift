import SwiftUI

struct AgentNodeJumpButton: View {
    let accent: Color
    let tooltip: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: "arrow.up.forward.app.fill")
                .font(.system(size: 10.5, weight: .semibold))
                .foregroundStyle(accent)
                .frame(width: 20, height: 20)
                .background(RoundedRectangle(cornerRadius: 5, style: .continuous).fill(accent.opacity(0.12)))
                .overlay(RoundedRectangle(cornerRadius: 5, style: .continuous).strokeBorder(accent.opacity(0.28), lineWidth: 0.7))
        }
        .buttonStyle(.plain)
        .help(tooltip)
    }
}

struct AgentOrchestratorCrownCard: View {
    let orchestrator: AgentOrchestratorHot
    let lang: AppLanguage
    let isSelected: Bool
    let onTap: () -> Void
    let onJump: () -> Void

    var body: some View {
        let style = AgentDriverStyle.forDriver(orchestrator.driver)
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                HarnessLogoView(driver: orchestrator.driver, appPath: orchestrator.guiAppPath, size: 30)
                    .shadow(color: style.accent.opacity(0.28), radius: 5)
                VStack(alignment: .leading, spacing: 1.5) {
                    HStack(spacing: 5) {
                        Text(orchestrator.displayName)
                            .font(.system(size: 12.5, weight: .bold))
                            .lineLimit(1)
                        Text("ORCH")
                            .font(.system(size: 8.5, weight: .heavy, design: .monospaced))
                            .foregroundStyle(style.accent)
                            .padding(.horizontal, 5)
                            .padding(.vertical, 1)
                            .background(Capsule().fill(style.accent.opacity(0.14)))
                    }
                    Text(orchestrator.workspaceName)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                Spacer(minLength: 2)
                Circle()
                    .fill(orchestrator.status == "active" ? Color.green : style.accent.opacity(0.7))
                    .frame(width: 7.5, height: 7.5)
                AgentNodeJumpButton(
                    accent: style.accent,
                    tooltip: lang.tr(
                        "跳转至 \(orchestrator.displayName) 主控对话上下文",
                        "Open \(orchestrator.displayName) orchestrator context"
                    ),
                    action: onJump
                )
            }

            HStack(spacing: 8) {
                Text("\(orchestrator.squadCount) squads")
                    .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                    .foregroundStyle(.secondary)
                Text("·").foregroundStyle(.tertiary)
                Text("\(orchestrator.activeWorkers) mcp")
                    .font(.system(size: 9.5, weight: .bold, design: .monospaced))
                    .foregroundStyle(style.accent)
                Spacer()
                Text("\(orchestrator.nativeSubagents) native")
                    .font(.system(size: 9.5, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 13, style: .continuous)
                .fill(.regularMaterial)
                .shadow(color: style.accent.opacity(isSelected ? 0.28 : 0.10), radius: isSelected ? 12 : 5)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 13, style: .continuous)
                .strokeBorder(style.gradient, lineWidth: isSelected ? 1.8 : 1.0)
        )
        .contentShape(Rectangle())
        .onTapGesture(perform: onTap)
    }
}

struct AgentNativeSubagentCard: View {
    let sub: AgentNativeSubagentHot
    let appPath: String
    let lang: AppLanguage
    let onJump: () -> Void

    var body: some View {
        let style = AgentDriverStyle.forDriver(sub.driver)
        let running = sub.status == "running"
        HStack(spacing: 7) {
            HarnessLogoView(driver: sub.driver, appPath: appPath, size: 16)
            VStack(alignment: .leading, spacing: 1.5) {
                HStack(spacing: 4) {
                    Text(sub.roleLabel)
                        .font(.system(size: 10.5, weight: .semibold))
                        .lineLimit(1)
                    Spacer()
                    Text("s\(sub.steps)")
                        .font(.system(size: 9, design: .monospaced))
                        .foregroundStyle(.secondary)
                    Circle()
                        .strokeBorder(running ? Color.green : style.accent, lineWidth: 1.5)
                        .frame(width: 6.5, height: 6.5)
                }
                Text(sub.lastTool.isEmpty ? sub.problem : "\(sub.problem) · \(sub.lastTool)")
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            AgentNodeJumpButton(
                accent: style.accent,
                tooltip: lang.tr(
                    "跳转至 \(style.displayName) 子代理对话：\(sub.title.isEmpty ? sub.roleLabel : sub.title)",
                    "Open \(style.displayName) subagent conversation"
                ),
                action: onJump
            )
        }
        .padding(.horizontal, 9)
        .padding(.vertical, 6)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(.regularMaterial))
        .overlay(
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .strokeBorder(style.accent.opacity(running ? 0.55 : 0.22), style: StrokeStyle(lineWidth: 1, dash: [4, 3]))
        )
    }
}

struct AgentExternalWorkerCard: View {
    let worker: AgentWorkerHot
    let lang: AppLanguage
    let isSelected: Bool
    var showProblemTitle: Bool = false
    let onTap: () -> Void
    let onJump: () -> Void
    let onHoverChange: (Bool) -> Void

    var body: some View {
        let style = AgentDriverStyle.forDriver(worker.driver)
        let stateColor: Color = {
            switch worker.status {
            case "working", "spawning": return style.accent
            case "waiting_input": return .orange
            case "error": return .red
            case "idle": return .green
            default: return .secondary
            }
        }()
        HStack(spacing: 8) {
            RoundedRectangle(cornerRadius: 2)
                .fill(style.accent.opacity(worker.isActive ? 0.95 : 0.35))
                .frame(width: 3)
                .padding(.vertical, 3)

            HarnessLogoView(driver: worker.driver, appPath: worker.guiAppPath, size: 22)

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 5) {
                    Text(showProblemTitle ? worker.problem : worker.roleLabel)
                        .font(.system(size: 11.5, weight: .semibold))
                        .lineLimit(1)
                    if !worker.axis.isEmpty {
                        Text(worker.axis)
                            .font(.system(size: 9, weight: .semibold, design: .monospaced))
                            .foregroundStyle(style.accent)
                            .padding(.horizontal, 5)
                            .padding(.vertical, 1)
                            .background(Capsule().fill(style.accent.opacity(0.13)))
                            .lineLimit(1)
                    }
                    Spacer(minLength: 2)
                    if worker.isStagnated {
                        Text("\(worker.noGainStreak)/6")
                            .font(.system(size: 8.5, weight: .heavy, design: .monospaced))
                            .foregroundStyle(.white)
                            .padding(.horizontal, 4.5)
                            .padding(.vertical, 1)
                            .background(Capsule().fill(Color.orange))
                    } else if worker.unreadCount > 0 {
                        Text("\(worker.unreadCount)")
                            .font(.system(size: 8.5, weight: .bold, design: .monospaced))
                            .foregroundStyle(.white)
                            .padding(.horizontal, 4.5)
                            .padding(.vertical, 1)
                            .background(Capsule().fill(style.accent))
                    }
                    Circle().fill(stateColor).frame(width: 7, height: 7)
                    AgentNodeJumpButton(
                        accent: style.accent,
                        tooltip: lang.tr(
                            "跳转至 \(style.displayName) 对话上下文 (\(worker.roleLabel))",
                            "Open \(style.displayName) conversation (\(worker.roleLabel))"
                        ),
                        action: onJump
                    )
                }

                HStack(spacing: 6) {
                    Text(showProblemTitle ? worker.roleLabel : (worker.lastCommitSha.isEmpty ? worker.worktreeMode : worker.lastCommitSha))
                        .font(.system(size: 9.5, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                    Spacer(minLength: 2)
                    Text(worker.lastTool.isEmpty ? "t\(worker.turnCount) · \(worker.elapsedSec)s" : worker.lastTool)
                        .font(.system(size: 9.5, design: .monospaced))
                        .foregroundStyle(worker.lastTool.isEmpty ? Color.secondary.opacity(0.75) : style.accent)
                        .lineLimit(1)
                }
            }
        }
        .padding(.horizontal, 9)
        .padding(.vertical, 8)
        .frame(minWidth: 195, maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(Color(nsColor: .controlBackgroundColor).opacity(0.85))
                .shadow(color: style.accent.opacity(isSelected ? 0.28 : 0.04), radius: isSelected ? 8 : 2)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(
                    worker.isStagnated ? Color.orange.opacity(0.8) : (isSelected ? style.accent : Color.primary.opacity(0.09)),
                    lineWidth: isSelected || worker.isStagnated ? 1.6 : 1
                )
        )
        .opacity(worker.isActive ? 1.0 : 0.58)
        .contentShape(Rectangle())
        .onTapGesture(perform: onTap)
        .onHover(perform: onHoverChange)
    }
}

struct AgentGhostDriverCard: View {
    let driver: AgentReadyDriverHot
    let onTap: () -> Void

    var body: some View {
        let style = AgentDriverStyle.forDriver(driver.kind)
        Button(action: onTap) {
            HStack(spacing: 10) {
                HarnessLogoView(driver: driver.kind, appPath: driver.guiAppPath, size: 28)
                VStack(alignment: .leading, spacing: 2) {
                    Text(driver.displayName)
                        .font(.system(size: 12, weight: .semibold))
                        .lineLimit(1)
                    Text(driver.protocolName)
                        .font(.system(size: 9.5, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Image(systemName: "plus.circle.fill")
                    .font(.system(size: 15))
                    .foregroundStyle(style.accent.opacity(0.85))
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 11, style: .continuous).fill(style.accent.opacity(0.055)))
            .overlay(
                RoundedRectangle(cornerRadius: 11, style: .continuous)
                    .strokeBorder(style.accent.opacity(0.35), style: StrokeStyle(lineWidth: 1.1, dash: [5, 4]))
            )
        }
        .buttonStyle(.plain)
    }
}
