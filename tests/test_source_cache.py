"""Production disk-cache/refresh loader with real loopback fetches and private data."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import os
import json
import subprocess
import tempfile
import threading

from test_source_updates import production, repository, SUPPORT


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
enum RyukSignAPI {
 static var premiumAuth: String? = "authenticated"
 static func catalogURL(for url: URL) -> URL { url }
 static func authHeaders(for url: URL) -> [String: String] {
  if isPremiumSource(url) { return premiumAuth.map { ["X-Fixture-Auth": $0] } ?? [:] }
  return ["X-Fixture-Auth": "authenticated"]
 }
 static func isPremiumSource(_ url: URL) -> Bool { url.lastPathComponent == "premium" }
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
  let regular = AltSource(base.appendingPathComponent("regular"))
  let premium = AltSource(base.appendingPathComponent("premium"))
  let failure = AltSource(base.appendingPathComponent("failure"))
  let cached = Data(#"{"name":"cached","apps":[{"bundleIdentifier":"fixture","version":"1","iconURL":"https://example.invalid/icon.png"}]}"#.utf8)
  let secret = Data(#"{"name":"previous-account","apps":[{"bundleIdentifier":"private","version":"1","iconURL":"https://example.invalid/icon.png"}]}"#.utf8)
  Fixture.defaults.set(SourceCache.Refresh.manual.rawValue, forKey: SourceCache.refreshKey)
  SourceCache.store(cached, for: regular.sourceURL!)
  SourceCache.store(secret, for: premium.sourceURL!)
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
  check(SourceCache.data(for: premium.sourceURL!)?.data == secret, "premium network response never writes URL-keyed cache")
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

  RyukSignAPI.premiumAuth = nil
  await model.refreshStale([premium])
  check(model.sources[premium] == nil, "sign-out and rejected anonymous refresh retain no private catalog")
  check(SourceCache.data(for: premium.sourceURL!)?.data == secret, "premium refreshes never modify disk cache")
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
            requests.append((self.path, self.headers.get("X-Fixture-Auth")))
            fail = self.path == "/failure"
            if self.path == "/premium" and held_response is not None:
                fail = held_response
                held_response = None
                pending.set()
                if not release.wait(10):
                    self.send_response(504)
                    self.end_headers()
                    return
            auth = self.headers.get("X-Fixture-Auth")
            name = "network" if auth == "authenticated" else "network-" + (auth or "anonymous")
            body = (b"invalid JSON" if fail else json.dumps({
                "name": name, "apps": [{"bundleIdentifier": "fixture", "version": "2",
                                       "iconURL": "https://example.invalid/icon.png"}],
            }).encode())
            self.send_response(401 if self.path == "/premium" and auth is None else 200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    cache = production("RyukSign/Backend/Observable/SourceCache.swift", ("NimbleExtensions",)).replace(
        "URL.cachesDirectory", "Fixture.root")
    model = production("RyukSign/Views/Sources/SourcesViewModel.swift", ("AltSourceKit", "NimbleJSON", "SwiftUI"))
    fetch = production("NimbleKit/Sources/NimbleJSON/NBFetchService.swift")
    helpers = production("RyukSign/Extensions/FileManager+documents.swift")
    start = helpers.index("\tfunc allocatedSize(")
    end = helpers.index("\n\t/// Best-effort", start)
    allocated_size = "extension FileManager {\n" + helpers[start:end] + "\n}"
    program = "\n".join([SUPPORT, repository(), STUBS, allocated_size, cache, fetch, model, CHECKS])
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
        assert requests and all(auth == "authenticated" for path, auth in requests if path != "/premium"), requests
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    test_source_cache()
