import Foundation

enum ProcessClientError: LocalizedError {
    case timedOut(Int)
    case nonZeroExit(Int32, String)

    var errorDescription: String? {
        switch self {
        case .timedOut(let sec):
            return "Command timed out after \(sec)s"
        case .nonZeroExit(let code, let detail):
            return detail.isEmpty ? "Process exited with status \(code)" : detail
        }
    }
}

private final class PipeCaptureBuffer: @unchecked Sendable {
    private let lock = NSLock()
    private var outData = Data()
    private var errData = Data()
    private var timedOut = false

    func setOut(_ data: Data) { lock.lock(); outData = data; lock.unlock() }
    func setErr(_ data: Data) { lock.lock(); errData = data; lock.unlock() }
    func markTimedOut() { lock.lock(); timedOut = true; lock.unlock() }
    func snapshot() -> (Data, Data, Bool) {
        lock.lock()
        defer { lock.unlock() }
        return (outData, errData, timedOut)
    }
}

/// Portable deadlock-free async subprocess runner with concurrent stdout/stderr pipe draining.
struct ProcessClient: Sendable {
    let executablePath: String

    init() {
        let fm = FileManager.default
        let home = fm.homeDirectoryForCurrentUser.path
        let envBin = ProcessInfo.processInfo.environment["OPSB_BIN"]?.trimmingCharacters(in: .whitespacesAndNewlines)
        let pathBins = (ProcessInfo.processInfo.environment["PATH"] ?? "")
            .split(separator: ":")
            .map { "\($0)/opsb" }

        var candidates: [String] = []
        if let envBin, !envBin.isEmpty {
            candidates.append(envBin)
        }
        candidates.append(contentsOf: [
            "\(home)/.local/bin/opsb",
            "/opt/homebrew/bin/opsb",
            "/usr/local/bin/opsb",
        ])
        if let bundled = Bundle.main.resourceURL?.appendingPathComponent("cli/opsb").path {
            candidates.append(bundled)
        }
        candidates.append(contentsOf: pathBins)

        self.executablePath = candidates.first(where: { fm.isExecutableFile(atPath: $0) })
            ?? Bundle.main.resourceURL?.appendingPathComponent("cli/opsb").path
            ?? "\(home)/.local/bin/opsb"
    }

    private static func extractWorkspaceArg(from args: [String]) -> String? {
        if let idx = args.firstIndex(of: "--workspace"), idx + 1 < args.count {
            let v = args[idx + 1].trimmingCharacters(in: .whitespacesAndNewlines)
            return v.isEmpty ? nil : v
        }
        for arg in args where arg.hasPrefix("--workspace=") {
            let v = String(arg.dropFirst("--workspace=".count)).trimmingCharacters(in: .whitespacesAndNewlines)
            if !v.isEmpty { return v }
        }
        return nil
    }

    private static func resolvePythonExecutable(workspace: String?) -> String? {
        let fm = FileManager.default
        let home = fm.homeDirectoryForCurrentUser.path
        var candidates: [String] = []
        if let envPy = ProcessInfo.processInfo.environment["OPSB_PYTHON"]?.trimmingCharacters(in: .whitespacesAndNewlines),
           !envPy.isEmpty {
            candidates.append(envPy)
        }
        if let ws = workspace, !ws.isEmpty {
            candidates.append("\(ws)/.venv/bin/python")
            candidates.append("\(ws)/.venv/bin/python3")
        }
        candidates.append(contentsOf: [
            "\(home)/.parhelion/venv/bin/python",
            "\(home)/.parhelion/venv/bin/python3",
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
        ])
        return candidates.first(where: { fm.isExecutableFile(atPath: $0) })
    }

    func execute(
        _ args: [String],
        stdinString: String? = nil,
        timeout: TimeInterval = 12.0,
        allowNonZeroExit: Bool = false
    ) async throws -> Data {
        let exe = executablePath
        let wsArg = Self.extractWorkspaceArg(from: args)
        let pythonPath = Self.resolvePythonExecutable(workspace: wsArg)
        return try await withCheckedThrowingContinuation { continuation in
            let proc = Process()
            if let pythonPath {
                proc.executableURL = URL(fileURLWithPath: pythonPath)
                proc.arguments = [exe] + args
            } else {
                proc.executableURL = URL(fileURLWithPath: "/usr/bin/env")
                proc.arguments = ["python3", exe] + args
            }

            let home = FileManager.default.homeDirectoryForCurrentUser.path
            var env = ProcessInfo.processInfo.environment
            var pathComponents = [
                "\(home)/.parhelion/venv/bin",
                "/opt/homebrew/bin",
                "/usr/local/bin",
                "\(home)/.local/bin",
                "\(home)/.local/share/pnpm/bin",
            ]
            if let wsArg, !wsArg.isEmpty {
                pathComponents.insert("\(wsArg)/.venv/bin", at: 0)
            }
            let extra = pathComponents.joined(separator: ":")
            env["PATH"] = "\(extra):\(env["PATH"] ?? "/usr/bin:/bin")"
            proc.environment = env

            let outPipe = Pipe()
            let errPipe = Pipe()
            proc.standardOutput = outPipe
            proc.standardError = errPipe

            var inPipe: Pipe?
            if stdinString != nil {
                let p = Pipe()
                proc.standardInput = p
                inPipe = p
            } else {
                proc.standardInput = FileHandle.nullDevice
            }

            let ioGroup = DispatchGroup()
            let buffer = PipeCaptureBuffer()

            let watchdog = DispatchWorkItem {
                buffer.markTimedOut()
                if proc.isRunning { proc.terminate() }
            }

            proc.terminationHandler = { finishedProc in
                watchdog.cancel()
                ioGroup.notify(queue: DispatchQueue.global(qos: .userInitiated)) {
                    let (capturedOut, capturedErr, timedOut) = buffer.snapshot()

                    if timedOut {
                        continuation.resume(throwing: ProcessClientError.timedOut(Int(timeout)))
                        return
                    }
                    if finishedProc.terminationStatus != 0 && !allowNonZeroExit {
                        let msg = String(data: capturedErr, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
                            ?? String(data: capturedOut, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
                            ?? ""
                        continuation.resume(throwing: ProcessClientError.nonZeroExit(finishedProc.terminationStatus, msg))
                        return
                    }
                    continuation.resume(returning: capturedOut)
                }
            }

            do {
                try proc.run()
                DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: watchdog)

                if let input = stdinString, let p = inPipe {
                    DispatchQueue.global(qos: .userInitiated).async {
                        if let bytes = input.data(using: .utf8) {
                            p.fileHandleForWriting.write(bytes)
                        }
                        try? p.fileHandleForWriting.close()
                    }
                }

                // Drain stdout & stderr concurrently while child runs to prevent 64KB pipe deadlock
                ioGroup.enter()
                DispatchQueue.global(qos: .userInitiated).async {
                    buffer.setOut(outPipe.fileHandleForReading.readDataToEndOfFile())
                    ioGroup.leave()
                }

                ioGroup.enter()
                DispatchQueue.global(qos: .utility).async {
                    buffer.setErr(errPipe.fileHandleForReading.readDataToEndOfFile())
                    ioGroup.leave()
                }
            } catch {
                watchdog.cancel()
                continuation.resume(throwing: error)
            }
        }
    }
}
