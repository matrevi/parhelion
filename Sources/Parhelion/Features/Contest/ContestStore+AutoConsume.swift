import Foundation

extension ContestStore {
    func jumpSubmitNow(
        problem: String,
        sha: String,
        note: String,
        force: Bool = false
    ) async {
        if isLaunchingSubmit { return }
        isLaunchingSubmit = true
        defer { isLaunchingSubmit = false }

        let key = SubmissionKey.make(problem, sha)
        do {
            let res = try await repository.jumpSubmitNow(
                workspace: selectedWorkspace,
                problem: problem,
                sha: sha,
                note: note,
                force: force
            )
            if res.ok {
                markLaunching(problem: problem, sha: sha)
                bannerMessage = tr(
                    "已启动提交 \(problem) (\(String(sha.prefix(7))))",
                    "Launched submit for \(problem) (\(String(sha.prefix(7))))"
                )
                await daemonClient.invalidate(workspace: selectedWorkspace)
                await refreshLocal(force: true)
                await refreshNpu(silent: true)
            } else {
                clearLaunching(key)
                errorMessage = res.message
            }
        } catch {
            clearLaunching(key)
            errorMessage = error.localizedDescription
        }
    }
}
