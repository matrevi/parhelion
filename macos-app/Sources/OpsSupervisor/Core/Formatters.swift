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
}

enum SystemNavigator {
    static func openURL(_ urlString: String) {
        guard let url = URL(string: urlString) else { return }
        NSWorkspace.shared.open(url)
    }

    static func revealInFinder(_ path: String) {
        NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: path)
    }
}
