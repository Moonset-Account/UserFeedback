// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "ReleaseGate",
    products: [
        .library(name: "ReleaseGateCore", targets: ["ReleaseGateCore"]),
        .executable(name: "release-gate", targets: ["release-gate"]),
    ],
    targets: [
        .target(name: "ReleaseGateCore"),
        .executableTarget(name: "release-gate", dependencies: ["ReleaseGateCore"]),
        .testTarget(name: "ReleaseGateCoreTests", dependencies: ["ReleaseGateCore"]),
    ]
)
