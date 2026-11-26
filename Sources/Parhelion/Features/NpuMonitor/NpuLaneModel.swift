import SwiftUI

/// Three-lane NPU queue lanes, in display order.
enum NpuLane: String, CaseIterable {
    case active = "ACTIVE"
    case build = "CPU_BUILD"
    case waiting = "WAITING"
}

struct NpuLaneCard: Identifiable {
    /// Stable logical identity (`problem|Q|B0[#n]`): independent of pid and lane, so a task moving
    /// CPU_BUILD → WAITING → ACTIVE (or handing off across worker processes) keeps ONE identity and
    /// glides smoothly between lanes instead of vanishing and re-appearing.
    let id: String
    let task: NpuTaskItem
    let lane: NpuLane
    let startedEpoch: Double
    let lingering: Bool
}

/// Reconciles successive hot NPU task lists into stable, gap-free lane cards.
///
/// - Logical identity stays continuous across lane transitions (`CPU_BUILD` ↔ `WAITING` ↔ `ACTIVE`)
///   and across worker PID hand-offs (reclaiming by `pid`, then `sha7`, then lingering same-problem).
/// - Multi-process slots preserve deterministic ordering via monotonic `slotRank` so processes never
///   jump to a new slot leaving an empty hole.
/// - A card absent from a frame lingers in place for `lingerSec` before fading out, absorbing brief
///   hand-off / probe vacuum frames.
@MainActor
final class NpuLaneModel: ObservableObject {
    @Published private(set) var cards: [NpuLaneCard] = []
    private(set) var hasIngested: Bool = false

    static let lingerSec: TimeInterval = 2.5

    private var gone: [String: (card: NpuLaneCard, index: Int, since: Date)] = [:]
    /// Keyed by `host#pid`: two device hosts may run processes with the same pid.
    private var pidOwner: [String: String] = [:]
    private var shaOwner: [String: String] = [:]
    private var slotRank: [String: Int] = [:]
    private var startedEpochById: [String: Double] = [:]
    private var nextRank: Int = 0
    private var lastTasks: [NpuTaskItem] = []
    private var expiryTask: Task<Void, Never>?

    deinit { expiryTask?.cancel() }

    var layoutSignature: [String] {
        cards.map { "\($0.id)@\($0.lane.rawValue)\($0.lingering ? "~" : "")" }
    }

    func cards(in lane: NpuLane) -> [NpuLaneCard] {
        cards.filter { $0.lane == lane }
    }

    func reset() {
        expiryTask?.cancel()
        expiryTask = nil
        gone.removeAll()
        pidOwner.removeAll()
        shaOwner.removeAll()
        slotRank.removeAll()
        startedEpochById.removeAll()
        lastTasks.removeAll()
        hasIngested = false
        if !cards.isEmpty { cards = [] }
    }

    func ingest(_ tasks: [NpuTaskItem], now: Date = Date()) {
        hasIngested = true
        lastTasks = tasks
        let nowEpoch = now.timeIntervalSince1970
        var used = Set<String>()
        var nextPidOwner: [String: String] = [:]
        var nextShaOwner: [String: String] = [:]
        var live: [NpuLaneCard] = []

        for task in tasks {
            guard let lane = NpuLane(rawValue: task.state) else { continue }
            let base = Self.baseKey(task)
            let sha7 = Self.shortSha(of: task)
            let shaKey = sha7.isEmpty ? nil : "\(base)|\(sha7)"

            let id = resolveId(base: base, pidKey: task.pid > 0 ? Self.pidKey(task) : nil, shaKey: shaKey, used: used)
            used.insert(id)
            if task.pid > 0 { nextPidOwner[Self.pidKey(task)] = id }
            if let shaKey { nextShaOwner[shaKey] = id }
            if slotRank[id] == nil {
                nextRank += 1
                slotRank[id] = nextRank
            }

            let estimatedStart = nowEpoch - Double(max(0, task.elapsedSec ?? 0))
            let anchoredStart: Double
            if let prev = startedEpochById[id], abs(prev - estimatedStart) <= 3.5 {
                anchoredStart = prev
            } else {
                anchoredStart = estimatedStart
                startedEpochById[id] = anchoredStart
            }

            live.append(NpuLaneCard(
                id: id,
                task: task,
                lane: lane,
                startedEpoch: anchoredStart,
                lingering: false
            ))
        }
        pidOwner = nextPidOwner
        shaOwner = nextShaOwner

        // Cards that just disappeared linger in their last lane position to absorb transition gaps.
        for (index, prev) in cards.enumerated() where !used.contains(prev.id) && gone[prev.id] == nil {
            let laneIndex = cards[..<index].filter { $0.lane == prev.lane }.count
            let lingeringCard = NpuLaneCard(
                id: prev.id,
                task: prev.task,
                lane: prev.lane,
                startedEpoch: prev.startedEpoch,
                lingering: true
            )
            gone[prev.id] = (lingeringCard, laneIndex, now)
        }
        for id in used { gone[id] = nil }
        let livePids = Set(live.map { Self.pidKey($0.task) })
        gone = gone.filter {
            let card = $0.value.card
            return card.lane != .active && !livePids.contains(Self.pidKey(card.task))
                && now.timeIntervalSince($0.value.since) < Self.lingerSec
        }

        let aliveIds = used.union(gone.keys)
        slotRank = slotRank.filter { aliveIds.contains($0.key) }
        startedEpochById = startedEpochById.filter { aliveIds.contains($0.key) }

        var result: [NpuLaneCard] = []
        for lane in NpuLane.allCases {
            var laneCards = sortedLive(live.filter { $0.lane == lane }, in: lane)
            let lingeringLane = gone.values
                .filter { $0.card.lane == lane }
                .sorted { $0.index < $1.index }
            for entry in lingeringLane {
                laneCards.insert(entry.card, at: min(entry.index, laneCards.count))
            }
            result.append(contentsOf: laneCards)
        }
        cards = result
        scheduleExpiry()
    }

    /// Synchronous preview before the first `onAppear` so frame 0 is never blank.
    static func preview(_ tasks: [NpuTaskItem]) -> [NpuLaneCard] {
        let nowEpoch = Date().timeIntervalSince1970
        var used = Set<String>()
        var out: [NpuLaneCard] = []
        for task in tasks {
            guard let lane = NpuLane(rawValue: task.state) else { continue }
            let base = baseKey(task)
            var id = base
            var n = 1
            while used.contains(id) { n += 1; id = "\(base)#\(n)" }
            used.insert(id)
            out.append(NpuLaneCard(
                id: id,
                task: task,
                lane: lane,
                startedEpoch: nowEpoch - Double(max(0, task.elapsedSec ?? 0)),
                lingering: false
            ))
        }
        return out
    }

    private func resolveId(base: String, pidKey: String?, shaKey: String?, used: Set<String>) -> String {
        if let pidKey, let owned = pidOwner[pidKey], matchesBase(owned, base), !used.contains(owned) {
            return owned
        }
        if let shaKey, let owned = shaOwner[shaKey], matchesBase(owned, base), !used.contains(owned) {
            return owned
        }
        if let reclaimed = gone.keys
            .filter({ matchesBase($0, base) && !used.contains($0) })
            .sorted(by: { (slotRank[$0] ?? .max) < (slotRank[$1] ?? .max) })
            .first {
            return reclaimed
        }
        var id = base
        var n = 1
        while used.contains(id) { n += 1; id = "\(base)#\(n)" }
        return id
    }

    private func sortedLive(_ items: [NpuLaneCard], in lane: NpuLane) -> [NpuLaneCard] {
        switch lane {
        case .active:
            return items.sorted { a, b in
                let ha = a.task.host ?? "", hb = b.task.host ?? ""
                if ha != hb { return ha < hb }
                let da = a.task.die ?? Int.max
                let db = b.task.die ?? Int.max
                if da != db { return da < db }
                return (slotRank[a.id] ?? 0) < (slotRank[b.id] ?? 0)
            }
        case .build:
            return items.sorted { (slotRank[$0.id] ?? 0) < (slotRank[$1.id] ?? 0) }
        case .waiting:
            // Preserve backend WSPT / boosted queue order; only stabilize equal items via slotRank.
            return items
        }
    }

    private func scheduleExpiry() {
        expiryTask?.cancel()
        guard let oldest = gone.values.map(\.since).min() else { return }
        let delay = max(0.05, Self.lingerSec - Date().timeIntervalSince(oldest)) + 0.05
        expiryTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            guard !Task.isCancelled, let self else { return }
            self.ingest(self.lastTasks)
        }
    }

    private func matchesBase(_ id: String, _ base: String) -> Bool {
        id == base || id.hasPrefix("\(base)#")
    }

    private static func shortSha(of task: NpuTaskItem) -> String {
        let raw = task.shortSha ?? task.sha ?? ""
        return String(raw.prefix(7))
    }

    private static func pidKey(_ task: NpuTaskItem) -> String {
        "\(task.host ?? "")#\(task.pid)"
    }

    private static func baseKey(_ task: NpuTaskItem) -> String {
        if let runId = task.runId, !runId.isEmpty { return "run-\(runId)" }
        let kind = task.isBackfillTask ? "B0" : "Q"
        guard let problem = task.problem, !problem.isEmpty else { return "pid-\(pidKey(task))|\(kind)" }
        return "\(problem)|\(kind)"
    }
}
