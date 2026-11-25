import AppKit
import SwiftUI
import UniformTypeIdentifiers
import WebKit

struct AccountsSettingsTab: View {
    @EnvironmentObject var store: ContestStore

    @UIState private var cjTargetSlot: String = "new"
    @UIState private var cjLabel: String = ""
    @UIState private var cjAccount: String = ""
    @UIState private var cjPassword: String = ""
    @UIState private var captchaId: String = ""
    @UIState private var captchaSvgPath: String? = nil
    @UIState private var captchaCode: String = ""
    @UIState private var cjFeedback: String? = nil
    @UIState private var isSigningInCJ: Bool = false
    @UIState private var savingCaptain = false

    @UIState private var gcToken: String = ""
    @UIState private var gcFeedback: String? = nil
    @UIState private var isSigningInGC: Bool = false

    private var lang: AppLanguage { store.lang }
    private var accountSlots: [JudgeAccountSlot] {
        store.snapshot?.auth.judgeAccounts ?? []
    }

    var body: some View {
        GeometryReader { geometry in
        HStack(alignment: .top, spacing: 14) {
            SurfaceCard(padding: 12) {
                ScrollView {
                    VStack(alignment: .leading, spacing: 10) {
                        HStack {
                            Label(lang.tr("Judge 队员账号", "Judge Accounts"), systemImage: "person.2.badge.gearshape.fill")
                                .font(.system(size: 13.5, weight: .bold))
                            Spacer()
                            let activeCnt = accountSlots.filter { $0.enabled && $0.loggedIn }.count
                            SemanticPill(
                                text: lang.tr("\(activeCnt) 个提交器在线", "\(activeCnt) Active"),
                                compact: true
                            )
                        }

                        // List configured team member submitter slots
                        if !accountSlots.isEmpty {
                            VStack(spacing: 6) {
                                ForEach(accountSlots) { slot in
                                    HStack(spacing: 8) {
                                        Circle()
                                            .fill(slot.loggedIn && slot.enabled ? Color.green : (slot.loggedIn ? Color.orange : Color.red))
                                            .frame(width: 7, height: 7)
                                        VStack(alignment: .leading, spacing: 1) {
                                            HStack(spacing: 5) {
                                                Text(slot.label)
                                                    .font(.system(size: 11.5, weight: .bold))
                                                if let hint = slot.accountHint, !hint.isEmpty {
                                                    Text("(\(hint))")
                                                        .font(.system(size: 10, design: .monospaced))
                                                        .foregroundStyle(.secondary)
                                                }
                                            }
                                            Text(
                                                slot.loggedIn
                                                    ? lang.tr(
                                                        "uid \(slot.userId?.prefix(8) ?? "—") · 剩余 \(String(format: "%.0fh", slot.remainingHours ?? 0)) · 独立 190s 冷却",
                                                        "uid \(slot.userId?.prefix(8) ?? "—") · \(String(format: "%.0fh", slot.remainingHours ?? 0)) left · 190s slot"
                                                    )
                                                    : lang.tr("未登录或会话已过期", "Expired / signed out")
                                            )
                                            .font(.system(size: 10, design: .monospaced))
                                            .foregroundStyle(.secondary)
                                        }
                                        Spacer()
                                        if let contest = store.snapshot?.contest {
                                            let selected = contest.captainAccountId == slot.id
                                            Button {
                                                savingCaptain = true
                                                Task {
                                                    defer { savingCaptain = false }
                                                    do {
                                                        try await store.repository.setCaptain(id: slot.id, workspace: contest.workspace)
                                                        await store.refreshLocal()
                                                    } catch { cjFeedback = error.localizedDescription }
                                                }
                                            } label: {
                                                Label(selected ? lang.tr("队长", "Captain") : lang.tr("设为队长", "Set captain"),
                                                      systemImage: selected ? "flag.fill" : "flag")
                                            }
                                            .controlSize(.small)
                                            .disabled(savingCaptain || !slot.loggedIn || selected)
                                            .help(lang.tr("用此账号的报名队伍查询本场比赛排名", "Use this account’s registered team for this contest’s rankings"))
                                        }
                                        Toggle("", isOn: Binding(
                                            get: { slot.enabled },
                                            set: { nv in
                                                Task {
                                                    try? await store.repository.toggleJudgeAccount(id: slot.id, enabled: nv)
                                                    await store.refreshLocal()
                                                }
                                            }
                                        ))
                                        .labelsHidden()
                                        .toggleStyle(.switch)
                                        .controlSize(.mini)

                                        if !slot.isPrimary {
                                            Button {
                                                Task {
                                                    try? await store.repository.removeJudgeAccount(id: slot.id)
                                                    await store.refreshLocal()
                                                }
                                            } label: {
                                                Image(systemName: "trash")
                                                    .font(.system(size: 10.5))
                                            }
                                            .buttonStyle(.borderless)
                                            .foregroundStyle(.secondary)
                                            .help(lang.tr("移除该队员会话", "Remove teammate session"))
                                        }
                                    }
                                    .padding(.horizontal, 8)
                                    .padding(.vertical, 6)
                                    .background(RoundedRectangle(cornerRadius: 6).fill(Color.primary.opacity(0.035)))
                                }
                            }
                        }

                        Divider()

                        HStack(spacing: 8) {
                            Picker(lang.tr("目标槽位", "Target"), selection: $cjTargetSlot) {
                                Text(lang.tr("新增队员账号", "Add Teammate")).tag("new")
                                Text(lang.tr("刷新主账号", "Primary Slot")).tag("primary")
                            }
                            .pickerStyle(.segmented)
                            .labelsHidden()
                            .controlSize(.small)

                            Button {
                                importSessionPanel()
                            } label: {
                                Label(lang.tr("导入 session.json", "Import Session"), systemImage: "square.and.arrow.down")
                                    .font(.system(size: 10.5))
                            }
                            .buttonStyle(.bordered)
                            .controlSize(.small)
                        }

                        if cjTargetSlot == "new" {
                            TextField(lang.tr("队员备注名 (如: 队员2)", "Teammate Label (e.g. Teammate 2)"), text: $cjLabel)
                                .textFieldStyle(.roundedBorder)
                        }

                        TextField(lang.tr("邮箱 / 手机号账号", "Email / Phone Account"), text: $cjAccount)
                            .textFieldStyle(.roundedBorder)
                        SecureField(lang.tr("密码 (仅走 stdin，绝不落盘)", "Password (stdin only, never stored)"), text: $cjPassword)
                            .textFieldStyle(.roundedBorder)

                        HStack(spacing: 8) {
                            Button(lang.tr("获取图形验证码", "Fetch Captcha")) {
                                Task {
                                    if let cap = try? await store.repository.fetchCaptcha() {
                                        captchaId = cap.0
                                        captchaSvgPath = cap.1
                                    }
                                }
                            }
                            .controlSize(.small)

                            if !captchaId.isEmpty {
                                TextField(lang.tr("验证码", "Code"), text: $captchaCode)
                                    .textFieldStyle(.roundedBorder)
                                    .frame(width: 90)
                            }
                        }

                        if let path = captchaSvgPath {
                            SVGCaptchaPreview(filePath: path)
                                .frame(height: 44)
                                .clipShape(RoundedRectangle(cornerRadius: 6))
                        }

                        HStack {
                            Button {
                                isSigningInCJ = true
                                let pw = cjPassword
                                let target = cjTargetSlot
                                let lbl = cjLabel
                                cjPassword = ""
                                Task {
                                    if let res = try? await store.repository.loginJudge(
                                        account: cjAccount,
                                        password: pw,
                                        captchaId: captchaId,
                                        captchaCode: captchaCode,
                                        accountId: target,
                                        label: lbl
                                    ) {
                                        cjFeedback = res.1
                                        if res.0 {
                                            cjAccount = ""
                                            cjLabel = ""
                                            captchaCode = ""
                                            captchaSvgPath = nil
                                            await store.refreshLocal()
                                        }
                                    }
                                    isSigningInCJ = false
                                }
                            } label: {
                                if isSigningInCJ { ProgressView().controlSize(.small) }
                                else {
                                    Text(
                                        cjTargetSlot == "new"
                                            ? lang.tr("登录并添加提交器", "Sign In & Add Submitter")
                                            : lang.tr("登录主账号", "Sign In Primary")
                                    )
                                }
                            }
                            .buttonStyle(.borderedProminent)
                            .disabled(cjAccount.isEmpty || cjPassword.isEmpty || isSigningInCJ)

                            if store.snapshot?.auth.judge.loggedIn == true && cjTargetSlot == "primary" {
                                Button(lang.tr("清除主会话", "Clear Primary")) {
                                    Task {
                                        try? await store.repository.logoutJudge()
                                        await store.refreshLocal()
                                    }
                                }
                                .buttonStyle(.bordered)
                            }
                        }

                        if let fb = cjFeedback {
                            Text(fb).font(.system(size: 11)).foregroundStyle(.secondary)
                        }
                    }
                }
            }
            .frame(maxWidth: .infinity)

            SurfaceCard(padding: 12) {
                VStack(alignment: .leading, spacing: 10) {
                    HStack {
                        Label("GitCode CLI (`gc`)", systemImage: "chevron.left.forwardslash.chevron.right")
                            .font(.system(size: 13.5, weight: .bold))
                        Spacer()
                        if let gc = store.snapshot?.auth.gitcode {
                            SemanticPill(text: gc.loggedIn ? lang.tr("已授权", "ACTIVE") : lang.tr("未登录", "OFFLINE"), compact: true)
                        }
                    }

                    if let gc = store.snapshot?.auth.gitcode {
                        Text(lang.tr("与 `gc` 及 git credential 共享凭据", "Shared with `gc` & git credential"))
                            .font(.system(size: 10.5, design: .monospaced))
                            .foregroundStyle(.secondary)
                        if gc.loggedIn {
                            Text(lang.tr(
                                "当前账号: @\(gc.username ?? "—") · \(gc.protocolName ?? "https")",
                                "Account: @\(gc.username ?? "—") · \(gc.protocolName ?? "https")"
                            ))
                            .font(.system(size: 11, weight: .medium, design: .monospaced))
                            .foregroundStyle(.green)
                        }
                    }

                    Divider()

                    SecureField(lang.tr("GitCode 个人访问令牌 (PAT)", "GitCode Personal Access Token"), text: $gcToken)
                        .textFieldStyle(.roundedBorder)

                    Button {
                        isSigningInGC = true
                        let tok = gcToken
                        gcToken = ""
                        Task {
                            if let res = try? await store.repository.loginGitCode(token: tok) {
                                gcFeedback = res.1
                                if res.0 { await store.refreshLocal() }
                            }
                            isSigningInGC = false
                        }
                    } label: {
                        if isSigningInGC { ProgressView().controlSize(.small) }
                        else { Text(lang.tr("通过 `gc auth login` 保存令牌", "Save Token via `gc auth login`")) }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(gcToken.isEmpty || isSigningInGC)

                    if let fb = gcFeedback {
                        Text(fb).font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                }
            }
            .frame(width: max(200, (geometry.size.width - 14) * 0.34), alignment: .top)
        }
        }
    }

    private func importSessionPanel() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [.json]
        panel.message = lang.tr("选择队友已登录导出的 session.json 会话文件", "Select a teammate's Judge session.json file")
        if panel.runModal() == .OK, let url = panel.url {
            let lbl = cjLabel
            Task {
                if let res = try? await store.repository.importJudgeSession(filePath: url.path, label: lbl) {
                    cjFeedback = res.1
                    if res.0 {
                        cjLabel = ""
                        await store.refreshLocal()
                    }
                }
            }
        }
    }
}

struct SVGCaptchaPreview: NSViewRepresentable {
    let filePath: String

    func makeNSView(context: Context) -> WKWebView {
        let web = WKWebView()
        web.setValue(false, forKey: "drawsBackground")
        return web
    }

    func updateNSView(_ nsView: WKWebView, context: Context) {
        if let svg = try? String(contentsOfFile: filePath, encoding: .utf8) {
            nsView.loadHTMLString("<html><body style='margin:0;display:flex;align-items:center;justify-content:center;background:#fff;'>\(svg)</body></html>", baseURL: nil)
        }
    }
}
