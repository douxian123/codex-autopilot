// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "CodexAutopilotCompanion",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "CodexAutopilotCompanion", targets: ["CodexAutopilotCompanion"])
    ],
    targets: [
        .executableTarget(name: "CodexAutopilotCompanion")
    ]
)
