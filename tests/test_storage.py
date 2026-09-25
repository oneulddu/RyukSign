"""Exercise production storage transactions with the real model and isolated SQLite."""
from pathlib import Path
import subprocess
import tempfile
import xml.etree.ElementTree as ET

root = Path(__file__).resolve().parents[1]
model_path = root / "RyukSign/Backend/Storage/Feather.xcdatamodeld"
model = ET.parse(model_path / "Feather.xcdatamodel/contents").getroot()
types = {"String": "String?", "Date": "Date?", "URI": "URL?", "Boolean": "Bool", "Integer 32": "Int32"}
classes = []
for entity in model.findall("entity"):
    name = entity.attrib["name"]
    properties = [f"@NSManaged var {a.attrib['name']}: {types[a.attrib['attributeType']]}" for a in entity.findall("attribute")]
    for relationship in entity.findall("relationship"):
        typ = "NSSet?" if relationship.get("toMany") == "YES" else relationship.get("destinationEntity") + "?"
        properties.append(f"@NSManaged var {relationship.get('name')}: {typ}")
    classes.append(f'''@objc({name}) class {name}: NSManagedObject {{
    {chr(10).join(properties)}
    @nonobjc class func fetchRequest() -> NSFetchRequest<{name}> {{ NSFetchRequest(entityName: "{name}") }}
}}''')


def source(name):
    return (root / "RyukSign/Backend/Storage" / name).read_text().replace("import UIKit.UIImpactFeedbackGenerator", "")


# Substitute only the container/model location and defaults suite; keep actual load/save methods.
storage = source("Storage.swift").replace("NSPersistentContainer(name: _name)", "makeContainer()").replace("UserDefaults.standard", "testDefaults")
certificate = source("Storage+Certificate.swift").replace("import ZsignSwift", "")
# Network revocation and profile decoding are not part of this transaction test.
certificate = certificate[:certificate.index("\n\tfunc revokagedCertificate")] + "\n}"
program = r'''
import Foundation
import CoreData
import Combine
import OSLog
extension Logger { static let misc = Logger(subsystem: "StorageCheck", category: "test") }
struct UIImpactFeedbackGenerator {
    enum Style { case light }
    init(style: Style) {}
    func impactOccurred() {}
}
let testRoot = URL(fileURLWithPath: CommandLine.arguments[1])
let defaultsName = "RyukSignStorageTest-" + UUID().uuidString
let testDefaults = UserDefaults(suiteName: defaultsName)!
let sharedModel = NSManagedObjectModel(contentsOf: testRoot.appendingPathComponent("Feather.momd"))!
var storeURL: URL { testRoot.appendingPathComponent("library.sqlite") }
func makeContainer() -> NSPersistentContainer {
    let container = NSPersistentContainer(name: "Feather", managedObjectModel: sharedModel)
    container.persistentStoreDescriptions = [NSPersistentStoreDescription(url: storeURL)]
    return container
}
extension FileManager {
    var signed: URL { testRoot.appendingPathComponent("Signed") }
    var unsigned: URL { testRoot.appendingPathComponent("Unsigned") }
    var certificates: URL { testRoot.appendingPathComponent("Certificates") }
    func signed(_ id: String) -> URL { signed.appendingPathComponent(id) }
    func unsigned(_ id: String) -> URL { unsigned.appendingPathComponent(id) }
    func getPath(in url: URL, for ext: String) -> URL? { url }
    func removeFileIfNeeded(at url: URL) throws {
        if fileExists(atPath: url.path) { try removeItem(at: url) }
    }
}
struct ASRepository { var name: String?; var id: String?; var currentIconURL: URL? = nil }
enum RyukSignAPI {
    static var unregistered = 0
    static func unregisterPremiumSourceIfNeeded(_ url: URL, remainingSourceURLs: [URL]) { unregistered += 1 }
}
''' + "\n".join(classes) + storage + source("Storage+Imported.swift") + source("Storage+Signed.swift") + source("Storage+Shared.swift") + certificate + source("Storage+Sources.swift").replace("import AltSourceKit", "") + r'''
var revocationChecks = 0
extension Storage {
    func revokagedCertificate(for cert: CertificatePair) { revocationChecks += 1 }
    func getUuidDirectory(for cert: CertificatePair) -> URL? {
        cert.uuid.map { FileManager.default.certificates.appendingPathComponent($0) }
    }
}
func insert(_ storage: Storage, id: String, valid: Bool = true) -> Result<Signed, Error> {
    var outcome: Result<Signed, Error>?
    var calls = 0
    storage.addSigned(uuid: id, appName: valid ? "App" : nil, appIdentifier: "test.app", appVersion: "1") {
        calls += 1
        assert(!storage.context.hasChanges, "Callback ran before commit/rollback")
        outcome = $0
    }
    assert(calls == 1)
    return outcome!
}
func close(_ storage: Storage) throws {
    for store in storage.container.persistentStoreCoordinator.persistentStores {
        try storage.container.persistentStoreCoordinator.remove(store)
    }
}
func sentinel(_ relative: String) throws -> URL {
    let url = testRoot.appendingPathComponent(relative)
    try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    try Data("keep".utf8).write(to: url)
    return url
}
@main struct Check {
    @MainActor static func main() async throws {
        let fm = FileManager.default
        defer { testDefaults.removePersistentDomain(forName: defaultsName) }
        let files = try ["Signed/keep/app", "Unsigned/keep/app", "Certificates/keep/key.p12"].map(sentinel)
        testDefaults.set(7, forKey: "feather.selectedCert")
        let corrupt = Data("not a SQLite database".utf8)
        try corrupt.write(to: storeURL)
        let storage = Storage()
        assert(!storage.isReady && storage.loadError != nil)
        assert(!testDefaults.bool(forKey: "feather.sortIndexMigrated"))
        assert(try Data(contentsOf: storeURL) == corrupt)
        assert(testDefaults.integer(forKey: "feather.selectedCert") == 7)
        if case .success = storage.saveContext() { fatalError("Saved without a usable store") }
        storage.retryLoad()
        assert(!storage.isReady && storage.loadError != nil)
        assert(try Data(contentsOf: storeURL) == corrupt)
        for file in files { assert(try Data(contentsOf: file) == Data("keep".utf8)) }

        // Only the test removes its corrupt fixture. Force migration's real context.save to fail.
        try fm.removeItem(at: storeURL)
        storage.container.loadPersistentStores { _, error in precondition(error == nil) }
        _ = Imported(context: storage.context)
        storage.retryLoad()
        assert(!storage.isReady && storage.loadError != nil)
        assert(!storage.context.hasChanges)
        assert(!testDefaults.bool(forKey: "feather.sortIndexMigrated"))
        storage.retryLoad()
        assert(storage.isReady && storage.loadError == nil)
        assert(testDefaults.bool(forKey: "feather.sortIndexMigrated"))

        let app = try insert(storage, id: "durable").get()
        assert(!app.objectID.isTemporaryID)
        if case .success = insert(storage, id: "invalid", valid: false) { fatalError("False signing success") }
        assert(storage.saveError != nil)
        assert(try storage.context.count(for: Signed.fetchRequest()) == 1)
        var callbacks = 0
        storage.addImported(uuid: "invalid-import") { error in
            callbacks += 1
            assert(error != nil && !storage.context.hasChanges)
        }
        assert(callbacks == 1 && !storage.context.hasChanges)
        assert(try storage.context.count(for: Imported.fetchRequest()) == 0)
        _ = Imported(context: storage.context)
        storage.addCertificate(uuid: "invalid-cert", expiration: Date()) { error in
            assert(error != nil && !storage.context.hasChanges)
        }
        assert(revocationChecks == 0)
        assert(try storage.context.count(for: CertificatePair.fetchRequest()) == 0)
        storage.addCertificate(uuid: "certificate", expiration: Date()) { assert($0 == nil) }
        assert(revocationChecks == 1)
        let cert = storage.getCertificate(for: 0)!

        // Both deletion paths must preserve durable rows AND bytes when save validation fails.
        let appFile = try sentinel("Signed/durable/App.app/keep")
        let certFile = try sentinel("Certificates/certificate/keep.p12")
        _ = Imported(context: storage.context)
        storage.deleteApps([app])
        assert(!storage.context.hasChanges)
        assert(try Data(contentsOf: appFile) == Data("keep".utf8))
        assert(try storage.context.count(for: Signed.fetchRequest()) == 1)
        _ = Imported(context: storage.context)
        storage.deleteCertificate(for: cert)
        assert(!storage.context.hasChanges)
        assert(try Data(contentsOf: certFile) == Data("keep".utf8))
        assert(try storage.context.count(for: CertificatePair.fetchRequest()) == 1)

        // The shared save contract also covers source insertion/batches and deletion side effects.
        let sourceURL = URL(string: "https://example.test/source")!
        let repo = ASRepository(name: "Source", id: "source")
        _ = Imported(context: storage.context)
        callbacks = 0
        storage.addSources(repos: [sourceURL: repo]) { error in callbacks += 1; assert(error != nil) }
        assert(callbacks == 1 && !storage.context.hasChanges)
        assert(try storage.context.count(for: AltSource.fetchRequest()) == 0)
        storage.addSource(sourceURL, repository: repo) { assert($0 == nil) }
        let source = try storage.context.fetch(AltSource.fetchRequest()).first!
        _ = Imported(context: storage.context)
        storage.deleteSource(for: source)
        assert(RyukSignAPI.unregistered == 0 && storage.sourceExists("source"))
        storage.deleteSource(for: source)
        assert(RyukSignAPI.unregistered == 1 && !storage.sourceExists("source"))

        // The background caller must receive the result after a durable, queue-confined save.
        let background = await Task.detached { insert(storage, id: "background").map { _ in () } }.value
        try background.get()
        assert(storage.saveError == nil)
        let imported = await Task.detached { () -> Result<Void, Error> in
            var outcome: Result<Void, Error>?
            var calls = 0
            storage.addImported(uuid: "background-import", appName: "App", appIdentifier: "import.app", appVersion: "1") { error in
                calls += 1
                assert(!storage.context.hasChanges)
                assert(storage.context.concurrencyType == .mainQueueConcurrencyType)
                outcome = error.map { .failure($0) } ?? .success(())
            }
            assert(calls == 1)
            return outcome!
        }.value
        try imported.get()
        let invalidImport = await Task.detached { () -> Bool in
            var failed = false
            var calls = 0
            storage.addImported(uuid: "background-invalid") { error in
                calls += 1
                failed = error != nil
                assert(!storage.context.hasChanges)
            }
            assert(calls == 1)
            return failed
        }.value
        assert(invalidImport)
        try close(storage)
        let reopened = Storage()
        assert(reopened.isReady)
        assert(try reopened.context.count(for: Imported.fetchRequest()) == 1)
        let apps = try reopened.context.fetch(Signed.fetchRequest())
        assert(Set(apps.compactMap(\.uuid)) == ["durable", "background"])
        let certificates = try reopened.context.fetch(CertificatePair.fetchRequest())
        assert(certificates.count == 1)
        reopened.deleteApps(apps)
        reopened.deleteCertificate(for: certificates[0])
        assert(!fm.fileExists(atPath: appFile.path) && !fm.fileExists(atPath: certFile.path))
        assert(try reopened.context.count(for: Signed.fetchRequest()) == 0)
        assert(try reopened.context.count(for: CertificatePair.fetchRequest()) == 0)
        try close(reopened)
        let empty = Storage()
        assert(try empty.context.count(for: Signed.fetchRequest()) == 0)
        assert(try empty.context.count(for: CertificatePair.fetchRequest()) == 0)
        try close(empty)
        for file in files { assert(try Data(contentsOf: file) == Data("keep".utf8)) }
        print("PASS: load/retry preserve files; migration flag waits for save; failed inserts/deletes roll back; callbacks follow commit; background writes are queue-confined; reopen preserves committed results")
    }
}
'''
program = program.replace("assert(try ", "assert(try! ")
with tempfile.TemporaryDirectory(prefix="ryuksign-storage-test-") as directory:
    tmp = Path(directory)
    sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
    subprocess.run(["xcrun", "momc", "--sdkroot", sdk, str(model_path), str(tmp / "Feather.momd")], check=True)
    swift = tmp / "check.swift"
    swift.write_text(program)
    subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library", str(swift), "-o", str(tmp / "check")], check=True)
    subprocess.run([str(tmp / "check"), directory, "-com.apple.CoreData.ConcurrencyDebug", "1"], check=True)
