import SwiftUI

enum AgentMeshGrouping: String, CaseIterable, Identifiable {
    case byProblem
    case byHarness
    var id: String { rawValue }
}

enum AgentMeshSelection: Equatable {
    case orchestrator
    case squad(String)
    case worker(String)
}

struct MeshConnectionAnchors: PreferenceKey {
    static let defaultValue: [String: Anchor<CGPoint>] = [:]
    static func reduce(value: inout [String: Anchor<CGPoint>], nextValue: () -> [String: Anchor<CGPoint>]) {
        value.merge(nextValue()) { _, next in next }
    }
}

struct AgentMeshCanvasView: View {
    let mesh: AgentMeshHotState
    let lang: AppLanguage
    let grouping: AgentMeshGrouping
    let selection: AgentMeshSelection
    let onSelect: (AgentMeshSelection) -> Void
    let onQuickSpawn: (_ driver: String, _ problem: String) -> Void
    let onJumpOrchestrator: () -> Void
    let onJumpSubagent: (AgentNativeSubagentHot) -> Void
    let onJumpWorker: (AgentWorkerHot) -> Void

    @Environment(\.appearsActive) private var appearsActive
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @UIState private var hoveredWorkerId: String? = nil

    private var readyExternalDrivers: [AgentReadyDriverHot] {
        mesh.readyDrivers.filter { $0.installed && $0.hasCli }
    }

    private var hasLiveAnimation: Bool {
        let now = Date().timeIntervalSince1970
        let working = mesh.workers.contains { $0.isLive } || mesh.nativeSubagents.contains { $0.status == "running" }
        let pulsing = mesh.pulses.contains { (now - $0.epoch) < 6.0 }
        return appearsActive && (working || pulsing || hoveredWorkerId != nil)
    }

    private var activeHarnessGroups: [(driver: AgentReadyDriverHot, workers: [AgentWorkerHot])] {
        var out: [(AgentReadyDriverHot, [AgentWorkerHot])] = []
        for drv in mesh.readyDrivers {
            let ws = mesh.workers.filter { $0.driver == drv.kind }
            if !ws.isEmpty || (drv.installed && drv.hasCli) {
                out.append((drv, ws))
            }
        }
        return out
    }

    var body: some View {
        ZStack(alignment: .topLeading) {
            dotGridBackdrop

            HStack(alignment: .top, spacing: 0) {
                // Left Fixed Strategic Hub (Orchestrator + 4 Strategic Triggers + Native Subagents)
                leftStrategicHub
                    .frame(width: 242)
                    .padding(.leading, 14)
                    .padding(.vertical, 14)

                // Center Bus-Trunk Vector Spine (1 clean curved trunk per Squad / Harness lane!)
                Color.clear
                    .frame(width: 66)

                // Right Scrollable Squad / Harness Swimlane Field
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        if mesh.workers.isEmpty {
                            emptyReadyGrid
                        } else if grouping == .byProblem {
                            ForEach(mesh.squads) { squad in
                                AgentProblemSquadLaneView(
                                    squad: squad,
                                    workers: mesh.workers.filter { squad.workerIds.contains($0.agentId) },
                                    lang: lang,
                                    selection: selection,
                                    onSelect: onSelect,
                                    onQuickSpawn: onQuickSpawn,
                                    onJumpWorker: onJumpWorker,
                                    onHoverWorker: handleHover
                                )
                            }
                        } else {
                            ForEach(activeHarnessGroups, id: \.driver.kind) { grp in
                                AgentHarnessSwimlaneView(
                                    driver: grp.driver,
                                    workers: grp.workers,
                                    lang: lang,
                                    selection: selection,
                                    onSelect: onSelect,
                                    onQuickSpawn: onQuickSpawn,
                                    onJumpWorker: onJumpWorker,
                                    onHoverWorker: handleHover
                                )
                            }
                        }

                        if !mesh.workers.isEmpty && !readyExternalDrivers.isEmpty {
                            AgentMeshQuickReadyDock(drivers: readyExternalDrivers, onQuickSpawn: onQuickSpawn)
                                .padding(.top, 4)
                        }
                    }
                    .padding(.trailing, 14)
                    .padding(.vertical, 14)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .overlayPreferenceValue(MeshConnectionAnchors.self) { anchors in
            GeometryReader { geometry in
                if let source = anchors["bus"] {
                    busTrunkBridge(source: geometry[source], targets: anchors.filter { $0.key != "bus" }
                        .mapValues { geometry[$0] })
                }
            }
            .allowsHitTesting(false)
        }
        .clipped()
    }

    private var dotGridBackdrop: some View {
        Canvas { ctx, size in
            var y: CGFloat = 14
            while y < size.height {
                var x: CGFloat = 14
                while x < size.width {
                    ctx.fill(Path(ellipseIn: CGRect(x: x, y: y, width: 1.3, height: 1.3)), with: .color(Color.primary.opacity(0.075)))
                    x += 22
                }
                y += 22
            }
        }
        .background(Color.primary.opacity(0.015))
    }

    private var leftStrategicHub: some View {
        let orchStyle = AgentDriverStyle.forDriver(mesh.orchestrator.driver)
        return VStack(alignment: .leading, spacing: 10) {
            AgentOrchestratorCrownCard(
                orchestrator: mesh.orchestrator,
                lang: lang,
                isSelected: selection == .orchestrator,
                onTap: { onSelect(.orchestrator) },
                onJump: onJumpOrchestrator
            )

            // Bus Status Pill
            HStack(spacing: 6) {
                Circle().fill(orchStyle.accent).frame(width: 6.5, height: 6.5)
                Text("MCP BUS")
                    .font(.system(size: 9.5, weight: .heavy, design: .monospaced))
                Text("#\(mesh.latestSeq)")
                    .font(.system(size: 9.5, design: .monospaced))
                    .foregroundStyle(.secondary)
                Spacer()
                Text(lang.tr("无并发限额", "UNLIMITED"))
                    .font(.system(size: 8.5, weight: .bold, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(.regularMaterial))
            .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(orchStyle.accent.opacity(0.32), lineWidth: 0.9))
            .anchorPreference(key: MeshConnectionAnchors.self, value: .trailing) { ["bus": $0] }

            // 4 Strategic Escalation Signal Strip (only non-zero triggers glow!)
            strategicTriggerGrid

            Divider().padding(.vertical, 2)

            // Native Subagents Compact Section
            HStack(spacing: 5) {
                Text(lang.tr("原生同构 · NATIVE", "NATIVE SUBAGENTS"))
                    .font(.system(size: 9, weight: .bold, design: .monospaced))
                    .foregroundStyle(.secondary)
                Spacer()
                Text("\(mesh.nativeSubagents.count)")
                    .font(.system(size: 9, weight: .bold, design: .monospaced))
                    .foregroundStyle(.tertiary)
            }

            if mesh.nativeSubagents.isEmpty {
                HStack(spacing: 6) {
                    Circle().strokeBorder(Color.secondary.opacity(0.45), lineWidth: 1.2).frame(width: 7, height: 7)
                    Text(lang.tr("由主控原生管理", "Managed natively"))
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.tertiary)
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 9).strokeBorder(Color.secondary.opacity(0.22), style: StrokeStyle(lineWidth: 1, dash: [4, 4])))
            } else {
                ForEach(mesh.nativeSubagents) { sub in
                    AgentNativeSubagentCard(
                        sub: sub,
                        appPath: mesh.orchestrator.guiAppPath,
                        lang: lang,
                        onJump: { onJumpSubagent(sub) }
                    )
                }
            }

            Spacer(minLength: 0)
        }
    }

    private var strategicTriggerGrid: some View {
        let btCount = mesh.strategicAlerts.filter { $0.kind == "breakthrough" }.count
        let stagCount = mesh.strategicAlerts.filter { $0.kind == "stagnation" }.count
        let escCount = mesh.strategicAlerts.filter { $0.kind == "escape_zero" }.count
        let dirCount = mesh.strategicAlerts.filter { $0.kind == "directive" }.count

        return LazyVGrid(columns: [GridItem(.flexible(), spacing: 6), GridItem(.flexible(), spacing: 6)], spacing: 6) {
            triggerPill(icon: "trophy.fill", title: lang.tr("突破泛化", "Breakthrough"), count: btCount, color: Color(red: 0.92, green: 0.68, blue: 0.05), kind: "breakthrough")
            triggerPill(icon: "arrow.triangle.2.circlepath", title: lang.tr("6轮停滞", "6-Stagnant"), count: stagCount, color: .orange, kind: "stagnation")
            triggerPill(icon: "exclamationmark.shield.fill", title: lang.tr("爆0逃逸", "Escape-0"), count: escCount, color: .red, kind: "escape_zero")
            triggerPill(icon: "megaphone.fill", title: lang.tr("指令分发", "Directives"), count: dirCount, color: .cyan, kind: "directive")
        }
    }

    private func triggerPill(icon: String, title: String, count: Int, color: Color, kind: String) -> some View {
        let active = count > 0
        return Button {
            if let hit = mesh.strategicAlerts.first(where: { $0.kind == kind }) {
                if !hit.agentId.isEmpty && hit.agentId != "admin" {
                    onSelect(.worker(hit.agentId))
                } else if !hit.problem.isEmpty {
                    onSelect(.squad(hit.problem))
                } else {
                    onSelect(.orchestrator)
                }
            } else {
                onSelect(.orchestrator)
            }
        } label: {
            HStack(spacing: 4) {
                Image(systemName: icon)
                    .font(.system(size: 9.5, weight: .bold))
                    .foregroundStyle(active ? color : .secondary.opacity(0.6))
                Text(title)
                    .font(.system(size: 9.5, weight: .semibold))
                    .foregroundStyle(active ? .primary : .secondary)
                    .lineLimit(1)
                Spacer(minLength: 2)
                Text("\(count)")
                    .font(.system(size: 9.5, weight: .heavy, design: .monospaced))
                    .foregroundStyle(active ? color : .secondary.opacity(0.5))
            }
            .padding(.horizontal, 7)
            .padding(.vertical, 5.5)
            .background(RoundedRectangle(cornerRadius: 7, style: .continuous).fill(active ? color.opacity(0.13) : Color.primary.opacity(0.03)))
            .overlay(RoundedRectangle(cornerRadius: 7, style: .continuous).strokeBorder(active ? color.opacity(0.45) : Color.primary.opacity(0.06), lineWidth: 0.8))
        }
        .buttonStyle(.plain)
    }

    private func busTrunkBridge(source src: CGPoint, targets: [String: CGPoint]) -> some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: reduceMotion || !hasLiveAnimation)) { ctx in
            let now = ctx.date.timeIntervalSince1970
            Canvas { gctx, size in
                let orchStyle = AgentDriverStyle.forDriver(mesh.orchestrator.driver)
                for (key, dst) in targets where dst.y >= 0 && dst.y <= size.height {
                    let gap = dst.x - src.x
                    let c1 = CGPoint(x: src.x + gap * 0.45, y: src.y)
                    let c2 = CGPoint(x: src.x + gap * 0.55, y: dst.y)
                    var path = Path()
                    path.move(to: src)
                    path.addCurve(to: dst, control1: c1, control2: c2)

                    let isQuiet = mesh.workers.isEmpty
                    let strokeColor = isQuiet ? Color.secondary.opacity(0.22) : orchStyle.accent.opacity(0.38)
                    gctx.stroke(
                        path,
                        with: .color(strokeColor),
                        style: StrokeStyle(lineWidth: isQuiet ? 1.1 : 1.6, dash: isQuiet ? [3, 4] : [])
                    )

                    if !isQuiet && !reduceMotion {
                        for pulse in mesh.pulses.prefix(6) {
                            let age = now - pulse.epoch
                            guard age >= 0, age < 5.5 else { continue }
                            let destination = grouping == .byProblem ? "problem:" + pulse.problem : "driver:" + pulse.driver
                            guard destination == key else { continue }
                            let rawT = CGFloat((age / 1.4).truncatingRemainder(dividingBy: 1.0))
                            let t = pulse.direction == "outbound" ? rawT : (1.0 - rawT)
                            let pColor: Color = {
                                switch pulse.kind {
                                case "breakthrough": return Color(red: 0.95, green: 0.72, blue: 0.05)
                                case "stagnation": return .orange
                                case "escape_zero", "error": return .red
                                case "directive": return .cyan
                                default: return pulse.direction == "outbound" ? orchStyle.accent : .green
                                }
                            }()
                            drawComet(&gctx, p0: src, c1: c1, c2: c2, p3: dst, t: t, color: pColor, reverse: pulse.direction != "outbound")
                        }
                    }
                }
            }
            .drawingGroup()
        }
        .allowsHitTesting(false)
    }

    private func drawComet(
        _ ctx: inout GraphicsContext,
        p0: CGPoint, c1: CGPoint, c2: CGPoint, p3: CGPoint,
        t: CGFloat, color: Color, reverse: Bool
    ) {
        let stops: [(CGFloat, CGFloat, Double)] = [(0, 4.0, 0.95), (0.05, 2.8, 0.55), (0.10, 1.8, 0.25)]
        for (dt, r, a) in stops {
            let s = reverse ? min(1, t + dt) : max(0, t - dt)
            let u = 1 - s
            let x = u * u * u * p0.x + 3 * u * u * s * c1.x + 3 * u * s * s * c2.x + s * s * s * p3.x
            let y = u * u * u * p0.y + 3 * u * u * s * c1.y + 3 * u * s * s * c2.y + s * s * s * p3.y
            ctx.fill(Path(ellipseIn: CGRect(x: x - r, y: y - r, width: r * 2, height: r * 2)), with: .color(color.opacity(a)))
        }
    }

    private func handleHover(_ workerId: String, _ hovering: Bool) {
        hoveredWorkerId = hovering ? workerId : (hoveredWorkerId == workerId ? nil : hoveredWorkerId)
    }

    private var emptyReadyGrid: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 210), spacing: 10)], spacing: 10) {
            ForEach(readyExternalDrivers) { drv in
                AgentGhostDriverCard(driver: drv) { onQuickSpawn(drv.kind, "") }
            }
        }
    }
}
