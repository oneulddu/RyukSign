"""Compile the production signing adapter and commit gate with a harmless signer stub."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
handler = (root / 'RyukSign/Utilities/Handlers/ZsignHandler.swift').read_text()
handler = handler.replace('import Zsign\n', '').replace('import UIKit\n', '')
workflow = (root / 'RyukSign/Utilities/Handlers/SigningHandler.swift').read_text()
start = workflow.index('\t\tlet handler = ZsignHandler(')
end = workflow.index('\n\t}\n', start)
commit_gate = workflow[start:end]

program = r'''
import Foundation
struct Options {
    enum Mode { case `default`, onlyModify }
    var signingOption = Mode.default
    var disInjectionFiles: [String] = []
    var appEntitlementsFile: URL? = nil
    var removeProvisioning = false
    var blockSandboxEscape = false
}
struct TweakHandler {
    init(app: URL, options: Options) {}
    func injectSandboxEscapeGuard() throws { fatalError("Guard not requested") }
}
final class OptionsManager { static let shared = OptionsManager(); var options = Options() }
final class CertificatePair { var password: String? = "test"; var nickname: String? = "test" }
enum SigningFileHandlerError: Error { case missingCertifcate, signFailed, disinjectFailed }
extension String {
    static func localized(_ value: String, arguments: String...) -> String { value }
}
extension Bundle { var exec: String? { nil } }
final class SigningLog { static let shared = SigningLog(); func info(_ value: String) {} }
final class StdoutCapture {
    static let shared = StdoutCapture()
    var active = false
    func start(_ receive: (String) -> Void) { assert(!active); active = true }
    func stop() { assert(active); active = false }
}
final class Storage {
    enum Kind { case provision, certificate }
    struct Profile { var Name = "test" }
    static let shared = Storage()
    func getFile(_ kind: Kind, from: CertificatePair) -> URL? { nil }
    func getProvisionFileDecoded(for: CertificatePair) -> Profile? { nil }
}
enum Zsign {
    static var result = false
    static var callback: Bool? = nil
    static var calls = 0
    static var unsigned: String?
    static func checkSigned(appExecutable: String) -> Bool {
        URL(fileURLWithPath: appExecutable).lastPathComponent != unsigned
    }
    static var provisioningRemoved: Bool?
    static var disinjectResult = true
    static func removeDylibs(appExecutable: String, using: [String]) -> Bool { disinjectResult }
    static func sign(appPath: String, provisionPath: String = "", p12Path: String = "",
                     p12Password: String = "", entitlementsPath: String, adhoc: Bool = false,
                     removeProvision: Bool, completion: ((Bool) -> Void)? = nil) -> Bool {
        calls += 1
        provisioningRemoved = removeProvision
        if let callback { completion?(callback) }
        return result
    }
}
''' + handler + r'''
final class CommitGate {
    var _options = Options()
    var appCertificate: CertificatePair? = CertificatePair()
    let movedAppPath = URL(fileURLWithPath: CommandLine.arguments[1])
    var moves = 0
    var registrations = 0
    func move() async throws { moves += 1 }
    func addToDatabase() async throws { registrations += 1 }
    func modify() async throws {
''' + commit_gate + r'''
    }
}
@main struct Check {
    static func main() async throws {
        let bundle = CommitGate().movedAppPath
        let resources = bundle.appendingPathComponent("_CodeSignature")
        try FileManager.default.createDirectory(at: resources, withIntermediateDirectories: true)
        try Data("resources".utf8).write(to: resources.appendingPathComponent("CodeResources"))
        let plist: NSDictionary = ["CFBundleExecutable": "Fixture"]
        assert(plist.write(to: bundle.appendingPathComponent("Info.plist"), atomically: true))
        Zsign.result = true
        Zsign.callback = true
        func mustNotPublish(_ reason: String) async throws {
            let gate = CommitGate()
            do { try await gate.modify() } catch {
                guard gate.moves == 0, gate.registrations == 0 else { exit(1) }
                return
            }
            print("FAIL: published \(reason)"); exit(1)
        }
        try await mustNotPublish("missing main executable")
        let exe = bundle.appendingPathComponent("Fixture")
        try FileManager.default.createDirectory(at: exe, withIntermediateDirectories: false)
        try await mustNotPublish("directory instead of executable")
        try FileManager.default.removeItem(at: exe)
        let magic = Data([0xcf, 0xfa, 0xed, 0xfe])
        try magic.write(to: exe)
        try FileManager.default.setAttributes([.posixPermissions: 0], ofItemAtPath: exe.path)
        try await mustNotPublish("unreadable main executable")
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: exe.path)
        Zsign.unsigned = "Fixture"
        try await mustNotPublish("unsigned main executable")
        Zsign.unsigned = "Resource.o"
        let resourceApp = CommitGate()
        do { try await resourceApp.modify() } catch {
            print("FAIL: rejected a valid app with unsigned resources: \(error)"); exit(1)
        }
        assert(resourceApp.moves == 1 && resourceApp.registrations == 1)
        // Return value and callback are independent failure signals in the existing bridge.
        let cases: [(Bool, Bool?)] = [
            (false, nil), (false, false), (false, true),
            (true, false), (true, true), (true, nil)
        ]
        for (result, callback) in cases {
            Zsign.result = result
            Zsign.callback = callback
            let expectFailure = !result || callback == false
            let gate = CommitGate()
            var caught: Error?
            do { try await gate.modify() } catch { caught = error }
            assert((caught != nil) == expectFailure)
            if expectFailure { assert(caught as? SigningFileHandlerError == .signFailed) }
            assert(gate.moves == (expectFailure ? 0 : 1))
            assert(gate.registrations == (expectFailure ? 0 : 1))
            assert(!StdoutCapture.shared.active)

            let adhoc = ZsignHandler(appUrl: gate.movedAppPath)
            caught = nil
            do { try await adhoc.adhocSign() } catch { caught = error }
            assert((caught != nil) == expectFailure)
            if expectFailure { assert(caught as? SigningFileHandlerError == .signFailed) }
            assert(!StdoutCapture.shared.active)
        }
        Zsign.result = true
        Zsign.callback = true
        for remove in [false, true] {
            let gate = CommitGate()
            gate._options.removeProvisioning = remove
            Zsign.provisioningRemoved = nil
            try await gate.modify()
            guard Zsign.provisioningRemoved == remove else {
                print("FAIL: certificate signer received incorrect removeProvision"); exit(1)
            }
            Zsign.provisioningRemoved = nil
            try await ZsignHandler(appUrl: gate.movedAppPath, options: gate._options).adhocSign()
            guard Zsign.provisioningRemoved == remove else {
                print("FAIL: ad-hoc signer received incorrect removeProvision"); exit(1)
            }
        }
        // Disinjection failure must also keep output uncommitted.
        let disinject = CommitGate()
        disinject._options.disInjectionFiles = ["remove.dylib"]
        Zsign.disinjectResult = false
        let previousCalls = Zsign.calls
        do { try await disinject.modify(); assertionFailure("Disinjection failure accepted") }
        catch SigningFileHandlerError.disinjectFailed {}
        assert(disinject.moves == 0 && disinject.registrations == 0 && Zsign.calls == previousCalls)
        Zsign.disinjectResult = true
        let missing = CommitGate()
        missing.appCertificate = nil
        let calls = Zsign.calls
        do { try await missing.modify(); assertionFailure("Missing certificate accepted") }
        catch SigningFileHandlerError.missingCertifcate {}
        assert(Zsign.calls == calls && missing.registrations == 0)

        // Modify-only remains independent of signing credentials and native failure.
        Zsign.result = false
        Zsign.callback = false
        missing._options.signingOption = .onlyModify
        try await missing.modify()
        assert(Zsign.calls == calls && missing.registrations == 1)
        print("PASS: early/later signing failures block move and registration; success, ad-hoc, missing certificate, modify-only, and log cleanup")
    }
}
'''
with tempfile.TemporaryDirectory(prefix='ryuksign-signing-check-') as directory:
    source = Path(directory) / 'Check.swift'
    binary = Path(directory) / 'check'
    source.write_text(program)
    subprocess.run(['swiftc', '-parse-as-library', str(source), '-o', str(binary)], check=True)
    bundle = Path(directory) / 'Fixture.app'
    (bundle / 'Templates' / 'Starter.app').mkdir(parents=True)
    subprocess.run(['xcrun', 'clang', '-x', 'c', '-c', '-', '-o', str(bundle / 'Resource.o')],
                   input=b'int fixture_resource;\n', check=True)
    subprocess.run([str(binary), str(bundle)], check=True)
