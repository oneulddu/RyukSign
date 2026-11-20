"""Compile production options and entitlement preparation with disposable plist data.

Like test_signing_failure.py, extract production methods because the full iOS
handler cannot be imported by a macOS command-line fixture. Platform services are
stubs; entitlement selection, transformations, persistence and gating are real.
"""
from pathlib import Path
import subprocess
import tempfile
import sys

root = Path(__file__).resolve().parents[1]
options = (root / 'RyukSign/Backend/Observable/OptionsManager.swift').read_text()
options = options[options.index('struct Options:'):]
handler = (root / 'RyukSign/Utilities/Handlers/SigningHandler.swift').read_text()
start = handler.index('\t\tif _options.signingOption == .default, _options.removeAppGroups')
gate = handler[start:handler.index('\n\t\tlet handler = ZsignHandler(', start)]
start = handler.index('\t@MainActor\n\tprivate func _isolateKeychainGroups')
methods = handler[start:handler.index('\n\tprivate func _modifyDict(', start)]
if '--mutation' in sys.argv:
    # Break the removal rule in the disposable compiled copy, never the repository.
    methods = methods.replace('entitlements.removeObject(forKey: key)', '')

program = r'''
import Foundation
struct TweakInjectionSpec: Codable, Equatable {}
extension String {
    static func localized(_ value: String, arguments: String...) -> String { value }
}
final class CertificatePair {}
struct EntitlementValue { let value: Any }
struct Profile { let Entitlements: [String: EntitlementValue] }
final class Storage {
    static let shared = Storage()
    var profile: Profile?
    func getProvisionFileDecoded(for cert: CertificatePair) -> Profile? { profile }
}
struct SigningValidationError: LocalizedError {
    let errorDescription: String?
    init(_ reason: String) { errorDescription = reason }
}
final class SigningLog {
    static let shared = SigningLog()
    func info(_ value: String) {}
}
enum MachOEntitlements {
    static func keychainAccessGroups(forExecutableAt url: URL) -> [String] { [] }
}
struct App { var identifier: String? = "example.app" }
''' + options + r'''
final class Preparation {
    var _options = Options.defaultOptions
    var _app = App()
    var appCertificate: CertificatePair?
    let _uniqueWorkDir: URL
    init(_ work: URL) { _uniqueWorkDir = work }
    func prepare() async throws {
        let movedAppPath = _uniqueWorkDir
''' + gate + r'''
    }
''' + methods + r'''
}
struct Failure: Error, CustomStringConvertible {
    let description: String
}
func require(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() { throw Failure(description: message) }
}
@main struct Check {
    static func main() async {
        do {
            try await run()
            print("PASS: app group removal, preserved entitlements/source, keychain compatibility, old options and modify-only")
        } catch {
            fputs("FAIL: \(error)\n", stderr)
            exit(1)
        }
    }
    static func run() async throws {
        let work = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        let source = work.appendingPathComponent("source.plist")
        let original: NSDictionary = [
            "com.apple.security.application-groups": ["group.example.shared"],
            "application-identifier": "ABCDEFGHIJ.example.app",
            "keychain-access-groups": ["ABCDEFGHIJ.*"],
            "aps-environment": "development",
            "get-task-allow": true
        ]
        let bytes = try PropertyListSerialization.data(fromPropertyList: original, format: .xml, options: 0)
        try bytes.write(to: source)

        let legacy = try JSONDecoder().decode(Options.self, from: Data(#"{"fileSharing":true}"#.utf8))
        try require(!legacy.removeAppGroups && legacy.fileSharing, "Legacy options must retain values with removal off")
        try require(!Options.defaultOptions.removeAppGroups, "Removal must be opt-in")
        var enabled = Options.defaultOptions
        enabled.removeAppGroups = true
        let roundTrip = try JSONDecoder().decode(Options.self, from: JSONEncoder().encode(enabled))
        try require(roundTrip.removeAppGroups, "Enabled option must persist")

        let preparation = Preparation(work)
        preparation._options.removeAppGroups = true
        preparation.appCertificate = CertificatePair()
        do {
            try await preparation.prepare()
            throw Failure(description: "Unreadable profile accepted")
        } catch is SigningValidationError {}
        Storage.shared.profile = Profile(Entitlements: ["get-task-allow": EntitlementValue(value: true)])
        try await preparation.prepare()
        try require(preparation._options.appEntitlementsFile == nil, "No groups should need no replacement")
        let bad = work.appendingPathComponent("invalid.plist")
        try Data("not a plist".utf8).write(to: bad)
        preparation._options.appEntitlementsFile = bad
        do {
            try await preparation.prepare()
            throw Failure(description: "Unreadable selected entitlements fell back to the profile")
        } catch is SigningValidationError {}

        for (name, removal, isolation, modifyOnly) in [
            ("off", false, false, false),
            ("remove", true, false, false),
            ("isolated", true, true, false),
            ("modify-only", true, false, true),
            ("modify-isolated", true, true, true)
        ] {
            let directory = work.appendingPathComponent(name, isDirectory: true)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let preparation = Preparation(directory)
            preparation._options.appEntitlementsFile = source
            preparation._options.removeAppGroups = removal
            preparation._options.keychainIsolation = isolation
            preparation._options.signingOption = modifyOnly ? .onlyModify : .default
            try await preparation.prepare()
            let output = try Data(contentsOf: preparation._options.appEntitlementsFile!)
            let actual = try PropertyListSerialization.propertyList(from: output, format: nil) as! NSDictionary
            let expected = original.mutableCopy() as! NSMutableDictionary
            if removal && !modifyOnly {
                expected.removeObject(forKey: "com.apple.security.application-groups")
            }
            if isolation {
                expected["keychain-access-groups"] = ["ABCDEFGHIJ.example.app"]
            }
            try require(actual == expected, "\(name): unexpected entitlement content")
            let unchangedSource = try Data(contentsOf: source)
            try require(unchangedSource == bytes, "\(name): caller-owned source changed")
            if !isolation && (!removal || modifyOnly) {
                try require(preparation._options.appEntitlementsFile == source, "\(name): ineffective option wrote a replacement")
            }
        }
    }
}
'''

with tempfile.TemporaryDirectory(prefix='ryuksign-app-groups-') as temporary:
    work = Path(temporary)
    swift = work / 'Check.swift'
    swift.write_text(program)
    binary = work / 'check'
    subprocess.run(['xcrun', 'swiftc', '-parse-as-library',
                    '-module-cache-path', str(work / 'modules'),
                    str(swift), '-o', str(binary)], check=True)
    subprocess.run([str(binary), str(work)], check=True)
