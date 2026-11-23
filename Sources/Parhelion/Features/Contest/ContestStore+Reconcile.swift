import Foundation

/// Canonical `title|sha7` identity shared by hot `inflight_keys`, hot `active_submissions`
/// and cold `submission_queue` items, so one logical submission keeps ONE view identity
/// across candidate → submitting → active → settling.
enum SubmissionKey {
    static func make(_ title: String, _ sha: String) -> String {
        "\(title)|\(String(sha.prefix(7)))"
    }

    static func normalize(_ raw: String) -> String {
        let parts = raw.split(separator: "|", maxSplits: 1, omittingEmptySubsequences: false)
        guard parts.count == 2 else { return raw }
        return make(String(parts[0]), String(parts[1]))
    }

    static func of(_ item: SubmissionQueueItem) -> String {
        make(item.title, item.sha.isEmpty ? item.shortSha : item.sha)
    }

    static func of(_ active: ActiveSubmissionItem) -> String {
        make(active.title, active.sha.isEmpty ? active.shortSha : active.sha)
    }

    static func title(of key: String) -> String {
        String(key.split(separator: "|", maxSplits: 1).first ?? Substring(key))
    }
}

struct LaunchingSubmission: Equatable {
    let key: String
    let title: String
    let sha: String
    let shortSha: String
    let startedEpoch: Double
    let expiresAt: Date

    func asActiveItem() -> ActiveSubmissionItem {
        ActiveSubmissionItem(
            id: "launching-\(key)",
            title: title,
            sha: sha,
            shortSha: shortSha,
            phase: "launching",
            accountId: "primary",
            accountLabel: "Launching",
            remoteSubmissionId: nil,
            remoteJudgeId: nil,
            remoteStatus: nil,
            startedEpoch: startedEpoch,
            elapsedSec: 0
        )
    }
}

/// Hot/cold reconciliation state machine (kept out of `HotTelemetryStore` to keep it lightweight).
extension ContestStore {
    /// Keys the hot pipeline currently reports as inflight/active.
    func liveInflightKeys(_ pipe: LivePipelineState?) -> Set<String> {
        guard let pipe else { return [] }
        var live = Set((pipe.inflightKeys ?? []).map(SubmissionKey.normalize))
        for active in pipe.activeSubmissions ?? [] where !active.title.isEmpty {
            live.insert(SubmissionKey.of(active))
        }
        if pipe.isBusy, let title = pipe.activeTitle, !title.isEmpty {
            live.insert(SubmissionKey.make(title, pipe.activeSha ?? pipe.activeShortSha ?? ""))
        }
        return live
    }

    /// All keys currently owned by the active submission deck (live + launching + settling).
    func activeDeckKeys(_ pipe: LivePipelineState?) -> Set<String> {
        let now = Date()
        let activeLaunching = launchingSubmits.values.filter { $0.expiresAt > now }.map(\.key)
        return liveInflightKeys(pipe)
            .union(settlingInflightKeys)
            .union(activeLaunching)
    }

    /// Called for every accepted hot frame. A key that leaves the hot inflight set enters
    /// "settling" until a cold snapshot fetched *after* it left confirms the outcome; this closes
    /// the window where the stale cold snapshot would resurrect the candidate card.
    func reconcileHotInflight(_ pipe: LivePipelineState) {
        let now = Date()
        let live = liveInflightKeys(pipe)
        for active in pipe.activeSubmissions ?? [] where !active.title.isEmpty {
            lastActiveByKey[SubmissionKey.of(active)] = active
        }
        for gone in hotLiveInflightKeys.subtracting(live) where settlingSince[gone] == nil {
            settlingSince[gone] = now
        }
        for key in live { settlingSince[key] = nil }
        settlingSince = settlingSince.filter { now.timeIntervalSince($0.value) < 45 }
        hotLiveInflightKeys = live

        let remainingLaunching = launchingSubmits.filter { !live.contains($0.key) && $0.value.expiresAt > now }
        if remainingLaunching != launchingSubmits { launchingSubmits = remainingLaunching }
        publishSettling()
    }

    /// A cold snapshot (or 304) whose fetch started at `confirmedAt` reflects every key that left
    /// the hot inflight set before that instant.
    func pruneSettling(confirmedAt: Date) {
        settlingSince = settlingSince.filter { $0.value > confirmedAt }
        publishSettling()
    }

    private func publishSettling() {
        let keys = Set(settlingSince.keys)
        lastActiveByKey = lastActiveByKey.filter { hotLiveInflightKeys.contains($0.key) || keys.contains($0.key) }
        if keys != settlingInflightKeys { settlingInflightKeys = keys }
    }

    /// Last known active card for a settling key, re-labelled as "syncing score".
    func settlingActive(for key: String) -> ActiveSubmissionItem? {
        guard let a = lastActiveByKey[key] else { return nil }
        return ActiveSubmissionItem(
            id: a.id, title: a.title, sha: a.sha, shortSha: a.shortSha, phase: "fetching_score",
            accountId: a.accountId, accountLabel: a.accountLabel, remoteSubmissionId: a.remoteSubmissionId,
            remoteJudgeId: a.remoteJudgeId, remoteStatus: a.remoteStatus, startedEpoch: a.startedEpoch,
            elapsedSec: a.elapsedSec
        )
    }

    var settlingInflightTitles: Set<String> {
        Set(settlingInflightKeys.map(SubmissionKey.title(of:)))
    }

    func resetHotColdReconciliation() {
        hotLiveInflightKeys = []
        settlingSince = [:]
        lastActiveByKey = [:]
        if !settlingInflightKeys.isEmpty { settlingInflightKeys = [] }
        if !launchingSubmits.isEmpty { launchingSubmits = [:] }
        clearAutoConsumerOverride()
    }

    // MARK: - Optimistic submit launch

    func markLaunching(problem: String, sha: String) {
        let now = Date()
        let shortSha = String(sha.prefix(7))
        let key = SubmissionKey.make(problem, sha)
        launchingSubmits[key] = LaunchingSubmission(
            key: key,
            title: problem,
            sha: sha,
            shortSha: shortSha,
            startedEpoch: now.timeIntervalSince1970,
            expiresAt: now.addingTimeInterval(25.0)
        )
    }

    func clearLaunching(_ key: String) {
        if launchingSubmits[key] != nil { launchingSubmits[key] = nil }
    }

    // MARK: - Optimistic auto-consumer toggle

    func setAutoConsumerOverride(_ value: Bool) {
        autoConsumerOverride = value
        autoConsumerOverrideUntil = Date().addingTimeInterval(6.0)
    }

    func clearAutoConsumerOverride() {
        if autoConsumerOverride != nil { autoConsumerOverride = nil }
        autoConsumerOverrideUntil = .distantPast
    }

    func reconcileAutoConsumerOverride(hotValue: Bool) {
        guard let pending = autoConsumerOverride else { return }
        if pending == hotValue || Date() >= autoConsumerOverrideUntil {
            clearAutoConsumerOverride()
        }
    }

    /// Single source of truth for "is auto-submit on": optimistic > hot > cold.
    func effectiveAutoConsumer(hot: Bool? = nil) -> Bool {
        if let pending = autoConsumerOverride { return pending }
        if let hot { return hot }
        return hotStore.schedulerHot?.autoConsumerEnabled ?? snapshot?.scheduler.autoConsumerEnabled ?? false
    }
}
