import AppKit
import Foundation

enum Formatters {
    static func duration(_ sec: Int) -> String {
        if sec <= 0 { return "0s" }
        if sec < 60 { return "\(sec)s" }
        let m = sec / 60
        let s = sec % 60
        if m < 60 { return s > 0 ? "\(m)m \(s)s" : "\(m)m" }
        return "\(m / 60)h \(m % 60)m"
    }

    static func shortTimestamp(_ iso: String?) -> String {
        guard let iso, !iso.isEmpty else { return "—" }
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
