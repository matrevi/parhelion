import AppKit
import SwiftUI

struct AgentDriverStyle {
    let code: String
    let displayName: String
    let accent: Color
    let secondaryAccent: Color
    let defaultAppPaths: [String]

    static func forDriver(_ driver: String) -> AgentDriverStyle {
        switch driver.lowercased() {
        case "antigravity":
            return AgentDriverStyle(
                code: "AGY",
                displayName: "Antigravity",
                accent: Color(red: 0.259, green: 0.522, blue: 0.957),        // Google AI Blue #4285F4
                secondaryAccent: Color(red: 0.204, green: 0.659, blue: 0.325), // Google Green #34A853
                defaultAppPaths: [
                    "/Applications/Antigravity.app",
                    NSHomeDirectory() + "/Applications/Antigravity.app",
                ]
            )
        case "codex":
            return AgentDriverStyle(
                code: "CDX",
                displayName: "Codex",
                accent: Color(red: 0.063, green: 0.639, blue: 0.498),        // OpenAI Emerald #10A37F
                secondaryAccent: Color(red: 0.45, green: 0.45, blue: 0.48),  // OpenAI Neutral Slate
                defaultAppPaths: [
                    "/Applications/Codex.app",
                    "/Applications/ChatGPT.app",
                    NSHomeDirectory() + "/Applications/Codex.app",
                    NSHomeDirectory() + "/Applications/ChatGPT.app",
                ]
            )
        case "claude":
            return AgentDriverStyle(
                code: "CLD",
                displayName: "Claude Code",
                accent: Color(red: 0.851, green: 0.467, blue: 0.341),        // Anthropic Terracotta #D97757
                secondaryAccent: Color(red: 0.761, green: 0.380, blue: 0.255),
                defaultAppPaths: [
                    "/Applications/Claude.app",
                    NSHomeDirectory() + "/Applications/Claude.app",
                ]
            )
        case "deepseek":
            return AgentDriverStyle(
                code: "DSH",
                displayName: "DeepSeek Harness",
                accent: Color(red: 0.302, green: 0.420, blue: 0.996),        // DeepSeek Whale Blue #4D6BFE
                secondaryAccent: Color(red: 0.204, green: 0.325, blue: 0.922),
                defaultAppPaths: [
                    "/Applications/DeepSeek Harness.app",
                    NSHomeDirectory() + "/Applications/DeepSeek Harness.app",
                ]
            )
        case "gemini":
            return AgentDriverStyle(
                code: "GEM",
                displayName: "Gemini CLI",
                accent: Color(red: 0.278, green: 0.588, blue: 0.890),        // Gemini Spark Blue #4796E3
                secondaryAccent: Color(red: 0.569, green: 0.467, blue: 0.780), // Gemini Violet #9177C7
                defaultAppPaths: []
            )
        case "pi":
            return AgentDriverStyle(
                code: "PI",
                displayName: "Pi",
                accent: Color(red: 0.392, green: 0.455, blue: 0.545),        // Slate #64748B
                secondaryAccent: Color(red: 0.580, green: 0.639, blue: 0.722),
                defaultAppPaths: []
            )
        default:
            return AgentDriverStyle(
                code: String(driver.prefix(3)).uppercased(),
                displayName: driver,
                accent: .secondary,
                secondaryAccent: .secondary,
                defaultAppPaths: []
            )
        }
    }

    var gradient: LinearGradient {
        LinearGradient(colors: [accent, secondaryAccent], startPoint: .topLeading, endPoint: .bottomTrailing)
    }
}

/// Loads and caches genuine macOS official `.app` icons via `NSWorkspace.shared.icon(forFile:)`.
@MainActor
final class HarnessBrandIconCache {
    static let shared = HarnessBrandIconCache()
    private var cache: [String: NSImage] = [:]

    func officialIcon(for driver: String, appPath: String? = nil) -> NSImage? {
        let key = "\(driver.lowercased())::\(appPath ?? "")"
        if let hit = cache[key] { return hit }

        let style = AgentDriverStyle.forDriver(driver)
        var candidates: [String] = []
        if let appPath, !appPath.isEmpty { candidates.append(appPath) }
        candidates.append(contentsOf: style.defaultAppPaths)

        let fm = FileManager.default
        for path in candidates where fm.fileExists(atPath: path) {
            let icon = NSWorkspace.shared.icon(forFile: path)
            icon.size = NSSize(width: 64, height: 64)
            cache[key] = icon
            return icon
        }
        return nil
    }
}

/// Renders the genuine official macOS Harness app icon; falls back cleanly to official brand monogram for CLI-only drivers.
struct HarnessLogoView: View {
    let driver: String
    var appPath: String = ""
    var size: CGFloat = 22

    var body: some View {
        let style = AgentDriverStyle.forDriver(driver)
        if let nsImg = HarnessBrandIconCache.shared.officialIcon(for: driver, appPath: appPath) {
            Image(nsImage: nsImg)
                .resizable()
                .interpolation(.high)
                .aspectRatio(contentMode: .fit)
                .frame(width: size, height: size)
                .clipShape(RoundedRectangle(cornerRadius: size * 0.22, style: .continuous))
        } else {
            ZStack {
                RoundedRectangle(cornerRadius: size * 0.24, style: .continuous)
                    .fill(style.gradient)
                Text(style.code)
                    .font(.system(size: max(8, size * 0.36), weight: .heavy, design: .monospaced))
                    .foregroundStyle(.white)
            }
            .frame(width: size, height: size)
        }
    }
}
