// Output model, JSON encoding, helper normalisation and option parsing of captation-probe.
// Pure Swift + Foundation: no system API, so it is unit-tested with `swift test`.
// The JSON schema is documented in README.md ("Output schema"); the Python client is
// src/captation/platform/macos.py (parse_probe_line).

import Foundation

public let schemaVersion = 1
public let probeVersion = "0.1.0"

/// Process exit codes (README "Exit codes").
public enum ExitCode: Int32 {
    case ok = 0
    /// Neither CoreAudio process objects nor the device fallback could be read.
    case apiUnavailable = 2
    /// Reserved: a required permission is missing (titles report `titles_status` instead).
    case permissionMissing = 3
    /// Bad command line (EX_USAGE).
    case usage = 64
}

/// Which API produced `mic_users`.
public enum API {
    /// CoreAudio process objects (macOS 14+): per-process attribution, confidence "high".
    public static let processObjects = "process_objects"
    /// Default input device running somewhere + frontmost app: confidence "low".
    public static let deviceRunning = "device_running"
}

public enum Confidence {
    public static let high = "high"
    public static let low = "low"
}

public enum TitlesStatus {
    public static let ok = "ok"
    public static let permissionMissing = "permission_missing"
    public static let unavailable = "unavailable"
}

/// One process that uses the microphone (or the frontmost app in the low-confidence path).
public struct AppUser: Codable, Equatable, Sendable {
    public var pid: Int32
    public var bundleID: String?
    /// Executable basename (what `process` rules match).
    public var name: String?
    /// NSRunningApplication.localizedName, if the process is a LaunchServices app.
    public var localizedName: String?
    /// Executable path (proc_pidpath).
    public var path: String?
    public var ppid: Int32?
    /// For browser/Electron helpers: the first non-helper ancestor (parent PID chain).
    public var parentPID: Int32?
    public var parentBundleID: String?
    public var parentName: String?

    public init(
        pid: Int32,
        bundleID: String? = nil,
        name: String? = nil,
        localizedName: String? = nil,
        path: String? = nil,
        ppid: Int32? = nil,
        parentPID: Int32? = nil,
        parentBundleID: String? = nil,
        parentName: String? = nil
    ) {
        self.pid = pid
        self.bundleID = bundleID
        self.name = name
        self.localizedName = localizedName
        self.path = path
        self.ppid = ppid
        self.parentPID = parentPID
        self.parentBundleID = parentBundleID
        self.parentName = parentName
    }

    enum CodingKeys: String, CodingKey {
        case pid
        case bundleID = "bundle_id"
        case name
        case localizedName = "localized_name"
        case path
        case ppid
        case parentPID = "parent_pid"
        case parentBundleID = "parent_bundle_id"
        case parentName = "parent_name"
    }
}

/// One JSON object (`--once`) or one JSON line (`--watch`).
public struct ProbeOutput: Codable, Equatable, Sendable {
    public var schema: Int
    /// Wall-clock seconds since 1970 (the Python side uses its own monotonic clock).
    public var ts: Double
    /// macOS version "major.minor.patch".
    public var macos: String
    public var api: String
    public var confidence: String
    /// Why the low-confidence path was used ("macos_lt_14", "process_objects_failed", "forced").
    public var reason: String?
    public var micUsers: [AppUser]
    /// Default input device running somewhere (any process, OBS included); nil if unreadable.
    public var micDeviceRunning: Bool?
    public var cameraActive: Bool
    /// Always empty: CoreMediaIO has no per-process attribution.
    public var cameraUsers: [AppUser]
    /// The frontmost app (low-confidence path only).
    public var frontmost: AppUser?
    /// Only with --titles: "ok", "permission_missing" or "unavailable".
    public var titlesStatus: String?
    /// Only with --titles and permission: PID (as string) → on-screen window titles.
    public var windowTitles: [String: [String]]?
    public var errors: [String]

    public init(
        ts: Double,
        macos: String,
        api: String,
        confidence: String,
        reason: String? = nil,
        micUsers: [AppUser] = [],
        micDeviceRunning: Bool? = nil,
        cameraActive: Bool = false,
        frontmost: AppUser? = nil,
        titlesStatus: String? = nil,
        windowTitles: [String: [String]]? = nil,
        errors: [String] = []
    ) {
        self.schema = schemaVersion
        self.ts = ts
        self.macos = macos
        self.api = api
        self.confidence = confidence
        self.reason = reason
        self.micUsers = micUsers
        self.micDeviceRunning = micDeviceRunning
        self.cameraActive = cameraActive
        self.cameraUsers = []
        self.frontmost = frontmost
        self.titlesStatus = titlesStatus
        self.windowTitles = windowTitles
        self.errors = errors
    }

    enum CodingKeys: String, CodingKey {
        case schema, ts, macos, api, confidence, reason
        case micUsers = "mic_users"
        case micDeviceRunning = "mic_device_running"
        case cameraActive = "camera_active"
        case cameraUsers = "camera_users"
        case frontmost
        case titlesStatus = "titles_status"
        case windowTitles = "window_titles"
        case errors
    }
}

/// JSON with sorted keys and a trailing newline (one line per object; nil fields are omitted).
public func encodeLine(_ output: ProbeOutput) throws -> Data {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    var data = try encoder.encode(output)
    data.append(0x0A)
    return data
}

/// Chromium/Electron helper processes: bundle `<app>.helper[.<kind>]`, name `<App> Helper (<kind>)`.
public enum Helper {
    /// `com.google.Chrome.helper.Renderer` → `com.google.Chrome`; nil if not a helper bundle ID.
    public static func parentBundleID(of bundleID: String) -> String? {
        var search = bundleID.startIndex..<bundleID.endIndex
        while let r = bundleID.range(of: ".helper", options: [.caseInsensitive], range: search) {
            let rest = bundleID[r.upperBound...]
            if rest.isEmpty || rest.hasPrefix(".") {
                let head = bundleID[..<r.lowerBound]
                return head.isEmpty ? nil : String(head)
            }
            search = r.upperBound..<bundleID.endIndex
        }
        return nil
    }

    /// `Google Chrome Helper (Renderer)` → `Google Chrome`; nil if not a helper name.
    public static func parentName(of name: String) -> String? {
        var search = name.startIndex..<name.endIndex
        while let r = name.range(of: " Helper", range: search) {
            let rest = name[r.upperBound...]
            if rest.isEmpty || (rest.hasPrefix(" (") && rest.hasSuffix(")")) {
                let head = name[..<r.lowerBound]
                return head.isEmpty ? nil : String(head)
            }
            search = r.upperBound..<name.endIndex
        }
        return nil
    }

    public static func looksLikeHelper(bundleID: String?, name: String?) -> Bool {
        if let bundleID, parentBundleID(of: bundleID) != nil { return true }
        if let name, parentName(of: name) != nil { return true }
        return false
    }
}

/// Command-line options.
public struct Options: Equatable, Sendable {
    public enum Mode: Equatable, Sendable { case once, watch, help, version }

    public var mode: Mode
    public var intervalMs: Int
    public var titles: Bool
    public var forceFallback: Bool

    public init(mode: Mode, intervalMs: Int = 1000, titles: Bool = false, forceFallback: Bool = false) {
        self.mode = mode
        self.intervalMs = intervalMs
        self.titles = titles
        self.forceFallback = forceFallback
    }
}

public enum OptionsError: Error, Equatable {
    case unknown(String)
    case missingValue(String)
    case badInterval(String)
    case noMode
}

public let usage = """
    usage: captation-probe --once [--titles] [--force-fallback]
           captation-probe --watch [--interval-ms N] [--titles] [--force-fallback]
           captation-probe --version | --help

      --once            print one JSON object and exit
      --watch           print one JSON line every --interval-ms (100…60000, default 1000)
      --titles          add window titles (needs the Screen Recording permission; never prompts)
      --force-fallback  use the low-confidence path (device running + frontmost app)
    """

public func parseOptions(_ args: [String]) -> Result<Options, OptionsError> {
    var mode: Options.Mode?
    var interval = 1000
    var titles = false
    var fallback = false
    var i = 0
    while i < args.count {
        let arg = args[i]
        switch arg {
        case "--once": mode = .once
        case "--watch": mode = .watch
        case "--help", "-h": mode = .help
        case "--version": mode = .version
        case "--titles": titles = true
        case "--force-fallback": fallback = true
        case "--interval-ms":
            guard i + 1 < args.count else { return .failure(.missingValue(arg)) }
            i += 1
            guard let value = Int(args[i]), (100...60000).contains(value) else {
                return .failure(.badInterval(args[i]))
            }
            interval = value
        default:
            return .failure(.unknown(arg))
        }
        i += 1
    }
    guard let mode else { return .failure(.noMode) }
    return .success(Options(mode: mode, intervalMs: interval, titles: titles, forceFallback: fallback))
}
