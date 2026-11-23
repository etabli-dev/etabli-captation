// swift-tools-version:5.9
// captation-probe: reports which processes use the microphone and whether a camera is in use
// (ADR-004, stage 06). Public APIs only. Build: `swift build -c release`.
import PackageDescription

let package = Package(
    name: "CaptationProbe",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "captation-probe", targets: ["CaptationProbe"]),
    ],
    targets: [
        // Pure logic (output model, JSON encoding, helper normalisation, options): testable
        // without any system API.
        .target(name: "CaptationProbeCore"),
        // CoreAudio / CoreMediaIO / AppKit / CoreGraphics calls and the CLI loop.
        .executableTarget(
            name: "CaptationProbe",
            dependencies: ["CaptationProbeCore"],
            linkerSettings: [
                .linkedFramework("AppKit"),
                .linkedFramework("CoreAudio"),
                .linkedFramework("CoreGraphics"),
                .linkedFramework("CoreMediaIO"),
            ]
        ),
        // `swift test` needs XCTest, which ships with Xcode (not with the Command Line Tools).
        .testTarget(name: "CaptationProbeCoreTests", dependencies: ["CaptationProbeCore"]),
    ]
)
