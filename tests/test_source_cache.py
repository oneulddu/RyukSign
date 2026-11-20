"""Production disk-cache/refresh loader with real loopback fetches and private data."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import os
import json
import subprocess
import tempfile
import threading

from test_source_updates import declaration, production, repository, SUPPORT


STUBS = r'''
typealias FetchedResults<T> = ContiguousArray<T>
final class AltSource: NSObject {
 let identifier: String?
 let sourceURL: URL?
 let name: String? = "Fixture"
 init(_ url: URL) { sourceURL = url; identifier = url.lastPathComponent }
}
final class BackgroundTaskManager {
 init(taskName: String, expirationTitle: String, expirationBody: String) {}
 func start() {}
 func stop() {}
}
@MainActor final class PremiumManager: ObservableObject {
 static let shared = PremiumManager()
 @Published var isActive = true
 var recovered: [URL] = []
 func recover() async throws -> Int {try await _recover()}
 private func _activate(with urls:[URL]) async throws -> Int { recovered = urls; return urls.count }
 // PRODUCTION_RECOVERY
}
enum RyukSignAPI {
 static var premiumRegistered = false
 static var premiumAuth: String? = "authenticated"
 static func catalogURL(for url: URL) -> URL { url }
 static var deviceUUID: String? { premiumAuth }
 static var apiURLsEndpoint = ""
 // PRODUCTION_IDENTITY_METHODS
 static func isPremiumSource(_ url: URL) -> Bool { premiumRegistered && url.lastPathComponent == "premium" }
}
enum IdentityVault {
 enum Key { case premiumActive, premiumURLs }
 static func delete(_ key: Key) {}
}
final class FetchLog {
 static let shared = FetchLog()
 private let lock = NSLock()
 private var messages = [String]()
 func error(_ message: String) { lock.withLock { messages.append(message) } }
 func debug(_ message: String) {}
 var errors: [String] { lock.withLock { messages } }
}
extension String { static func localized(_ string: String) -> String { string } }
'''

CHECKS = r'''
@main struct Check {
 static func control(_ base: URL, _ command: String) async throws {
  let (_, response) = try await URLSession.shared.data(from: base.appendingPathComponent(command))
  check((response as? HTTPURLResponse)?.statusCode == 200, "server coordination completed")
 }
 @MainActor static func run() async throws {
  defer { Fixture.defaults.removePersistentDomain(forName: Fixture.defaultsName) }
  let base = URL(string: CommandLine.arguments[2])!
  URLCache.shared = URLCache(memoryCapacity: 1024 * 1024, diskCapacity: 1024 * 1024,
                             directory: Fixture.root.appendingPathComponent("HTTP", isDirectory: true))
  let regular = AltSource(base.appendingPathComponent("regular"))
  let premium = AltSource(base.appendingPathComponent("premium"))
  let failure = AltSource(base.appendingPathComponent("failure"))
  let cached = Data(#"{"name":"cached","apps":[{"bundleIdentifier":"fixture","version":"1","iconURL":"https://example.invalid/icon.png"}]}"#.utf8)
  let secret = Data(#"{"name":"previous-account","apps":[{"bundleIdentifier":"private","version":"1","iconURL":"https://example.invalid/icon.png"}]}"#.utf8)
  Fixture.defaults.set(SourceCache.Refresh.manual.rawValue, forKey: SourceCache.refreshKey)
  // The upstream cache hashed the URL alone; filter/query variants cannot be
  // enumerated from the current source rows when a premium registration is reset.
  let oldDirectory = Fixture.root.appendingPathComponent("Sources", isDirectory: true)
  let filename = SHA256.hash(data: Data(premium.sourceURL!.absoluteString.utf8)).map { String(format: "%02x", $0) }.joined() + ".json"
  try FileManager.default.createDirectory(at: oldDirectory, withIntermediateDirectories: true)
  let legacyURL = oldDirectory.appendingPathComponent(filename)
  try secret.write(to: legacyURL)
  RyukSignAPI.premiumRegistered = true
  SourceCache.store(cached, for: premium.sourceURL!)
  check(SourceCache.data(for: premium.sourceURL!) == nil, "premium URL cannot read pre-existing cache bytes")
  SourceCache.store(cached, for: regular.sourceURL!)
  SourceCache.store(cached, for: failure.sourceURL!)
  check(SourceCache.size() > 0, "cache accounts for allocated disk bytes")
  check(!SourceCache.isStale(.distantPast), "manual cache never expires automatically")
  Fixture.defaults.set(SourceCache.Refresh.always.rawValue, forKey: SourceCache.refreshKey)
  check(SourceCache.isStale(Date()), "always refresh ignores age")
  Fixture.defaults.set(SourceCache.Refresh.hourly.rawValue, forKey: SourceCache.refreshKey)
  check(SourceCache.isStale(Date().addingTimeInterval(-7200)) && !SourceCache.isStale(Date().addingTimeInterval(3600)), "hourly refresh honors age")
  Fixture.defaults.set(SourceCache.Refresh.manual.rawValue, forKey: SourceCache.refreshKey)
  let model = SourcesViewModel.shared
  var sawSecret = false
  let observer = model.$sources.sink { sources in
   if sources[premium]?.name == "previous-account" { sawSecret = true }
  }
  await model.fetchSources([regular, premium])
  check(model.sources[regular]?.name == "cached", "manual load hydrates regular disk cache")
  check(model.sources[regular]?.apps[0].sourceIdentifier == "regular", "disk-loaded listing receives trusted source identity")
  check(model.sources[premium]?.name == "network" && !sawSecret, "premium fetch never publishes another account's disk cache")
  check(model.sources[premium]?.apps[0].sourceIdentifier == "premium", "network-loaded listing receives trusted source identity")
  await model.fetchSources([regular, premium], refresh: true)
  check(model.sources[regular]?.name == "network", "forced refresh bypasses manual cache")
  let regularDisk = try JSONDecoder().decode(ASRepository.self, from: SourceCache.data(for: regular.sourceURL!)!.data)
  check(regularDisk.name == "network", "successful decoded response reaches disk")
  await model.fetchSources([failure], refresh: true)
  check(model.sources[failure]?.name == "cached", "failed refresh preserves usable ordinary cache")
  check(SourceCache.data(for: failure.sourceURL!)?.data == cached, "failed decoding cannot poison disk cache")
  check(model.sources[regular] == nil && model.sources[premium] == nil, "removed sources disappear")
  Fixture.defaults.set(false, forKey: SourceCache.enabledKey)
  SourceCache.store(cached, for: regular.sourceURL!)
  await model.fetchSources([regular], refresh: true)
  check(model.sources[regular]?.name == "network" && SourceCache.data(for: regular.sourceURL!)?.data == cached, "disabled cache neither reads nor writes disk")
  Fixture.defaults.set(true, forKey: SourceCache.enabledKey)
  await model.refreshStale([premium])
  check(model.sources[premium]?.name == "network", "foreground stale refresh includes premium sources")
  try await control(base, "arm-failure")
  let sameAuthRefresh = Task { await model.fetchSources([premium], refresh: true) }
  try await control(base, "pending")
  check(model.sources[premium]?.name == "network", "same-auth catalog stays visible while refresh is pending")
  try await control(base, "release")
  await sameAuthRefresh.value
  check(model.sources[premium]?.name == "network", "same-auth failed refresh retains usable memory catalog")

  try await control(base, "arm-success")
  let oldAccountRefresh = Task { await model.fetchSources([premium], refresh: true) }
  try await control(base, "pending")
  var watchOldAccount = false
  var republishedOldAccount = false
  let accountObserver = model.$sources.sink {
   if watchOldAccount && $0[premium]?.name == "network" { republishedOldAccount = true }
  }
  RyukSignAPI.premiumAuth = "changed"
  watchOldAccount = true
  let removed = Task { @MainActor in
   for await snapshot in model.$sources.values {
    if snapshot[premium] == nil { return }
   }
  }
  let changedAccountRefresh = Task { await model.refreshStale([premium]) }
  await removed.value
  check(model.sources[premium] == nil, "changed-auth catalog is discarded before the previous request finishes")
  try await control(base, "release")
  await oldAccountRefresh.value
  await changedAccountRefresh.value
  check(!republishedOldAccount && model.sources[premium]?.name == "network-changed",
        "old in-flight response cannot republish another account's catalog")
  accountObserver.cancel()

  try await control(base, "arm-success")
  let beforeReset = Task { await model.fetchSources([premium], refresh: true) }
  try await control(base, "pending")
  RyukSignAPI.premiumRegistered = false
  PremiumManager.shared.isActive = false
  check(model.sources[premium] == nil, "reset clears the private catalog without waiting for a fetch")
  RyukSignAPI.premiumRegistered = true
  PremiumManager.shared.isActive = true
  try await control(base, "release")
  await beforeReset.value
  check(model.sources[premium] == nil, "reactivation with the same credential rejects the pre-reset response")

  RyukSignAPI.premiumAuth = nil
  await model.refreshStale([premium])
  check(model.sources[premium] == nil, "sign-out and rejected anonymous refresh retain no private catalog")
  // A failed database deletion leaves the row in the next ordinary load.
  RyukSignAPI.premiumRegistered = false
  PremiumManager.shared.isActive = false
  let request = URLRequest(url: premium.sourceURL!)
  let cache = URLCache.shared
  let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
  cache.storeCachedResponse(CachedURLResponse(response: response, data: secret), for: request)
  check(cache.cachedResponse(for: request)?.data == secret, "seeded a legacy URLSession cache entry")
  RyukSignAPI.clearPremiumIdentity()
  await model.refreshStale([premium])
  check(!sawSecret && model.sources[premium] == nil, "reset with a surviving ordinary row cannot hydrate a legacy authenticated response")
  check(!FileManager.default.fileExists(atPath: legacyURL.path) &&
        !FileManager.default.fileExists(atPath: SourceCache.directory.appendingPathComponent(filename).path),
        "neither legacy nor refreshed premium bytes persist in the source cache")
  check(cache.cachedResponse(for: request) == nil, "logout purges shared URL cache from previous builds")

  RyukSignAPI.premiumRegistered = true
  RyukSignAPI.premiumAuth = "activation"
  let activated = await FR.fetchRepositories(from: [premium.sourceURL!])
  check(activated.first?.data.name == "network-activation", "activation fetch uses authenticated network response")
  check(cache.cachedResponse(for: request) == nil, "activation did not populate shared URL cache")
  let recovery = base.appendingPathComponent("api/urls")
  RyukSignAPI.apiURLsEndpoint = recovery.absoluteString
  let count = try await PremiumManager.shared.recover()
  check(count == 1 && PremiumManager.shared.recovered == [premium.sourceURL!], "authenticated recovery receives the private URL list")
  check(cache.cachedResponse(for: URLRequest(url: recovery)) == nil, "recovery URL list did not populate shared URL cache")
  let errors = FetchLog.shared.errors
  check(errors.contains { $0.contains("HTTP 401") } && errors.contains { $0.contains("PARSE FAIL") },
        "HTTP and decoding failures retain diagnostics")
  check(!errors.contains { $0.contains("private-response-marker") }, "HTTP and decoding error logs exclude response bodies")
  check(model.isFinished, "loader returns to idle")
  observer.cancel()
  SourceCache.clear()
  check(SourceCache.data(for: regular.sourceURL!) == nil && SourceCache.size() == 0, "clear removes cached source bytes")
  print("PASS: disk hydration and refresh; premium disk exclusion; same-auth pending/failure retention; changed-auth immediate invalidation; stale-response rejection; sign-out")
 }
 static func main() async {
  DispatchQueue.global().asyncAfter(deadline: .now() + 20) {
   print("FAIL: cache fixture event deadline"); exit(1)
  }
  do { try await run() }
  catch { print("FAIL: \(error)"); exit(1) }
 }
}
'''


def test_source_cache():
    requests = []
    pending = threading.Event()
    release = threading.Event()
    held_response = None

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal held_response
            if self.path in ("/arm-failure", "/arm-success"):
                pending.clear()
                release.clear()
                held_response = self.path == "/arm-failure"
                self.send_response(200)
                self.end_headers()
                return
            if self.path == "/pending":
                self.send_response(200 if pending.wait(10) else 504)
                self.end_headers()
                return
            if self.path == "/release":
                release.set()
                self.send_response(200)
                self.end_headers()
                return
            requests.append((self.path, self.headers.get("ryukSignUUID"), self.headers.get("Cache-Control")))
            fail = self.path == "/failure"
            if self.path == "/premium" and held_response is not None:
                fail = held_response
                held_response = None
                pending.set()
                if not release.wait(10):
                    self.send_response(504)
                    self.end_headers()
                    return
            auth = self.headers.get("ryukSignUUID")
            name = "network" if self.path != "/premium" or auth == "authenticated" else "network-" + (auth or "anonymous")
            body = (b"private-response-marker" if fail or (self.path == "/premium" and auth is None) else json.dumps({
                "name": name, "apps": [{"bundleIdentifier": "fixture", "version": "2",
                                       "iconURL": "https://example.invalid/icon.png"}],
            }).encode())
            self.send_response(401 if self.path == "/premium" and auth is None else 200)
            if self.path == "/api/urls":
                body = json.dumps({"urls": [{"url": f"http://127.0.0.1:{self.server.server_port}/premium"}]}).encode()
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    cache = production("RyukSign/Backend/Observable/SourceCache.swift", ("NimbleExtensions",)).replace(
        "URL.cachesDirectory", "Fixture.root")
    model = production("RyukSign/Views/Sources/SourcesViewModel.swift", ("AltSourceKit", "NimbleJSON", "SwiftUI"))
    fetch = production("NimbleKit/Sources/NimbleJSON/NBFetchService.swift").replace(
        'Logger(subsystem: "com.ryuksign.network", category: "NBFetchService")', 'FetchLog.shared'
    ).replace(", privacy: .public", "")
    api = production("RyukSign/Utilities/RyukSignAPI.swift")
    stubs = STUBS.replace("// PRODUCTION_IDENTITY_METHODS", "\n".join(declaration(api, method) for method in (
        "static func authHeaders(", "static func clearPremiumIdentity(", "struct ErrorResponse:", "struct URLsResponse:")))
    premium_manager = production("RyukSign/Backend/Observable/PremiumManager.swift")
    stubs = stubs.replace("// PRODUCTION_RECOVERY", "\n".join(declaration(premium_manager, method) for method in (
        "enum PremiumError:", "private func _recover(", "private func _requestURLs(")))
    activation = declaration(production("RyukSign/Utilities/FR.swift"), "static func fetchRepositories(")
    helpers = production("RyukSign/Extensions/FileManager+documents.swift")
    start = helpers.index("\tfunc allocatedSize(")
    end = helpers.index("\n\t/// Best-effort", start)
    allocated_size = "extension FileManager {\n" + helpers[start:end] + "\n}"
    program = "\n".join([SUPPORT, repository(), stubs, "enum FR {\n" + activation + "\n}",
                         allocated_size, cache, fetch, model, CHECKS])
    program = program.replace("UserDefaults.standard", "Fixture.defaults")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="ryuksign-source-cache-") as directory:
            work = Path(directory)
            env = dict(os.environ, CLANG_MODULE_CACHE_PATH=str(work / "modules"),
                       CFFIXED_USER_HOME=str(work), PYTHONDONTWRITEBYTECODE="1")
            (work / "check.swift").write_text(program)
            subprocess.run(["swiftc", "-swift-version", "5", "-parse-as-library",
                            "-module-cache-path", str(work / "modules"), str(work / "check.swift"),
                            "-o", str(work / "check")], check=True, env=env, timeout=90)
            subprocess.run([str(work / "check"), str(work), f"http://127.0.0.1:{server.server_port}"],
                           check=True, env=env, timeout=25)
        assert all(auth is None for path, auth, _ in requests if path not in ("/premium", "/api/urls")), requests
        assert ("/api/urls", "activation", "no-store") in requests, requests
        assert any(auth == "activation" and policy == "no-store" for _, auth, policy in requests), requests
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    test_source_cache()
