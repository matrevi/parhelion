import SwiftUI

/// `btop`-style 32-core compact bar matrix (`4 rows × 8 cols`).
struct BtopCoreMatrixGrid: View {
    let perCorePct: [Double]

    var body: some View {
        let cores = perCorePct.isEmpty ? Array(repeating: 0.0, count: 32) : perCorePct
        let cols = Array(repeating: GridItem(.flexible(), spacing: 5), count: 8)

        LazyVGrid(columns: cols, spacing: 4) {
            ForEach(0..<min(32, cores.count), id: \.self) { idx in
                let pct = cores[idx]
                let color = BtopPalette.thermalColor(pct: pct)
                VStack(alignment: .leading, spacing: 1.5) {
                    HStack(spacing: 1) {
                        Text(String(format: "%02d", idx))
                            .font(.system(size: 7.5, weight: .medium, design: .monospaced))
                            .foregroundStyle(BtopPalette.textDim)
                        Spacer(minLength: 0)
                        Text(String(format: "%.0f", pct))
                            .font(.system(size: 7.5, weight: .bold, design: .monospaced))
                            .foregroundStyle(pct >= 5 ? color : BtopPalette.textDim)
                    }
                    GeometryReader { geo in
                        ZStack(alignment: .leading) {
                            RoundedRectangle(cornerRadius: 1.5)
                                .fill(Color.white.opacity(0.08))
                            RoundedRectangle(cornerRadius: 1.5)
                                .fill(color)
                                .frame(width: max(2, geo.size.width * CGFloat(min(100, max(0, pct)) / 100.0)))
                        }
                    }
                    .frame(height: 3.5)
                }
                .padding(.horizontal, 3.5)
                .padding(.vertical, 2)
                .background(RoundedRectangle(cornerRadius: 3).fill(Color.black.opacity(0.22)))
            }
        }
    }
}

/// `btop`-style discrete segmented block meter (`▮▮▮▯▯▯...`) supporting primary + secondary (cache) fill.
struct BtopSegmentedBar: View {
    var primaryFraction: Double
    var secondaryFraction: Double = 0.0
    var primaryColor: Color
    var secondaryColor: Color = BtopPalette.indigo
    var segments: Int = 22

    var body: some View {
        let pCount = Int(round(min(1.0, max(0.0, primaryFraction)) * Double(segments)))
        let sCount = Int(round(min(1.0 - min(1.0, max(0.0, primaryFraction)), max(0.0, secondaryFraction)) * Double(segments)))

        HStack(spacing: 2) {
            ForEach(0..<segments, id: \.self) { i in
                let fill: Color = {
                    if i < pCount { return primaryColor }
                    if i < pCount + sCount { return secondaryColor.opacity(0.75) }
                    return Color.white.opacity(0.08)
                }()
                RoundedRectangle(cornerRadius: 1.5)
                    .fill(fill)
                    .frame(maxWidth: .infinity)
            }
        }
        .frame(height: 8)
    }
}
