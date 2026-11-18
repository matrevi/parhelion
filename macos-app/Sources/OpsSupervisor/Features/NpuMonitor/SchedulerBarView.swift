import SwiftUI

struct SchedulerBarView: View {
    @EnvironmentObject var store: ContestStore
    let scheduler: SchedulerState

    @StateObject private var clock = WallClockTicker()
    @UIState private var showGatePopover: Bool = false
    @UIState private var reserveVal: Double = 12
    @UIState private var noiseVal: Double = 2.7
    @UIState private var initialized: Bool = false

    private var pipeline: LivePipelineState? { scheduler.pipeline }

    var body: some View {
        let cdRem = clock.cooldownRemaining(endsAt: pipeline?.cooldownEndsEpoch ?? scheduler.cooldownEndsEpoch)
        let gap = max(1, scheduler.rateLimitGapSec)
        let judgeEl = clock.elapsedSince(startedAt: pipeline?.judgeStartedEpoch)

        HStack(spacing: 12) {
            // 1. Remote Judge Live Status Pill
            judgeStatusPill(judgeElapsed: judgeEl)

            // 2. Platform 190s Rate-Limit Cooldown Pill
            SemanticPill(
                text: cdRem == 0 ? "冷却已就绪 (可立即提交)" : "平台限流冷却 \(cdRem)s / \(gap)s",
                icon: cdRem == 0 ? "checkmark.bolt.fill" : "timer",
                compact: false
            )

            Spacer()

            // 3. Daily Quota Summary
            Text("今日额度: \(scheduler.usedToday24h) / \(scheduler.dailyQuotaLimit)")
                .font(.system(size: 11.5, weight: .semibold, design: .monospaced))
                .foregroundStyle(.secondary)

            Divider().frame(height: 18)

            // 4. Auto-Queue Switch
            Toggle(
                isOn: Binding(
                    get: { scheduler.autoConsumerEnabled },
                    set: { v in Task { await store.updateScheduler(autoConsumer: v) } }
                )
            ) {
                Text("自动按序提交")
                    .font(.system(size: 12, weight: .medium))
            }
            .toggleStyle(.switch)
            .controlSize(.small)

            // 5. Tucked-away Gate Settings Popover
            Button {
                showGatePopover.toggle()
            } label: {
                Label("门禁参数", systemImage: "slider.horizontal.3")
                    .font(.system(size: 11.5))
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .popover(isPresented: $showGatePopover, arrowEdge: .bottom) {
                gateConfigPopover
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(Color.primary.opacity(0.025))
        .onAppear {
            if !initialized {
                reserveVal = Double(scheduler.closingReserve)
                noiseVal = scheduler.noiseFloorDelta
                initialized = true
            }
        }
    }

    @ViewBuilder
    private func judgeStatusPill(judgeElapsed: Int) -> some View {
        if let pipe = pipeline, pipe.isBusy {
            let title = pipe.activeTitle ?? "当前任务"
            let jid = pipe.remoteJudgeId.map { "#\($0) · " } ?? ""
            switch pipe.phase {
            case "waiting_cooldown":
                SemanticPill(text: "已锁定下一发: \(title) · 等待限流冷却后自动发出", icon: "hourglass")
            case "packaging":
                SemanticPill(text: "本地校验打包中: \(title) (检出源码与核对模板)…", icon: "shippingbox.fill")
            case "uploading":
                SemanticPill(text: "正在上传至判题机: \(title)…", icon: "arrow.up.circle.fill")
            case "remote_queued":
                SemanticPill(text: "远程排队中: \(title) (\(jid)等待分配 NPU · \(Formatters.duration(judgeElapsed)))", icon: "clock.fill")
            case "remote_compiling":
                SemanticPill(text: "远程编译中: \(title) (\(jid)Compiling · \(Formatters.duration(judgeElapsed)))", icon: "hammer.fill")
            case "remote_running":
                SemanticPill(text: "远程评测运行中: \(title) (\(jid)\(pipe.remoteStatus ?? "Running") · \(Formatters.duration(judgeElapsed)))", icon: "bolt.horizontal.circle.fill")
            case "fetching_score":
                SemanticPill(text: "判题完成 (\(pipe.remoteStatus ?? "Done")) · 正在同步榜单分数: \(title)…", icon: "chart.bar.fill")
            default:
                SemanticPill(text: "处理中: \(title) (\(pipe.phase))", icon: "gearshape.2.fill")
            }
        } else {
            SemanticPill(text: "提交通道空闲", icon: "checkmark.circle.fill")
        }
    }

    private var gateConfigPopover: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("自动门禁参数设置")
                .font(.system(size: 13, weight: .bold))

            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("收官预留额度")
                        .font(.system(size: 11.5))
                    Spacer()
                    Text("\(Int(reserveVal)) 次")
                        .font(.system(size: 11.5, weight: .bold, design: .monospaced))
                }
                Slider(value: $reserveVal, in: 0...25, step: 1) { editing in
                    if !editing {
                        Task { await store.updateScheduler(closingReserve: Int(reserveVal)) }
                    }
                }
            }

            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("防抖最小提分阈值 (自动入队)")
                        .font(.system(size: 11.5))
                    Spacer()
                    Stepper(String(format: "Δ ≥ %.1f", noiseVal), value: $noiseVal, in: 0.5...8.0, step: 0.2)
                        .font(.system(size: 11.5, weight: .bold, design: .monospaced))
                        .onChange(of: noiseVal) { _, nv in
                            Task { await store.updateScheduler(noiseFloor: nv) }
                        }
                }
                Text("注：手动点击卡片上的「插队提交」可随时绕过阈值直接发送。")
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
            }
        }
        .padding(14)
        .frame(width: 290)
    }
}
