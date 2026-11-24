import SwiftUI

struct AgentProblemSquadLaneView: View {
    let squad: AgentSquadHot
    let workers: [AgentWorkerHot]
    let lang: AppLanguage
    let selection: AgentMeshSelection
    let onSelect: (AgentMeshSelection) -> Void
    let onQuickSpawn: (_ driver: String, _ problem: String) -> Void
    let onJumpWorker: (AgentWorkerHot) -> Void
    let onHoverWorker: (_ workerId: String, _ hovering: Bool) -> Void

    var body: some View {
        let selected = selection == .squad(squad.problem)
        let isPipeline = squad.collabMode == "pipeline"
        let modeColor: Color = squad.escapeLocked
            ? .red
            : (isPipeline ? .purple : (squad.collabMode == "orthogonal" ? .cyan : .secondary))

        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 7) {
                Button { onSelect(.squad(squad.problem)) } label: {
                    HStack(spacing: 6) {
                        DifficultyBadge(difficulty: squad.difficulty)
                        Text(squad.problem)
                            .font(.system(size: 12.5, weight: .bold))
                        if squad.currentScore > 0 {
                            Text(String(format: "%.2f", squad.currentScore))
                                .font(.system(size: 11, weight: .bold, design: .monospaced))
                                .foregroundStyle(.secondary)
                        }
                        Text(squad.collabMode.uppercased())
                            .font(.system(size: 8.5, weight: .heavy, design: .monospaced))
                            .foregroundStyle(modeColor)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(Capsule().fill(modeColor.opacity(0.14)))
                        if squad.escapeLocked {
                            Text("ESCAPE_LOCK")
                                .font(.system(size: 8.5, weight: .heavy, design: .monospaced))
                                .foregroundStyle(.white)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 2)
                                .background(Capsule().fill(Color.red))
                        }
                    }
                }
                .buttonStyle(.plain)

                Spacer()

                Button { onQuickSpawn("", squad.problem) } label: {
                    Image(systemName: "plus")
                        .font(.system(size: 10, weight: .bold))
                }
                .buttonStyle(.bordered)
                .controlSize(.mini)
                .help(lang.tr("向 \(squad.problem) 增派异构 Worker", "Add worker to \(squad.problem) squad"))
            }

            if isPipeline {
                HStack(spacing: 6) {
                    ForEach(Array(workers.enumerated()), id: \.element.id) { idx, w in
                        if idx > 0 {
                            Image(systemName: "chevron.right")
                                .font(.system(size: 10, weight: .bold))
                                .foregroundStyle(.secondary)
                        }
                        AgentExternalWorkerCard(
                            worker: w,
                            lang: lang,
                            isSelected: selection == .worker(w.agentId),
                            onTap: { onSelect(.worker(w.agentId)) },
                            onJump: { onJumpWorker(w) },
                            onHoverChange: { onHoverWorker(w.agentId, $0) }
                        )
                    }
                }
            } else {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 205), spacing: 8)], spacing: 8) {
                    ForEach(workers) { w in
                        AgentExternalWorkerCard(
                            worker: w,
                            lang: lang,
                            isSelected: selection == .worker(w.agentId),
                            onTap: { onSelect(.worker(w.agentId)) },
                            onJump: { onJumpWorker(w) },
                            onHoverChange: { onHoverWorker(w.agentId, $0) }
                        )
                    }
                }
            }
        }
        .padding(11)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(.regularMaterial))
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(
                    squad.escapeLocked ? Color.red.opacity(0.65) : (selected ? modeColor : Color.primary.opacity(0.09)),
                    lineWidth: selected || squad.escapeLocked ? 1.5 : 1
                )
        )
    }
}

struct AgentHarnessSwimlaneView: View {
    let driver: AgentReadyDriverHot
    let workers: [AgentWorkerHot]
    let lang: AppLanguage
    let selection: AgentMeshSelection
    let onSelect: (AgentMeshSelection) -> Void
    let onQuickSpawn: (_ driver: String, _ problem: String) -> Void
    let onJumpWorker: (AgentWorkerHot) -> Void
    let onHoverWorker: (_ workerId: String, _ hovering: Bool) -> Void

    var body: some View {
        let style = AgentDriverStyle.forDriver(driver.kind)
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                HarnessLogoView(driver: driver.kind, appPath: driver.guiAppPath, size: 22)
                Text(driver.displayName)
                    .font(.system(size: 12.5, weight: .bold))
                Text(driver.protocolName)
                    .font(.system(size: 9.5, design: .monospaced))
                    .foregroundStyle(.secondary)
                Spacer()
                Text("\(workers.filter(\.isActive).count) active")
                    .font(.system(size: 10, weight: .bold, design: .monospaced))
                    .foregroundStyle(style.accent)
                Button { onQuickSpawn(driver.kind, "") } label: {
                    Image(systemName: "plus").font(.system(size: 10, weight: .bold))
                }
                .buttonStyle(.bordered)
                .controlSize(.mini)
            }

            if workers.isEmpty {
                Text(lang.tr("待命 · 点击 + 派发任务", "Ready · click + to dispatch"))
                    .font(.system(size: 10.5, design: .monospaced))
                    .foregroundStyle(.tertiary)
                    .padding(.vertical, 4)
            } else {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 205), spacing: 8)], spacing: 8) {
                    ForEach(workers) { w in
                        AgentExternalWorkerCard(
                            worker: w,
                            lang: lang,
                            isSelected: selection == .worker(w.agentId),
                            showProblemTitle: true,
                            onTap: { onSelect(.worker(w.agentId)) },
                            onJump: { onJumpWorker(w) },
                            onHoverChange: { onHoverWorker(w.agentId, $0) }
                        )
                    }
                }
            }
        }
        .padding(11)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(.regularMaterial))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(style.accent.opacity(0.28), lineWidth: 1))
    }
}

struct AgentMeshQuickReadyDock: View {
    let drivers: [AgentReadyDriverHot]
    let onQuickSpawn: (_ driver: String, _ problem: String) -> Void

    var body: some View {
        HStack(spacing: 6) {
            ForEach(drivers) { drv in
                let style = AgentDriverStyle.forDriver(drv.kind)
                Button { onQuickSpawn(drv.kind, "") } label: {
                    HStack(spacing: 5) {
                        HarnessLogoView(driver: drv.kind, appPath: drv.guiAppPath, size: 15)
                        Text(drv.displayName)
                            .font(.system(size: 10, weight: .semibold))
                        Image(systemName: "plus")
                            .font(.system(size: 8.5, weight: .bold))
                            .foregroundStyle(style.accent)
                    }
                    .padding(.horizontal, 9)
                    .padding(.vertical, 5)
                    .background(Capsule().fill(style.accent.opacity(0.09)))
                    .overlay(Capsule().strokeBorder(style.accent.opacity(0.28), lineWidth: 0.8))
                }
                .buttonStyle(.plain)
            }
        }
    }
}
