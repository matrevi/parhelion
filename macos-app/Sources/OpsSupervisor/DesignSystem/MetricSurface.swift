import SwiftUI

struct SurfaceCard<Content: View>: View {
    var highlighted: Bool = false
    var highlightColor: Color = .accentColor
    var padding: CGFloat = 12
    @ViewBuilder var content: () -> Content

    var body: some View {
        content()
            .padding(padding)
            .background(
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(Color(nsColor: .controlBackgroundColor))
                    .overlay(
                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .strokeBorder(
                                highlighted ? highlightColor.opacity(0.45) : Color.primary.opacity(0.06),
                                lineWidth: highlighted ? 1.4 : 1
                            )
                    )
            )
    }
}

struct MetricTile: View {
    let title: String
    let value: String
    var detail: String? = nil
    let icon: String
    var accent: Color = .accentColor

    var body: some View {
        SurfaceCard(padding: 11) {
            VStack(alignment: .leading, spacing: 5) {
                HStack(spacing: 5) {
                    Image(systemName: icon)
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(accent)
                    Text(title)
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(.secondary)
                    Spacer(minLength: 0)
                }
                Text(value)
                    .font(.system(size: 18, weight: .bold, design: .rounded))
                    .monospacedDigit()
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
                if let detail {
                    Text(detail)
                        .font(.system(size: 10.5))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

struct GaugeTile: View {
    let label: String
    let valueText: String
    let trailingText: String
    let fraction: Double
    let tint: Color
    let icon: String

    var body: some View {
        SurfaceCard(padding: 10) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 5) {
                    Image(systemName: icon)
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(tint)
                    Text(label)
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(.secondary)
                    Spacer()
                    Text(trailingText)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
                Text(valueText)
                    .font(.system(size: 13.5, weight: .bold, design: .rounded))
                    .monospacedDigit()
                    .lineLimit(1)
                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        Capsule().fill(Color.primary.opacity(0.07))
                        Capsule()
                            .fill(tint.gradient)
                            .frame(width: max(4, geo.size.width * min(1.0, max(0.0, fraction))))
                    }
                }
                .frame(height: 5)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}
