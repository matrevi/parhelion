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
    guard let ctx = NSGraphicsContext(bitmapImageRep: rep)?.cgContext else { return nil }
    let s = CGFloat(size)

    // macOS squircle inset
    let inset = s * 0.08
    let rect = CGRect(x: inset, y: inset, width: s - inset * 2, height: s - inset * 2)
    let radius = rect.width * 0.225
    let bgPath = CGPath(roundedRect: rect, cornerWidth: radius, cornerHeight: radius, transform: nil)

    ctx.saveGState()
    ctx.addPath(bgPath)
    ctx.clip()

    // Deep NPU slate-indigo gradient background
    let colorSpace = CGColorSpaceCreateDeviceRGB()
    let gradColors = [
        CGColor(red: 0.07, green: 0.10, blue: 0.18, alpha: 1.0),
        CGColor(red: 0.11, green: 0.17, blue: 0.32, alpha: 1.0),
        CGColor(red: 0.06, green: 0.24, blue: 0.34, alpha: 1.0)
    ] as CFArray
    if let grad = CGGradient(colorsSpace: colorSpace, colors: gradColors, locations: [0.0, 0.55, 1.0]) {
        ctx.drawLinearGradient(
            grad,
            start: CGPoint(x: rect.minX, y: rect.maxY),
            end: CGPoint(x: rect.maxX, y: rect.minY),
            options: []
        )
    }

    // Subtle radial cyan glow
    let glowColors = [
        CGColor(red: 0.0, green: 0.82, blue: 0.95, alpha: 0.28),
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

    // Draw 4x4 Accel AI Core Matrix Die in center
    let dieMargin = s * 0.24
    let dieSize = s - dieMargin * 2
    let dieRect = CGRect(x: dieMargin, y: dieMargin, width: dieSize, height: dieSize)
    let diePath = CGPath(roundedRect: dieRect, cornerWidth: dieSize * 0.14, cornerHeight: dieSize * 0.14, transform: nil)
    ctx.setFillColor(CGColor(red: 0.05, green: 0.08, blue: 0.14, alpha: 0.78))
    ctx.addPath(diePath)
    ctx.fillPath()

    ctx.setStrokeColor(CGColor(red: 0.22, green: 0.78, blue: 0.96, alpha: 0.75))
    ctx.setLineWidth(max(1.0, s * 0.016))
    ctx.addPath(diePath)
    ctx.strokePath()

    let gridPad = dieSize * 0.14
    let inner = dieSize - gridPad * 2
    let gap = inner * 0.08
    let cell = (inner - gap * 3) / 4.0

    for row in 0..<4 {
        for col in 0..<4 {
            let cx = dieRect.minX + gridPad + CGFloat(col) * (cell + gap)
            let cy = dieRect.minY + gridPad + CGFloat(row) * (cell + gap)
            let cRect = CGRect(x: cx, y: cy, width: cell, height: cell)
            let cPath = CGPath(roundedRect: cRect, cornerWidth: cell * 0.24, cornerHeight: cell * 0.24, transform: nil)

            let isHot = (row == 2 && col == 1) || (row == 2 && col == 2) || (row == 1 && col == 2)
            let isGold = (row == 3 && col == 3)
            if isGold {
                ctx.setFillColor(CGColor(red: 0.98, green: 0.74, blue: 0.20, alpha: 0.95))
            } else if isHot {
                ctx.setFillColor(CGColor(red: 0.12, green: 0.88, blue: 0.62, alpha: 0.92))
            } else {
                ctx.setFillColor(CGColor(red: 0.18, green: 0.56, blue: 0.88, alpha: 0.70))
            }
            ctx.addPath(cPath)
            ctx.fillPath()
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
