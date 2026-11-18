import SwiftUI
import WebKit

struct AccountsSettingsTab: View {
    @EnvironmentObject var store: ContestStore

    @UIState private var cjAccount: String = ""
    @UIState private var cjPassword: String = ""
    @UIState private var captchaId: String = ""
    @UIState private var captchaSvgPath: String? = nil
    @UIState private var captchaCode: String = ""
    @UIState private var cjFeedback: String? = nil
    @UIState private var isSigningInCJ: Bool = false

    @UIState private var gcToken: String = ""
    @UIState private var gcFeedback: String? = nil
    @UIState private var isSigningInGC: Bool = false

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            SurfaceCard(padding: 12) {
                VStack(alignment: .leading, spacing: 10) {
                    HStack {
                        Label("Judge Platform", systemImage: "checkmark.seal.fill")
                            .font(.system(size: 13.5, weight: .bold))
                        Spacer()
                        if let cj = store.snapshot?.auth.judge {
                            SemanticPill(text: cj.loggedIn ? "ACTIVE" : "EXPIRED", compact: true)
                        }
                    }

                    if let cj = store.snapshot?.auth.judge {
                        Text("Shared Session: ~/.judge/session.json")
                            .font(.system(size: 10.5, design: .monospaced))
                            .foregroundStyle(.secondary)
                        if cj.loggedIn {
                            Text("user_id: \(cj.userId ?? "—") · Expires in \(String(format: "%.0fh", cj.remainingHours ?? 0))")
                                .font(.system(size: 11, weight: .medium, design: .monospaced))
                                .foregroundStyle(.green)
                        }
                    }

                    Divider()

                    TextField("Email / Phone Account", text: $cjAccount).textFieldStyle(.roundedBorder)
                    SecureField("Password (stdin only, never stored)", text: $cjPassword).textFieldStyle(.roundedBorder)

                    HStack(spacing: 8) {
                        Button("Fetch Captcha") {
                            Task {
                                if let cap = try? await store.repository.fetchCaptcha() {
                                    captchaId = cap.0
                                    captchaSvgPath = cap.1
                                }
                            }
                        }
                        .controlSize(.small)

                        if !captchaId.isEmpty {
                            TextField("Code", text: $captchaCode)
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
                            cjPassword = ""
                            Task {
                                if let res = try? await store.repository.loginJudge(
                                    account: cjAccount,
                                    password: pw,
                                    captchaId: captchaId,
                                    captchaCode: captchaCode
                                ) {
                                    cjFeedback = res.1
                                    if res.0 { await store.refreshLocal() }
                                }
                                isSigningInCJ = false
                            }
                        } label: {
                            if isSigningInCJ { ProgressView().controlSize(.small) }
                            else { Text("Sign In Judge") }
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(cjAccount.isEmpty || cjPassword.isEmpty || isSigningInCJ)

                        if store.snapshot?.auth.judge.loggedIn == true {
                            Button("Clear Session") {
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
                    Spacer()
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
                            SemanticPill(text: gc.loggedIn ? "ACTIVE" : "OFFLINE", compact: true)
                        }
                    }

                    if let gc = store.snapshot?.auth.gitcode {
                        Text("Shared with `gc` & git credential")
                            .font(.system(size: 10.5, design: .monospaced))
                            .foregroundStyle(.secondary)
                        if gc.loggedIn {
                            Text("Account: @\(gc.username ?? "—") · \(gc.protocolName ?? "https")")
                                .font(.system(size: 11, weight: .medium, design: .monospaced))
                                .foregroundStyle(.green)
                        }
                    }

                    Divider()

                    SecureField("GitCode Personal Access Token", text: $gcToken).textFieldStyle(.roundedBorder)

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
                        else { Text("Save Token via `gc auth login`") }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(gcToken.isEmpty || isSigningInGC)

                    if let fb = gcFeedback {
                        Text(fb).font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                    Spacer()
                }
            }
            .frame(maxWidth: .infinity)
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
