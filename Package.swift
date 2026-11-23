// swift-tools-version: 5.10
import PackageDescription

let package = Package(
    name: "Parhelion",
    platforms: [
        .macOS(.v14)
    ],
    products: [
        .executable(name: "Parhelion", targets: ["Parhelion"])
    ],
    targets: [
        .executableTarget(
            name: "Parhelion",
            path: "Sources/Parhelion"
        )
    ]
)
