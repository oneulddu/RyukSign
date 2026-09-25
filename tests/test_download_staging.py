"""Production download staging with fake URLSession files; no network or app data."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'RyukSign/Backend/Observable/Download.swift').read_text()


def declaration(signature):
    start = source.index(signature)
    comment = source.rfind('\n\n', 0, start)
    opening = source.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[comment:end]


start = source.index('\t/// Owned by this Download instance')
property_decl = source[start:source.index('\n\t\n', start)]
methods = declaration('\tfunc stageFile(') + declaration('\tfunc removeStagedFiles(')
delegate = (root / 'RyukSign/Backend/Observable/DownloadManager+Delegates.swift').read_text()
assert 'download.stageFile(' in delegate and 'removeFileIfNeeded(at: destinationURL)' not in delegate
manager = (root / 'RyukSign/Backend/Observable/DownloadManager.swift').read_text()
for name in ['func cancelDownload(', 'private func finishImport(']:
    body = manager[manager.index(name):]
    assert 'download.removeStagedFiles()' in body[:body.index('\n\t}\n')], name

program = r'''
import Foundation
var stagingRoot: URL!
extension FileManager { var downloadStaging: URL { stagingRoot } }
final class Download {
    let fileName: String
    init(fileName: String) { self.fileName = fileName }
''' + property_decl + methods + r'''
}
@main struct Check {
    static func main() throws {
        let fm = FileManager.default
        let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        stagingRoot = root.appendingPathComponent("staging")
        func finished(_ text: String) throws -> URL {
            let url = root.appendingPathComponent(UUID().uuidString)
            try Data(text.utf8).write(to: url)
            return url
        }
        // Two downloads with the same server filename keep separate bytes.
        let first = Download(fileName: "App")
        let second = Download(fileName: "App")
        let a = try first.stageFile(at: try finished("first"), suggestedFilename: "app.ipa")
        let b = try second.stageFile(at: try finished("second"), suggestedFilename: "app.ipa")
        precondition(a != b && a.lastPathComponent == "app.ipa" && b.lastPathComponent == "app.ipa")
        precondition(try! String(contentsOf: a, encoding: .utf8) == "first")
        precondition(try! String(contentsOf: b, encoding: .utf8) == "second")

        // Cleanup removes only the owner's directory.
        first.removeStagedFiles()
        precondition(!fm.fileExists(atPath: a.path) && fm.fileExists(atPath: b.path))
        first.removeStagedFiles()

        // Path-like or empty names stay inside the owner's directory.
        for (name, expected) in [("../../escape.ipa", "escape.ipa"), ("dir\\evil.ipa", "evil.ipa"),
                                 ("..", "download.ipa"), ("", "download.ipa"), (nil, "App")] as [(String?, String)] {
            let owner = Download(fileName: "App")
            let staged = try owner.stageFile(at: try finished("x"), suggestedFilename: name)
            precondition(staged.deletingLastPathComponent() == owner.stagingDirectory)
            precondition(staged.lastPathComponent == expected, "\(String(describing: name)) -> \(staged.lastPathComponent)")
            owner.removeStagedFiles()
        }
        second.removeStagedFiles()
        precondition(try! fm.contentsOfDirectory(atPath: stagingRoot.path).isEmpty)
        print("PASS: same-name downloads isolated; owner-only cleanup; unsafe names contained; manager cleanup hooks present")
    }
}
'''

with tempfile.TemporaryDirectory(prefix='ryuksign-download-staging-') as temporary:
    work = Path(temporary)
    (work / 'Check.swift').write_text(program)
    subprocess.run(['swiftc', '-parse-as-library', str(work / 'Check.swift'), '-o', str(work / 'check')], check=True)
    subprocess.run([str(work / 'check'), temporary], check=True)
