import Foundation

/// Hot (SSE) / cold (ETag snapshot) synchronisation for `ContestStore`.
///
/// Invariants:
/// - Hot frames are applied synchronously and never await cold I/O, so a slow cold fetch or an
///   auto-submit can no longer stall the SSE loop and replay a burst of stale frames afterwards.
/// - Cold fetches are serialized and coalesced: a refresh requested while one is in flight is
///   re-run afterwards (a forced refresh is never silently dropped).
/// - Responses for a workspace that is no longer selected, and hot frames from an older stream
///   generation, are discarded.
extension ContestStore {
    // MARK: - Cold path

    func refreshLocal(force: Bool = false) async {
        if isRefreshingLocal {
            coldRefreshPending = true
            if force { coldRefreshForcePending = true }
            return
        }
        isRefreshingLocal = true
        defer { isRefreshingLocal = false }

        var doForce = force
        for _ in 0..<4 {
            coldRefreshPending = false
            coldRefreshForcePending = false
            await performColdFetch(force: doForce)
            guard coldRefreshPending else { break }
            doForce = coldRefreshForcePending
        }
    }

    private func performColdFetch(force: Bool) async {
        let targetWs = selectedWorkspace
        let fetchStartedAt = Date()
        let ifNoneMatch = (force || lastLoadedWorkspace != targetWs) ? nil : lastColdETag

        do {
            let result = try await daemonClient.fetchSnapshot(
                workspace: targetWs,
                host: activeSSHHost,
                ifNoneMatch: ifNoneMatch
            )
            guard targetWs == selectedWorkspace else { return }
            switch result {
            case .notModified(let etag):
                lastLoadedWorkspace = targetWs
                if !etag.isEmpty { lastColdETag = etag }
                clearColdError()
                pruneSettling(confirmedAt: fetchStartedAt)
            case .updated(let snap, let etag):
                lastLoadedWorkspace = targetWs
                if !etag.isEmpty { lastColdETag = etag }
                applyColdSnapshot(snap, fetchStartedAt: fetchStartedAt)
            }
            return
        } catch {
            // Fall back transparently to ProcessClient CLI if daemon is unreachable
        }

        do {
            let localSnap = try await repository.fetchLocalSnapshot(workspace: targetWs)
            guard targetWs == selectedWorkspace else { return }
            lastLoadedWorkspace = targetWs
            lastColdETag = nil
            applyColdSnapshot(localSnap, fetchStartedAt: fetchStartedAt)
        } catch {
            guard targetWs == selectedWorkspace else { return }
            errorMessage = error.localizedDescription
            lastColdError = errorMessage
        }
    }

    private func applyColdSnapshot(_ localSnap: SupervisorSnapshot, fetchStartedAt: Date) {
        snapshot = localSnap
        hotStore.applyColdSeed(from: localSnap)
        if let bt = localSnap.scheduler.breakthroughThreshold, Date() >= breakthroughThresholdHoldUntil {
            breakthroughThreshold = bt
        }
        if !didSetLangLocally, let rawLang = localSnap.language, let parsed = AppLanguage(rawValue: rawLang) {
            lang = parsed
            UserDefaults.standard.set(parsed.rawValue, forKey: "parhelion.lang")
        }
        if selectedProblemTitle == nil || !localSnap.problems.contains(where: { $0.title == selectedProblemTitle }) {
            selectedProblemTitle = localSnap.problems.first?.title
        }
        clearColdError()
        pruneSettling(confirmedAt: fetchStartedAt)
        scheduleAutoConsume(localSnap)
        Task { [weak self] in await self?.reloadBackfillStatusIfNeeded(localSnap) }
    }

    /// Only clear errors produced by the cold path; user-action errors stay visible.
    private func clearColdError() {
        if let err = lastColdError, errorMessage == err { errorMessage = nil }
        lastColdError = nil
    }

    private func scheduleColdRefreshFromETag() {
        guard !isFetchingColdFromETag else { return }
        isFetchingColdFromETag = true
        Task { [weak self] in
            await self?.refreshLocal(force: false)
            self?.isFetchingColdFromETag = false
        }
    }

    func scheduleAutoConsume(
        _ snap: SupervisorSnapshot,
        livePipeline: LivePipelineState? = nil,
        liveCooldownRem: Int? = nil
    ) {
        guard autoConsumeTask == nil, !isLaunchingSubmit else { return }
        autoConsumeTask = Task { [weak self] in
            await self?.maybeAutoConsumeTopCandidate(snap, livePipeline: livePipeline, liveCooldownRem: liveCooldownRem)
            self?.autoConsumeTask = nil
        }
    }

    // MARK: - Hot path

    func refreshNpu(silent: Bool = false) async {
        if hotStore.isRefreshingNpu { return }
        hotStore.isRefreshingNpu = true
        defer { hotStore.isRefreshingNpu = false }

        let gen = streamGeneration
        let ws = selectedWorkspace
        let host = activeSSHHost
        do {
            let frame = try await daemonClient.fetchHot(
                workspace: ws,
                host: host,
                interval: max(0.5, pollIntervalSec > 0 ? pollIntervalSec : 1.0)
            )
            handleHotFrame(frame, decodeMs: 0, generation: gen)
            return
        } catch {
            // Fall back to ProcessClient if daemon unavailable
        }

        do {
            let npu = try await repository.fetchNpuTelemetry(workspace: ws, host: host)
            guard gen == streamGeneration else { return }
            hotStore.applyFallbackNpu(npu)
        } catch {
            if !silent { errorMessage = error.localizedDescription }
        }
    }

    func handleHotFrame(_ frame: HotStreamFrame, decodeMs: Double, generation: Int) {
        guard generation == streamGeneration else { return }
        hotStore.applyHotFrame(frame, decodeMs: decodeMs)
        reconcileHotInflight(frame.pipeline)
        reconcileAutoConsumerOverride(hotValue: frame.schedulerHot.autoConsumerEnabled)

        let wsMismatch = lastLoadedWorkspace != selectedWorkspace
        let etagChanged = !frame.coldEtag.isEmpty && frame.coldEtag != lastColdETag
        if etagChanged || (!frame.coldEtag.isEmpty && wsMismatch) {
            scheduleColdRefreshFromETag()
        }
        let coldStale = isFetchingColdFromETag || isRefreshingLocal || etagChanged || wsMismatch
        if !coldStale, let snap = snapshot, effectiveAutoConsumer(hot: frame.schedulerHot.autoConsumerEnabled) {
            scheduleAutoConsume(
                snap,
                livePipeline: frame.pipeline,
                liveCooldownRem: frame.schedulerHot.cooldownRemainingSec
            )
        }
    }

    func startTelemetryStream() {
        stopTelemetryStream()

        let interval = pollIntervalSec
        guard interval > 0 else {
            hotStore.markStreamDisconnected()
            return
        }

        let gen = streamGeneration
        let ws = selectedWorkspace
        let host = activeSSHHost
        streamTask = Task { [weak self] in
            var consecutiveErrors = 0
            while !Task.isCancelled {
                guard let self else { break }
                do {
                    let stream = await self.daemonClient.openHotStream(
                        workspace: ws,
                        host: host,
                        interval: max(0.5, interval)
                    )
                    for try await (frame, decodeMs) in stream {
                        if Task.isCancelled || gen != self.streamGeneration { break }
                        consecutiveErrors = 0
                        self.stopFallbackPolling()
                        self.handleHotFrame(frame, decodeMs: decodeMs, generation: gen)
                    }
                    if !Task.isCancelled {
                        self.hotStore.markStreamDisconnected()
                        try? await Task.sleep(nanoseconds: 1_000_000_000)
                    }
                } catch {
                    if Task.isCancelled { break }
                    consecutiveErrors += 1
                    self.hotStore.markStreamDisconnected()
                    if consecutiveErrors >= 2 {
                        self.hotStore.markDaemonOffline()
                        self.startFallbackPolling(interval: max(2.0, interval))
                    }
                    let delayNs: UInt64 = consecutiveErrors >= 2 ? 6_000_000_000 : 1_500_000_000
                    try? await Task.sleep(nanoseconds: delayNs)
                }
            }
        }
    }

    /// Cancels the SSE loop + fallback timer and invalidates every in-flight hot frame.
    func stopTelemetryStream() {
        streamGeneration &+= 1
        streamTask?.cancel()
        streamTask = nil
        stopFallbackPolling()
    }

    private func startFallbackPolling(interval: Double) {
        guard fallbackTimer == nil, interval > 0 else { return }
        fallbackTimer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                async let localLoad: () = self.refreshLocal()
                async let npuLoad: () = self.refreshNpu(silent: true)
                _ = await (localLoad, npuLoad)
            }
        }
    }

    private func stopFallbackPolling() {
        fallbackTimer?.invalidate()
        fallbackTimer = nil
    }

    func reschedulePolling() {
        startTelemetryStream()
    }
}
