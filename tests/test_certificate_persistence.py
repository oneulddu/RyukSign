"""Actual certificate files + Core Data; synthetic profiles and injected filesystem failures."""
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

storage = (root / "RyukSign/Backend/Storage/Storage.swift").read_text()
storage = storage.replace("NSPersistentContainer(name: _name)", "makeContainer()").replace("UserDefaults.standard", "testDefaults")
certificate = (root / "RyukSign/Backend/Storage/Storage+Certificate.swift").read_text()
certificate = certificate.replace("import UIKit.UIImpactFeedbackGenerator", "").replace("import ZsignSwift", "")
certificate = certificate[:certificate.index("\n\tfunc revokagedCertificate")] + "\n}"
handler = (root / "RyukSign/Utilities/Handlers/CertificateFileHandler.swift").read_text()
auto = (root / "RyukSign/Utilities/CertificateAutoImporter.swift").read_text()
update = auto[auto.index("\tprivate func updateCertificate("):auto.index("\n// SHA256 extension")]
# Reuse the actual bundled-update callback, including its success-only hash policy.
callback_start = auto.index(") { error in", auto.index("self.updateCertificate(")) + len(") { error in")
callback_end = auto.index("\n\t\t\t\t\t\t}\n\t\t\t\t\t} else {", callback_start)
callback = auto[callback_start:callback_end].replace("UserDefaults.standard", "testDefaults")
# Production filesystem operations still touch real files; only chosen operations throw.
handler = handler.replace("FileManager.default", "FaultFiles.shared")
update = update.replace("FileManager.default", "FaultFiles.shared")
program = r'''
import Foundation
import CoreData
import Combine
import OSLog
extension Logger { static let misc = Logger(subsystem: "CertificatePersistence", category: "test") }
struct UIImpactFeedbackGenerator {
    enum Style { case light }; init(style: Style) {}; func impactOccurred() {}
}
struct UINotificationFeedbackGenerator {
    enum Kind { case error, success }; func notificationOccurred(_ kind: Kind) {}
}
enum UIAlertController { static func showAlertWithOk(title: String, message: String) {} }
extension String { static func localized(_ value: String) -> String { value } }
let testRoot = URL(fileURLWithPath: CommandLine.arguments[1])
let defaultsName = "CertificatePersistence-" + UUID().uuidString
let testDefaults = UserDefaults(suiteName: defaultsName)!
let sharedModel = NSManagedObjectModel(contentsOf: testRoot.appendingPathComponent("Feather.momd"))!
func makeContainer() -> NSPersistentContainer {
    let container = NSPersistentContainer(name: "Feather", managedObjectModel: sharedModel)
    container.persistentStoreDescriptions = [NSPersistentStoreDescription(url: testRoot.appendingPathComponent("library.sqlite"))]
    return container
}
extension FileManager {
    var certificates: URL { testRoot.appendingPathComponent("Certificates") }
    func certificates(_ id: String) -> URL { certificates.appendingPathComponent(id) }
}
final class FaultFiles: FileManager, @unchecked Sendable {
    static let shared = FaultFiles()
    var failCopy: URL?
    var failPublish = false
    var failRestore = false
    var copyFailures = 0
    override func copyItem(at srcURL: URL, to dstURL: URL) throws {
        if srcURL == failCopy { copyFailures += 1; throw CocoaError(.fileWriteUnknown) }
        try super.copyItem(at: srcURL, to: dstURL)
    }
    override func moveItem(at srcURL: URL, to dstURL: URL) throws {
        if failPublish && srcURL.lastPathComponent == "replacement" { throw CocoaError(.fileWriteUnknown) }
        if failRestore && srcURL.lastPathComponent == "original" { throw CocoaError(.fileWriteUnknown) }
        try super.moveItem(at: srcURL, to: dstURL)
    }
}
// The real CMS/crypto reader is replaced only for synthetic test credentials.
struct Certificate { var PPQCheck: Bool?; var ExpirationDate: Date? }
struct CertificateReader {
    let decoded: Certificate?
    init(_ url: URL) {
        if let data = try? Data(contentsOf: url),
           let value = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
           let expiration = value["ExpirationDate"] as? Date {
            decoded = Certificate(PPQCheck: value["PPQCheck"] as? Bool, ExpirationDate: expiration)
        } else { decoded = nil }
    }
}
''' + "\n".join(classes) + storage + certificate + r'''
var revocationChecks = 0
extension Storage {
    func revokagedCertificate(for cert: CertificatePair) {
        assert(!context.hasChanges, "Revocation ran before save completed")
        revocationChecks += 1
    }
    func getUuidDirectory(for cert: CertificatePair) -> URL? {
        cert.uuid.map { FileManager.default.certificates($0) }
    }
}
''' + handler + r'''
final class CertificateAutoImporter {
    var callbacks = 0
    @MainActor func run(cert: CertificatePair, key: URL, profile: URL) async -> Error? {
        let certName = "Bundled"
        let certHash = "new-hash"
        let hashKey = "Feather.certHash.Bundled"
        return await withCheckedContinuation { continuation in
            updateCertificate(cert: cert, p12URL: key, provisionURL: profile, password: "new-password") { error in
''' + callback + r'''
                self.callbacks += 1
                continuation.resume(returning: error)
            }
        }
    }
''' + update + r'''
struct Metadata: Equatable {
    var password: String?; var ppq: Bool; var expiration: Date?; var revoked: Bool; var date: Date?
    init(_ cert: CertificatePair) {
        password = cert.password; ppq = cert.ppQCheck; expiration = cert.expiration
        revoked = cert.revoked; date = cert.date
    }
}
func write(_ data: Data, _ url: URL) throws {
    try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    try data.write(to: url)
}
func entries() throws -> Set<String> {
    Set(try FileManager.default.contentsOfDirectory(atPath: FileManager.default.certificates.path))
}
@MainActor func persisted(_ cert: CertificatePair) throws -> Metadata {
    let id = cert.objectID
    let context = Storage.shared.container.newBackgroundContext()
    return try context.performAndWait { Metadata(try context.existingObject(with: id) as! CertificatePair) }
}
@main struct Check {
    @MainActor static func main() async throws {
        let fm = FileManager.default, faults = FaultFiles.shared, storage = Storage.shared
        defer { testDefaults.removePersistentDomain(forName: defaultsName) }
        let key = testRoot.appendingPathComponent("Inputs/new.p12")
        let profile = testRoot.appendingPathComponent("Inputs/new.mobileprovision")
        let newKey = Data("synthetic-new-key".utf8)
        let expiry = Date(timeIntervalSince1970: 2_000_000_000)
        let newProfile = try PropertyListSerialization.data(fromPropertyList: ["PPQCheck": true, "ExpirationDate": expiry], format: .xml, options: 0)
        try write(newKey, key); try write(newProfile, profile)
        let unrelated = fm.certificates("unrelated/keep.p12")
        try write(Data("unrelated".utf8), unrelated)
        // Force a destination collision without changing the production UUID API.
        let collision = CertificateFileHandler(key: key, provision: profile)
        let collisionID = Mirror(reflecting: collision).children.first { $0.label == "_uuid" }!.value as! String
        let collisionSentinel = fm.certificates(collisionID).appendingPathComponent("keep")
        try write(Data("preexisting".utf8), collisionSentinel)
        do { try await collision.copy(); fatalError("Preexisting destination accepted") } catch {}
        assert(try Data(contentsOf: collisionSentinel) == Data("preexisting".utf8))
        let baseline = try entries()

        // Handler: first file copied, second copy fails; remove only its own folder.
        let partial = CertificateFileHandler(key: key, provision: profile, password: "pw")
        faults.failCopy = profile
        do { try await partial.copy(); fatalError("Partial copy accepted") } catch {}
        assert(faults.copyFailures == 1 && (try entries()) == baseline)
        faults.failCopy = nil
        assert(try Data(contentsOf: key) == newKey && Data(contentsOf: profile) == newProfile)

        // Registration failure uses the real Core Data validation path and is thrown to the caller.
        let rejected = CertificateFileHandler(key: key, provision: profile, password: "pw")
        try await rejected.copy()
        assert(try entries().count == baseline.count + 1)
        _ = Imported(context: storage.context)
        do { try await rejected.addToDatabase(); fatalError("Failed registration accepted") } catch {}
        assert(!storage.context.hasChanges && (try entries()) == baseline)
        assert(try storage.context.count(for: CertificatePair.fetchRequest()) == 0)
        assert(revocationChecks == 0)

        let accepted = CertificateFileHandler(key: key, provision: profile, password: "pw", nickname: "Imported", isDefault: true)
        try await accepted.copy(); try await accepted.addToDatabase()
        let imported = try storage.context.fetch(CertificatePair.fetchRequest()).first!
        let importedDirectory = storage.getUuidDirectory(for: imported)!
        assert(imported.nickname == "Imported" && imported.isDefault && imported.password == "pw")
        assert(try Data(contentsOf: importedDirectory.appendingPathComponent(key.lastPathComponent)) == newKey)
        assert(try Data(contentsOf: importedDirectory.appendingPathComponent(profile.lastPathComponent)) == newProfile)
        assert(try persisted(imported).password == "pw")

        storage.addCertificate(uuid: "existing", password: "old-password", nickname: "Bundled", expiration: Date(timeIntervalSince1970: 123)) { assert($0 == nil) }
        let cert = try storage.context.fetch(CertificatePair.fetchRequest()).first { $0.uuid == "existing" }!
        cert.revoked = true; cert.date = Date(timeIntervalSince1970: 456)
        try storage.saveContext().get()
        let old = Metadata(cert), certDir = fm.certificates("existing")
        let oldKey = Data("synthetic-old-key".utf8), oldProfile = Data("synthetic-old-profile".utf8)
        try write(oldKey, certDir.appendingPathComponent("original.p12"))
        try write(oldProfile, certDir.appendingPathComponent("original.mobileprovision"))
        try write(Data("extra".utf8), certDir.appendingPathComponent("notes.txt"))
        let stableEntries = try entries(), revocationsBefore = revocationChecks
        testDefaults.set("old-hash", forKey: "Feather.certHash.Bundled")
        let updater = CertificateAutoImporter()
        func assertOld() throws {
            assert(Metadata(cert) == old && (try persisted(cert)) == old)
            assert(try Data(contentsOf: certDir.appendingPathComponent("original.p12")) == oldKey)
            assert(try Data(contentsOf: certDir.appendingPathComponent("original.mobileprovision")) == oldProfile)
            assert(try entries() == stableEntries)
            assert(testDefaults.string(forKey: "Feather.certHash.Bundled") == "old-hash")
            assert(revocationChecks == revocationsBefore)
        }

        // Updating: failed second copy leaves the live pair/metadata untouched.
        faults.failCopy = profile
        var error = await updater.run(cert: cert, key: key, profile: profile)
        assert(error != nil && updater.callbacks == 1)
        try assertOld()
        faults.failCopy = nil

        // Failure after moving originals to backup restores the directory.
        faults.failPublish = true
        error = await updater.run(cert: cert, key: key, profile: profile)
        assert(error != nil && updater.callbacks == 2)
        try assertOld()
        faults.failPublish = false

        // Both new files published, but real database validation fails: restore bytes and metadata.
        _ = Imported(context: storage.context)
        error = await updater.run(cert: cert, key: key, profile: profile)
        assert(error != nil && updater.callbacks == 3 && !storage.context.hasChanges)
        try assertOld()

        error = await updater.run(cert: cert, key: key, profile: profile)
        assert(error == nil && updater.callbacks == 4)
        assert(cert.password == "new-password" && cert.ppQCheck && cert.expiration == expiry && !cert.revoked)
        assert(try persisted(cert) == Metadata(cert))
        assert(try Data(contentsOf: certDir.appendingPathComponent("cert.p12")) == newKey)
        assert(try Data(contentsOf: certDir.appendingPathComponent("cert.mobileprovision")) == newProfile)
        assert(try Data(contentsOf: certDir.appendingPathComponent("notes.txt")) == Data("extra".utf8))
        assert(!fm.fileExists(atPath: certDir.appendingPathComponent("original.p12").path))
        assert(try entries() == stableEntries)
        assert(testDefaults.string(forKey: "Feather.certHash.Bundled") == "new-hash")
        assert(revocationChecks == revocationsBefore + 1)

        // If even rollback I/O fails, keep recoverable originals in the reported backup directory.
        let committed = Metadata(cert)
        _ = Imported(context: storage.context)
        faults.failRestore = true
        error = await updater.run(cert: cert, key: key, profile: profile)
        assert(error != nil && updater.callbacks == 5 && Metadata(cert) == committed)
        let leftover = try fm.contentsOfDirectory(at: fm.certificates, includingPropertiesForKeys: nil)
            .first { $0.lastPathComponent.hasPrefix(".certificate-update-") }!
        let backup = leftover.appendingPathComponent("original")
        assert(error!.localizedDescription.contains(leftover.lastPathComponent + "/original"))
        assert(try Data(contentsOf: backup.appendingPathComponent("cert.p12")) == newKey)
        assert(try Data(contentsOf: backup.appendingPathComponent("cert.mobileprovision")) == newProfile)
        faults.failRestore = false
        try fm.moveItem(at: backup, to: certDir)
        try fm.removeItem(at: leftover)

        assert(try Data(contentsOf: key) == newKey && Data(contentsOf: profile) == newProfile)
        assert(try Data(contentsOf: unrelated) == Data("unrelated".utf8))
        for store in storage.container.persistentStoreCoordinator.persistentStores {
            try storage.container.persistentStoreCoordinator.remove(store)
        }
        print("PASS: owned import cleanup; real save failure propagation; staged copy/publish/save rollback; durable metadata; success-only hashes/revocation; one callback; recoverable originals retained on restore failure")
    }
}
'''
# Swift assert's autoclosure does not throw; keep each check in the assertion.
program = program.replace("assert(try ", "assert(try! ").replace("(try entries())", "(try! entries())").replace("(try persisted(cert))", "(try! persisted(cert))")
with tempfile.TemporaryDirectory(prefix="ryuksign-certificate-test-") as directory:
    tmp = Path(directory)
    sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
    subprocess.run(["xcrun", "momc", "--sdkroot", sdk, str(model_path), str(tmp / "Feather.momd")], check=True)
    swift = tmp / "check.swift"
    swift.write_text(program)
    subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library", str(swift), "-o", str(tmp / "check")], check=True)
    subprocess.run([str(tmp / "check"), directory, "-com.apple.CoreData.ConcurrencyDebug", "1"], check=True)
