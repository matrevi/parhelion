import Foundation

/// User-initiated queue / gate / NPU mutations on `ContestStore`.
/// Every mutation invalidates the daemon cold cache and forces a coalesced cold refresh.
extension ContestStore {
    func boostNpuTicket(pid: Int, problem: String, sha: String) async {
        do {
            try await repository.boostNpuTicket(host: activeSSHHost, pid: pid, problem: problem, sha: sha)
            bannerMessage = tr("已将 \(problem) 置顶为下一顺位获卡", "Boosted \(problem) to #1 NPU ticket")
            await refreshNpu(silent: true)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func cancelNpuTicket(pid: Int, problem: String) async {
        do {
            try await repository.cancelNpuTask(host: activeSSHHost, pid: pid)
            bannerMessage = tr("已取消远程任务 \(problem) (PID \(pid))", "Cancelled remote task \(problem) (PID \(pid))")
            await refreshNpu(silent: true)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    // MARK: - Queue & Gate Mutations

    func updateScheduler(
        autoConsumer: Bool? = nil,
        closingReserve: Int? = nil,
        noiseFloor: Double? = nil,
        breakthroughThreshold: Double? = nil
    ) async {
        if let bt = breakthroughThreshold {
            self.breakthroughThreshold = bt
            // Don't let an in-flight (pre-mutation) cold snapshot bounce the stepper back.
            breakthroughThresholdHoldUntil = Date().addingTimeInterval(5.0)
        }
        if let autoConsumer {
            // Optimistic: keep the toggle (and the auto-consume guard) on the new value until a
            // hot frame confirms it, instead of bouncing back to the stale hot/cold value for ~1s.
            setAutoConsumerOverride(autoConsumer)
        }
        do {
            try await repository.updateQueueConfig(
                autoConsumer: autoConsumer,
                closingReserve: closingReserve,
                noiseFloor: noiseFloor,
                breakthroughThreshold: breakthroughThreshold
            )
            await daemonClient.invalidate(workspace: selectedWorkspace)
            await refreshLocal(force: true)
        } catch {
            if autoConsumer != nil { clearAutoConsumerOverride() }
            errorMessage = error.localizedDescription
        }
    }

    func syncBreakthroughs() async {
        do {
            let (created, total) = try await repository.syncBreakthroughPlaybook(
                workspace: selectedWorkspace,
                minDelta: breakthroughThreshold
            )
            await reloadBreakthroughs()
            await daemonClient.invalidate(workspace: selectedWorkspace)
            await refreshLocal(force: true)
            if created > 0 {
                bannerMessage = tr("已同步 \(created) 条新突破至经验库 (共 \(total) 条)", "Synced \(created) new breakthrough(s) (\(total) total)")
            } else {
                bannerMessage = tr("经验库已是最新 (共 \(total) 条结构化突破)", "Playbook up to date (\(total) structured entries)")
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func approveCandidate(problem: String, sha: String, note: String) async {
        do {
            try await repository.approveSubmission(workspace: selectedWorkspace, problem: problem, sha: sha, note: note)
            bannerMessage = tr("已批准入队 \(problem) \(String(sha.prefix(7)))", "Approved & enqueued \(problem) \(String(sha.prefix(7)))")
            await daemonClient.invalidate(workspace: selectedWorkspace)
            await refreshLocal(force: true)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func skipCandidate(problem: String, sha: String) async {
        do {
            try await repository.skipSubmission(workspace: selectedWorkspace, problem: problem, sha: sha, reason: "held via Parhelion")
            bannerMessage = tr("已忽略 \(problem) \(String(sha.prefix(7)))", "Skipped \(problem) \(String(sha.prefix(7)))")
            await daemonClient.invalidate(workspace: selectedWorkspace)
            await refreshLocal(force: true)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func restoreBest(problem: String) async {
        do {
            try await repository.restoreBestSubmission(workspace: selectedWorkspace, problem: problem)
            bannerMessage = tr("已将 \(problem) 历史最优版本加入恢复队列", "Queued historical best SHA restore for \(problem)")
            await daemonClient.invalidate(workspace: selectedWorkspace)
            await refreshLocal(force: true)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func recalibrateProblem(_ problem: String) async {
        do {
            try await repository.calibrateProblemBench(workspace: selectedWorkspace, problem: problem)
            bannerMessage = tr("已完成 \(problem) 5层权重闭环校准", "Recalibrated 5-stratum weights for \(problem)")
            await daemonClient.invalidate(workspace: selectedWorkspace)
            await refreshLocal(force: true)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func backfillProblem(_ problem: String) async {
        if isBackfilling { return }
        isBackfilling = true
        bannerMessage = tr("正在补测 \(problem) 缺失性能桶…", "Backfilling missing perf buckets for \(problem)…")
        defer { isBackfilling = false }
        do {
            let res = try await repository.backfillProblemBench(workspace: selectedWorkspace, problem: problem)
            if res.measuredCount > 0 {
                bannerMessage = tr(
                    "已完成 \(problem) 性能桶补测（新增配对 \(res.measuredCount) 桶）",
                    "Backfilled \(res.measuredCount) bucket(s) for \(problem)"
                )
            } else if res.result == "die_busy" {
                bannerMessage = tr(
                    "\(problem) 补测因 Die 0 正忙被让行（die_busy），空卡后将自动补测",
                    "\(problem) backfill deferred (die_busy); B0 will retry when Die 0 is free"
                )
            } else if !res.dispatched {
                bannerMessage = tr(
                    "\(problem) 当前无可补测的实测性能桶（\(res.result)）",
                    "\(problem) has no measurable unpaired buckets (\(res.result))"
                )
            } else {
                bannerMessage = tr(
                    "\(problem) 补测结束：\(res.result)（新增 0 桶）",
                    "\(problem) backfill finished: \(res.result) (0 new buckets)"
                )
            }
            await daemonClient.invalidate(workspace: selectedWorkspace)
            await refreshLocal(force: true)
            await reloadBackfillStatus(force: true)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func drainQueueOnce() async {
        bannerMessage = "Running rate-limited queue consumer…"
        do {
            bannerMessage = try await repository.drainQueueOnce(workspace: selectedWorkspace)
            await daemonClient.invalidate(workspace: selectedWorkspace)
            await refreshLocal(force: true)
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}
