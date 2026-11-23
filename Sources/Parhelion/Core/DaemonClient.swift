import Foundation

enum DaemonClientError: LocalizedError {
    case invalidStateFile
    case unauthorized
    case badHttpStatus(Int)
    case daemonUnavailable(String)

    var errorDescription: String? {
        switch self {
        case .invalidStateFile:
            return "Missing or invalid ~/.parhelion/daemon.json"
        case .unauthorized:
            return "Daemon rejected X-Opsb-Token (401)"
        case .badHttpStatus(let code):
            return "Daemon returned HTTP \(code)"
        case .daemonUnavailable(let detail):
            return detail.isEmpty ? "Parhelion daemon unavailable" : detail
        }
    }
}

struct DaemonAuthMetadata: Codable, Sendable {
    let pid: Int
    let port: Int
    let token: String
    let workspace: String?
    let sshHost: String?

    enum CodingKeys: String, CodingKey {
        case pid, port, token, workspace
        case sshHost = "ssh_host"
    }
}

enum SnapshotFetchResult {
    case notModified(etag: String)
    case updated(snapshot: SupervisorSnapshot, etag: String)
}

/// Thread-safe localhost HTTP/SSE client for `opsb daemon` with auto-start and off-MainActor JSON decoding.
actor DaemonClient {
    private let processClient = ProcessClient()
    private var cachedMeta: DaemonAuthMetadata?

    private let httpSession: URLSession = {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.requestCachePolicy = .reloadIgnoringLocalCacheData
        cfg.urlCache = nil
        cfg.timeoutIntervalForRequest = 5.0
        cfg.timeoutIntervalForResource = 15.0
        return URLSession(configuration: cfg)
    }()

    private let sseSession: URLSession = {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.requestCachePolicy = .reloadIgnoringLocalCacheData
        cfg.urlCache = nil
        cfg.timeoutIntervalForRequest = 30.0
        cfg.timeoutIntervalForResource = 0
        return URLSession(configuration: cfg)
    }()

    private var stateFileCandidates: [URL] {
        let home = FileManager.default.homeDirectoryForCurrentUser
        var list: [URL] = []
        if let override = ProcessInfo.processInfo.environment["PARHELION_HOME"]?.trimmingCharacters(in: .whitespacesAndNewlines),
           !override.isEmpty {
            list.append(URL(fileURLWithPath: override).appendingPathComponent("daemon.json"))
        }
        list.append(home.appendingPathComponent(".parhelion/daemon.json"))
        list.append(home.appendingPathComponent(".config/parhelion/daemon.json"))
        return list
    }

    private func loadMetadataFromDisk() -> DaemonAuthMetadata? {
        for url in stateFileCandidates {
            if let data = try? Data(contentsOf: url),
               let meta = try? JSONDecoder().decode(DaemonAuthMetadata.self, from: data),
               meta.port > 0, !meta.token.isEmpty {
                return meta
            }
        }
        return nil
    }

    private func probeHealth(_ meta: DaemonAuthMetadata) async -> DaemonHealthResponse? {
        guard let url = URL(string: "http://127.0.0.1:\(meta.port)/v1/health") else { return nil }
        var req = URLRequest(url: url)
        req.httpMethod = "GET"
        req.setValue(meta.token, forHTTPHeaderField: "X-Opsb-Token")
        req.timeoutInterval = 1.5
        do {
            let (data, resp) = try await httpSession.data(for: req)
            guard let http = resp as? HTTPURLResponse, http.statusCode == 200 else { return nil }
            let health = try await Self.decodeOffMain(DaemonHealthResponse.self, from: data)
            return health.ok ? health : nil
        } catch {
            return nil
        }
    }

    func ensureConnected(workspace: String, host: String) async throws -> DaemonAuthMetadata {
        if let meta = cachedMeta, await probeHealth(meta) != nil {
            return meta
        }
        if let diskMeta = loadMetadataFromDisk(), await probeHealth(diskMeta) != nil {
            cachedMeta = diskMeta
            return diskMeta
        }

        // Auto-spawn `opsb daemon start --background` via ProcessClient (fd 0 is /dev/null).
        let args = [
            "daemon", "start",
            "--background",
            "--workspace", workspace,
            "--host", host,
            "--json"
        ]
        _ = try? await processClient.execute(args, timeout: 10.0, allowNonZeroExit: true)

        if let freshMeta = loadMetadataFromDisk(), await probeHealth(freshMeta) != nil {
            cachedMeta = freshMeta
            return freshMeta
        }
        cachedMeta = nil
        throw DaemonClientError.daemonUnavailable("Failed to connect or start opsb daemon")
    }

    func fetchHot(workspace: String, host: String, interval: Double) async throws -> HotStreamFrame {
        let meta = try await ensureConnected(workspace: workspace, host: host)
        var comps = URLComponents(string: "http://127.0.0.1:\(meta.port)/v1/hot")!
        comps.queryItems = [
            URLQueryItem(name: "workspace", value: workspace),
            URLQueryItem(name: "host", value: host),
            URLQueryItem(name: "interval", value: String(format: "%.2f", max(0.5, interval)))
        ]
        var req = URLRequest(url: comps.url!)
        req.setValue(meta.token, forHTTPHeaderField: "X-Opsb-Token")
        let (data, resp) = try await httpSession.data(for: req)
        guard let http = resp as? HTTPURLResponse else {
            throw DaemonClientError.daemonUnavailable("Non-HTTP response")
        }
        if http.statusCode == 401 {
            cachedMeta = nil
            throw DaemonClientError.unauthorized
        }
        guard http.statusCode == 200 else {
            throw DaemonClientError.badHttpStatus(http.statusCode)
        }
        return try await Self.decodeOffMain(HotStreamFrame.self, from: data)
    }

    func fetchSnapshot(workspace: String, host: String, ifNoneMatch: String?) async throws -> SnapshotFetchResult {
        let meta = try await ensureConnected(workspace: workspace, host: host)
        var comps = URLComponents(string: "http://127.0.0.1:\(meta.port)/v1/snapshot")!
        comps.queryItems = [URLQueryItem(name: "workspace", value: workspace)]
        var req = URLRequest(url: comps.url!)
        req.setValue(meta.token, forHTTPHeaderField: "X-Opsb-Token")
        if let etag = ifNoneMatch, !etag.isEmpty {
            req.setValue("\"\(etag)\"", forHTTPHeaderField: "If-None-Match")
        }
        req.timeoutInterval = 8.0

        let (data, resp) = try await httpSession.data(for: req)
        guard let http = resp as? HTTPURLResponse else {
            throw DaemonClientError.daemonUnavailable("Non-HTTP snapshot response")
        }
        if http.statusCode == 401 {
            cachedMeta = nil
            throw DaemonClientError.unauthorized
        }
        let respEtag = Self.cleanETag(http.value(forHTTPHeaderField: "ETag"))
        if http.statusCode == 304 {
            return .notModified(etag: respEtag ?? ifNoneMatch ?? "")
        }
        guard http.statusCode == 200 else {
            throw DaemonClientError.badHttpStatus(http.statusCode)
        }
        let snap = try await Self.decodeOffMain(SupervisorSnapshot.self, from: data)
        return .updated(snapshot: snap, etag: respEtag ?? "")
    }

    func invalidate(workspace: String) async {
        guard let meta = cachedMeta ?? loadMetadataFromDisk(),
              let url = URL(string: "http://127.0.0.1:\(meta.port)/v1/invalidate") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue(meta.token, forHTTPHeaderField: "X-Opsb-Token")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["workspace": workspace])
        req.timeoutInterval = 3.0
        _ = try? await httpSession.data(for: req)
    }

    func configure(host: String?, interval: Double?) async {
        guard let meta = cachedMeta ?? loadMetadataFromDisk(),
              let url = URL(string: "http://127.0.0.1:\(meta.port)/v1/configure") else { return }
        var body: [String: Any] = [:]
        if let host, !host.isEmpty { body["host"] = host }
        if let interval, interval > 0 { body["interval"] = interval }
        guard !body.isEmpty else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue(meta.token, forHTTPHeaderField: "X-Opsb-Token")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        req.timeoutInterval = 2.5
        _ = try? await httpSession.data(for: req)
    }

    func openHotStream(
        workspace: String,
        host: String,
        interval: Double
    ) -> AsyncThrowingStream<(HotStreamFrame, Double), Error> {
        let session = self.sseSession
        // Keep only the newest frame: if the consumer is momentarily busy, replaying a backlog of
        // stale frames would make cards/lanes jump through outdated states in quick succession.
        return AsyncThrowingStream(bufferingPolicy: .bufferingNewest(1)) { continuation in
            let runner = Task.detached(priority: .utility) { [weak self] in
                guard let self else {
                    continuation.finish()
                    return
                }
                do {
                    let meta = try await self.ensureConnected(workspace: workspace, host: host)
                    var comps = URLComponents(string: "http://127.0.0.1:\(meta.port)/v1/stream/hot")!
                    comps.queryItems = [
                        URLQueryItem(name: "workspace", value: workspace),
                        URLQueryItem(name: "host", value: host),
                        URLQueryItem(name: "interval", value: String(format: "%.2f", max(0.5, interval)))
                    ]
                    var req = URLRequest(url: comps.url!)
                    req.setValue(meta.token, forHTTPHeaderField: "X-Opsb-Token")
                    req.setValue("text/event-stream", forHTTPHeaderField: "Accept")

                    let (bytes, resp) = try await session.bytes(for: req)
                    guard let http = resp as? HTTPURLResponse else {
                        throw DaemonClientError.daemonUnavailable("Non-HTTP SSE response")
                    }
                    if http.statusCode == 401 {
                        await self.clearCachedMeta()
                        throw DaemonClientError.unauthorized
                    }
                    guard http.statusCode == 200 else {
                        throw DaemonClientError.badHttpStatus(http.statusCode)
                    }

                    let decoder = JSONDecoder()
                    for try await line in bytes.lines {
                        if Task.isCancelled { break }
                        guard line.hasPrefix("data:") else { continue }
                        let rawPayload = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
                        guard !rawPayload.isEmpty, let jsonData = rawPayload.data(using: .utf8) else { continue }
                        let t0 = CFAbsoluteTimeGetCurrent()
                        let frame = try decoder.decode(HotStreamFrame.self, from: jsonData)
                        let decodeMs = (CFAbsoluteTimeGetCurrent() - t0) * 1000.0
                        continuation.yield((frame, decodeMs))
                    }
                    continuation.finish()
                } catch {
                    if !Task.isCancelled {
                        continuation.finish(throwing: error)
                    } else {
                        continuation.finish()
                    }
                }
            }
            continuation.onTermination = { _ in
                runner.cancel()
            }
        }
    }

    private func clearCachedMeta() {
        cachedMeta = nil
    }

    private static func cleanETag(_ raw: String?) -> String? {
        guard var s = raw?.trimmingCharacters(in: .whitespacesAndNewlines), !s.isEmpty else { return nil }
        if s.hasPrefix("W/") { s = String(s.dropFirst(2)).trimmingCharacters(in: .whitespaces) }
        s = s.trimmingCharacters(in: CharacterSet(charactersIn: "\""))
        return s.isEmpty ? nil : s
    }

    nonisolated static func decodeOffMain<T: Decodable>(_ type: T.Type, from data: Data) async throws -> T {
        try await Task.detached(priority: .utility) {
            try JSONDecoder().decode(T.self, from: data)
        }.value
    }
}
