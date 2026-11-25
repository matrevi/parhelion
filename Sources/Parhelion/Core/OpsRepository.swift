import Foundation

/// Typed repository layer mediating between ViewModels and `opsb` CLI commands.
struct OpsRepository: Sendable {
    private let client = ProcessClient()

    func fetchLocalSnapshot(workspace: String) async throws -> SupervisorSnapshot {
        try await decodeCLI(["status", "--workspace", workspace, "--offline-npu", "--compact", "--json"], timeout: 8.0)
    }

    func fetchNpuTelemetry(workspace: String, host: String) async throws -> NpuTelemetry {
        try await decodeCLI(["npu", "status", "--workspace", workspace, "--host", host, "--json"], timeout: 10.0)
    }

    func listWorkspaces() async throws -> WorkspaceListResponse {
        try await decodeCLI(["workspace", "list", "--json"])
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
        contestUrl: String = "",
        activate: Bool
    ) async throws {
        var args = [
            "workspace", "init", targetDir,
            "--git-owner", gitOwner,
            "--repo-prefix", repoPrefix,
            "--npu-host", npuHost,
            "--json"
        ]
        if !title.trimmingCharacters(in: .whitespaces).isEmpty { args += ["--title", title] }
        if !contestId.trimmingCharacters(in: .whitespaces).isEmpty { args += ["--contest-id", contestId] }
        if !contestUrl.trimmingCharacters(in: .whitespaces).isEmpty { args += ["--contest-url", contestUrl] }
        // Blank operator list lets the CLI scrape the real problem set from the contest link
        // instead of scaffolding the old placeholder operators.
        if !problemsSpec.trimmingCharacters(in: .whitespaces).isEmpty { args += ["--problems", problemsSpec] }
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
        try await decodeCLI(["breakthrough", "list", "--workspace", workspace, "--min-delta", String(format: "%.2f", minDelta), "--json"], timeout: 8.0)
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

    func backfillProblemBench(workspace: String, problem: String) async throws -> (dispatched: Bool, result: String, measuredCount: Int) {
        let data = try await client.execute(["bench", "backfill", problem, "--workspace", workspace, "--json"], timeout: 120.0)
        if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            let dispatched = (obj["dispatched"] as? Bool) ?? false
            let result = (obj["result"] as? String) ?? (obj["reason"] as? String) ?? (obj["action"] as? String) ?? "unknown"
            let measured = (obj["measured"] as? [String: Any])?.count ?? 0
            return (dispatched, result, measured)
        }
        return (false, "unknown", 0)
    }

    func jumpSubmitNow(workspace: String, problem: String, sha: String, note: String, force: Bool = false) async throws -> JumpSubmitResult {
        var args = ["queue", "submit-now", problem, sha, "--note", note, "--workspace", workspace]
        if force { args.append("--force") }
        let data = try await client.execute(args, timeout: 10.0, allowNonZeroExit: true)
        return JumpSubmitResult.parse(from: data, problem: problem, sha: sha)
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
        return try await decodeCLI(args, timeout: 45.0, allowNonZeroExit: true)
    }

    func listSSHHosts() async throws -> SSHHostListResponse {
        try await decodeCLI(["ssh", "list"])
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
        try await decodeCLI(["npu", "test-ssh", "--host", alias, "--json"], timeout: 10.0, allowNonZeroExit: true)
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

    func loginJudge(
        account: String,
        password: String,
        captchaId: String,
        captchaCode: String,
        accountId: String = "primary",
        label: String = ""
    ) async throws -> (Bool, String) {
        var args = ["auth", "judge-login", "--account", account, "--account-id", accountId, "--json"]
        if !label.isEmpty {
            args += ["--label", label]
        }
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

    func importJudgeSession(filePath: String, label: String) async throws -> (Bool, String) {
        var args = ["auth", "judge-import", filePath, "--json"]
        if !label.isEmpty { args += ["--label", label] }
        let data = try await client.execute(args, timeout: 10.0, allowNonZeroExit: true)
        if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            let ok = (obj["ok"] as? Bool) ?? false
            return (ok, ok ? "Imported (\(obj["user_id"] as? String ?? "OK"))" : (obj["error"] as? String ?? "Import failed"))
        }
        return (false, "Invalid response")
    }

    func setCaptain(id: String, workspace: String) async throws {
        _ = try await client.execute(["auth", "judge-captain", id, "--workspace", workspace, "--json"])
    }

    func toggleJudgeAccount(id: String, enabled: Bool) async throws {
        _ = try await client.execute(["auth", "judge-toggle", id, "--enabled", enabled ? "1" : "0", "--json"])
    }

    func removeJudgeAccount(id: String) async throws {
        _ = try await client.execute(["auth", "judge-remove", id, "--json"])
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

    /// Reads `python -m scheduler.backfill_status --json` through the CLI; never throws for
    /// a missing module or malformed payload — the banner needs a value, not an error.
    func fetchBackfillStatus(workspace: String, problem: String?) async throws -> BackfillStatusReport {
        var args = ["bench", "backfill-status", "--workspace", workspace, "--json"]
        if let problem, !problem.isEmpty { args.insert(problem, at: 2) }
        do {
            let data = try await client.execute(args, timeout: 10.0, allowNonZeroExit: true)
            return try await DaemonClient.decodeOffMain(BackfillStatusReport.self, from: data)
        } catch {
            return BackfillStatusReport.unavailable("cli_unavailable")
        }
    }

    // MARK: - Parhelion v3 Off-MainActor CLI Calls

    private func decodeCLI<T: Decodable>(_ args: [String], timeout: TimeInterval = 12.0, allowNonZeroExit: Bool = false) async throws -> T {
        let data = try await client.execute(args, timeout: timeout, allowNonZeroExit: allowNonZeroExit)
        return try await DaemonClient.decodeOffMain(T.self, from: data)
    }

    func fetchTriadKBList(pillar: String = "all", status: String? = nil, portability: String? = nil) async throws -> TriadKBListResponse {
        var args = ["kb", "list", "--pillar", pillar, "--json"]
        if let status, !status.isEmpty, status != "all" { args += ["--status", status] }
        if let portability, !portability.isEmpty, portability != "all" { args += ["--portability", portability] }
        return try await decodeCLI(args, timeout: 10.0)
    }

    func fetchTriadKBEntry(id: String) async throws -> TriadKBShowResponse {
        try await decodeCLI(["kb", "show", id, "--json"], timeout: 8.0, allowNonZeroExit: true)
    }

    func queryTriadKB(bottlenecks: [String] = [], regime: String? = nil, dtype: String? = nil, pillar: String = "all", text: String = "", limit: Int = 20) async throws -> TriadKBQueryResponse {
        var args = ["kb", "query", "--pillar", pillar, "--limit", "\(limit)", "--json"]
        for bn in bottlenecks where !bn.isEmpty { args += ["--bottleneck", bn] }
        if let regime, !regime.isEmpty { args += ["--regime", regime] }
        if let dtype, !dtype.isEmpty { args += ["--dtype", dtype] }
        if !text.trimmingCharacters(in: .whitespaces).isEmpty { args += ["--text", text] }
        return try await decodeCLI(args, timeout: 10.0)
    }

    func recordTriadKBFeedback(
        entryId: String, verdict: String, problem: String, sha: String,
        deltaRaw: Double? = nil, deltaUs: Double? = nil, speedup: Double? = nil, note: String = "", workspace: String = ""
    ) async throws -> TriadKBFeedbackResponse {
        var args = ["kb", "feedback", entryId, "--verdict", verdict, "--problem", problem, "--sha", sha, "--json"]
        if let deltaRaw { args += ["--delta-raw", String(format: "%.4f", deltaRaw)] }
        if let deltaUs { args += ["--delta-us", String(format: "%.4f", deltaUs)] }
        if let speedup { args += ["--speedup", String(format: "%.4f", speedup)] }
        if !note.isEmpty { args += ["--note", note] }
        if !workspace.isEmpty { args += ["--workspace", workspace] }
        return try await decodeCLI(args, timeout: 12.0, allowNonZeroExit: true)
    }

    func fetchKBEvolutionHistory(limit: Int = 50) async throws -> KBEvolutionHistoryResponse {
        try await decodeCLI(["kb", "history", "--limit", "\(limit)", "--json"], timeout: 8.0)
    }

    func rollbackKBEvent(eventId: String? = nil) async throws -> KBRollbackResponse {
        var args = ["kb", "rollback", "--json"]
        if let eventId, !eventId.isEmpty { args += ["--event-id", eventId] }
        return try await decodeCLI(args, timeout: 10.0, allowNonZeroExit: true)
    }

    func fetchDriverDoctor() async throws -> DriverDoctorResponse {
        try await decodeCLI(["driver", "doctor", "--json"], timeout: 10.0)
    }

    func selectDriver(_ kind: String) async throws -> DriverSelectResponse {
        try await decodeCLI(["driver", "select", kind, "--json"], timeout: 8.0, allowNonZeroExit: true)
    }

    func syncDriverWorkspace(workspace: String, driver: String? = nil) async throws -> DriverSyncResponse {
        var args = ["driver", "sync", "--workspace", workspace, "--json"]
        if let driver, !driver.isEmpty { args += ["--driver", driver] }
        return try await decodeCLI(args, timeout: 15.0, allowNonZeroExit: true)
    }

    func launchDriverSession(
        workspace: String, problem: String, driver: String? = nil, mode: String = "gui", track: String = "", goal: String = "", preseed: Bool = false
    ) async throws -> DriverLaunchResponse {
        var args = ["driver", "launch", problem, "--mode", mode, "--workspace", workspace, "--json"]
        if let driver, !driver.isEmpty { args += ["--driver", driver] }
        if !track.isEmpty { args += ["--track", track] }
        if !goal.isEmpty { args += ["--goal", goal] }
        if preseed { args.append("--preseed") }
        return try await decodeCLI(args, timeout: 25.0, allowNonZeroExit: true)
    }

    func fetchDriverSessions(workspace: String, problem: String? = nil, driver: String? = nil, includeGui: Bool = true) async throws -> DriverSessionsResponse {
        var args = ["driver", "sessions", "--workspace", workspace, includeGui ? "--include-gui" : "--no-gui", "--json"]
        if let problem, !problem.isEmpty { args += ["--problem", problem] }
        if let driver, !driver.isEmpty { args += ["--driver", driver] }
        return try await decodeCLI(args, timeout: 10.0, allowNonZeroExit: true)
    }

    func fetchElfGuardAll(workspace: String) async throws -> ElfBatchResponse {
        try await decodeCLI(["bench", "elf", "--workspace", workspace, "--json"], timeout: 15.0)
    }

    func fetchElfGuard(workspace: String, problem: String) async throws -> ElfGuardResult {
        try await decodeCLI(["bench", "elf", problem, "--workspace", workspace, "--json"], timeout: 12.0)
    }

    func fetchCold3Probe(workspace: String, problem: String) async throws -> Cold3Result {
        try await decodeCLI(["bench", "cold3", problem, "--workspace", workspace, "--json"], timeout: 12.0)
    }

    func fetchPmuTorchCard(workspace: String, problem: String, top: Int = 8) async throws -> PmuTorchCard {
        try await decodeCLI(["bench", "pmu", problem, "--top", "\(top)", "--workspace", workspace, "--json"], timeout: 15.0)
    }
}
