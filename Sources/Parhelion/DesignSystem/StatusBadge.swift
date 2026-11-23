import SwiftUI

struct SemanticPill: View {
    let text: String
    var icon: String? = nil
    var compact: Bool = false
    var customForeground: Color? = nil
    var customBackground: Color? = nil

    private var palette: (fg: Color, bg: Color) {
        if let fg = customForeground, let bg = customBackground {
            return (fg, bg)
        }
        let u = text.uppercased()
        if u.contains("已满") || u.contains("EXHAUSTED") || u.contains(" · FULL") {
            return (.white, Color(red: 0.78, green: 0.22, blue: 0.26))
        }
        if u.contains("CRITICAL") || u.contains("ESCAPE") || u.contains("逃逸锁定") || u.contains("榜上0分") || u.contains("清零待恢复") || u == "REDLINE" || u == "ANTI-PATTERN" || u == "DEMOTE" {
            return (.white, Color(red: 0.84, green: 0.23, blue: 0.26))
        }
        if u.contains("BACKFILL") || u.contains("样例桶") || u == "B0" || u == "WARN" || u == "REMAP-TO-ACCEL" {
            return (Color(red: 0.78, green: 0.42, blue: 0.04), Color(red: 0.94, green: 0.62, blue: 0.14).opacity(0.18))
        }
        if u.contains("CPU-BUILD") || u.contains("无锁编译") || u == "NATIVE-ACCEL" {
            return (Color(red: 0.12, green: 0.68, blue: 0.62), Color(red: 0.12, green: 0.68, blue: 0.62).opacity(0.15))
        }
        if u.contains("突破") || u.contains("BREAKTHROUGH") {
            return (.white, Color(red: 0.88, green: 0.46, blue: 0.08))
        }
        if u.contains("PASS ↑") || u == "PASS" || u == "通过" || u.contains("稳定在榜") || u.contains("STEADY") || u.contains("READY") || u.contains("就绪") || u == "FREE" || u == "INVITED" || u == "OK" || u == "ACTIVE" || u.contains("IDLE") || u.contains("空闲") || u.contains("冷却已就绪") || u.contains("已登录") || u.contains("已授权") || u.contains("启用") || u.contains("已生效") || u == "VERIFIED" || u == "WIN" || u == "PROMOTE" {
            return (Color(red: 0.10, green: 0.72, blue: 0.43), Color(red: 0.10, green: 0.72, blue: 0.43).opacity(0.14))
        }
        if u.contains("SYNC ERROR") || u.contains("SYNC ERR") || u.contains("同步中断") {
            return (Color(red: 0.86, green: 0.48, blue: 0.06), Color(red: 0.94, green: 0.62, blue: 0.14).opacity(0.18))
        }
        if u.contains("PASS ↓") || u.contains("RESTORE") || u.contains("可恢复") || u.contains("排队等卡") || u.contains("WAITING-DIE") || u.contains("RATE") || u.contains("冷却等待") || u.contains("COOLDOWN") || u.contains("COOLING") || u.contains("HELD") || u.contains("HOLD") || u.contains("LOCKED") || u.contains("EXPIRED") || u.contains("OFFLINE") || u.contains("MISSING") || u.contains("限流冷却") || u.contains("已锁定") || u.contains("提分较小") || u.contains("已过期") || u.contains("未登录") || u.contains("未安装") || u == "UNVERIFIED" || u == "DEPRECATED" {
            return (Color(red: 0.90, green: 0.56, blue: 0.10), Color(red: 0.90, green: 0.56, blue: 0.10).opacity(0.15))
        }
        if u.contains("PROFILER") || u.contains("性能采集") || u.contains("HIGH ROI") || u.contains("高收益") || u.contains("PACKAGING") || u.contains("COMPILING") || u.contains("打包") || u.contains("编译") || u == "REVERT" {
            return (Color(red: 0.58, green: 0.38, blue: 0.95), Color(red: 0.58, green: 0.38, blue: 0.95).opacity(0.15))
        }
        if u.contains("PRECISION") || u.contains("精度验证") || u.contains("JUDGING") || u.contains("评测中") || u.contains("HEADROOM") || u.contains("提分空间") || u.contains("REMOTE") || u.contains("UPLOADING") || u.contains("SYNCING") || u.contains("EXPLORE") || u.contains("探索") || u == "PUBLIC" || u == "公开" || u.contains("远程") || u.contains("上传") || u.contains("同步") || u == "PORTABLE" || u == "FEEDBACK" || u == "CREATE" {
            return (Color(red: 0.20, green: 0.56, blue: 0.96), Color(red: 0.20, green: 0.56, blue: 0.96).opacity(0.15))
        }
        if u.contains("BUILD") {
            return (.indigo, Color.indigo.opacity(0.15))
        }
        if u.contains("FAST-LANE") || u.contains("快速冒烟") {
            return (.teal, Color.teal.opacity(0.15))
        }
        if u.contains("STRESS") || u.contains("并发压测") || u.contains("CIRCUIT") || u.contains("频控") || u.contains("无突破") {
            return (.orange, Color.orange.opacity(0.16))
        }
        if u.contains("REJECTED") || u.contains("门禁拒绝") || u.contains("GATE HELD") || u.contains("门禁未通过") || u.contains("WRONG") || u.contains("ERROR") || u.contains("TIME LIMIT") || u == "FAILED" || u.contains("未通过") || u.contains("精度未过") || u.contains("运行超时") || u.contains("运行报错") || u.contains("编译失败") || u.contains("本地拦截") {
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

struct ChevronStackGlyph: View {
    let level: Int
    let color: Color

    var body: some View {
        Canvas { ctx, size in
            let cx = size.width / 2.0
            let halfW: CGFloat = 4.6
            let chevH: CGFloat = 3.1
            let stepY: CGFloat = 4.1
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
                    with: .color(color),
                    style: StrokeStyle(lineWidth: 1.9, lineCap: .round, lineJoin: .round)
                )
            }
        }
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
        ChevronStackGlyph(level: level, color: tint)
            .frame(width: 24, height: 20)
            .background(
                RoundedRectangle(cornerRadius: 5, style: .continuous)
                    .fill(tint.opacity(0.12))
            )
            .help("难度级别 \(level) 级（计分权重 \(level) 倍，折算满分 \(level * 100) 分）")
    }
}

/// Unified single badge merging the solid 1/2/3 chevron tier icon + clean priority label (no duplicate ASCII `^`).
struct TierTagBadge: View {
    let difficulty: Int
    let text: String

    private var level: Int { min(3, max(1, difficulty)) }

    private var palette: (fg: Color, bg: Color) {
        let u = text.uppercased()
        if u.contains("CRITICAL") || u.contains("ESCAPE") || u.contains("逃逸") || u.contains("清零") {
            return (.white, Color(red: 0.84, green: 0.23, blue: 0.26))
        }
        if u.contains("RESTORE") || u.contains("可恢复") {
            let c = Color(red: 0.90, green: 0.56, blue: 0.10)
            return (c, c.opacity(0.15))
        }
        let tierTint: Color = {
            switch level {
            case 3: return Color(red: 0.62, green: 0.36, blue: 0.95)
            case 2: return Color(red: 0.20, green: 0.56, blue: 0.95)
            default: return .teal
            }
        }()
        return (tierTint, tierTint.opacity(0.14))
    }

    var body: some View {
        HStack(spacing: 5.5) {
            ChevronStackGlyph(level: level, color: palette.fg)
                .frame(width: 13, height: 14)
            Text(text)
                .font(.system(size: 10.5, weight: .semibold, design: .rounded))
                .lineLimit(1)
        }
        .foregroundStyle(palette.fg)
        .padding(.horizontal, 8.5)
        .padding(.vertical, 3.5)
        .background(Capsule(style: .continuous).fill(palette.bg))
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
