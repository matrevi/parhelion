import AppKit
import Foundation

enum Formatters {
    static let shanghaiTimeZone: TimeZone = TimeZone(identifier: "Asia/Shanghai") ?? TimeZone(secondsFromGMT: 8 * 3600)!

    private static let isoFracParser: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    private static let isoPlainParser: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    private static let shanghaiShortFormatter: DateFormatter = {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = shanghaiTimeZone
        f.dateFormat = "MM-dd HH:mm"
        return f
    }()

    static func duration(_ sec: Int) -> String {
        if sec <= 0 { return "0s" }
        if sec < 60 { return "\(sec)s" }
        let m = sec / 60
        let s = sec % 60
        if m < 60 { return s > 0 ? "\(m)m \(s)s" : "\(m)m" }
        return "\(m / 60)h \(m % 60)m"
    }

    static func parseISO(_ iso: String?) -> Date? {
        guard let raw = iso?.trimmingCharacters(in: .whitespacesAndNewlines), !raw.isEmpty else { return nil }
        if let d = isoFracParser.date(from: raw) { return d }
        if let d = isoPlainParser.date(from: raw) { return d }
        // Handle microsecond fractional precision (>3 digits) by normalizing to milliseconds or trimming
        if let dotIdx = raw.firstIndex(of: ".") {
            let tail = raw[raw.index(after: dotIdx)...]
            if let tzIdx = tail.firstIndex(where: { $0 == "Z" || $0 == "+" || $0 == "-" }) {
                let frac = String(tail[..<tzIdx].prefix(3)).padding(toLength: 3, withPad: "0", startingAt: 0)
                let norm = "\(raw[..<dotIdx]).\(frac)\(tail[tzIdx...])"
                if let d = isoFracParser.date(from: norm) { return d }
            }
        }
        return nil
    }

    static func shortTimestamp(_ iso: String?) -> String {
        guard let iso, !iso.isEmpty else { return "—" }
        if let date = parseISO(iso) {
            return shanghaiShortFormatter.string(from: date)
        }
        let cleaned = iso.replacingOccurrences(of: "T", with: " ")
        if cleaned.count >= 16 {
            let start = cleaned.index(cleaned.startIndex, offsetBy: 5)
            let end = cleaned.index(cleaned.startIndex, offsetBy: 16)
            return String(cleaned[start..<end])
        }
        return cleaned
    }

    static func shortTimestamp(epoch: Double?) -> String {
        guard let epoch, epoch > 0 else { return "—" }
        return shanghaiShortFormatter.string(from: Date(timeIntervalSince1970: epoch))
    }
}

enum SystemNavigator {
    static func openURL(_ urlString: String) {
        guard let url = URL(string: urlString) else { return }
        NSWorkspace.shared.open(url)
    }

    static func revealInFinder(_ path: String) {
        NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: path)
    }

    static func openTerminalCommand(_ shellCommand: String) -> Bool {
        let trimmed = shellCommand.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return false }
        let scriptPath = NSTemporaryDirectory() + "parhelion-jump-\(UUID().uuidString.prefix(8)).command"
        let script = "#!/bin/zsh\nrm -f \"$0\"\n\(trimmed)\n"
        do {
            try script.write(toFile: scriptPath, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: scriptPath)
            return NSWorkspace.shared.open(URL(fileURLWithPath: scriptPath))
        } catch {
            return false
        }
    }

    /// Jumps directly into a Harness conversation context (Codex direct thread, Desktop deep-link + clipboard, or Terminal resume).
    @MainActor
    static func jumpToHarnessContext(
        driver: String,
        deepLink: String,
        resumeCommand: String = "",
        jumpMode: String = "deeplink",
        guiAppPath: String = "",
        clipboardHint: String = "",
        lang: AppLanguage = .zh
    ) -> String? {
        let drv = driver.lowercased()
        let dl = deepLink.trimmingCharacters(in: .whitespacesAndNewlines)
        let cmd = resumeCommand.trimmingCharacters(in: .whitespacesAndNewlines)

        if jumpMode == "terminal" && !cmd.isEmpty {
            if openTerminalCommand(cmd) {
                return lang.tr(
                    "已在终端打开 \(driver) 对话上下文",
                    "Opened \(driver) session context in Terminal"
                )
            }
        }

        let isDirectThread = dl.lowercased().hasPrefix("codex://threads/")
            && !dl.lowercased().hasPrefix("codex://threads/new")
        let hint = clipboardHint.trimmingCharacters(in: .whitespacesAndNewlines)
        if !isDirectThread && !hint.isEmpty {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(hint, forType: .string)
        }

        if let url = URL(string: dl), !dl.isEmpty, NSWorkspace.shared.open(url) {
            if isDirectThread { return nil }
            let tip = drv == "deepseek" ? "⌘K 粘贴" : "搜索粘贴"
            return hint.isEmpty
                ? lang.tr("已唤起 \(driver) 上下文窗口", "Focused \(driver) window")
                : lang.tr(
                    "已唤起 \(driver)（已复制「\(hint)」方便\(tip)直达对话）",
                    "Focused \(driver) (copied \"\(hint)\" to locate thread)"
                )
        }

        if !guiAppPath.isEmpty && FileManager.default.fileExists(atPath: guiAppPath) {
            NSWorkspace.shared.openApplication(
                at: URL(fileURLWithPath: guiAppPath),
                configuration: NSWorkspace.OpenConfiguration()
            )
            return lang.tr("已唤起 \(driver) 应用", "Focused \(driver) app")
        }

        if !cmd.isEmpty && openTerminalCommand(cmd) {
            return lang.tr("已在终端打开 \(driver) 对话上下文", "Opened \(driver) session context in Terminal")
        }

        return lang.tr("未找到 \(driver) 可用的对话跳转入口", "No jump target available for \(driver)")
    }
}
