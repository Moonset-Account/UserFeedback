// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "AuthzValidator",
    targets: [
        .target(
            name: "AuthzValidatorCore",
            path: "Sources/AuthzValidatorCore"
        ),
        .executableTarget(
            name: "AuthzValidator",
            dependencies: ["AuthzValidatorCore"],
            path: "Sources/AuthzValidatorCLI"
        ),
        .testTarget(
            name: "AuthzValidatorTests",
            dependencies: ["AuthzValidatorCore"],
            path: "Tests/AuthzValidatorTests"
        )
    ]
)
