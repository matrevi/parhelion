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
        // Optimistic toggle wins: switching auto-submit off must stop dispatch immediately,
        // not after the next hot frame confirms it.
        guard effectiveAutoConsumer(),
              (pipe?.canSubmit ?? true),
              !(pipe?.isLocalBusy ?? false),
              cdRem == 0,
              !isLaunchingSubmit else { return }

        let now = Date()
        autoConsumeBackoffUntil = autoConsumeBackoffUntil.filter { $0.value > now }

        let busyKeys = activeDeckKeys(pipe)
        let busyTitles = Set(busyKeys.map(SubmissionKey.title(of:)))

        // Never auto-consume `advisory` restore cards or `ESCAPE LOCK` / gated / backed-off items.
        // "RATE" is not excluded by badge: cooldown is checked live above and quota exhaustion
        // is already reflected in `gates.all_pass == false`.
        guard let top = snap.submissionQueue.first(where: { item in
            let u = item.badge.uppercased()
            let actionableKind = item.kind == "pending" || item.kind == "candidate"
            let notBlockedBadge = !u.contains("ESCAPE")
                && !u.contains("HOLD")
                && !u.contains("CIRCUIT")
                && !u.contains("REJECTED")
                && !u.contains("QUOTA")
                && !u.contains("SKIP")
            let key = SubmissionKey.of(item)
            let notBackedOff = (autoConsumeBackoffUntil[key] ?? .distantPast) <= now
            return actionableKind
                && notBlockedBadge
                && notBackedOff
                && !busyKeys.contains(key)
                && !busyTitles.contains(item.title)
                && item.gates.allPass
        }) else { return }

        autoConsumeBackoffUntil[SubmissionKey.of(top)] = now.addingTimeInterval(45.0)
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
        let key = SubmissionKey.make(problem, sha)
        // Keep the card visible as "submitting" until the hot pipeline picks it up.
        markLaunching(problem: problem, sha: sha)
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
                await refreshNpu(silent: true)
            } else if isAuto && res.isDuplicateOrMutex {
                clearLaunching(key)
                autoConsumeBackoffUntil[key] = Date().addingTimeInterval(180.0)
                await daemonClient.invalidate(workspace: selectedWorkspace)
                await refreshLocal(force: true)
            } else if isAuto {
                clearLaunching(key)
                autoConsumeBackoffUntil[key] = Date().addingTimeInterval(45.0)
            } else {
                clearLaunching(key)
                errorMessage = res.message
            }
        } catch {
            clearLaunching(key)
            if isAuto {
                autoConsumeBackoffUntil[key] = Date().addingTimeInterval(45.0)
            } else {
                errorMessage = error.localizedDescription
            }
        }
    }
}
