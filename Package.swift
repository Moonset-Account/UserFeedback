// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "AuthzAudit",
    targets: [
        .executableTarget(
            name: "AuthzAudit",
            path: "Sources/AuthzAudit"
        )
    ]
)
