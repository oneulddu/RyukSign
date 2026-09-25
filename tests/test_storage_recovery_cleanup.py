"""Execute production scan/snapshot/deletion guards against isolated filesystem roots."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / "RyukSign/Backend/Observable/StorageManager.swift").read_text()
models = source[source.index("enum StorageCategory:"):source.index("// MARK: - Manager")]
scanner = source[source.index("struct AppDescriptor {"):]
# Assert execution context at the actual bulk-deletion entry point; keep its body intact.
scanner = scanner.replace(
    "static func clear(_ category: StorageCategory, _ library: LibrarySnapshot) {",
    "static func clear(_ category: StorageCategory, _ library: LibrarySnapshot) {\n"
    "        if category == .leftovers { dispatchPrecondition(condition: .notOnQueue(.main)) }",
)
start = source.index("\tfunc delete(")
methods = source[start:source.index("\n}\n", start)]
program = r'''
import Foundation
let testRoot = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
protocol SortableItem {}
extension String { static func localized(_ value: String) -> String { value } }
''' + models + scanner + r'''
extension StorageCategory { var title: String { rawValue } }
extension FileManager {
    var signed: URL { testRoot.appendingPathComponent("Documents/Signed") }
    var unsigned: URL { testRoot.appendingPathComponent("Documents/Unsigned") }
    var certificates: URL { testRoot.appendingPathComponent("Documents/Certificates") }
    var archives: URL { testRoot.appendingPathComponent("Documents/Archives") }
    var tweaksLibrary: URL { testRoot.appendingPathComponent("Documents/Tweaks") }
    var logs: URL { testRoot.appendingPathComponent("Documents/Logs") }
    var webManagerInbox: URL { testRoot.appendingPathComponent("Documents/WebManager") }
    func allocatedSize(at url: URL) -> Int64 {
        if let children = try? contentsOfDirectory(at: url, includingPropertiesForKeys: nil) {
            return children.reduce(0) { $0 + allocatedSize(at: $1) }
        }
        return Int64((try? Data(contentsOf: url).count) ?? 0)
    }
    func availableImportantCapacity(at url: URL) -> Int64? { 0 }
}
enum FileLogger { static func clear() { StorageScanner.purge(contentsOf: FileManager.default.logs) } }
struct App {
    var uuid: String?; var name: String? = "App"; var version: String? = "1"
    var date: Date? = Date(); var isSigned = true
}
struct CertificatePair {
    var uuid: String?
    static func fetchRequest() -> Int { 0 }
}
struct Description { var url: URL? }
struct Container {
    let persistentStoreDescriptions = [Description(url: testRoot.appendingPathComponent("Library/library.sqlite"))]
}
@MainActor final class Context {
    func fetch(_ request: Int) throws -> [CertificatePair] {
        precondition(Storage.shared.isReady, "Unavailable database was queried as authoritative")
        return Storage.shared.certificates
    }
}
@MainActor final class Storage {
    static let shared = Storage()
    var isReady = true
    var apps: [App] = []
    var certificates: [CertificatePair] = []
    let context = Context()
    let container = Container()
    var deletedApps = 0
    func getAllApps() -> [App] {
        precondition(isReady, "Unavailable database was queried as authoritative")
        return apps
    }
    func deleteApps(_ selected: [App]) { deletedApps += selected.count }
}
@MainActor final class StorageManager {
    var entries: [StorageLocation: [StorageEntry]] = [:]
    var report: StorageReport?
    func snapshot() -> LibrarySnapshot { _librarySnapshot() }
    func refresh() { report = StorageScanner.scan(_librarySnapshot()) }
    nonisolated static func purgeCaches() { StorageScanner.purge(contentsOf: StorageScanner.cachesDirectory) }
''' + methods + r'''
}
@main struct Check {
    @MainActor static func main() async throws {
        let fm = FileManager.default
        func write(_ relative: String) throws -> URL {
            let url = testRoot.appendingPathComponent(relative)
            try fm.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            try Data("keep".utf8).write(to: url)
            return url
        }
        func row(_ url: URL, uuid: String? = nil) -> StorageEntry {
            StorageEntry(id: url.path, name: "stale", version: nil, size: 4, date: Date(), url: url,
                         isDirectory: false, childCount: 0, isProtected: false, appUuid: uuid)
        }
        func kept(_ urls: [URL]) {
            for url in urls { assert(try! Data(contentsOf: url) == Data("keep".utf8), url.path) }
        }
        let signed = try write("Documents/Signed/orphan/App.app/binary")
        let imported = try write("Documents/Unsigned/orphan/App.app/binary")
        let certificate = try write("Documents/Certificates/orphan/key.p12")
        let known = try write("Documents/Signed/known/App.app/binary")
        let recoveryKey = try write("Documents/Certificates/.certificate-update-retained/original/key.p12")
        let recoveryProfile = try write("Documents/Certificates/.certificate-update-retained/original/subdir/profile.mobileprovision")
        let replacement = try write("Documents/Certificates/.certificate-update-retained/failed-replacement/key.p12")
        let recoveryRoot = recoveryKey.deletingLastPathComponent().deletingLastPathComponent()
        let inbox = try write("Documents/WebManager/disposable.ipa")
        let cache = try write("Library/Caches/icon")
        let log = try write("Documents/Logs/activity.log")
        let database = try write("Library/library.sqlite")
        let protectedFiles = [signed, imported, certificate, known, recoveryKey, recoveryProfile, replacement, database]
        Storage.shared.apps = [App(uuid: "known")]
        let manager = StorageManager()
        let healthy = manager.snapshot()
        let stale = StorageScanner.entries(at: .category(.leftovers), healthy)
        assert(stale.count == 4 && stale.allSatisfy { !$0.isProtected })
        assert(!stale.contains { $0.url.path.contains(".certificate-update-") })
        let healthyReport = StorageScanner.scan(healthy)
        assert(healthyReport.usages.first { $0.category == .leftovers }?.size == 16)

        // Failed load looks like empty query results, but must not turn preserved data into leftovers.
        Storage.shared.isReady = false
        Storage.shared.apps = []
        let unavailable = manager.snapshot()
        let report = StorageScanner.scan(unavailable)
        assert(report.usages.first { $0.category == .leftovers }?.size == 4)
        assert(report.reclaimable == 12) // inbox + disposable cache/log, no library/recovery files
        assert(StorageScanner.entries(at: .category(.leftovers), unavailable).map { $0.url.resolvingSymlinksInPath().path } == [inbox.resolvingSymlinksInPath().path])
        for directory in [fm.signed, fm.unsigned, fm.certificates] {
            assert(StorageScanner.entries(at: .directory(directory), unavailable).allSatisfy(\.isProtected))
        }

        // Old healthy rows cannot bypass the current unavailable state.
        manager.entries[.category(.leftovers)] = stale
        manager.delete(stale)
        kept(protectedFiles)
        assert(!fm.fileExists(atPath: inbox.path))
        assert(manager.entries[.category(.leftovers)]?.count == 3)
        manager.delete([row(known, uuid: "known"), row(signed), row(imported), row(certificate),
                        row(fm.signed), row(fm.unsigned), row(fm.certificates), row(testRoot.appendingPathComponent("Documents"))])
        assert(Storage.shared.deletedApps == 0)
        kept(protectedFiles)
        await manager.clear([.leftovers, .caches, .logs])
        kept(protectedFiles)
        assert(!fm.fileExists(atPath: cache.path) && !fm.fileExists(atPath: log.path))

        // Recovery roots, descendants and containing directories stay protected even when healthy.
        Storage.shared.isReady = true
        Storage.shared.apps = [App(uuid: "known")]
        let healthyAgain = manager.snapshot()
        let recoveryRows = StorageScanner.entries(at: .directory(recoveryRoot), healthyAgain)
        assert(!recoveryRows.isEmpty && recoveryRows.allSatisfy(\.isProtected))
        manager.delete([row(recoveryRoot), row(recoveryKey), row(recoveryProfile), row(replacement),
                        row(fm.certificates), row(testRoot.appendingPathComponent("Documents"))])
        kept(protectedFiles)

        // A recovery folder created after a snapshot is also protected at actual deletion time.
        let later = try write("Documents/Certificates/.certificate-update-later/original/key.p12")
        assert(healthyAgain.protects(later))
        assert(!healthyAgain.protects(testRoot.appendingPathComponent("Documents/Certificates-other/.certificate-update-later/original/key.p12")))
        assert(!healthyAgain.protects(fm.certificates.appendingPathComponent("ordinary/.certificate-update-later/key.p12")))
        manager.delete([row(later), row(later.deletingLastPathComponent().deletingLastPathComponent())])
        let alias = testRoot.appendingPathComponent("recovery-alias")
        try fm.createSymbolicLink(at: alias, withDestinationURL: recoveryRoot)
        manager.delete([row(alias.appendingPathComponent("original/key.p12"))])
        kept([recoveryKey, later])

        // Healthy ordinary leftovers and normal cache entries remain removable.
        await manager.clear([.leftovers])
        for url in [signed, imported, certificate] { assert(!fm.fileExists(atPath: url.path)) }
        kept([known, recoveryKey, recoveryProfile, replacement, later, database])
        let laterCache = try write("Library/Caches/new-icon")
        manager.delete([row(laterCache)])
        assert(!fm.fileExists(atPath: laterCache.path))
        assert(StorageScanner.entries(at: .category(.leftovers), manager.snapshot()).isEmpty)
        print("PASS: unready scan and stale-row deletion preserve library; recovery roots/descendants/parents retained; fresh bulk-delete readiness on utility task; healthy leftovers and disposable caches still removable")
    }
}
'''
# Redirect platform roots only, leaving production scanner, snapshots and deletion guards intact.
program = program.replace("URL.documentsDirectory", 'testRoot.appendingPathComponent("Documents")')
program = program.replace("fm.temporaryDirectory", 'testRoot.appendingPathComponent("tmp")')
program = program.replace("fm.urls(for: .cachesDirectory, in: .userDomainMask)[0]", 'testRoot.appendingPathComponent("Library/Caches")')
program = program.replace("fm.urls(for: .libraryDirectory, in: .userDomainMask)[0]", 'testRoot.appendingPathComponent("Library")')
with tempfile.TemporaryDirectory(prefix="ryuksign-recovery-cleanup-") as directory:
    main = Path(directory) / "Check.swift"
    main.write_text(program)
    binary = Path(directory) / "check"
    subprocess.run(["swiftc", "-swift-version", "5", "-parse-as-library", str(main), "-o", str(binary)], check=True)
    subprocess.run([str(binary), directory], check=True)
