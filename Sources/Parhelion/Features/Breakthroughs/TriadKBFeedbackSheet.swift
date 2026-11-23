import AppKit
import SwiftUI

struct TriadKBFeedbackRequest: Identifiable {
    var id: String { "\(entry.id)-\(initialVerdict)" }
    let entry: TriadKBEntry
    let initialVerdict: String
}

struct TriadKBFeedbackSheet: View {
    @EnvironmentObject var store: ContestStore
    @Environment(\.dismiss) private var dismiss

    let request: TriadKBFeedbackRequest
    var onSuccess: (() -> Void)? = nil

    @UIState private var verdict: String = "win"
    @UIState private var selectedProblem: String = ""
    @UIState private var sha: String = ""
    @UIState private var deltaRawText: String = ""
    @UIState private var deltaUsText: String = ""
    @UIState private var speedupText: String = ""
    @UIState private var note: String = ""
    @UIState private var isSubmitting: Bool = false
    @UIState private var localError: String?

    private var lang: AppLanguage { store.lang }
    private var problems: [ProblemItem] { store.snapshot?.problems ?? [] }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Label(
                    lang.tr("记录实证反馈 · \(request.entry.id)", "Record KB Evidence · \(request.entry.id)"),
                    systemImage: "checkmark.seal.fill"
                )
                .font(.system(size: 14, weight: .bold))
                Spacer()
                Button(lang.tr("取消", "Cancel")) { dismiss() }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
            }

            Text(request.entry.title)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(.secondary)
                .lineLimit(2)

            Picker(lang.tr("结论", "Verdict"), selection: $verdict) {
                Text(lang.tr("有效涨分 (Win)", "Win (+)")).tag("win")
                Text(lang.tr("无效 / 退化 (Fail)", "Fail (-)")).tag("fail")
                Text(lang.tr("反模式踩坑 (Anti-Pattern)", "Anti-Pattern (!)")).tag("anti-pattern")
            }
            .pickerStyle(.segmented)

            Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 8) {
                GridRow {
                    Text(lang.tr("算子", "Operator")).font(.system(size: 11.5)).foregroundStyle(.secondary)
                    if problems.isEmpty {
                        TextField("ConcatD", text: $selectedProblem).textFieldStyle(.roundedBorder)
                    } else {
                        Picker("", selection: $selectedProblem) {
                            ForEach(problems) { p in
                                Text(p.title).tag(p.title)
                            }
                        }
                        .labelsHidden()
                        .onChange(of: selectedProblem) { _, newProb in
                            if let matched = problems.first(where: { $0.title == newProb }),
                               !matched.headSha.isEmpty {
                                sha = String(matched.headSha.prefix(12))
                            }
                        }
                    }
                }
                GridRow {
                    Text("Git SHA").font(.system(size: 11.5)).foregroundStyle(.secondary)
                    TextField("7-40 hex chars", text: $sha)
                        .textFieldStyle(.roundedBorder)
                        .font(.system(size: 12, design: .monospaced))
                }
                GridRow {
                    Text(lang.tr("实测指标", "Metrics")).font(.system(size: 11.5)).foregroundStyle(.secondary)
                    HStack(spacing: 8) {
                        TextField("Δ pts", text: $deltaRawText).textFieldStyle(.roundedBorder)
                        TextField("Δ µs", text: $deltaUsText).textFieldStyle(.roundedBorder)
                        TextField("speedup x", text: $speedupText).textFieldStyle(.roundedBorder)
                    }
                }
                GridRow {
                    Text(lang.tr("说明", "Note")).font(.system(size: 11.5)).foregroundStyle(.secondary)
                    TextField(
                        lang.tr("若未填数值，说明需含 profiler 或 online", "Describe profiler or online observation"),
                        text: $note
                    )
                    .textFieldStyle(.roundedBorder)
                }
            }

            if let err = localError {
                Text(err)
                    .font(.system(size: 11))
                    .foregroundStyle(.red)
            }

            HStack {
                Text(lang.tr("需要 7-40 位 hex SHA 及至少一项指标 (Δ pts / Δ µs / speedup / profiler|online 说明)",
                             "Requires 7-40 hex SHA + at least one metric or profiler/online note"))
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                Spacer()
                Button {
                    submit()
                } label: {
                    if isSubmitting {
                        ProgressView().controlSize(.small)
                    } else {
                        Text(lang.tr("提交实证", "Submit Evidence"))
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(isSubmitting)
            }
        }
        .padding(16)
        .frame(width: 500)
        .onAppear {
            verdict = request.initialVerdict
            if selectedProblem.isEmpty {
                let initial = store.selectedProblemTitle
                    ?? request.entry.problem
                    ?? problems.first?.title
                    ?? ""
                selectedProblem = initial
                if let matched = problems.first(where: { $0.title == initial }), !matched.headSha.isEmpty {
                    sha = String(matched.headSha.prefix(12))
                } else if let entrySha = request.entry.shortSha {
                    sha = entrySha
                }
            }
        }
    }

    private func submit() {
        localError = nil
        let cleanProblem = selectedProblem.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanSha = sha.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanProblem.isEmpty else {
            localError = lang.tr("请选择或填写算子名称", "Problem name is required")
            return
        }
        guard cleanSha.count >= 7 else {
            localError = lang.tr("SHA 至少需要 7 位十六进制字符", "SHA must be 7-40 hex characters")
            return
        }
        let dRaw = Double(deltaRawText.trimmingCharacters(in: .whitespaces))
        let dUs = Double(deltaUsText.trimmingCharacters(in: .whitespaces))
        let spd = Double(speedupText.trimmingCharacters(in: .whitespaces))
        let cleanNote = note.trimmingCharacters(in: .whitespacesAndNewlines)

        isSubmitting = true
        Task {
            let ok = await store.submitTriadFeedback(
                entryId: request.entry.id,
                verdict: verdict,
                problem: cleanProblem,
                sha: cleanSha,
                deltaRaw: dRaw,
                deltaUs: dUs,
                speedup: spd,
                note: cleanNote
            )
            isSubmitting = false
            if ok {
                onSuccess?()
                dismiss()
            } else {
                localError = store.errorMessage
            }
        }
    }
}
