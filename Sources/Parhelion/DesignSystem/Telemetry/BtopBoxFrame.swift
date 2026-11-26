import SwiftUI

enum BtopPalette {
    static let deckBgTop = Color(red: 0.055, green: 0.075, blue: 0.115)
    static let deckBgBottom = Color(red: 0.075, green: 0.100, blue: 0.150)
    static let boxFill = Color(red: 0.085, green: 0.115, blue: 0.172)
    static let border = Color(red: 0.19, green: 0.26, blue: 0.38)
    static let gridLine = Color.white.opacity(0.07)
    static let textPrimary = Color(red: 0.93, green: 0.96, blue: 1.0)
    static let textDim = Color(red: 0.53, green: 0.62, blue: 0.76)

    static let cyan = Color(red: 0.13, green: 0.83, blue: 0.94)
    static let amber = Color(red: 0.98, green: 0.74, blue: 0.18)
    static let emerald = Color(red: 0.20, green: 0.85, blue: 0.60)
    static let violet = Color(red: 0.75, green: 0.52, blue: 0.99)
    static let sky = Color(red: 0.22, green: 0.74, blue: 0.97)
    static let indigo = Color(red: 0.42, green: 0.48, blue: 0.95)
    static let coral = Color(red: 0.96, green: 0.36, blue: 0.40)

    static func thermalColor(pct: Double) -> Color {
        if pct >= 80 { return coral }
        if pct >= 45 { return amber }
        return emerald
    }
}

/// `btop`-inspired HUD panel with inline top-border title cutout and crisp monospaced chrome.
struct BtopBox<Trailing: View, Content: View>: View {
    let keyIndex: String
    let title: String
    let accent: Color
    /// Shown after the title as written (host aliases are case-sensitive), e.g. `@npu2`.
    var titleSuffix: String = ""
    @ViewBuilder var trailing: () -> Trailing
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            // Top inline title bar
            HStack(spacing: 6) {
                Text(keyIndex)
                    .font(.system(size: 9.5, weight: .heavy, design: .monospaced))
                    .foregroundStyle(BtopPalette.deckBgTop)
                    .padding(.horizontal, 4.5)
                    .padding(.vertical, 1)
                    .background(RoundedRectangle(cornerRadius: 3).fill(accent))

                Text(title.uppercased())
                    .font(.system(size: 10.5, weight: .bold, design: .monospaced))
                    .foregroundStyle(BtopPalette.textPrimary)
                    .tracking(0.5)

                if !titleSuffix.isEmpty {
                    Text(titleSuffix)
                        .font(.system(size: 10.5, weight: .bold, design: .monospaced))
                        .foregroundStyle(accent)
                }

                Spacer(minLength: 4)
                trailing()
            }

            content()
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(BtopPalette.boxFill)
                .overlay(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .strokeBorder(
                            LinearGradient(
                                colors: [accent.opacity(0.50), BtopPalette.border],
                                startPoint: .topLeading,
                                endPoint: .bottomTrailing
                            ),
                            lineWidth: 1
                        )
                )
        )
    }
}
