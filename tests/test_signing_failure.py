"""Compile the production signing adapter and commit gate with a harmless signer stub."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
handler = (root / 'RyukSign/Utilities/Handlers/ZsignHandler.swift').read_text()
handler = handler.replace('import ZsignSwift\n', '').replace('import UIKit\n', '')
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
    static var callbackError: Error? = nil
    static var disinjectResult = true
    static func removeDylibs(appExecutable: String, using: [String]) -> Bool { disinjectResult }
    static func sign(appPath: String, provisionPath: String = "", p12Path: String = "",
                     p12Password: String = "", entitlementsPath: String, adhoc: Bool = false,
                     removeProvision: Bool, completion: (Bool, Error?) -> Void) -> Bool {
        calls += 1
        if let callback { completion(callback, callbackError) }
        return result
    }
}
''' + handler + r'''
final class CommitGate {
    var _options = Options()
    var appCertificate: CertificatePair? = CertificatePair()
    let movedAppPath = URL(fileURLWithPath: "/unused-test-bundle")
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
        // Return value and callback are independent failure signals in the existing bridge.
        let nativeError = NSError(domain: "NativeSigner", code: 42)
        let cases: [(Bool, Bool?, Error?)] = [
            (false, nil, nil), (false, false, nil), (false, true, nil),
            (true, false, nil), (true, true, nativeError), (false, false, nativeError),
            (true, true, nil), (true, nil, nil)
        ]
        for (result, callback, error) in cases {
            Zsign.result = result
            Zsign.callback = callback
            Zsign.callbackError = error
            let expectFailure = !result || callback == false || error != nil
            let gate = CommitGate()
            var caught: Error?
            do { try await gate.modify() } catch { caught = error }
            assert((caught != nil) == expectFailure)
            if error != nil { assert((caught as NSError?)?.domain == "NativeSigner") }
            assert(gate.moves == (expectFailure ? 0 : 1))
            assert(gate.registrations == (expectFailure ? 0 : 1))
            assert(!StdoutCapture.shared.active)

            let adhoc = ZsignHandler(appUrl: gate.movedAppPath)
            caught = nil
            do { try await adhoc.adhocSign() } catch { caught = error }
            assert((caught != nil) == expectFailure)
            if error != nil { assert((caught as NSError?)?.domain == "NativeSigner") }
            assert(!StdoutCapture.shared.active)
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
    subprocess.run([str(binary)], check=True)
