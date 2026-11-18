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

/// Deadlock-free async subprocess runner with concurrent stdout/stderr pipe draining.
struct ProcessClient: Sendable {
    let executablePath: String

    init() {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let candidates = [
            "/Users/winbeau/Competitions/ops-bench/ops-supervisor/cli/opsb",
            "\(home)/.local/bin/opsb",
            Bundle.main.resourceURL?.appendingPathComponent("cli/opsb").path
        ].compactMap { $0 }

        self.executablePath = candidates.first(where: { FileManager.default.isExecutableFile(atPath: $0) })
            ?? "\(home)/.local/bin/opsb"
    }

    func execute(
        _ args: [String],
        stdinString: String? = nil,
        timeout: TimeInterval = 12.0,
        allowNonZeroExit: Bool = false
    ) async throws -> Data {
        let exe = executablePath
        return try await withCheckedThrowingContinuation { continuation in
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            proc.arguments = ["python3", exe] + args

            var env = ProcessInfo.processInfo.environment
            let extra = "/opt/homebrew/bin:/usr/local/bin:\(NSHomeDirectory())/.local/bin:\(NSHomeDirectory())/.local/share/pnpm/bin"
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
