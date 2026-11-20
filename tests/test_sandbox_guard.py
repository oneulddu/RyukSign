"""Run production tweak placement and guard injection on disposable flat iOS bundles."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1] / 'RyukSign/Utilities'
handlers = root / 'Handlers'
tweaks = (handlers / 'TweakHandler.swift').read_text().replace('import Zsign\n', '')
signing = (handlers / 'SigningHandler.swift').read_text()
jobs = signing[signing.index('\t\tlet hasTweakSpecs'):signing.index('\t\t// iOS "26"')]
guard = signing[signing.index('\t\tlet handler = ZsignHandler'):signing.index('\n\t\tif\n\t\t\t_options.signingOption')]
inject = signing[signing.index('\tprivate func _inject(for'):signing.index('\tprivate func _locateMachosAndChangeToSDK26')]
files = (handlers / 'ZsignHandler.swift').read_text().split('\nstruct SigningValidationError:', 1)[1]
extensions = (root / 'AppExtensionEnumerator.swift').read_text().split('\t/// Resolves the on-disk', 1)[0] + '}'

program = r'''
import Foundation
import OSLog
let fm = FileManager.default
let work = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let payload = Data("bundled guard".utf8)
let resource = work.appendingPathComponent("SandboxEscapeBlock.dylib")
extension Logger { static let misc = Logger(subsystem: "test", category: "guard") }
extension String { static func localized(_ text: String, arguments: String...) -> String { text } }
struct Bundle {
    static let main = Bundle(url: work)!
    var url: URL
    init?(url: URL) { self.url = url }
    func url(forResource name: String, withExtension ext: String) -> URL? {
        let file = url.appendingPathComponent("\(name).\(ext)")
        return fm.fileExists(atPath: file.path) ? file : nil
    }
    var executableURL: URL? { try? SigningBundleFiles.executable(in: url) }
}
extension FileManager {
    func createDirectoryIfNeeded(at url: URL) throws { try createDirectory(at: url, withIntermediateDirectories: true) }
    func removeFileIfNeeded(at url: URL) throws { if fileExists(atPath: url.path) { try removeItem(at: url) } }
    func moveFileIfNeeded(from: URL, to: URL) throws { try moveItem(at: from, to: to) }
    func uniqueTemporaryDirectory(_ name: String) -> URL { work.appendingPathComponent(UUID().uuidString) }
}
enum ExtensionTargeting { case all, mainOnly, selected([String]) }
struct Options {
    enum InjectFolder: String { case root = "/", frameworks = "/Frameworks/" }
    enum InjectPath: String { case rpath = "@rpath", executable_path = "@executable_path" }
    var injectFolder = InjectFolder.frameworks, injectPath = InjectPath.rpath
    var injectionFiles: [URL] = [], tweakInjections: [TweakInjectionSpec]?
    var fixFilePicker = false, blockSandboxEscape = true, experiment_replaceSubstrateWithEllekit = false
    var injectIntoExtensions = true, appIdentifier: String? = "fixture.app"
}
enum OptionsManager { static let shared = OptionsManagerStub() }
struct OptionsManagerStub { var options = Options() }
struct FileConfig {
    var targeting = ExtensionTargeting.all
    func resolvedPath(global: Options.InjectPath) -> Options.InjectPath { global }
    func resolvedFolder(global: Options.InjectFolder) -> Options.InjectFolder { global }
}
enum FileType { case appex }
struct TweakInjectionFile {
    let fileURL: URL
    var fileName: String { fileURL.lastPathComponent }
    var fileType = FileType.appex, config = FileConfig(), enabled = true
}
struct TweakInjectionSpec { var files: [TweakInjectionFile]; var enabled = true }
struct AR {
    init(with: URL) throws {}
    struct Member { let name: String; let content: Data }
    func extract() async throws -> [Member] { fatalError("no .deb fixtures") }
}
func extractFile(at: inout URL) throws { fatalError("no .deb fixtures") }
final class SigningLog {
    static let shared = SigningLog()
    var lines: [String] = []
    func info(_ text: String, category: String = "") { lines.append(text) }
    func error(_ text: String, category: String = "") { lines.append(text) }
}
enum Zsign {
    static var reject = ""
    static var guarded = Set<String>()
    static var touchedWatch = false
    static func changeDylibPath(appExecutable: String, for old: String, with new: String) -> Bool { false }
    static func injectDyLib(appExecutable: String, with path: String, weak: Bool = true) -> Bool {
        let bundle = URL(fileURLWithPath: appExecutable).deletingLastPathComponent()
        if bundle.pathComponents.contains("Watch") { touchedWatch = true }
        if bundle.lastPathComponent == reject { return false }
        if !weak, path.hasPrefix("@loader_path/") {
            let file = bundle.appendingPathComponent(String(path.dropFirst("@loader_path/".count)))
            if (try? Data(contentsOf: file)) == payload { guarded.insert(bundle.lastPathComponent) }
        }
        return true
    }
}
struct ZsignHandler {
    init(appUrl: URL, options: Options, cert: String?) {}
    func disinject() async throws {}
}
''' + tweaks + extensions + '\nstruct SigningValidationError:' + files + r'''
struct Workflow {
    let movedAppPath: URL
    let _options: Options
    let appCertificate: String? = nil
    func prepare() async throws {
''' + jobs + guard + '\n    }\n' + inject + r'''
}
func makeBundle(_ dir: URL) throws {
    try fm.createDirectoryIfNeeded(at: dir)
    let plist: NSDictionary = ["CFBundleExecutable": "Target"]
    try PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
        .write(to: dir.appendingPathComponent("Info.plist"))
    try Data("executable".utf8).write(to: dir.appendingPathComponent("Target"))
}
func require(_ ok: Bool, _ reason: String) throws {
    if !ok { throw SigningValidationError(reason) }
}
@main struct Check {
    static func run() async throws {
        let managed = work.appendingPathComponent("Managed.appex")
        try makeBundle(managed)
        for scenario in ["missing", "main", "extension", "success", "ordinary"] {
            let app = work.appendingPathComponent("\(scenario)/Host.app")
            try makeBundle(app)
            try makeBundle(app.appendingPathComponent("Extensions/BuiltIn.appex"))
            let watch = app.appendingPathComponent("Watch/Companion.app")
            try makeBundle(watch)
            try makeBundle(watch.appendingPathComponent("PlugIns/WatchKit.appex"))
            var options = Options()
            options.tweakInjections = [TweakInjectionSpec(files: [TweakInjectionFile(fileURL: managed)])]
            if scenario != "missing" { try payload.write(to: resource) }
            if scenario == "ordinary" {
                options.blockSandboxEscape = false
                let tweak = work.appendingPathComponent("User.dylib")
                try Data("ordinary".utf8).write(to: tweak)
                options.injectionFiles = [tweak]
            }
            Zsign.reject = scenario == "main" ? "Host.app" :
                (scenario == "extension" || scenario == "ordinary" ? "BuiltIn.appex" : "")
            Zsign.guarded = []
            Zsign.touchedWatch = false
            SigningLog.shared.lines = []
            var failure: Error?
            do { try await Workflow(movedAppPath: app, _options: options).prepare() }
            catch { failure = error }
            let successLog = SigningLog.shared.lines.contains("Injected the sandbox escape guard")
            switch scenario {
            case "success":
                try require(failure == nil && successLog, "guard success not reported")
                try require(!Zsign.touchedWatch, "injected the iPhone guard into the Watch companion")
                try require(Zsign.guarded == ["Host.app", "Managed.appex", "BuiltIn.appex"],
                            "guard missing from host, pre-existing or newly placed extension")
            case "ordinary":
                try require(failure == nil && !successLog && Zsign.guarded.isEmpty, "ordinary tweak no longer tolerant")
            default:
                try require(!successLog && failure != nil, "fail-open guard: \(scenario)")
                let target = scenario == "missing" ? "SandboxEscapeBlock.dylib" : Zsign.reject
                try require(failure?.localizedDescription.contains(target) == true, "failure did not identify \(target)")
            }
        }
    }
    static func main() async {
        do { try await run(); print("PASS: guard required on every final target; ordinary tweaks remain optional") }
        catch { print("FAIL: \(error.localizedDescription)"); exit(1) }
    }
}
'''
with tempfile.TemporaryDirectory(prefix='ryuksign-sandbox-guard-') as directory:
    work = Path(directory)
    source = work / 'Check.swift'
    source.write_text(program)
    binary = work / 'check'
    subprocess.run(['xcrun', 'swiftc', '-parse-as-library', str(source), '-o', str(binary)], check=True)
    subprocess.run([str(binary), str(work)], check=True)
