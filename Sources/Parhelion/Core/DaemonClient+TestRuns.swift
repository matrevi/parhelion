import Foundation

extension DaemonClient {
    /// One registry row with its digest, fetched only when the user expands it.
    func fetchTestRun(workspace: String, host: String, requestId: String) async throws -> TestRunDetail? {
        let enc = requestId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? requestId
        let data = try await requestAuthedData(
            method: "GET", path: "/v1/test-runs/\(enc)", workspace: workspace, host: host, timeout: 8.0
        )
        return try await Self.decodeOffMain(TestRunDetailResponse.self, from: data).run
    }

    /// The administrator's explicit release of a provider-fault driver suspension.
    func resumeDriver(workspace: String, host: String, driver: String) async throws {
        let enc = driver.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? driver
        _ = try await requestAuthedData(
            method: "POST", path: "/v1/drivers/\(enc)/resume", body: [:],
            workspace: workspace, host: host, timeout: 8.0
        )
    }
}
