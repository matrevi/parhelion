import SwiftUI

struct SemanticPill: View {
    let text: String
    var icon: String? = nil
    var compact: Bool = false

    private var palette: (fg: Color, bg: Color) {
        let u = text.uppercased()
        if u.contains("CRITICAL") || u.contains("榜上0分") {
            return (.white, Color(red: 0.84, green: 0.23, blue: 0.26))
        }
        if u.contains("PASS ↑") || u == "PASS" || u.contains("READY") || u == "FREE" || u == "INVITED" || u == "OK" || u == "ACTIVE" || u.contains("空闲") || u.contains("冷却已就绪") {
            return (Color(red: 0.10, green: 0.72, blue: 0.43), Color(red: 0.10, green: 0.72, blue: 0.43).opacity(0.14))
        }
        if u.contains("PASS ↓") || u.contains("RESTORE") || u.contains("RATE WAIT") || u.contains("COOLING") || u.contains("HELD") || u.contains("限流冷却") || u.contains("已锁定") || u.contains("提分较小") {
            return (Color(red: 0.90, green: 0.56, blue: 0.10), Color(red: 0.90, green: 0.56, blue: 0.10).opacity(0.15))
        }
        if u.contains("PROFILER") || u.contains("HIGH ROI") || u.contains("打包") || u.contains("编译") {
            return (Color(red: 0.58, green: 0.38, blue: 0.95), Color(red: 0.58, green: 0.38, blue: 0.95).opacity(0.15))
        }
        if u.contains("PRECISION") || u.contains("JUDGING") || u.contains("HEADROOM") || u.contains("远程") || u.contains("上传") || u.contains("同步") {
            return (Color(red: 0.20, green: 0.56, blue: 0.96), Color(red: 0.20, green: 0.56, blue: 0.96).opacity(0.15))
        }
        if u.contains("BUILD") {
            return (.indigo, Color.indigo.opacity(0.15))
        }
        if u.contains("FAST-LANE") {
            return (.teal, Color.teal.opacity(0.15))
        }
        if u.contains("STRESS") || u.contains("CIRCUIT") || u.contains("频控") {
            return (.orange, Color.orange.opacity(0.16))
        }
        if u.contains("REJECTED") || u.contains("WRONG") || u.contains("ERROR") || u.contains("TIME LIMIT") || u == "FAILED" || u.contains("未通过") {
            return (Color(red: 0.90, green: 0.28, blue: 0.32), Color(red: 0.90, green: 0.28, blue: 0.32).opacity(0.15))
        }
        return (.secondary, Color.secondary.opacity(0.12))
    }

    var body: some View {
        HStack(spacing: 4) {
            if let icon {
                Image(systemName: icon)
                    .font(.system(size: compact ? 9 : 10, weight: .semibold))
            }
            Text(text)
                .font(.system(size: compact ? 10 : 11, weight: .semibold, design: .rounded))
                .lineLimit(1)
        }
        .foregroundStyle(palette.fg)
        .padding(.horizontal, compact ? 6.5 : 8.5)
        .padding(.vertical, compact ? 2 : 3.5)
        .background(Capsule(style: .continuous).fill(palette.bg))
    }
}

/// Stacked upward chevron tier badge: draws EXACTLY `level` (1, 2, or 3) solid chevrons, vertically centered.
struct DifficultyBadge: View {
    let difficulty: Int

    private var level: Int { min(3, max(1, difficulty)) }

    private var tint: Color {
        switch level {
        case 3: return Color(red: 0.66, green: 0.34, blue: 0.95)
        case 2: return Color(red: 0.20, green: 0.56, blue: 0.95)
        default: return .teal
        }
    }

    var body: some View {
        Canvas { ctx, size in
            let cx = size.width / 2.0
            let halfW: CGFloat = 4.8
            let chevH: CGFloat = 3.2
            let stepY: CGFloat = 4.2
            let totalH = chevH + CGFloat(level - 1) * stepY
            let startY = (size.height - totalH) / 2.0

            for i in 0..<level {
                let topY = startY + CGFloat(i) * stepY
                var path = Path()
                path.move(to: CGPoint(x: cx - halfW, y: topY + chevH))
                path.addLine(to: CGPoint(x: cx, y: topY))
                path.addLine(to: CGPoint(x: cx + halfW, y: topY + chevH))

                ctx.stroke(
                    path,
                    with: .color(tint),
                    style: StrokeStyle(lineWidth: 1.95, lineCap: .round, lineJoin: .round)
                )
            }
        }
        .frame(width: 24, height: 20)
        .background(
            RoundedRectangle(cornerRadius: 5, style: .continuous)
                .fill(tint.opacity(0.12))
        )
        .help("难度级别 \(level) 级（计分权重 \(level) 倍，折算满分 \(level * 100) 分）")
    }
}

struct GateCheckPill: View {
    let label: String
    let ok: Bool

    var body: some View {
        HStack(spacing: 3) {
            Image(systemName: ok ? "checkmark.circle.fill" : "pause.circle.fill")
                .font(.system(size: 9))
            Text(label)
                .font(.system(size: 9.5, weight: .medium, design: .rounded))
        }
        .foregroundStyle(ok ? Color(red: 0.12, green: 0.70, blue: 0.42) : .orange)
        .padding(.horizontal, 5.5)
        .padding(.vertical, 2)
        .background(RoundedRectangle(cornerRadius: 4, style: .continuous).fill((ok ? Color.green : Color.orange).opacity(0.10)))
    }
}
