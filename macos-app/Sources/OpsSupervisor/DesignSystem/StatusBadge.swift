import SwiftUI

struct SemanticPill: View {
    let text: String
    var icon: String? = nil
    var compact: Bool = false

    private var palette: (fg: Color, bg: Color) {
        let u = text.uppercased()
        if u.contains("CRITICAL RESTORE") {
            return (.white, Color(red: 0.84, green: 0.23, blue: 0.26))
        }
        if u.contains("PASS ↑") || u == "PASS" || u == "READY" || u == "FREE" || u == "INVITED" || u == "OK" || u == "ACTIVE" {
            return (Color(red: 0.12, green: 0.72, blue: 0.44), Color(red: 0.12, green: 0.72, blue: 0.44).opacity(0.14))
        }
        if u.contains("PASS ↓") || u.contains("RESTORE") || u.contains("RATE WAIT") || u.contains("COOLING") || u.contains("HELD") {
            return (Color(red: 0.90, green: 0.58, blue: 0.12), Color(red: 0.90, green: 0.58, blue: 0.12).opacity(0.15))
        }
        if u.contains("PROFILER") || u.contains("HIGH ROI") {
            return (Color(red: 0.62, green: 0.40, blue: 0.95), Color(red: 0.62, green: 0.40, blue: 0.95).opacity(0.14))
        }
        if u.contains("PRECISION") || u.contains("JUDGING") || u.contains("HEADROOM") {
            return (Color(red: 0.22, green: 0.58, blue: 0.96), Color(red: 0.22, green: 0.58, blue: 0.96).opacity(0.14))
        }
        if u.contains("BUILD") {
            return (.indigo, Color.indigo.opacity(0.15))
        }
        if u.contains("FAST-LANE") {
            return (.teal, Color.teal.opacity(0.15))
        }
        if u.contains("STRESS") || u.contains("CIRCUIT OPEN") {
            return (.orange, Color.orange.opacity(0.16))
        }
        if u.contains("REJECTED") || u.contains("WRONG") || u.contains("ERROR") || u.contains("TIME LIMIT") || u == "FAILED" {
            return (Color(red: 0.90, green: 0.30, blue: 0.33), Color(red: 0.90, green: 0.30, blue: 0.33).opacity(0.15))
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

struct DifficultyBadge: View {
    let difficulty: Int

    private var tint: Color {
        switch difficulty {
        case 3: return Color(red: 0.66, green: 0.36, blue: 0.94)
        case 2: return Color(red: 0.22, green: 0.56, blue: 0.94)
        default: return .teal
        }
    }

    var body: some View {
        Text("×\(difficulty)")
            .font(.system(size: 11, weight: .bold, design: .monospaced))
            .foregroundStyle(tint)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(RoundedRectangle(cornerRadius: 5, style: .continuous).fill(tint.opacity(0.13)))
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
