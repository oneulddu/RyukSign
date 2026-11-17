"""Run production queue/cleanup/status and installer terminal methods with boundary doubles.

The deterministic scheduler replaces DispatchQueue delays, not queue transitions.
UIKit presentation, installation transport and storage use disposable test doubles.
Run: python3 -B tests/test_install_queue.py
"""
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
OBSERVABLE = ROOT / "RyukSign/Backend/Observable"


def source(name):
    return (OBSERVABLE / name).read_text()


def portable(text):
    return "\n".join(
        line for line in text.splitlines()
        if line not in ("import NimbleExtensions", "import IDeviceSwift")
    )


queue = portable(source("InstallQueue.swift")).replace(
    "DispatchQueue.main", "Scheduler.main"
)
cleanup = source("InstallCleanup.swift").replace(
    "UserDefaults.standard", "Fixture.defaults!"
)
status = portable(source("InstallerStatusViewModel.swift"))
installer = source("AppInstaller.swift")
error_message = (ROOT / "RyukSign/Extensions/Error+message.swift").read_text()
stop = installer[installer.index("\tfunc stop()"):installer.index("\n\t// MARK: Pipeline")]
finish = installer[
    installer.index("\tprivate func _finish("):
    installer.index("\tprivate static func error(")
]

stubs = r'''
import Foundation
import SwiftUI

struct TestFailure: LocalizedError {
    let text: String
    var errorDescription: String? { text }
}
func expect(_ value: @autoclosure () throws -> Bool, _ reason: String) throws {
    if try !value() { throw TestFailure(text: reason) }
}
extension String { static func localized(_ value: String) -> String { value } }
protocol AppInfoPresentable {
    var uuid: String? { get }
    var name: String? { get }
}
struct App: AppInfoPresentable {
    let uuid: String?
    let name: String?
}
struct AnyApp: Identifiable {
    let base: AppInfoPresentable
    let archive: Bool
    var id: String { base.uuid! }
}
final class InstallerStatusViewModel: ObservableObject {
    enum InstallerStatus {
        case none, ready, sendingManifest, sendingPayload, installing
        case completed(Result<Void, Error>), broken(Error)
    }
    @Published var status = InstallerStatus.none
}
enum Fixture {
    static var defaults: UserDefaults!
    static var directory: URL!
}
@MainActor final class Storage {
    static let shared = Storage()
    var apps: [AppInfoPresentable] = []
    func getAllApps() -> [AppInfoPresentable] { apps }
    func deleteApps(_ deleting: [AppInfoPresentable]) {
        for app in deleting {
            do { try FileManager.default.removeItem(at: Fixture.directory.appendingPathComponent(app.uuid!)) }
            catch { fputs("Storage deletion failed: \(error)\n", stderr); exit(1) }
        }
        apps.removeAll { app in deleting.contains { $0.uuid == app.uuid } }
    }
}
enum StorageManager { static func purgeCaches() {} }
@MainActor final class Scheduler {
    static let main = Scheduler()
    var pending: [() -> Void] = []
    func asyncAfter(deadline: DispatchTime, execute: @escaping () -> Void) {
        pending.append(execute)
    }
    func next() throws {
        try expect(!pending.isEmpty, "Missing scheduled transition")
        pending.removeFirst()()
    }
}
final class UIApplication {
    enum State { case active, background }
    static let shared = UIApplication()
    static let didBecomeActiveNotification = Notification.Name("FixtureBecameActive")
    var applicationState = State.active
}
final class UIViewController {}
final class UIAlertAction {
    enum Style { case cancel }
    init(title: String, style: Style) {}
}
enum UIAlertController {
    static var messages: [String] = []
    static func showAlert(_ presenter: UIViewController, title: String, message: String, actions: [UIAlertAction]) {
        messages.append(message)
    }
}
enum UIActivityViewController { static func show(activityItems: [URL]) {} }
final class HeartbeatManager {
    static let shared = HeartbeatManager()
    func start(_ force: Bool) {}
}
final class InstallQueueWindow {
    static let shared = InstallQueueWindow()
    let presenter: UIViewController? = UIViewController()
    func ensure() {}
    func teardown() {}
}
final class ServerDouble { func stop() {} }
@MainActor final class AppInstaller: ObservableObject {
    enum Outcome { case installed, cancelled, exported(URL?) }
    let app: AppInfoPresentable
    let viewModel = InstallerStatusViewModel()
    var fallbackPage: URL?
    var _hasFinished = false
    var _completion: ((Result<Outcome, Error>) -> Void)?
    var _server: ServerDouble?
    var _statusObserver: Int?
    var _progressTask: Task<Void, Never>?
    init(app: AppInfoPresentable, isSharing: Bool = false) { self.app = app }
    func start(completion: @escaping (Result<Outcome, Error>) -> Void) { _completion = completion }
    func _disarmDeclineWatch() {}
    func complete(_ result: Result<Outcome, Error>) { _finish(result) }
'''

checks = r'''
@main struct Check {
    @MainActor static func main() {
        do { try run() }
        catch { fputs("FAIL: \(error.localizedDescription)\n", stderr); exit(1) }
    }
    @MainActor static func run() throws {
        let suite = "InstallQueueFixture.\(UUID().uuidString)"
        Fixture.defaults = UserDefaults(suiteName: suite)!
        defer { Fixture.defaults.removePersistentDomain(forName: suite) }
        Fixture.directory = URL(fileURLWithPath: CommandLine.arguments[1])
        let queue = InstallQueue.shared
        let a = App(uuid: "a", name: "Alpha")
        let b = App(uuid: "b", name: "Beta")
        let c = App(uuid: "c", name: "Gamma")
        let denied = TestFailure(text: "denied")
        func reset() {
            queue.clear()
            Scheduler.main.pending.removeAll()
            UIAlertController.messages.removeAll()
        }

        queue.enqueue(a); queue.enqueue(b); queue.enqueue(c)
        queue.installer!.complete(.success(.installed))
        queue.skip()
        try Scheduler.main.next()
        try expect(queue.current?.base.uuid == "b", "Stale completion skipped the next app")
        reset()

        queue.enqueue(a); queue.enqueue(b)
        UIApplication.shared.applicationState = .background
        queue.installer!.complete(.success(.installed))
        try Scheduler.main.next()
        try expect(queue.installer == nil, "Started install in background")
        UIApplication.shared.applicationState = .active
        NotificationCenter.default.post(name: UIApplication.didBecomeActiveNotification, object: nil)
        try expect(queue.installer?.app.uuid == "b", "Foreground did not resume queued install")
        reset()

        queue.enqueue(a); queue.enqueue(b); queue.enqueue(c)
        queue.installer!.complete(.failure(denied))
        try expect(queue.installer!.viewModel.failure?.message == "denied", "Terminal error not published")
        try Scheduler.main.next()
        try expect(queue.current?.base.uuid == "b", "Failure blocked progression")
        queue.installer!.complete(.failure(TestFailure(text: "invalid")))
        try Scheduler.main.next()
        queue.isSheetPresented = false
        queue.installer!.complete(.success(.installed))
        try Scheduler.main.next()
        try expect(queue.isFinished && queue.isSheetPresented, "Failures did not reopen final sheet")
        try Scheduler.main.next()
        try expect(UIAlertController.messages == ["Alpha: denied\n\nBeta: invalid"], "Lost aggregate failure details")
        reset()

        queue.enqueue(a)
        queue.installer!.complete(.failure(denied))
        try Scheduler.main.next()
        try expect(queue.isFinished && queue.isSheetPresented && queue.installer!.viewModel.isFailed,
                   "Last failed card unavailable")
        try expect(Scheduler.main.pending.isEmpty && UIAlertController.messages.isEmpty,
                   "Lone final failure duplicated as aggregate alert")
        reset()

        queue.enqueue(a); queue.enqueue(b)
        queue.installer!.complete(.failure(denied))
        try Scheduler.main.next()
        queue.skip()
        try Scheduler.main.next()
        try expect(UIAlertController.messages == ["Alpha: denied"], "Skipping final app lost earlier failure")
        reset()

        queue.enqueue(a); queue.enqueue(b)
        queue.installer!.complete(.failure(denied))
        try Scheduler.main.next()
        queue.installer!.complete(.success(.installed))
        try Scheduler.main.next()
        queue.clear()
        queue.enqueue(c)
        try Scheduler.main.next()
        try expect(UIAlertController.messages.isEmpty, "Retired run presented an alert over a new run")
        reset()

        let terminal = AppInstaller(app: a)
        terminal.fallbackPage = URL(string: "https://example.invalid")
        terminal.stop()
        terminal.complete(.failure(denied))
        try expect(terminal.fallbackPage == nil && !terminal.viewModel.isFailed,
                   "Stopped installer accepted a late terminal event")
        let completedFailure = InstallerStatusViewModel()
        completedFailure.status = .completed(.failure(denied))
        try expect(completedFailure.isFailed && completedFailure.failure?.message == "denied",
                   "Completed failure rendered as success")

        Fixture.defaults.set(true, forKey: InstallCleanup.deleteKey)
        Storage.shared.apps = [a, b, c]
        for app in [a, b, c] {
            try Data(app.uuid!.utf8).write(to: Fixture.directory.appendingPathComponent(app.uuid!))
            InstallCleanup.stage(app)
        }
        queue.enqueue(a); queue.enqueue(b)
        InstallCleanup.flush()
        try expect(Storage.shared.apps.compactMap(\.uuid) == ["a", "b"], "Cleanup deleted a queued app")
        try expect(try Data(contentsOf: Fixture.directory.appendingPathComponent("a")) == Data("a".utf8),
                   "Current app data missing")
        try expect(try Data(contentsOf: Fixture.directory.appendingPathComponent("b")) == Data("b".utf8),
                   "Upcoming app data missing")
        queue.clear()
        try expect(Storage.shared.apps.isEmpty, "Deferred queued cleanup was lost")
        try expect(try FileManager.default.contentsOfDirectory(atPath: Fixture.directory.path).isEmpty,
                   "Deferred cleanup left disposable app files")
        print("PASS: queue transitions, foreground resume, failure reporting, terminal safety, queued cleanup")
    }
}
'''

with tempfile.TemporaryDirectory(prefix="install-queue-") as directory:
    work = Path(directory)
    data = work / "data"
    data.mkdir()
    swift = work / "Check.swift"
    swift.write_text(error_message + stubs + stop + finish + "}\n" + queue + cleanup + status + checks)
    executable = work / "check"
    subprocess.run(
        ["swiftc", "-parse-as-library", "-module-cache-path", str(work / "modules"),
         str(swift), "-o", str(executable)],
        check=True,
    )
    subprocess.run([str(executable), str(data)], check=True)
