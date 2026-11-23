import AppKit
import SwiftUI

struct SchedulerBarView: View {
    private static let judgeMeURL = URL(string: "https://123.249.115.173/me")!

    @EnvironmentObject var store: ContestStore
    @EnvironmentObject var hotStore: HotTelemetryStore
    let scheduler: SchedulerState

    @StateObject private var clock = WallClockTicker()
    @UIState private var showGatePopover: Bool = false
    @UIState private var reserveVal: Double = 12
    @UIState private var noiseVal: Double = 2.7
    @UIState private var initialized: Bool = false

    private var lang: AppLanguage { store.lang }
    private var pipeline: LivePipelineState? { hotStore.pipeline ?? scheduler.pipeline }
    private var usedToday24h: Int { hotStore.schedulerHot?.usedToday24h ?? scheduler.usedToday24h }
    private var dailyQuotaLimit: Int { hotStore.schedulerHot?.dailyQuotaLimit ?? scheduler.dailyQuotaLimit }
    private var rateLimitGapSec: Int { hotStore.schedulerHot?.rateLimitGapSec ?? scheduler.rateLimitGapSec }
    private var cooldownEndsEpoch: Double? { hotStore.schedulerHot?.cooldownEndsEpoch ?? scheduler.cooldownEndsEpoch }
    private var autoConsumerEnabled: Bool { hotStore.schedulerHot?.autoConsumerEnabled ?? scheduler.autoConsumerEnabled }

    var body: some View {
        let cdRem = clock.cooldownRemaining(endsAt: pipeline?.cooldownEndsEpoch ?? cooldownEndsEpoch)
        let gap = max(1, rateLimitGapSec)
        let judgeEl = clock.elapsedSince(startedAt: pipeline?.judgeStartedEpoch)

        HStack(spacing: 12) {
            judgeStatusPill(judgeElapsed: judgeEl)

            let activeSlots = (pipeline?.accountSlots ?? []).filter { $0.enabled && $0.loggedIn }
            if !activeSlots.isEmpty {
                HStack(spacing: 6) {
                    ForEach(activeSlots) { slot in
                        let sRem = clock.cooldownRemaining(endsAt: slot.cooldownEndsEpoch)
                        let used = slot.usedToday ?? usedToday24h
                        let lim = slot.dailyLimit ?? dailyQuotaLimit
                        let exhausted = slot.isExhausted || used >= lim
                        if exhausted {
                            SemanticPill(
                                text: "\(slot.label) · \(lang.tr("已满", "FULL")) · \(used)/\(lim)",
                                icon: "xmark.octagon.fill",
                                compact: false,
                                customForeground: .white,
                                customBackground: Color(red: 0.82, green: 0.24, blue: 0.28)
                            )
                            .help(lang.tr(
                                "\(slot.label) 今日已交满 \(used)/\(lim)，提交负载均衡已自动跳过该账号",
                                "\(slot.label) reached daily cap \(used)/\(lim); automatically skipped by load balancer"
                            ))
                        } else if sRem == 0 {
                            SemanticPill(
                                text: "\(slot.label) · \(lang.tr("就绪", "Ready")) · \(used)/\(lim)",
                                icon: "checkmark.bolt.fill",
                                compact: false,
                                customForeground: Color(red: 0.10, green: 0.72, blue: 0.43),
                                customBackground: Color(red: 0.10, green: 0.72, blue: 0.43).opacity(0.14)
                            )
                            .help(lang.tr(
                                "\(slot.label) 冷却已就绪，剩余 \(max(0, lim - used)) 次（负载均衡优先使用余量多的账号）",
                                "\(slot.label) ready, \(max(0, lim - used)) shots left (load-balanced by remaining quota)"
                            ))
                        } else {
                            SemanticPill(
                                text: "\(slot.label) · \(sRem)s · \(used)/\(lim)",
                                icon: "timer",
                                compact: false,
                                customForeground: Color(red: 0.90, green: 0.56, blue: 0.10),
                                customBackground: Color(red: 0.90, green: 0.56, blue: 0.10).opacity(0.15)
                            )
                            .help(lang.tr(
                                "\(slot.label) 冷却剩余 \(sRem)s，今日已用 \(used)/\(lim)",
                                "\(slot.label) cooling down (\(sRem)s left), \(used)/\(lim) used today"
                            ))
                        }
                    }
                }
            } else {
                SemanticPill(
                    text: cdRem == 0
                        ? lang.tr("冷却已就绪 · \(usedToday24h)/\(dailyQuotaLimit)", "Ready · \(usedToday24h)/\(dailyQuotaLimit)")
                        : lang.tr("冷却 \(cdRem)s/\(gap)s · \(usedToday24h)/\(dailyQuotaLimit)", "Cooldown \(cdRem)s/\(gap)s · \(usedToday24h)/\(dailyQuotaLimit)"),
                    icon: cdRem == 0 ? "checkmark.bolt.fill" : "timer",
                    compact: false
                )
            }

            Button {
                NSWorkspace.shared.open(Self.judgeMeURL)
            } label: {
                Label(lang.tr("浏览器查看", "Open Judge"), systemImage: "safari")
                    .font(.system(size: 11.5, weight: .medium))
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .help(lang.tr("在默认浏览器中打开判题记录 (https://123.249.115.173/me)", "Open judge submissions in default browser (https://123.249.115.173/me)"))

            Spacer()

            Divider().frame(height: 18)

            Toggle(
                isOn: Binding(
                    get: { autoConsumerEnabled },
                    set: { v in Task { await store.updateScheduler(autoConsumer: v) } }
                )
            ) {
                Text(lang.tr("自动按序提交", "Auto-Submit"))
                    .font(.system(size: 12, weight: .medium))
            }
            .toggleStyle(.switch)
            .controlSize(.small)

            Button {
                showGatePopover.toggle()
            } label: {
                Label(lang.tr("门禁参数", "Gate Config"), systemImage: "slider.horizontal.3")
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
            let title = pipe.activeTitle ?? lang.tr("当前任务", "Active Op")
            let jid = pipe.remoteJudgeId.map { "#\($0) · " } ?? ""
            let dur = Formatters.duration(judgeElapsed)
            switch pipe.phase {
            case "waiting_cooldown":
                SemanticPill(text: lang.tr("已锁定下一发: \(title) · 等待冷却后发出", "Locked Next: \(title) · Waiting Cooldown"), icon: "hourglass")
            case "packaging":
                SemanticPill(text: lang.tr("本地校验打包中: \(title)…", "Packaging & Verifying: \(title)…"), icon: "shippingbox.fill")
            case "uploading":
                SemanticPill(text: lang.tr("正在上传至判题机: \(title)…", "Uploading to Judge: \(title)…"), icon: "arrow.up.circle.fill")
            case "remote_queued":
                SemanticPill(text: lang.tr("远程排队中: \(title) (\(jid)\(dur))", "Remote Queued: \(title) (\(jid)\(dur))"), icon: "clock.fill")
            case "remote_compiling":
                SemanticPill(text: lang.tr("远程编译中: \(title) (\(jid)\(dur))", "Remote Compiling: \(title) (\(jid)\(dur))"), icon: "hammer.fill")
            case "remote_running":
                SemanticPill(text: lang.tr("远程评测运行中: \(title) (\(jid)\(dur))", "Remote Running: \(title) (\(jid)\(dur))"), icon: "bolt.horizontal.circle.fill")
            case "fetching_score":
                SemanticPill(text: lang.tr("判题完成 · 同步榜单中: \(title)…", "Judge Done · Syncing Score: \(title)…"), icon: "chart.bar.fill")
            default:
                SemanticPill(text: "\(title) (\(pipe.phase))", icon: "gearshape.2.fill")
            }
        } else {
            SemanticPill(text: lang.tr("提交通道空闲", "Submit Channel Idle"), icon: "checkmark.circle.fill")
        }
    }

    private var gateConfigPopover: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(lang.tr("自动门禁与筛选阈值", "Gate & Filter Thresholds"))
                .font(.system(size: 13, weight: .bold))

            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(lang.tr("收官预留额度", "Closing Reserve"))
                        .font(.system(size: 11.5))
                    Spacer()
                    Text(lang.tr("\(Int(reserveVal)) 次", "\(Int(reserveVal)) shots"))
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
                    Text(lang.tr("突破筛选阈值 (Breakthrough)", "Breakthrough Filter"))
                        .font(.system(size: 11.5))
                    Spacer()
                    Stepper(
                        String(format: "Δ ≥ %.1f", store.breakthroughThreshold),
                        value: Binding(
                            get: { store.breakthroughThreshold },
                            set: { nv in Task { await store.updateScheduler(breakthroughThreshold: nv) } }
                        ),
                        in: 1.0...25.0,
                        step: 1.0
                    )
                    .font(.system(size: 11.5, weight: .bold, design: .monospaced))
                }
            }

            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(lang.tr("防抖最小提分阈值", "Noise-Floor Gate"))
                        .font(.system(size: 11.5))
                    Spacer()
                    Stepper(String(format: "Δ ≥ %.1f", noiseVal), value: $noiseVal, in: 0.5...8.0, step: 0.2)
                        .font(.system(size: 11.5, weight: .bold, design: .monospaced))
                        .onChange(of: noiseVal) { _, nv in
                            Task { await store.updateScheduler(noiseFloor: nv) }
                        }
                }
                Text(lang.tr(
                    "注：点击卡片上的「插队提交」可随时绕过自动阈值直接发送。",
                    "Note: 'Jump Queue' on any card bypasses the automatic threshold."
                ))
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
            }
        }
        .padding(14)
        .frame(width: 295)
    }
}
