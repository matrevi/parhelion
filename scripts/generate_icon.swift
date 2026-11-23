#!/usr/bin/env swift
import AppKit
import CoreGraphics
import Foundation

let args = CommandLine.arguments
guard args.count >= 2 else {
    fputs("Usage: generate_icon.swift <output_iconset_dir>\n", stderr)
    exit(1)
}

let iconsetURL = URL(fileURLWithPath: args[1])
try? FileManager.default.removeItem(at: iconsetURL)
try FileManager.default.createDirectory(at: iconsetURL, withIntermediateDirectories: true)

func drawIcon(size: Int) -> Data? {
    guard let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil,
        pixelsWide: size,
        pixelsHigh: size,
        bitsPerSample: 8,
        samplesPerPixel: 4,
        hasAlpha: true,
        isPlanar: false,
        colorSpaceName: .deviceRGB,
        bytesPerRow: 0,
        bitsPerPixel: 0
    ) else { return nil }

    NSGraphicsContext.saveGraphicsState()
    guard let nsctx = NSGraphicsContext(bitmapImageRep: rep) else { return nil }
    NSGraphicsContext.current = nsctx   // required so AppKit text drawing lands in this bitmap
    let ctx = nsctx.cgContext
    let s = CGFloat(size)

    // macOS squircle inset
    let inset = s * 0.08
    let rect = CGRect(x: inset, y: inset, width: s - inset * 2, height: s - inset * 2)
    let radius = rect.width * 0.225
    let bgPath = CGPath(roundedRect: rect, cornerWidth: radius, cornerHeight: radius, transform: nil)

    ctx.saveGState()
    ctx.addPath(bgPath)
    ctx.clip()

    // Light, brand-clean surface: the official SDK mark is black + red, it needs a
    // neutral background to stay legible at every size.
    let colorSpace = CGColorSpaceCreateDeviceRGB()
    let gradColors = [
        CGColor(red: 1.00, green: 1.00, blue: 1.00, alpha: 1.0),
        CGColor(red: 0.96, green: 0.97, blue: 0.99, alpha: 1.0),
        CGColor(red: 0.92, green: 0.94, blue: 0.97, alpha: 1.0)
    ] as CFArray
    if let grad = CGGradient(colorsSpace: colorSpace, colors: gradColors, locations: [0.0, 0.62, 1.0]) {
        ctx.drawLinearGradient(
            grad,
            start: CGPoint(x: rect.minX, y: rect.maxY),
            end: CGPoint(x: rect.maxX, y: rect.minY),
            options: []
        )
    }

    // Subtle radial cyan glow
    let glowColors = [
        CGColor(red: 0.42, green: 0.55, blue: 0.72, alpha: 0.12),
        CGColor(red: 0.0, green: 0.82, blue: 0.95, alpha: 0.0)
    ] as CFArray
    if let rGrad = CGGradient(colorsSpace: colorSpace, colors: glowColors, locations: [0.0, 1.0]) {
        ctx.drawRadialGradient(
            rGrad,
            startCenter: CGPoint(x: s * 0.5, y: s * 0.54),
            startRadius: 0,
            endCenter: CGPoint(x: s * 0.5, y: s * 0.54),
            endRadius: s * 0.42,
            options: []
        )
    }

    // MARK: - Official SDK mark
    // Drawn from `assets/logo.png` (example.invalid SDK wordmark: black letters, red A).
    let scriptDir = URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent()
    let logoCandidates = [
        scriptDir.appendingPathComponent("assets/logo.png"),
        URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent("assets/logo.png")
    ]
    if size < 64 {
        // Small sizes: the wordmark turns to mush below ~64 px, so fall back to the
        // mark's signature element — the Vendor-red "A".
        let glyph = "A" as NSString
        let f = NSFont.systemFont(ofSize: s * 0.60, weight: .heavy)
        let para = NSMutableParagraphStyle()
        para.alignment = .center
        let attrs: [NSAttributedString.Key: Any] = [
            .font: f,
            .foregroundColor: NSColor(srgbRed: 0.855, green: 0.102, blue: 0.118, alpha: 1.0),
            .paragraphStyle: para
        ]
        let gs = glyph.size(withAttributes: attrs)
        glyph.draw(at: NSPoint(x: rect.midX - gs.width / 2, y: rect.midY - gs.height / 2), withAttributes: attrs)
    } else if let logoURL = logoCandidates.first(where: { FileManager.default.fileExists(atPath: $0.path) }),
       let logoImage = NSImage(contentsOf: logoURL),
       let logoCG = logoImage.cgImage(forProposedRect: nil, context: nil, hints: nil) {
        let aspect = CGFloat(logoCG.width) / CGFloat(logoCG.height)
        let maxW = rect.width * 0.76
        let maxH = rect.height * 0.40
        var w = maxW
        var h = w / aspect
        if h > maxH { h = maxH; w = h * aspect }
        let logoRect = CGRect(x: rect.midX - w / 2, y: rect.midY - h / 2, width: w, height: h)
        ctx.saveGState()
        ctx.interpolationQuality = .high
        ctx.draw(logoCG, in: logoRect)
        ctx.restoreGState()

        // one thin operator rule under the mark: 3 cores, brand red accent in the middle
        if size > 48 {
            let ruleW = w * 0.56
            let ruleY = logoRect.minY - rect.height * 0.10
            let seg = ruleW / 3
            let barH = max(1.0, rect.height * 0.012)
            let colors: [CGColor] = [
                CGColor(red: 0.10, green: 0.10, blue: 0.12, alpha: 0.55),
                CGColor(red: 0.86, green: 0.12, blue: 0.16, alpha: 0.95),
                CGColor(red: 0.10, green: 0.10, blue: 0.12, alpha: 0.55)
            ]
            for i in 0..<3 {
                let r = CGRect(x: rect.midX - ruleW / 2 + CGFloat(i) * seg + seg * 0.12,
                               y: ruleY,
                               width: seg * 0.76,
                               height: barH)
                ctx.setFillColor(colors[i])
                ctx.addPath(CGPath(roundedRect: r, cornerWidth: barH / 2, cornerHeight: barH / 2, transform: nil))
                ctx.fillPath()
            }
        }
    }

    ctx.restoreGState()

    // Squircle highlight border
    ctx.setStrokeColor(CGColor(red: 1.0, green: 1.0, blue: 1.0, alpha: 0.18))
    ctx.setLineWidth(max(1.0, s * 0.012))
    ctx.addPath(bgPath)
    ctx.strokePath()

    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])
}

let specs: [(String, Int)] = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024)
]

for (name, px) in specs {
    if let png = drawIcon(size: px) {
        try png.write(to: iconsetURL.appendingPathComponent(name))
    }
}
