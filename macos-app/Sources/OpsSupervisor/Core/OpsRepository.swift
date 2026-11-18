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

    func updateQueueConfig(
        autoConsumer: Bool? = nil,
        closingReserve: Int? = nil,
        noiseFloor: Double? = nil,
        breakthroughThreshold: Double? = nil,
        language: String? = nil
    ) async throws {
        var args = ["queue", "config"]
        if let ac = autoConsumer { args += ["--auto-consumer", ac ? "on" : "off"] }
        if let cr = closingReserve { args += ["--closing-reserve", "\(cr)"] }
        if let nf = noiseFloor { args += ["--noise-floor", String(format: "%.2f", nf)] }
        if let bt = breakthroughThreshold { args += ["--breakthrough-threshold", String(format: "%.2f", bt)] }
        if let lg = language { args += ["--lang", lg] }
        _ = try await client.execute(args)
    }

    func fetchBreakthroughCatalog(workspace: String, minDelta: Double = 0.5) async throws -> BreakthroughCatalogResponse {
        let data = try await client.execute(
            ["breakthrough", "list", "--workspace", workspace, "--min-delta", String(format: "%.2f", minDelta), "--json"],
            timeout: 8.0
        )
        return try JSONDecoder().decode(BreakthroughCatalogResponse.self, from: data)
    }

    func syncBreakthroughPlaybook(workspace: String, minDelta: Double) async throws -> (Int, Int) {
        let data = try await client.execute(
            ["breakthrough", "sync", "--workspace", workspace, "--min-delta", String(format: "%.2f", minDelta), "--json"],
            timeout: 10.0
        )
        if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            let created = (obj["created_ids"] as? [String])?.count ?? 0
            let total = (obj["total_entries"] as? Int) ?? 0
            return (created, total)
        }
        return (0, 0)
    }

    func calibrateProblemBench(workspace: String, problem: String) async throws {
        _ = try await client.execute(["bench", "calibrate", problem, "--workspace", workspace, "--json"], timeout: 25.0)
    }

    func backfillProblemBench(workspace: String, problem: String) async throws {
        _ = try await client.execute(["bench", "backfill", problem, "--workspace", workspace, "--json"], timeout: 120.0)
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

    func boostNpuTicket(host: String, pid: Int, problem: String, sha: String) async throws {
        _ = try await client.execute(["npu", "boost", "\(pid)", "--problem", problem, "--sha", sha, "--host", host, "--json"], timeout: 8.0)
    }

    func cancelNpuTask(host: String, pid: Int) async throws {
        _ = try await client.execute(["npu", "cancel", "\(pid)", "--host", host, "--json"], timeout: 8.0)
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

    // MARK: - Breakthrough hand-off bridge

    func fetchBridgeStatus(workspace: String) async throws -> BridgeStatusResponse {
        let data = try await client.execute(
            ["bridge", "status", "--workspace", workspace, "--json"],
            timeout: 12.0,
            allowNonZeroExit: true
        )
        return try JSONDecoder().decode(BridgeStatusResponse.self, from: data)
    }

    func syncBridge(workspace: String) async throws -> (delivered: Int, skipped: Int, errors: Int) {
        let data = try await client.execute(
            ["bridge", "sync", "--workspace", workspace, "--json"],
            timeout: 25.0,
            allowNonZeroExit: true
        )
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return (0, 0, 0) }
        return ((obj["delivered_count"] as? Int) ?? 0, (obj["skipped_count"] as? Int) ?? 0, (obj["error_count"] as? Int) ?? 0)
    }

    func collectBridgeReceipts(workspace: String) async throws -> (collected: Int, total: Int, orphaned: Int) {
        let data = try await client.execute(
            ["bridge", "collect", "--workspace", workspace, "--json"],
            timeout: 20.0,
            allowNonZeroExit: true
        )
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return (0, 0, 0) }
        return ((obj["collected_count"] as? Int) ?? 0, (obj["total_summaries"] as? Int) ?? 0, (obj["orphaned_count"] as? Int) ?? 0)
    }

    func pullBridge(workspace: String, agentId: String, ack: Bool, entryIds: [String]) async throws -> Int {
        var args = ["bridge", "pull", agentId, "--workspace", workspace, "--json"]
        if ack { args.append("--ack") }
        for entryId in entryIds { args += ["--entry", entryId] }
        let data = try await client.execute(args, timeout: 20.0, allowNonZeroExit: true)
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return 0 }
        return (obj["count"] as? Int) ?? 0
    }

    func pushBridge(workspace: String, agentId: String, entryId: String?) async throws -> (pushed: Int, failed: Int, detail: String) {
        var args = ["bridge", "push", agentId, "--workspace", workspace, "--json"]
        if let entryId, !entryId.isEmpty { args += ["--entry", entryId] }
        let data = try await client.execute(args, timeout: 60.0, allowNonZeroExit: true)
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return (0, 0, "no response") }
        let pushed = (obj["pushed_count"] as? Int) ?? 0
        let failed = (obj["failed_count"] as? Int) ?? 0
        var detail = ""
        if let failedRows = obj["failed"] as? [[String: Any]], let first = failedRows.first {
            detail = (first["error"] as? String) ?? ""
        }
        return (pushed, failed, detail)
    }

    /// Reads `python -m scheduler.backfill_status --json` through the CLI; never throws for
    /// a missing module or malformed payload — the banner needs a value, not an error.
    func fetchBackfillStatus(workspace: String, problem: String?) async throws -> BackfillStatusReport {
        var args = ["bench", "backfill-status", "--workspace", workspace, "--json"]
        if let problem, !problem.isEmpty { args.insert(problem, at: 2) }
        do {
            let data = try await client.execute(args, timeout: 10.0, allowNonZeroExit: true)
            return try JSONDecoder().decode(BackfillStatusReport.self, from: data)
        } catch {
            return BackfillStatusReport.unavailable("cli_unavailable")
        }
    }
}
