"""Exercise release parsing and availability with installed bundle metadata."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / "RyukSign/Backend/Observable/SelfUpdateManager.swift").read_text()


def section(start, end):
    return source[source.index(start):source.index(end)]


program = '''
import Foundation
struct Bundle {
    static var main = Bundle()
    var version = "4.1.0"
    var infoDictionary: [String: Any]? = nil
}
''' + section("struct SelfUpdateRelease:", "enum SelfUpdatePhase:") + '''
final class SelfUpdateManager {
    var available: SelfUpdateRelease?
    var latestSemver: SelfUpdateRelease?
    var ignoredVersions: Set<String> = []
''' + section("private func recomputeAvailable()", "// MARK: - Releases fetch") + section(
    "private static func parse(", "// MARK: - Install"
) + '''
    func offered(_ tags: String...) -> String? {
        let releases = tags.enumerated().compactMap { Self.parse(["id": $0.offset, "tag_name": $0.element]) }
''' + section("latestSemver = releases", "UserDefaults.standard.set(Date()") + '''
        recomputeAvailable()
        return available?.version
    }
}
func expect(_ ok: Bool, _ message: String) {
    if !ok { fputs("FAIL: " + message + "\\n", stderr); exit(1) }
}
let manager = SelfUpdateManager()
let versions = ["4.1.0", "4.1.0-r1", "4.1.0-r2", "4.1.0-r10", "4.1.1"]
for (i, a) in versions.enumerated() {
    for (j, b) in versions.enumerated() {
        expect(SelfUpdateManager.compare(a, b) == (i < j ? .orderedAscending : i > j ? .orderedDescending : .orderedSame), "ordering " + a + " vs " + b)
    }
}
expect(manager.offered("v4.1.0-r1") == "4.1.0-r1", "base installation missed rebuild")
Bundle.main.infoDictionary = ["RyukSignRevision": "2"]
expect(manager.offered("v4.1.0-r1") == nil, "offered older rebuild")
expect(manager.offered("v4.1.0-r2") == nil && manager.latestSemver?.isInstalled == true, "installed rebuild not recognized")
expect(manager.offered("v4.1.0-r10") == "4.1.0-r10" && manager.latestSemver?.isInstalled == false, "missed later rebuild")
expect(manager.offered("v4.1.1") == "4.1.1", "missed newer base")
expect(manager.offered("v4.0.0-r3", "v4.1.0-r10") == "4.1.0-r10", "later-published older rebuild hid a newer release")
for tag in ["v5.0", "v5.0.0.1", "v5.0.0-r0", "v5.0.0-rc1", "v5.0.0-r1-beta"] {
    expect(manager.offered(tag) == nil, "offered invalid tag " + tag)
}
print("Self-update version checks passed")
'''

with tempfile.TemporaryDirectory() as temp:
    main = Path(temp) / "main.swift"
    main.write_text(program)
    subprocess.run(["swiftc", str(main), "-o", str(main.with_suffix(""))], check=True)
    subprocess.run([str(main.with_suffix(""))], check=True)
