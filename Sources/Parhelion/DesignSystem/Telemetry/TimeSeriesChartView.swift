import SwiftUI

struct TelemetryTrace: Identifiable {
    let id: String
    let label: String
    let currentText: String
    let values: [Double] // Normalized 0.0 ... 100.0
    let color: Color
    let showBrailleColumns: Bool
}

/// High-DPI `btop` oscilloscope time-utilization waveform chart rendered via SwiftUI Canvas.
struct BtopWaveChart: View {
    let traces: [TelemetryTrace]
    let timestamps: [Double]
    var yTopLabel: String = "100%"
    var yMidLabel: String = "50%"

    private var spanText: String {
        guard let first = timestamps.first, let last = timestamps.last, last > first else {
            return "live stream"
        }
        let span = Int(round(last - first))
        return span < 60 ? "-\(span)s → now" : "-\(span / 60)m \(span % 60)s → now"
    }

    var body: some View {
        VStack(spacing: 5) {
            ZStack(alignment: .topLeading) {
                Canvas { ctx, size in
                    drawGrid(ctx: &ctx, size: size)
                    for trace in traces.reversed() {
                        drawTrace(ctx: &ctx, size: size, trace: trace)
                    }
                }

                // Y-axis micro reticle labels
                VStack(alignment: .leading) {
                    Text(yTopLabel)
                    Spacer()
                    Text(yMidLabel)
                    Spacer()
                    Text("0%")
                }
                .font(.system(size: 8.5, weight: .medium, design: .monospaced))
                .foregroundStyle(BtopPalette.textDim.opacity(0.8))
                .padding(.leading, 3)
                .padding(.vertical, 2)
                .allowsHitTesting(false)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(
                RoundedRectangle(cornerRadius: 5, style: .continuous)
                    .fill(Color.black.opacity(0.26))
            )

            // Legend & X-axis time span bar
            HStack(spacing: 10) {
                ForEach(traces) { tr in
                    HStack(spacing: 4) {
                        Circle().fill(tr.color).frame(width: 6, height: 6)
                        Text(tr.label)
                            .font(.system(size: 9.5, weight: .medium, design: .monospaced))
                            .foregroundStyle(BtopPalette.textDim)
                        Text(tr.currentText)
                            .font(.system(size: 10, weight: .bold, design: .monospaced))
                            .foregroundStyle(tr.color)
                    }
                }
                Spacer()
                Text(spanText)
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundStyle(BtopPalette.textDim)
            }
        }
    }

    private func drawGrid(ctx: inout GraphicsContext, size: CGSize) {
        let dash = StrokeStyle(lineWidth: 0.7, dash: [3, 3])
        for frac in [0.25, 0.50, 0.75] {
            let y = size.height * frac
            var p = Path()
            p.move(to: CGPoint(x: 0, y: y))
            p.addLine(to: CGPoint(x: size.width, y: y))
            ctx.stroke(p, with: .color(BtopPalette.gridLine), style: dash)
        }
        let cols = 6
        for i in 1..<cols {
            let x = size.width * CGFloat(i) / CGFloat(cols)
            var p = Path()
            p.move(to: CGPoint(x: x, y: 0))
            p.addLine(to: CGPoint(x: x, y: size.height))
            ctx.stroke(p, with: .color(BtopPalette.gridLine.opacity(0.6)), style: dash)
        }
    }

    private func drawTrace(ctx: inout GraphicsContext, size: CGSize, trace: TelemetryTrace) {
        let raw = trace.values
        guard !raw.isEmpty else { return }

        // Pad single-point startup smoothly across right half so a live flat/initial line is immediately visible
        let vals = raw.count == 1 ? [raw[0], raw[0]] : raw
        let maxSlots = max(24, vals.count)
        let padLeft = maxSlots - vals.count

        let insetY: CGFloat = 4
        let usableH = max(8, size.height - insetY * 2)

        func point(at idx: Int, val: Double) -> CGPoint {
            let slot = padLeft + idx
            let x = size.width * CGFloat(slot) / CGFloat(max(1, maxSlots - 1))
            let clamped = min(100.0, max(0.0, val)) / 100.0
            let y = size.height - insetY - usableH * CGFloat(clamped)
            return CGPoint(x: x, y: y)
        }

        let pts = vals.enumerated().map { point(at: $0.offset, val: $0.element) }
        guard let firstPt = pts.first, let lastPt = pts.last else { return }

        // 1. Soft gradient area fill
        var area = Path()
        area.move(to: CGPoint(x: firstPt.x, y: size.height))
        for pt in pts { area.addLine(to: pt) }
        area.addLine(to: CGPoint(x: lastPt.x, y: size.height))
        area.closeSubpath()

        ctx.fill(
            area,
            with: .linearGradient(
                Gradient(colors: [trace.color.opacity(0.32), trace.color.opacity(0.03)]),
                startPoint: CGPoint(x: 0, y: 0),
                endPoint: CGPoint(x: 0, y: size.height)
            )
        )

        // 2. btop-style discrete phosphor column ticks inside the waveform
        if trace.showBrailleColumns {
            let step = max(3.5, size.width / 56.0)
            var xCursor = firstPt.x
            while xCursor <= lastPt.x {
                // Linearly interpolate y on pts
                let rel = (xCursor - firstPt.x) / max(1.0, lastPt.x - firstPt.x)
                let fIdx = Double(pts.count - 1) * Double(rel)
                let i0 = max(0, min(pts.count - 1, Int(fIdx)))
                let i1 = min(pts.count - 1, i0 + 1)
                let frac = CGFloat(fIdx - Double(i0))
                let yInterp = pts[i0].y + (pts[i1].y - pts[i0].y) * frac

                var bar = Path()
                bar.move(to: CGPoint(x: xCursor, y: size.height - 2))
                bar.addLine(to: CGPoint(x: xCursor, y: yInterp))
                ctx.stroke(bar, with: .color(trace.color.opacity(0.24)), style: StrokeStyle(lineWidth: 1.5, dash: [2, 1.5]))
                xCursor += step
            }
        }

        // 3. Crisp primary polyline
        var line = Path()
        line.move(to: firstPt)
        for pt in pts.dropFirst() { line.addLine(to: pt) }
        ctx.stroke(line, with: .color(trace.color), style: StrokeStyle(lineWidth: 1.8, lineCap: .round, lineJoin: .round))

        // 4. Glowing live endpoint beacon
        let halo = CGRect(x: lastPt.x - 5, y: lastPt.y - 5, width: 10, height: 10)
        ctx.fill(Path(ellipseIn: halo), with: .color(trace.color.opacity(0.28)))
        let dot = CGRect(x: lastPt.x - 2.4, y: lastPt.y - 2.4, width: 4.8, height: 4.8)
        ctx.fill(Path(ellipseIn: dot), with: .color(trace.color))
    }
}
