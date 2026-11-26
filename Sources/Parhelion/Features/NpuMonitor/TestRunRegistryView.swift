import SwiftUI

/// `parhelion_test` registry next to the server queue: problem, SHA, level, status, case progress
/// and the queue / hold / end-to-end seconds of each run. A finished run expands to its digest,
/// fetched on demand so the 1Hz hot frame stays small.
struct TestRunRegistryView: View {
    @EnvironmentObject var store: ContestStore
    @EnvironmentObject var hotStore: HotTelemetryStore
    @State private var expanded: Set<String> = []
    @State private var details: [String: TestRunDetail] = [:]
    @State private var loading: Set<String> = []

    private var lang: AppLanguage { store.lang }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Label(lang.tr("测试登记表", "Test Registry"), systemImage: "list.bullet.rectangle")
                    .font(.system(size: 12, weight: .semibold))
                Spacer()
                Text("\(hotStore.testRuns.filter(\.isActive).count) " + lang.tr("运行中", "active"))
                    .font(.system(size: 10)).foregroundStyle(.secondary)
            }
            if hotStore.testRuns.isEmpty {
                Text(lang.tr("暂无测试运行", "No test runs yet"))
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 40)
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 4) {
                        ForEach(hotStore.testRuns) { run in
                            row(run)
                        }
                    }
                }
            }
        }
        .padding(10)
    }

    @ViewBuilder
    private func row(_ run: TestRunEntry) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Image(systemName: icon(run)).foregroundStyle(color(run)).font(.system(size: 10))
                Text(run.problem).font(.system(size: 11, weight: .semibold)).lineLimit(1)
                Text(run.sha7).font(.system(size: 10, design: .monospaced)).foregroundStyle(.secondary)
                SemanticPill(text: run.level.uppercased(), compact: true)
                if let base = run.compare {
                    Text("A/B " + base).font(.system(size: 9, design: .monospaced)).foregroundStyle(.secondary)
                }
                Spacer()
                Text(progress(run)).font(.system(size: 10, design: .monospaced))
                if run.hasDigest {
                    Button {
                        Task { await toggle(run) }
                    } label: {
                        Image(systemName: expanded.contains(run.id) ? "chevron.up" : "chevron.down")
                    }
                    .buttonStyle(.borderless)
                    .help(lang.tr("展开 digest", "Show digest"))
                }
            }
            Text(timings(run)).font(.system(size: 9, design: .monospaced)).foregroundStyle(.secondary)
            if expanded.contains(run.id) {
                if loading.contains(run.id) {
                    ProgressView().controlSize(.small)
                } else if let digest = details[run.id]?.digest {
                    TestRunDigestView(digest: digest, lang: lang)
                }
            }
        }
        .padding(6)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.secondary.opacity(0.06)))
    }

    private func progress(_ run: TestRunEntry) -> String {
        let done = run.casesDone.map(String.init) ?? "–"
        let total = run.cases.map(String.init) ?? "–"
        return "\(done)/\(total) " + lang.tr("例", "cases")
    }

    private func timings(_ run: TestRunEntry) -> String {
        func fmt(_ v: Double?) -> String { v.map { String(format: "%.1fs", $0) } ?? "–" }
        return lang.tr("排队 ", "queue ") + fmt(run.waitSec) + lang.tr(" · 持锁 ", " · hold ") + fmt(run.holdSec)
            + lang.tr(" · 端到端 ", " · e2e ") + fmt(run.e2eSec) + " · " + run.status
    }

    private func icon(_ run: TestRunEntry) -> String {
        switch run.status {
        case "running", "queued": return "circle.dotted"
        case "finished": return run.exitCode == 0 ? "checkmark.circle.fill" : "exclamationmark.triangle.fill"
        default: return "xmark.octagon.fill"
        }
    }

    private func color(_ run: TestRunEntry) -> Color {
        switch run.status {
        case "running", "queued": return .blue
        case "finished": return run.exitCode == 0 ? .green : .orange
        default: return .red
        }
    }

    private func toggle(_ run: TestRunEntry) async {
        if expanded.contains(run.id) {
            expanded.remove(run.id)
            return
        }
        expanded.insert(run.id)
        guard details[run.id] == nil else { return }
        loading.insert(run.id)
        defer { loading.remove(run.id) }
        if let detail = try? await store.daemonClient.fetchTestRun(
            workspace: store.selectedWorkspace, host: store.activeSSHHost, requestId: run.requestId
        ) {
            details[run.id] = detail
        }
    }
}

/// Verdicts, warm timing with drift, and the speedup against the best official SHA.
struct TestRunDigestView: View {
    let digest: TestRunDigest
    let lang: AppLanguage

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            if let s = digest.summary {
                Text(lang.tr("通过 ", "passed ") + "\(s.passed ?? 0)/\(s.total ?? 0)")
                    .font(.system(size: 10, weight: .medium))
            }
            ForEach(digest.failures ?? [], id: \.id) { f in
                Text("\(f.id) \(f.status) \(f.detail ?? "")").font(.system(size: 9)).foregroundStyle(.orange)
                    .lineLimit(2)
            }
            if let t = digest.timing {
                Text(lang.tr("计时 ", "timing ") + (t.source ?? "?")
                     + (t.drift.map { String(format: lang.tr(" · 漂移 x%.2f", " · drift x%.2f"), $0) } ?? "")
                     + ((t.noisy ?? false) ? lang.tr(" · 慢档", " · slow mode") : ""))
                    .font(.system(size: 9, design: .monospaced))
                let cases = (t.cases ?? [:]).sorted { $0.key < $1.key }
                ForEach(cases, id: \.key) { key, value in
                    Text(String(format: "%@  min %.2f  med %.2f µs", key, value.minUs ?? 0, value.medianUs ?? 0))
                        .font(.system(size: 9, design: .monospaced)).foregroundStyle(.secondary)
                }
            }
            if let b = digest.baseline, let geo = b.geomeanSpeedup {
                Text(String(format: lang.tr("相对 %@ 提速 x%.3f", "vs %@ speedup x%.3f"), b.sha ?? "?", geo))
                    .font(.system(size: 9, weight: .medium))
            }
            if let hint = digest.hint, !hint.isEmpty {
                Text(hint).font(.system(size: 9)).foregroundStyle(.secondary).lineLimit(3)
            }
        }
        .padding(.leading, 16)
    }
}
