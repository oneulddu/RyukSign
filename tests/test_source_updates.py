"""Production grouped updates/origin persistence with disposable service boundaries.

Core Data reads and downloads are stubs; Combine scheduling, repository decoding,
update computation, favorites and origin-file persistence are production Swift.
"""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def production(path, excluding=()):
    return "\n".join(line for line in (ROOT / path).read_text().splitlines()
                     if line not in {"import " + module for module in excluding})


def repository():
    return production("AltSourceKit/Sources/AltSourceKit/Models/ASRepository.swift", ("SwiftUI",)) + "\n" + production(
        "AltSourceKit/Sources/AltSourceKit/Extensions/Array/Array+safe.swift")


def declaration(source, signature):
    start = source.index(signature)
    end = source.index("{", start) + 1
    depth = 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


SUPPORT = r'''
import Foundation
import Combine
import OSLog
// Color rendering is outside these backend fixtures.
public struct Color: Codable, Hashable, Sendable {}
enum Fixture {
 static let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
 static let defaultsName = "source-fixture-" + UUID().uuidString
 static let defaults = UserDefaults(suiteName: defaultsName)!
}
extension Logger { static let misc = Logger(subsystem: "SourceFixture", category: "test") }
func check(_ value: @autoclosure () -> Bool, _ message: String) {
 if !value() { print("FAIL: " + message); exit(1) }
}
'''

STUBS = r'''
class NSManagedObject: NSObject {}
let NSInsertedObjectsKey = "inserted"
let NSDeletedObjectsKey = "deleted"
let NSUpdatedObjectsKey = "updated"
extension Notification.Name {
 static let NSManagedObjectContextObjectsDidChange = Notification.Name("library")
 static let skippedUpdatesChanged = Notification.Name("ignored")
}
protocol AppInfoPresentable {
 var uuid: String? { get }
 var identifier: String? { get }
 var originalIdentifier: String? { get }
 var name: String? { get }
 var version: String? { get }
}
class Row: NSManagedObject, AppInfoPresentable {
 var uuid: String?
 var identifier: String?
 var originalIdentifier: String?
 var name: String? = "Library"
 var version: String?
 init(_ uuid: String, _ id: String?, _ version: String?) {
  self.uuid = uuid; self.identifier = id; self.version = version
 }
}
final class Signed: Row { static func fetchRequest() -> Signed.Type { Self.self } }
final class Imported: Row { static func fetchRequest() -> Imported.Type { Self.self } }
final class Context: NSObject {
 var signed: [Signed] = []
 var imported: [Imported] = []
 var unreadable = false
 var hasChanges = false
 func fetch(_ request: Signed.Type) throws -> [Signed] { signed }
 func fetch(_ request: Imported.Type) throws -> [Imported] {
  if unreadable { throw NSError(domain: "UnreadableImported", code: 1) }
  return imported
 }
}
final class Storage: ObservableObject {
 static let shared = Storage()
 @Published var isReady = false
 let context = Context()
 var onRead: (() -> Void)?
 func requireReady() throws {
  if let callback = onRead { onRead = nil; DispatchQueue.main.async(execute: callback) }
  if !isReady { throw NSError(domain: "UnavailableLibrary", code: 1) }
 }
 func app(withUuid uuid: String) -> AppInfoPresentable? {
  (context.signed as [Row] + context.imported as [Row]).first { $0.uuid == uuid }
 }
}
final class AltSource: NSObject {
 let identifier: String?
 let name: String?
 init(_ id: String?) { identifier = id; name = id }
}
final class SourcesViewModel: ObservableObject {
 static let shared = SourcesViewModel()
 @Published var sources: [AltSource: ASRepository] = [:]
 @MainActor func admit(_ decoded: [AltSource: ASRepository]) {
  sources = Dictionary(uniqueKeysWithValues: decoded.map {
   ($0.key, repository($0.value, attributedTo: $0.key))
  })
 }
 // PRODUCTION_SOURCE_ATTRIBUTION
}
final class SkippedUpdatesManager {
 static let shared = SkippedUpdatesManager()
 var bundleIDs: Set<String> = []
}
final class Download { var origin: AppOrigin? }
final class DownloadManager {
 static let shared = DownloadManager()
 var lastURL: URL?
 func startDownload(from url: URL, id: String, appName: String?, appDescription: String?) -> Download {
  lastURL = url; return Download()
 }
}
'''

CHECKS = r'''
@main struct Check {
 @MainActor static func next(_ action: () -> Void) async -> [AppUpdate] {
  var subscription: AnyCancellable?
  let result: [AppUpdate] = await withCheckedContinuation { continuation in
   subscription = AppUpdateChecker.shared.$updates.dropFirst().first().sink { continuation.resume(returning: $0) }
   action()
  }
  subscription?.cancel()
  return result
 }
 @MainActor static func readAttempt(_ action: () -> Void) async {
  await withCheckedContinuation { continuation in
   Storage.shared.onRead = { continuation.resume() }
   action()
  }
 }
 static func repo(_ version: String, _ name: String, _ url: String, id: String = "source.app") throws -> ASRepository {
  let json = ["name": name, "apps": [["bundleIdentifier": id, "name": name, "version": version, "downloadURL": url, "iconURL": "https://example.invalid/icon.png", "sourceIdentifier": "untrusted-json"]]] as [String: Any]
  return try JSONDecoder().decode(ASRepository.self, from: JSONSerialization.data(withJSONObject: json))
 }
 @MainActor static func run() async throws {
  defer { Fixture.defaults.removePersistentDomain(forName: Fixture.defaultsName) }
  let storage = Storage.shared
  let origins = AppOrigins.shared
  let origin = AppOrigin(source: "A", appID: "source.app", name: "App A", version: "1.0")
  origins.set(origin, for: "imported")
  origins.set(origin, for: "incomplete")
  origins.set(origin, for: "orphan")
  storage.context.signed = [Signed("signed", "local.app", "0.5")]
  storage.context.imported = [Imported("imported", "local.app", "99.0"), Imported("incomplete", nil, nil)]
  let a = AltSource("A"), b = AltSource("B")
  let repoA = try repo("2.0", "App A", "https://example.invalid/a.ipa")
  let repoB = try repo("3.0", "App B", "https://example.invalid/b.ipa")
  let mirror = AltSource("Mirror")
  let mirrorRepo = try repo("2.0", "App A", "https://example.invalid/a.ipa")
  let historical = try JSONDecoder().decode(ASRepository.App.Version.self, from:
   Data(#"{"version":"0.9","downloadURL":"https://example.invalid/archive.ipa"}"#.utf8))
  let model = SourcesViewModel.shared
  let checker = AppUpdateChecker.shared
  check(repoA.apps[0].sourceIdentifier == nil && checker.origin(for: repoA.apps[0]) == nil,
        "untrusted JSON cannot assign provenance to an unattributed listing")
  var selected: ASRepository.App!
  await readAttempt {
   model.admit([a: repoA, mirror: mirrorRepo])
   selected = model.sources[a]!.apps[0]
   check(selected.uuid == repoA.apps[0].uuid, "source attribution preserves listing UUID")
   let immediate = DownloadManager.shared.startDownload(for: selected)
   check(immediate?.origin == AppOrigin(source: "A", appID: "source.app", name: "App A", version: "2.0"),
         "download origin is available immediately before recomputation")
   let mirrored = DownloadManager.shared.startDownload(for: model.sources[mirror]!.apps[0], version: historical)
   check(mirrored?.origin == AppOrigin(source: "Mirror", appID: "source.app", name: "App A", version: "0.9")
         && DownloadManager.shared.lastURL?.lastPathComponent == "archive.ipa",
         "identical listings retain exact source and requested version")
  }
  check(origins.all.count == 3, "unavailable database retains all origins")
  storage.context.unreadable = true
  await readAttempt { storage.isReady = true }
  check(origins.all.count == 3, "partial fetch failure retains all origins")
  storage.context.unreadable = false
  let updates = await next { model.admit([a: repoA, b: repoB]) }
  check(origins.all["incomplete"] == origin && origins.all["orphan"] == nil,
        "prune retains persisted rows excluded from update eligibility")
  check(updates.count == 1, "same app groups signed and imported copies")
  let update = updates[0]
  check(update.id == "local.app" && update.installedVersion == "1.0", "origin ID and version override tweaked metadata")
  check(Set(update.libraryUUIDs) == ["signed", "imported"], "group maps every library copy")
  check(update.best.sourceID == "B" && update.candidates.count == 2, "newest source wins")
  check(checker.update(for: storage.context.signed[0])?.id == "local.app", "library query resolves group")
  check(update.scoped(to: ["A"])?.best.sourceID == "A", "source-scoped updates")
  check(!checker.isAutomatic(update), "multiple sources require selection by default")
  let pinned = await next { checker.pin(update.candidates.first { $0.sourceID == "A" }, for: update.id) }
  check(pinned[0].best.sourceID == "A" && pinned[0].isPinned, "pin overrides newest source")
  check(checker.autoUpdate(pinned[0]) && DownloadManager.shared.lastURL?.lastPathComponent == "a.ipa", "automatic pinned download")
  let download = DownloadManager.shared.startDownload(for: selected)
  check(download?.origin == AppOrigin(source: "A", appID: "source.app", name: "App A", version: "2.0"), "download carries source origin")
  let noPinnedUpdate = await next { model.admit([a: try! repo("1.0", "App A", "https://example.invalid/a.ipa"), b: repoB]) }
  check(noPinnedUpdate.isEmpty, "up-to-date pinned source suppresses other sources")
  let fallback = await next { model.admit([b: repoB]) }
  check(fallback.count == 1 && !fallback[0].isPinned, "missing pinned source falls back")
  let ignored = await next {
   SkippedUpdatesManager.shared.bundleIDs = ["source.app"]
   NotificationCenter.default.post(name: .skippedUpdatesChanged, object: nil)
  }
  check(ignored.isEmpty, "ignore recognizes original source ID")
  let replacement = try repo("4.0", "App A", "https://example.invalid/a.ipa")
  model.admit([a: replacement, b: repoB])
  check(model.sources[a]!.apps[0].uuid != selected.uuid, "refresh decodes a new listing value")
  let retained = DownloadManager.shared.startDownload(for: selected)
  check(retained?.origin == AppOrigin(source: "A", appID: "source.app", name: "App A", version: "2.0"),
        "old open route keeps original source and version after successful refresh")
  let retainedHistorical = DownloadManager.shared.startDownload(for: selected, version: historical)
  check(retainedHistorical?.origin == AppOrigin(source: "A", appID: "source.app", name: "App A", version: "0.9"),
        "old route preserves explicit selected version after refresh")
  let unidentified = AltSource(nil)
  model.admit([unidentified: repoA])
  check(checker.origin(for: model.sources[unidentified]!.apps[0]) == nil,
        "source without a trusted identifier leaves provenance absent")
  check(AppUpdateChecker.isNewerVersion("v1.3 (tweak 9.0)", than: "1.2"), "version numeric prefix")
  check(!AppUpdateChecker.isNewerVersion("1.2.0", than: "1.2"), "version zero padding")
  let blank = try repo("1", " \n ", "https://example.invalid/blank", id: "bundle.fallback")
  check(blank.apps[0].currentName == "bundle.fallback", "blank names use bundle ID")
  let favorites = SourceFavorites.shared
  favorites.toggle(a)
  check(favorites.contains(a) && !favorites.contains(b), "favorites distinguish source identity")
  favorites.toggle(a)
  check(!favorites.contains(a), "favorite toggle removes persisted identity")
  origins.copy(from: "imported", to: "signed")
  let disk = try JSONDecoder().decode([String: AppOrigin].self, from: Data(contentsOf: Fixture.root.appendingPathComponent("AppOrigins.json")))
  check(disk["signed"] == origin && disk["incomplete"] == origin && disk["orphan"] == nil, "origin copy and prune persist atomically")
  print("PASS: grouped updates and pins; immediate and retained-route origins; distinct sources and exact versions; nil/untrusted provenance; DB preservation; favorites; names")
 }
 static func main() {
  DispatchQueue.global().asyncAfter(deadline: .now() + 20) { print("FAIL: update event deadline"); exit(1) }
  Task { @MainActor in
   do { try await run(); exit(0) }
   catch { print("FAIL: \(error)"); exit(1) }
  }
  RunLoop.main.run()
 }
}
'''


def test_source_updates():
    checker = production("RyukSign/Backend/Observable/AppUpdateChecker.swift", ("AltSourceKit", "CoreData"))
    origins = production("RyukSign/Backend/Storage/AppOrigins.swift").replace(
        "URL.applicationSupportDirectory", "Fixture.root")
    favorites = production("RyukSign/Backend/Observable/SourceFavorites.swift")
    manager = production("RyukSign/Backend/Observable/DownloadManager.swift")
    source_download = manager[manager.index("// MARK: - Source Apps"):]
    model = production("RyukSign/Views/Sources/SourcesViewModel.swift")
    attribution = declaration(model, "\tprivate func repository(")
    stubs = STUBS.replace("// PRODUCTION_SOURCE_ATTRIBUTION", "@MainActor\n" + attribution)
    program = "\n".join([SUPPORT, repository(), stubs, origins, favorites, checker, source_download, CHECKS])
    program = program.replace("UserDefaults.standard", "Fixture.defaults")
    with tempfile.TemporaryDirectory(prefix="ryuksign-source-updates-") as directory:
        work = Path(directory)
        env = dict(os.environ, CLANG_MODULE_CACHE_PATH=str(work / "modules"),
                   CFFIXED_USER_HOME=str(work), PYTHONDONTWRITEBYTECODE="1")
        (work / "check.swift").write_text(program)
        subprocess.run(["swiftc", "-swift-version", "5", "-parse-as-library",
                        "-module-cache-path", str(work / "modules"), str(work / "check.swift"),
                        "-o", str(work / "check")], check=True, env=env, timeout=90)
        subprocess.run([str(work / "check"), str(work)], check=True, env=env, timeout=25)


if __name__ == "__main__":
    test_source_updates()
