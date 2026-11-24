import Foundation

extension DaemonClient {
    func fetchAgentMesh(workspace: String, host: String) async throws -> AgentMeshHotState? {
        let data = try await requestAuthedData(
            method: "GET", path: "/v1/agents", workspace: workspace, host: host, timeout: 6.0
        )
        let resp = try await Self.decodeOffMain(AgentMeshResponse.self, from: data)
        return resp.agentMesh
    }

    func spawnExternalAgent(
        workspace: String,
        host: String,
        driver: String,
        roleLabel: String,
        problem: String,
        collabMode: String,
        axis: String,
        task: String,
        worktreeMode: String
    ) async throws {
        _ = try await requestAuthedData(
            method: "POST",
            path: "/v1/agents/spawn",
            body: [
                "driver": driver,
                "role_label": roleLabel,
                "problem": problem,
                "collab_mode": collabMode,
                "axis": axis,
                "task": task,
                "worktree_mode": worktreeMode,
            ],
            workspace: workspace,
            host: host,
            timeout: 20.0
        )
    }

    func sendAgentMessage(
        workspace: String,
        host: String,
        agentId: String,
        message: String,
        delivery: String = "queue"
    ) async throws {
        let enc = agentId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? agentId
        _ = try await requestAuthedData(
            method: "POST",
            path: "/v1/agents/\(enc)/send",
            body: ["message": message, "delivery": delivery],
            workspace: workspace,
            host: host,
            timeout: 12.0
        )
    }

    func sendAdminDirective(
        workspace: String,
        host: String,
        body: String,
        problem: String = "",
        broadcastWorkers: Bool = false
    ) async throws {
        _ = try await requestAuthedData(
            method: "POST",
            path: "/v1/agents/directive",
            body: [
                "body": body,
                "problem": problem,
                "broadcast_workers": broadcastWorkers,
            ],
            workspace: workspace,
            host: host,
            timeout: 12.0
        )
    }

    func fetchAgentMessages(
        workspace: String,
        host: String,
        agentId: String?,
        problem: String? = nil,
        sinceSeq: Int = 0,
        ack: Bool = true
    ) async throws -> AgentMessagesResponse {
        let path: String
        if let agentId, !agentId.isEmpty {
            let enc = agentId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? agentId
            path = "/v1/agents/\(enc)/messages"
        } else {
            path = "/v1/agents/messages"
        }
        var items = [
            URLQueryItem(name: "since_seq", value: "\(sinceSeq)"),
            URLQueryItem(name: "limit", value: "80"),
            URLQueryItem(name: "ack", value: ack ? "1" : "0"),
        ]
        if let problem, !problem.isEmpty {
            items.append(URLQueryItem(name: "problem", value: problem))
        }
        let data = try await requestAuthedData(
            method: "GET",
            path: path,
            queryItems: items,
            workspace: workspace,
            host: host,
            timeout: 8.0
        )
        return try await Self.decodeOffMain(AgentMessagesResponse.self, from: data)
    }

    func stopAgent(workspace: String, host: String, agentId: String, cleanupWorktree: Bool = false) async throws {
        let enc = agentId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? agentId
        _ = try await requestAuthedData(
            method: "POST",
            path: "/v1/agents/\(enc)/stop",
            body: ["cleanup_worktree": cleanupWorktree],
            workspace: workspace,
            host: host,
            timeout: 12.0
        )
    }

    func cleanupStoppedAgents(workspace: String, host: String) async throws -> Int {
        let data = try await requestAuthedData(
            method: "POST", path: "/v1/agents/cleanup", body: [:], workspace: workspace, host: host, timeout: 8.0
        )
        let resp = try await Self.decodeOffMain(AgentActionResponse.self, from: data)
        return resp.removed ?? 0
    }
}
