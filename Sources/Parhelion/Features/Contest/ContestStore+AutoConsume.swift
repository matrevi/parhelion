import Foundation

/// Auto-consume candidate selection, backoff deduplication, and jump-submit dispatch (`V3-026` / `V3-028`).
extension ContestStore {
    func maybeAutoConsumeTopCandidate(
        _ snap: SupervisorSnapshot,
        livePipeline: LivePipelineState? = nil,
        liveCooldownRem: Int? = nil
    ) async {
        let pipe = livePipeline ?? hotStore.pipeline ?? snap.scheduler.pipeline
        let cdRem = liveCooldownRem ?? pipe?.cooldownRemainingSec ?? snap.scheduler.cooldownRemainingSec
        let autoEnabled = hotStore.schedulerHot?.autoConsumerEnabled ?? snap.scheduler.autoConsumerEnabled
        guard autoEnabled,
              (pipe?.canSubmit ?? true),
              !(pipe?.isLocalBusy ?? false),
              cdRem == 0,
              !isLaunchingSubmit else { return }

        let now = Date()
        autoConsumeBackoffUntil = autoConsumeBackoffUntil.filter { $0.value > now }

        let inflightSet = Set(pipe?.inflightKeys ?? [])
        var inflightTitles = Set<String>()
        for key in pipe?.inflightKeys ?? [] {
            if let opTitle = key.split(separator: "|", maxSplits: 1).first, !opTitle.isEmpty {
                inflightTitles.insert(String(opTitle))
            }
        }
        for active in pipe?.activeSubmissions ?? [] where !active.title.isEmpty {
            inflightTitles.insert(active.title)
        }
        if let activeTitle = pipe?.activeTitle, !activeTitle.isEmpty {
            inflightTitles.insert(activeTitle)
        }

        // Never auto-consume `advisory` restore cards or `ESCAPE LOCK` / gated / backed-off items.
        guard let top = snap.submissionQueue.first(where: { item in
            let u = item.badge.uppercased()
            let actionableKind = item.kind == "pending" || item.kind == "candidate"
            let notBlockedBadge = !u.contains("ESCAPE")
                && !u.contains("HOLD")
                && !u.contains("CIRCUIT")
                && !u.contains("REJECTED")
                && !u.contains("RATE")
                && !u.contains("SKIP")
            let key = "\(item.title)|\(item.shortSha)"
            let notBackedOff = (autoConsumeBackoffUntil[key] ?? .distantPast) <= now
            return actionableKind
                && notBlockedBadge
                && notBackedOff
                && !inflightSet.contains(key)
                && !inflightTitles.contains(item.title)
                && item.gates.allPass
        }) else { return }

        autoConsumeBackoffUntil["\(top.title)|\(top.shortSha)"] = now.addingTimeInterval(45.0)
        await jumpSubmitNow(problem: top.title, sha: top.sha, note: top.note, isAuto: true, force: false)
    }

    func jumpSubmitNow(
        problem: String,
        sha: String,
        note: String,
        isAuto: Bool = false,
        force: Bool = false
    ) async {
        if isLaunchingSubmit { return }
        isLaunchingSubmit = true
        defer { isLaunchingSubmit = false }

        let effectiveForce = !isAuto && force
        let key = "\(problem)|\(String(sha.prefix(7)))"
        do {
            let res = try await repository.jumpSubmitNow(
                workspace: selectedWorkspace,
                problem: problem,
                sha: sha,
                note: note,
                force: effectiveForce
            )
            if res.ok {
                bannerMessage = tr(
                    "已启动提交 \(problem) (\(String(sha.prefix(7))))",
                    "Launched submit for \(problem) (\(String(sha.prefix(7))))"
                )
                await daemonClient.invalidate(workspace: selectedWorkspace)
                await refreshLocal(force: true)
            } else if isAuto && res.isDuplicateOrMutex {
                autoConsumeBackoffUntil[key] = Date().addingTimeInterval(180.0)
                await daemonClient.invalidate(workspace: selectedWorkspace)
                await refreshLocal(force: true)
            } else if isAuto {
                autoConsumeBackoffUntil[key] = Date().addingTimeInterval(45.0)
            } else {
                errorMessage = res.message
            }
        } catch {
            if isAuto {
                autoConsumeBackoffUntil[key] = Date().addingTimeInterval(45.0)
            } else {
                errorMessage = error.localizedDescription
            }
        }
    }
}
