"""Exercise production database registration/cleanup with delayed storage on macOS.

Uses real temporary bundles and production metadata/error helpers. Only storage
and the Unsigned directory root are substituted; no app data or network is used.
"""
from pathlib import Path
import os
import re
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "RyukSign/Utilities/Handlers/AppFileHandler.swift").read_text()
FILE_HELPERS = (ROOT / "NimbleKit/Sources/NimbleExtensions/FileManager/FileManager+shortcuts.swift").read_text()
BUNDLE_HELPERS = (ROOT / "NimbleKit/Sources/NimbleExtensions/Bundle/Bundle+keys.swift").read_text()


def declaration(source, signature):
    start = source.index(signature)
    end = source.index("{", start) + 1
    depth = 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


DATABASE = declaration(SOURCE, "\tfunc addToDatabase()")
# Accelerate only the historical deadline if restored; the production method is
# otherwise compiled unchanged. A 120 ms successful save must not become a timeout.
DATABASE = re.sub(r"(asyncAfter\(deadline:\s*\.now\(\)\s*\+\s*)10\b",
                  r"\g<1>0.02", DATABASE)
HANDLER_METHODS = "\n".join([
    declaration(SOURCE, "\tprivate func _error("), DATABASE,
    declaration(SOURCE, "\tprivate func _directory()"),
    declaration(SOURCE, "\tfunc clean()"),
])
FILE_METHODS = "\n".join(declaration(FILE_HELPERS, signature) for signature in [
    "\tpublic func getPath(", "\tpublic func removeFileIfNeeded(",
])

PROGRAM = r'''
import Foundation
import OSLog
import Darwin

extension Logger {
 static let misc = Logger(subsystem: "ImportDatabaseLifecycleTest", category: "test")
}
enum Fixture {
 static let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
}
extension FileManager {
 func unsigned(_ uuid: String) -> URL {
  Fixture.root.appendingPathComponent("Unsigned").appendingPathComponent(uuid)
 }
''' + FILE_METHODS + "\n}\n" + BUNDLE_HELPERS + "\n" + declaration(SOURCE, "struct ImportError:") + r'''

protocol AppInfoPresentable { var uuid: String { get } }
struct Row: AppInfoPresentable { let uuid: String }
final class Storage: @unchecked Sendable {
 static let shared = Storage()
 private let lock = NSLock()
 private var saved: [String: Row] = [:]
 private var completed: Set<String> = []
 private var readbacks: [String: Int] = [:]

 func addImported(uuid: String, appName: String?, appIdentifier: String?,
                  appVersion: String?, appIcon: String?, appDescription: String?,
                  completion: @escaping (Error?) -> Void) {
  // These values come from a real on-disk Bundle through the production helpers.
  precondition(appName == "Fixture App" && appIdentifier == "test.import.\(uuid)")
  precondition(appVersion == "1.2.3" && appIcon == "FixtureIcon")
  precondition(appDescription == "Fixture description")
  DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 0.12) {
   // The payload must still exist at the instant the actual save completes.
   precondition(FileManager.default.fileExists(atPath:
    FileManager.default.unsigned(uuid).appendingPathComponent("Fixture.app/Info.plist").path))
   self.lock.lock()
   if uuid != "failure" { self.saved[uuid] = Row(uuid: uuid) }
   self.completed.insert(uuid)
   self.lock.unlock()
   completion(uuid == "failure" ? NSError(domain: "Fixture.Database", code: 73,
    userInfo: [NSLocalizedDescriptionKey: "fixture save failed"]) : nil)
  }
 }
 func app(withUuid uuid: String) -> AppInfoPresentable? {
  MainActor.preconditionIsolated()
  dispatchPrecondition(condition: .onQueue(.main))
  precondition(Thread.isMainThread)
  lock.lock(); defer { lock.unlock() }
  precondition(completed.contains(uuid), "Readback preceded actual completion")
  readbacks[uuid, default: 0] += 1
  return uuid == "missing-readback" ? nil : saved[uuid]
 }
 func state(_ uuid: String) -> (completed: Bool, saved: Bool, readbacks: Int) {
  lock.lock(); defer { lock.unlock() }
  return (completed.contains(uuid), saved[uuid] != nil, readbacks[uuid, default: 0])
 }
}
final class Handler: @unchecked Sendable {
 let _fileManager = FileManager.default
 let _uuid: String
 let _uniqueWorkDir: URL
 let _ipa: URL
 let _fileName: String
 let _appDescription: String? = "Fixture description"
 private var _didAddToDatabase = false
 var didAddToDatabase: Bool { _didAddToDatabase }
 let originalIPA: URL

 init(_ mode: String) throws {
  _uuid = mode
  _fileName = "\(mode).ipa"
  originalIPA = Fixture.root.appendingPathComponent(_fileName)
  _uniqueWorkDir = Fixture.root.appendingPathComponent("work-\(mode)")
  _ipa = _uniqueWorkDir.appendingPathComponent(_fileName)
  try _fileManager.createDirectory(at: _uniqueWorkDir, withIntermediateDirectories: true)
  try Data("original archive".utf8).write(to: originalIPA)
  try _fileManager.copyItem(at: originalIPA, to: _ipa)
  let app = _fileManager.unsigned(mode).appendingPathComponent("Fixture.app")
  try _fileManager.createDirectory(at: app, withIntermediateDirectories: true)
  let metadata: [String: Any] = [
   "CFBundleDisplayName": "Fixture App", "CFBundleIdentifier": "test.import.\(mode)",
   "CFBundleShortVersionString": "1.2.3", "CFBundleVersion": "123",
   "CFBundlePackageType": "APPL", "CFBundleExecutable": "Fixture",
   "CFBundleIcons": ["CFBundlePrimaryIcon": ["CFBundleIconFiles": ["FixtureIcon"]]],
  ]
  let plist = try PropertyListSerialization.data(fromPropertyList: metadata, format: .xml, options: 0)
  try plist.write(to: app.appendingPathComponent("Info.plist"))
 }
''' + HANDLER_METHODS + r'''
}

@main struct Check {
 @MainActor static func verifyCleanup(_ handler: Handler, retainsPayload: Bool) async throws {
  try await handler.clean()
  precondition(!FileManager.default.fileExists(atPath: handler._uniqueWorkDir.path))
  precondition(FileManager.default.fileExists(atPath:
   FileManager.default.unsigned(handler._uuid).appendingPathComponent("Fixture.app/Info.plist").path) == retainsPayload)
  let original = try Data(contentsOf: handler.originalIPA)
  precondition(original == Data("original archive".utf8), "Cleanup touched original IPA")
 }
 @MainActor static func run() async throws {
  let success = try Handler("success")
  precondition(!success.didAddToDatabase)
  let started = Date()
  let row = try await success.addToDatabase()
  precondition(Date().timeIntervalSince(started) >= 0.10)
  let successState = Storage.shared.state("success")
  precondition(successState.completed && successState.saved && successState.readbacks == 1)
  precondition(row.uuid == "success" && success.didAddToDatabase)
  try await verifyCleanup(success, retainsPayload: true)

  let failure = try Handler("failure")
  do {
   _ = try await failure.addToDatabase()
   fatalError("Failed save reported success")
  } catch let error as ImportError {
   precondition(error.step == .database && error.fileName == "failure.ipa")
   precondition(error.id == "failure" && error.reason == "fixture save failed")
   precondition(error.underlying?.domain == "Fixture.Database" && error.underlying?.code == 73)
   precondition(error.localizedDescription.contains("Detail: Fixture.Database (73)"))
  }
  let failureState = Storage.shared.state("failure")
  precondition(failureState.completed && !failureState.saved && failureState.readbacks == 0)
  precondition(!failure.didAddToDatabase)
  try await verifyCleanup(failure, retainsPayload: false)

  // A successful save followed by failed readback is still registered: cleanup
  // must retain its payload even though addToDatabase reports a contextual error.
  let missing = try Handler("missing-readback")
  do {
   _ = try await missing.addToDatabase()
   fatalError("Missing readback reported success")
  } catch let error as ImportError {
   precondition(error.step == .database && error.fileName == "missing-readback.ipa")
   precondition(error.reason.contains("saved but could not be read back"))
   precondition(error.underlying == nil)
  }
  let missingState = Storage.shared.state("missing-readback")
  precondition(missingState.completed && missingState.saved && missingState.readbacks == 1)
  precondition(missing.didAddToDatabase)
  try await verifyCleanup(missing, retainsPayload: true)
  // Cleanup of another import must not remove a previously registered payload.
  precondition(FileManager.default.fileExists(atPath:
   FileManager.default.unsigned("success").appendingPathComponent("Fixture.app/Info.plist").path))
  print("PASS: slow save awaited; main-actor readback; saved flag; registered payload retained; failure context and cleanup; originals preserved; missing readback retains saved payload")
 }
 static func main() {
  Task { @MainActor in
   do { try await run(); exit(0) }
   catch { fatalError("Unexpected database lifecycle result: \(error)") }
  }
  RunLoop.main.run()
 }
}
'''


def test_import_database_lifecycle():
    with tempfile.TemporaryDirectory(prefix="ryuksign-import-database-") as directory:
        work = Path(directory)
        for name in ("data", "preferences", "tmp", "module-cache"):
            (work / name).mkdir()
        env = dict(os.environ, CFFIXED_USER_HOME=str(work / "preferences"),
                   TMPDIR=str(work / "tmp"), CLANG_MODULE_CACHE_PATH=str(work / "module-cache"))
        (work / "check.swift").write_text(PROGRAM)
        subprocess.run(["swiftc", "-swift-version", "5", "-parse-as-library",
                        "-module-cache-path", str(work / "module-cache"),
                        str(work / "check.swift"), "-o", str(work / "check")],
                       check=True, env=env, timeout=90)
        subprocess.run([str(work / "check"), str(work / "data")],
                       check=True, env=env, timeout=15)


if __name__ == "__main__":
    test_import_database_lifecycle()
