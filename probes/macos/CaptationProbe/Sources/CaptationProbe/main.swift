// captation-probe: which processes use the microphone, and is a camera in use (stage 06).
//
// Public APIs only (no private frameworks, no responsibility_* calls):
// - Microphone per process (macOS 14+, PF-113): kAudioHardwarePropertyProcessObjectList, then per
//   process object kAudioProcessPropertyPID, kAudioProcessPropertyBundleID and
//   kAudioProcessPropertyIsRunningInput. Polled: property listeners for IsRunningInput are
//   reported not to fire (reported in Apple's developer forums), so no listener is used at all.
// - Fallback (PF-114, confidence "low"): kAudioDevicePropertyDeviceIsRunningSomewhere on the
//   default input device plus NSWorkspace.frontmostApplication.
// - Camera (PF-115): kCMIODevicePropertyDeviceIsRunningSomewhere over all CoreMediaIO devices.
// - Process identity (PF-116): proc_pidpath, proc_pidinfo(PROC_PIDTBSDINFO) for the parent PID,
//   NSRunningApplication for bundle ID and name. Helpers walk up the parent chain (PF-117).
// - Window titles (only with --titles, PF-118): CGWindowListCopyWindowInfo, read only when
//   CGPreflightScreenCaptureAccess() is true (it never prompts).
//
// Every identifier here is a pending fact until verified on the Mac (docs/runbook-macos.md).

import AppKit
import CaptationProbeCore
import CoreAudio
import CoreGraphics
import CoreMediaIO
import Darwin
import Foundation

// MARK: - CoreAudio

let systemObject = AudioObjectID(kAudioObjectSystemObject)

func audioAddress(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: AudioObjectPropertyScope(kAudioObjectPropertyScopeGlobal),
        mElement: AudioObjectPropertyElement(kAudioObjectPropertyElementMain)
    )
}

func readUInt32(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> UInt32? {
    var address = audioAddress(selector)
    var value: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    let status = AudioObjectGetPropertyData(object, &address, 0, nil, &size, &value)
    return status == noErr ? value : nil
}

func readPID(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> pid_t? {
    var address = audioAddress(selector)
    var value: pid_t = 0
    var size = UInt32(MemoryLayout<pid_t>.size)
    let status = AudioObjectGetPropertyData(object, &address, 0, nil, &size, &value)
    return status == noErr ? value : nil
}

/// CFString properties are returned retained (CoreAudio convention): take ownership.
func readString(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
    var address = audioAddress(selector)
    var value: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    let status = withUnsafeMutablePointer(to: &value) { pointer in
        AudioObjectGetPropertyData(object, &address, 0, nil, &size, pointer)
    }
    guard status == noErr, let string = value?.takeRetainedValue() else { return nil }
    let text = string as String
    return text.isEmpty ? nil : text
}

func readObjectList(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> [AudioObjectID]? {
    var address = audioAddress(selector)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(object, &address, 0, nil, &size) == noErr else { return nil }
    let itemSize = MemoryLayout<AudioObjectID>.size
    let count = Int(size) / itemSize
    if count == 0 { return [] }
    var ids = [AudioObjectID](repeating: 0, count: count)
    let status = ids.withUnsafeMutableBytes { buffer in
        AudioObjectGetPropertyData(object, &address, 0, nil, &size, buffer.baseAddress!)
    }
    guard status == noErr else { return nil }
    return Array(ids.prefix(Int(size) / itemSize))
}

// MARK: - Process identity

func executablePath(_ pid: pid_t) -> String? {
    var buffer = [CChar](repeating: 0, count: 4 * Int(MAXPATHLEN))
    let length = proc_pidpath(pid, &buffer, UInt32(buffer.count))
    return length > 0 ? String(cString: buffer) : nil
}

func parentPID(_ pid: pid_t) -> pid_t? {
    var info = proc_bsdinfo()
    let size = Int32(MemoryLayout<proc_bsdinfo>.size)
    let length = proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, size)
    return length == size ? pid_t(info.pbi_ppid) : nil
}

func lastComponent(_ path: String?) -> String? {
    guard let path else { return nil }
    return (path as NSString).lastPathComponent
}

/// The first ancestor (max. 4 hops, never launchd) that is not a helper itself.
func appAncestor(of pid: pid_t) -> (pid: pid_t, bundleID: String?, name: String?)? {
    var current = pid
    for _ in 0..<4 {
        guard let parent = parentPID(current), parent > 1 else { return nil }
        current = parent
        let app = NSRunningApplication(processIdentifier: current)
        let bundleID = app?.bundleIdentifier
        let name = app?.localizedName ?? lastComponent(executablePath(current))
        if !Helper.looksLikeHelper(bundleID: bundleID, name: name) {
            return (current, bundleID, name)
        }
    }
    return nil
}

func describe(pid: pid_t, bundleID: String?) -> AppUser {
    let app = NSRunningApplication(processIdentifier: pid)
    let path = executablePath(pid) ?? app?.executableURL?.path
    var user = AppUser(
        pid: pid,
        bundleID: bundleID ?? app?.bundleIdentifier,
        name: lastComponent(path),
        localizedName: app?.localizedName,
        path: path,
        ppid: parentPID(pid)
    )
    if Helper.looksLikeHelper(bundleID: user.bundleID, name: user.name ?? user.localizedName),
       let ancestor = appAncestor(of: pid)
    {
        user.parentPID = ancestor.pid
        user.parentBundleID = ancestor.bundleID
        user.parentName = ancestor.name
    }
    return user
}

// MARK: - Microphone

/// Processes whose CoreAudio process object is running input (PF-113); nil if unreadable.
@available(macOS 14.0, *)
func processObjectUsers(errors: inout [String]) -> [AppUser]? {
    let listSelector = AudioObjectPropertySelector(kAudioHardwarePropertyProcessObjectList)
    guard let objects = readObjectList(systemObject, listSelector) else {
        errors.append("process_object_list_unreadable")
        return nil
    }
    var users: [AppUser] = []
    var seen = Set<pid_t>()
    for object in objects {
        let runningSelector = AudioObjectPropertySelector(kAudioProcessPropertyIsRunningInput)
        guard let running = readUInt32(object, runningSelector), running != 0 else { continue }
        guard let pid = readPID(object, AudioObjectPropertySelector(kAudioProcessPropertyPID)),
              pid > 0, !seen.contains(pid)
        else { continue }
        seen.insert(pid)
        let bundleID = readString(object, AudioObjectPropertySelector(kAudioProcessPropertyBundleID))
        users.append(describe(pid: pid, bundleID: bundleID))
    }
    return users.sorted { $0.pid < $1.pid }
}

/// Default input device running somewhere (PF-114); nil if unreadable.
func defaultInputRunning() -> Bool? {
    let deviceSelector = AudioObjectPropertySelector(kAudioHardwarePropertyDefaultInputDevice)
    guard let device = readUInt32(systemObject, deviceSelector), device != 0 else { return nil }
    let runningSelector = AudioObjectPropertySelector(kAudioDevicePropertyDeviceIsRunningSomewhere)
    guard let running = readUInt32(AudioObjectID(device), runningSelector) else { return nil }
    return running != 0
}

func frontmostUser() -> AppUser? {
    guard let app = NSWorkspace.shared.frontmostApplication else { return nil }
    return describe(pid: app.processIdentifier, bundleID: app.bundleIdentifier)
}

// MARK: - Camera (CoreMediaIO)

func cmioAddress(_ selector: CMIOObjectPropertySelector) -> CMIOObjectPropertyAddress {
    CMIOObjectPropertyAddress(
        mSelector: selector,
        mScope: CMIOObjectPropertyScope(kCMIOObjectPropertyScopeGlobal),
        mElement: CMIOObjectPropertyElement(kCMIOObjectPropertyElementMain)
    )
}

/// Any CoreMediaIO device running somewhere (PF-115). No attribution is possible.
func cameraActive(errors: inout [String]) -> Bool {
    let system = CMIOObjectID(kCMIOObjectSystemObject)
    var address = cmioAddress(CMIOObjectPropertySelector(kCMIOHardwarePropertyDevices))
    var size: UInt32 = 0
    guard CMIOObjectGetPropertyDataSize(system, &address, 0, nil, &size) == noErr else {
        errors.append("cmio_devices_unreadable")
        return false
    }
    let itemSize = MemoryLayout<CMIOObjectID>.size
    let count = Int(size) / itemSize
    if count == 0 { return false }
    var devices = [CMIOObjectID](repeating: 0, count: count)
    var used: UInt32 = 0
    let status = devices.withUnsafeMutableBytes { buffer in
        CMIOObjectGetPropertyData(system, &address, 0, nil, size, &used, buffer.baseAddress!)
    }
    guard status == noErr else {
        errors.append("cmio_devices_unreadable")
        return false
    }
    let runningSelector = CMIOObjectPropertySelector(kCMIODevicePropertyDeviceIsRunningSomewhere)
    for device in devices.prefix(Int(used) / itemSize) {
        var runningAddress = cmioAddress(runningSelector)
        var running: UInt32 = 0
        var runningUsed: UInt32 = 0
        let result = CMIOObjectGetPropertyData(
            device, &runningAddress, 0, nil, UInt32(MemoryLayout<UInt32>.size), &runningUsed, &running
        )
        if result == noErr && running != 0 { return true }
    }
    return false
}

// MARK: - Window titles (optional)

func windowTitles() -> (status: String, titles: [String: [String]]?) {
    guard CGPreflightScreenCaptureAccess() else { return (TitlesStatus.permissionMissing, nil) }
    let options: CGWindowListOption = [.optionAll, .excludeDesktopElements]
    guard let list = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] else {
        return (TitlesStatus.unavailable, nil)
    }
    var titles: [String: Set<String>] = [:]
    for window in list {
        guard let pid = (window[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value,
              (window[kCGWindowLayer as String] as? NSNumber)?.intValue == 0,
              let name = window[kCGWindowName as String] as? String, !name.isEmpty
        else { continue }
        titles[String(pid), default: []].insert(name)
    }
    return (TitlesStatus.ok, titles.mapValues { $0.sorted() })
}

// MARK: - One snapshot

func macOSVersion() -> String {
    let v = ProcessInfo.processInfo.operatingSystemVersion
    return "\(v.majorVersion).\(v.minorVersion).\(v.patchVersion)"
}

func collect(_ options: Options) -> (ProbeOutput, ExitCode) {
    var errors: [String] = []
    var reason: String?
    var processUsers: [AppUser]?
    if options.forceFallback {
        reason = "forced"
    } else if #available(macOS 14.0, *) {
        processUsers = processObjectUsers(errors: &errors)
        if processUsers == nil { reason = "process_objects_failed" }
    } else {
        reason = "macos_lt_14"
    }
    let deviceRunning = defaultInputRunning()
    var output = ProbeOutput(
        ts: Date().timeIntervalSince1970,
        macos: macOSVersion(),
        api: processUsers != nil ? API.processObjects : API.deviceRunning,
        confidence: processUsers != nil ? Confidence.high : Confidence.low,
        reason: reason,
        micDeviceRunning: deviceRunning
    )
    if let processUsers {
        output.micUsers = processUsers
    } else if deviceRunning == true, let front = frontmostUser() {
        output.micUsers = [front]
        output.frontmost = front
    }
    output.cameraActive = cameraActive(errors: &errors)
    if options.titles {
        let (status, titles) = windowTitles()
        output.titlesStatus = status
        output.windowTitles = titles
    }
    output.errors = errors
    let code: ExitCode = (processUsers == nil && deviceRunning == nil) ? .apiUnavailable : .ok
    return (output, code)
}

// MARK: - Output

/// Write all bytes to stdout; false if the reader is gone (EPIPE).
func writeAll(_ data: Data) -> Bool {
    data.withUnsafeBytes { raw -> Bool in
        guard var pointer = raw.baseAddress else { return true }
        var left = raw.count
        while left > 0 {
            let written = Darwin.write(STDOUT_FILENO, pointer, left)
            if written < 0 {
                if errno == EINTR { continue }
                return false
            }
            pointer = pointer.advanced(by: written)
            left -= written
        }
        return true
    }
}

func emit(_ output: ProbeOutput) -> Bool {
    guard let data = try? encodeLine(output) else {
        FileHandle.standardError.write(Data("captation-probe: cannot encode output\n".utf8))
        return true
    }
    return writeAll(data)
}

// MARK: - Main

// SIGPIPE stays at its default (terminate): when the agent dies, the next write ends the probe.
let arguments = Array(CommandLine.arguments.dropFirst())
let options: Options
switch parseOptions(arguments) {
case let .success(parsed):
    options = parsed
case let .failure(error):
    FileHandle.standardError.write(Data("captation-probe: \(error)\n\(usage)\n".utf8))
    exit(ExitCode.usage.rawValue)
}

switch options.mode {
case .help:
    print(usage)
    exit(ExitCode.ok.rawValue)
case .version:
    print("captation-probe \(probeVersion)")
    exit(ExitCode.ok.rawValue)
case .once:
    let (output, code) = collect(options)
    _ = emit(output)
    exit(code.rawValue)
case .watch:
    let interval = Double(options.intervalMs) / 1000
    let originalParent = getppid()
    func tick() {
        // Re-parented to launchd: the agent is gone, so stop.
        if getppid() != originalParent { exit(ExitCode.ok.rawValue) }
        let (output, _) = collect(options)
        if !emit(output) { exit(ExitCode.ok.rawValue) }
    }
    tick()
    // The main run loop also delivers NSWorkspace updates (frontmost app) to this process.
    let timer = Timer(timeInterval: interval, repeats: true) { _ in tick() }
    RunLoop.main.add(timer, forMode: .common)
    RunLoop.main.run()
}
