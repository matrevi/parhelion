import Foundation

/// Typed repository layer mediating between ViewModels and `opsb` CLI commands.
struct OpsRepository: Sendable {
    private let client = ProcessClient()

    func fetchLocalSnapshot(workspace: String) async throws -> SupervisorSnapshot {
        let data = try await client.execute(
            ["status", "--workspace", workspace, "--offline-npu", "--compact", "--json"],
            timeout: 8.0
        )
        return try JSONDecoder().decode(SupervisorSnapshot.self, from: data)
    }

    func fetchNpuTelemetry(workspace: String, host: String) async throws -> NpuTelemetry {
        let data = try await client.execute(
            ["npu", "status", "--workspace", workspace, "--host", host, "--json"],
            timeout: 10.0
        )
        return try JSONDecoder().decode(NpuTelemetry.self, from: data)
    }

    func listWorkspaces() async throws -> WorkspaceListResponse {
        let data = try await client.execute(["workspace", "list", "--json"])
        return try JSONDecoder().decode(WorkspaceListResponse.self, from: data)
    }

    func selectWorkspace(_ path: String) async throws {
        _ = try await client.execute(["workspace", "select", path, "--json"])
    }

    func scaffoldWorkspace(
        targetDir: String,
        title: String,
        contestId: String,
        gitOwner: String,
        repoPrefix: String,
        npuHost: String,
        problemsSpec: String,
        activate: Bool
    ) async throws {
        var args = [
            "workspace", "init", targetDir,
            "--title", title,
            "--contest-id", contestId,
            "--git-owner", gitOwner,
            "--repo-prefix", repoPrefix,
            "--npu-host", npuHost,
            "--problems", problemsSpec,
            "--json"
        ]
        if activate { args.append("--activate") }
        _ = try await client.execute(args)
    }

    func regenerateAgentsMd(workspace: String) async throws {
        _ = try await client.execute(["workspace", "gen-agents", workspace, "--json"])
    }

    func updateQueueConfig(autoConsumer: Bool?, closingReserve: Int?, noiseFloor: Double?) async throws {
        var args = ["queue", "config"]
        if let ac = autoConsumer { args += ["--auto-consumer", ac ? "on" : "off"] }
        if let cr = closingReserve { args += ["--closing-reserve", "\(cr)"] }
        if let nf = noiseFloor { args += ["--noise-floor", String(format: "%.2f", nf)] }
        _ = try await client.execute(args)
    }

    func jumpSubmitNow(workspace: String, problem: String, sha: String, note: String) async throws -> (Bool, String) {
        let data = try await client.execute(
            ["queue", "submit-now", problem, sha, "--note", note, "--workspace", workspace],
            timeout: 10.0,
            allowNonZeroExit: true
        )
        if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            let ok = (obj["ok"] as? Bool) ?? false
            let err = (obj["error"] as? String) ?? "已启动提交"
            return (ok, ok ? "已提交 \(problem) (\(String(sha.prefix(7)))) 至发送通道" : err)
        }
        return (false, "提交启动失败")
    }

    func approveSubmission(workspace: String, problem: String, sha: String, note: String) async throws {
        _ = try await client.execute(["queue", "approve", problem, sha, "--note", note, "--workspace", workspace])
    }

    func skipSubmission(workspace: String, problem: String, sha: String, reason: String) async throws {
        _ = try await client.execute(["queue", "skip", problem, sha, "--reason", reason, "--workspace", workspace])
    }

    func restoreBestSubmission(workspace: String, problem: String) async throws {
        _ = try await client.execute(["queue", "restore", problem, "--workspace", workspace])
    }

    func drainQueueOnce(workspace: String) async throws -> String {
        let data = try await client.execute(["queue", "drain", "--once", "--workspace", workspace, "--json"], timeout: 120.0)
        if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let out = obj["output"] as? String, !out.isEmpty {
            return out.components(separatedBy: .newlines).last ?? "Queue consumer completed"
        }
        return "Queue consumer completed"
    }

    func inviteCollaborator(workspace: String, username: String, permission: String, dryRun: Bool) async throws -> CollabInviteResponse {
        var args = ["collab", "invite", username, "--workspace", workspace, "--permission", permission, "--json"]
        if dryRun { args.append("--dry-run") }
        let data = try await client.execute(args, timeout: 45.0, allowNonZeroExit: true)
        return try JSONDecoder().decode(CollabInviteResponse.self, from: data)
    }

    func listSSHHosts() async throws -> SSHHostListResponse {
        let data = try await client.execute(["ssh", "list"])
        return try JSONDecoder().decode(SSHHostListResponse.self, from: data)
    }

    func selectSSHHost(_ alias: String) async throws {
        _ = try await client.execute(["ssh", "select", alias])
    }

    func saveSSHHost(alias: String, hostname: String, port: Int, user: String, identityFile: String, writeConfig: Bool, activate: Bool) async throws {
        var args = ["ssh", "save", "--alias", alias, "--hostname", hostname, "--port", "\(port)", "--user", user, "--identity-file", identityFile]
        if writeConfig { args.append("--write-ssh-config") }
        if activate { args.append("--activate") }
        _ = try await client.execute(args)
    }

    func testSSH(_ alias: String) async throws -> SSHTestResult {
        let data = try await client.execute(["npu", "test-ssh", "--host", alias, "--json"], timeout: 10.0, allowNonZeroExit: true)
        return try JSONDecoder().decode(SSHTestResult.self, from: data)
    }

    func deployNpuProbe(_ alias: String) async throws {
        _ = try await client.execute(["npu", "agent-install", "--host", alias, "--json"], timeout: 15.0)
    }

    func fetchCaptcha() async throws -> (String, String)? {
        let data = try await client.execute(["auth", "judge-captcha", "--json"], timeout: 20.0, allowNonZeroExit: true)
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              (obj["ok"] as? Bool) == true,
              let cid = obj["captcha_id"] as? String,
              let path = obj["svg_path"] as? String else { return nil }
        return (cid, path)
    }

    func loginJudge(account: String, password: String, captchaId: String, captchaCode: String) async throws -> (Bool, String) {
        var args = ["auth", "judge-login", "--account", account, "--json"]
        if !captchaId.isEmpty && !captchaCode.isEmpty {
            args += ["--captcha-id", captchaId, "--captcha-code", captchaCode]
        }
        let data = try await client.execute(args, stdinString: password, timeout: 25.0, allowNonZeroExit: true)
        if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            let ok = (obj["ok"] as? Bool) ?? false
            return (ok, ok ? "Signed in (\(obj["user_id"] as? String ?? "OK"))" : (obj["error"] as? String ?? "Login failed"))
        }
        return (false, "Invalid response")
    }

    func logoutJudge() async throws {
        _ = try await client.execute(["auth", "judge-logout", "--json"])
    }

    func loginGitCode(token: String) async throws -> (Bool, String) {
        let data = try await client.execute(["auth", "gitcode-login", "--json"], stdinString: token, timeout: 20.0, allowNonZeroExit: true)
        if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any], (obj["ok"] as? Bool) == true {
            return (true, "GitCode token verified & saved")
        }
        return (false, "GitCode authentication failed")
    }

    func installFallbackSkills() async throws {
        _ = try await client.execute(["skills", "install", "--targets", "dsh,codex", "--json"])
    }
}
