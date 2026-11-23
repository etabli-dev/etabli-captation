// Unit tests for the pure logic of captation-probe (`swift test`, needs Xcode for XCTest).
#if canImport(XCTest)
import XCTest
@testable import CaptationProbeCore

final class CoreTests: XCTestCase {
    func testHelperBundleIDs() {
        XCTAssertEqual(Helper.parentBundleID(of: "com.google.Chrome.helper"), "com.google.Chrome")
        XCTAssertEqual(Helper.parentBundleID(of: "com.google.Chrome.helper.Renderer"), "com.google.Chrome")
        XCTAssertEqual(
            Helper.parentBundleID(of: "org.whispersystems.signal-desktop.helper.GPU"),
            "org.whispersystems.signal-desktop"
        )
        XCTAssertEqual(Helper.parentBundleID(of: "com.microsoft.edgemac.HELPER"), "com.microsoft.edgemac")
        XCTAssertNil(Helper.parentBundleID(of: "us.zoom.xos"))
        XCTAssertNil(Helper.parentBundleID(of: "com.example.helperapp"))
        XCTAssertNil(Helper.parentBundleID(of: ".helper"))
    }

    func testHelperNames() {
        XCTAssertEqual(Helper.parentName(of: "Google Chrome Helper"), "Google Chrome")
        XCTAssertEqual(Helper.parentName(of: "Google Chrome Helper (Renderer)"), "Google Chrome")
        XCTAssertNil(Helper.parentName(of: "zoom.us"))
        XCTAssertNil(Helper.parentName(of: "Helper Tool"))
        XCTAssertTrue(Helper.looksLikeHelper(bundleID: nil, name: "Signal Helper (GPU)"))
        XCTAssertFalse(Helper.looksLikeHelper(bundleID: "us.zoom.xos", name: "zoom.us"))
    }

    func testEncodingUsesSnakeCaseAndOmitsNil() throws {
        let user = AppUser(pid: 42, bundleID: "us.zoom.xos", name: "zoom.us")
        let output = ProbeOutput(
            ts: 1, macos: "15.1.0", api: API.processObjects, confidence: Confidence.high,
            micUsers: [user], micDeviceRunning: true, cameraActive: true
        )
        let data = try encodeLine(output)
        XCTAssertEqual(data.last, 0x0A)
        XCTAssertFalse(data.dropLast().contains(0x0A), "one object per line")
        let doc = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(doc["schema"] as? Int, 1)
        XCTAssertEqual(doc["camera_active"] as? Bool, true)
        XCTAssertEqual(doc["mic_device_running"] as? Bool, true)
        XCTAssertNil(doc["reason"])
        XCTAssertNil(doc["window_titles"])
        let users = try XCTUnwrap(doc["mic_users"] as? [[String: Any]])
        XCTAssertEqual(users.first?["bundle_id"] as? String, "us.zoom.xos")
        XCTAssertNil(users.first?["parent_bundle_id"])
        XCTAssertEqual((doc["camera_users"] as? [Any])?.count, 0)
    }

    func testOptions() {
        XCTAssertEqual(try parseOptions(["--once"]).get(), Options(mode: .once))
        XCTAssertEqual(
            try parseOptions(["--watch", "--interval-ms", "500", "--titles"]).get(),
            Options(mode: .watch, intervalMs: 500, titles: true)
        )
        XCTAssertEqual(parseOptions([]), .failure(.noMode))
        XCTAssertEqual(parseOptions(["--watch", "--interval-ms"]), .failure(.missingValue("--interval-ms")))
        XCTAssertEqual(parseOptions(["--watch", "--interval-ms", "5"]), .failure(.badInterval("5")))
        XCTAssertEqual(parseOptions(["--bogus"]), .failure(.unknown("--bogus")))
    }

    /// The sample the Python tests replay must decode with this model (schema agreement).
    func testSampleDecodes() throws {
        let sample = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Samples/watch-sample.jsonl")
        let text = try String(contentsOf: sample, encoding: .utf8)
        let lines = text.split(separator: "\n")
        XCTAssertGreaterThanOrEqual(lines.count, 5)
        for line in lines {
            let output = try JSONDecoder().decode(ProbeOutput.self, from: Data(line.utf8))
            XCTAssertEqual(output.schema, schemaVersion)
            XCTAssertTrue([API.processObjects, API.deviceRunning].contains(output.api))
        }
    }
}
#endif
