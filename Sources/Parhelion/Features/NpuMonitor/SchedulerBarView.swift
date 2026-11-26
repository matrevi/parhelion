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
    @UIState private var lowGainVal: Double = 0.6
    @UIState private var syncingGateValues: Bool = false

    private var lang: AppLanguage { store.lang }
    private var pipeline: LivePipelineState? { hotStore.pipeline ?? scheduler.pipeline }
    private var usedToday24h: Int { hotStore.schedulerHot?.usedToday24h ?? scheduler.usedToday24h }
    private var dailyQuotaLimit: Int { hotStore.schedulerHot?.dailyQuotaLimit ?? scheduler.dailyQuotaLimit }
    private var rateLimitGapSec: Int { hotStore.schedulerHot?.rateLimitGapSec ?? scheduler.rateLimitGapSec }
    private var cooldownEndsEpoch: Double? { hotStore.schedulerHot?.cooldownEndsEpoch ?? scheduler.cooldownEndsEpoch }
    private var autoConsumerEnabled: Bool {
        store.effectiveAutoConsumer(hot: hotStore.schedulerHot?.autoConsumerEnabled)
    }

    private func syncGateValuesFromStore() {
        syncingGateValues = true
        reserveVal = Double(hotStore.schedulerHot?.closingReserve ?? scheduler.closingReserve)
        noiseVal = hotStore.schedulerHot?.noiseFloorDelta ?? scheduler.noiseFloorDelta
        lowGainVal = scheduler.gateSwitches?.lowGainMinProb ?? lowGainVal
        syncingGateValues = false
    }

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
            syncGateValuesFromStore()
        }
        .onChange(of: showGatePopover) { _, open in
            if open { syncGateValuesFromStore() }
        }
        .onChange(of: store.selectedWorkspace) { _, _ in
            syncGateValuesFromStore()
        }
    }

    @ViewBuilder
    private func judgeStatusPill(judgeElapsed: Int) -> some View {
        let now = Date()
        let optimisticLaunching = store.launchingSubmits.values
            .filter { $0.expiresAt > now }
            .sorted { $0.startedEpoch < $1.startedEpoch }
            .first

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
        } else if let launching = optimisticLaunching {
            SemanticPill(
                text: lang.tr("正在启动提交: \(launching.title)…", "Launching Submit: \(launching.title)…"),
                icon: "paperplane.fill"
            )
        } else if let settlingKey = store.settlingInflightKeys.sorted().first,
                  let settling = store.settlingActive(for: settlingKey) {
            SemanticPill(
                text: lang.tr("判题完成 · 同步榜单中: \(settling.title)…", "Judge Done · Syncing Score: \(settling.title)…"),
                icon: "chart.bar.fill"
            )
        } else {
            SemanticPill(text: lang.tr("提交通道空闲", "Submit Channel Idle"), icon: "checkmark.circle.fill")
        }
    }

    /// `low_gain_min_prob`: how sure the estimate must be before a below-floor candidate is approved.
    private var lowGainSection: some View {
        let enabled = scheduler.gateSwitches?.approveLowGainPositive ?? true
        let floor = hotStore.schedulerHot?.noiseFloorDelta ?? scheduler.noiseFloorDelta
        return VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(lang.tr("低增益试探门槛", "Low-Gain Explore Bar"))
                    .font(.system(size: 11.5))
                Spacer()
                Stepper(String(format: "P ≥ %.2f", lowGainVal), value: $lowGainVal, in: 0.5...0.95, step: 0.05)
                    .font(.system(size: 11.5, weight: .bold, design: .monospaced))
                    .disabled(!enabled)
                    .onChange(of: lowGainVal) { _, nv in
                        guard !syncingGateValues else { return }
                        Task { await store.updateGateSwitch("low_gain_min_prob", value: String(format: "%.2f", nv)) }
                    }
            }
            Text(enabled
                 ? lang.tr(
                    String(format: "期望增益低于防抖阈值（Δ %.1f）的候选，只有提升概率 P(增益>0) ≥ %.0f%% 才批准为「试探」；平台不代为请求这类候选，由 worker 自己决定是否提交。调高：少交低收益的试探；调低：多试探、多耗额度。", floor, lowGainVal * 100),
                    String(format: "A candidate below the noise floor (Δ %.1f) is approved as EXPLORE only when P(gain>0) ≥ %.0f%%. The platform never requests these on a worker's behalf. Higher: fewer low-yield shots; lower: more exploration, more quota.", floor, lowGainVal * 100))
                 : lang.tr("低增益批准已关闭（approve_low_gain_positive=false）：低于防抖阈值的候选一律暂缓。",
                           "Low-gain approval is off (approve_low_gain_positive=false): every below-floor candidate is held."))
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .help(lang.tr("门禁开关 low_gain_min_prob，等同 opsb queue config --gate low_gain_min_prob=<值>。",
                      "Gate switch low_gain_min_prob, same as opsb queue config --gate low_gain_min_prob=<value>."))
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
                            guard !syncingGateValues else { return }
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

            Divider()

            lowGainSection
        }
        .padding(14)
        .frame(width: 320)
    }
}
