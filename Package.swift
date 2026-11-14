// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "QuotaIsolationBench",
    products: [
        .library(name: "QuotaIsolationCore", targets: ["QuotaIsolationCore"]),
        .executable(name: "quota-bench", targets: ["quota-bench"])
    ],
    targets: [
        .target(name: "QuotaIsolationCore"),
        .executableTarget(
            name: "quota-bench",
            dependencies: ["QuotaIsolationCore"]
        ),
        .testTarget(
            name: "QuotaIsolationCoreTests",
            dependencies: ["QuotaIsolationCore"]
        )
    ]
)
