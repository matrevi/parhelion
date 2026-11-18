// swift-tools-version: 5.10
import PackageDescription

let package = Package(
    name: "OpsSupervisor",
    platforms: [
        .macOS(.v14)
    ],
    products: [
        .executable(name: "OpsSupervisor", targets: ["OpsSupervisor"])
    ],
    targets: [
        .executableTarget(
            name: "OpsSupervisor",
            path: "Sources/OpsSupervisor"
        )
    ]
)
