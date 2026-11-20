"""Exercise the signing client's successful HTTP reply path without contacting a server."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / "RyukSign/Backend/Observable/SelfUpdateManager.swift").read_text()
policy = source[source.index("private static func installURL("):source.index("private func requestServerSign(")]
success = source[source.index("if (200..<300).contains(status)", source.index("private func requestServerSign(")):
                 source.index("let rawBody", source.index("private func requestServerSign("))]

program = '''
import Foundation
extension String { static func localized(_ s: String) -> String { s } }
enum Client {
''' + policy + '''
    static func reply(_ install: String, base: URL) throws -> URL {
        let status = 200, json: [String: Any]? = ["install": install]
''' + success + '''
        fatalError("unreachable HTTP status")
    }
}
''' + r'''
let base = URL(string: "https://sign.rootios.com")!
let prefix = "itms-services://?action=download-manifest&url="
// Same alphabet as the server's URL-safe job token.
let manifest = "https%3A%2F%2Fsign.rootios.com%2Finstall%2FaZ09-_token%2Fmanifest.plist"
func fail(_ message: String) -> Never { fputs("FAIL: \(message)\n", stderr); exit(1) }
for (origin, target) in [
    (base, manifest),
    (base, manifest.replacingOccurrences(of: ".com", with: ".COM%3A443")),
    (URL(string: "https://sign.rootios.com:8443/base")!,
     manifest.replacingOccurrences(of: ".com", with: ".com%3A8443"))
] {
    let url = try Client.reply(prefix + target, base: origin)
    let opened = URLComponents(url: url, resolvingAgainstBaseURL: false)
    guard opened?.scheme == "itms-services",
          opened?.queryItems?.contains(URLQueryItem(name: "action", value: "download-manifest")) == true,
          opened?.queryItems?.first(where: { $0.name == "url" })?.value == target.removingPercentEncoding
    else { fail("valid manifest was not preserved") }
}
for invalid in [
    "https://sign.rootios.com/install",
    "itms-services://?action=other&url=" + manifest,
    "itms-services://?action=download-manifest",
    prefix, prefix + "%", prefix + "%2Frelative",
    prefix + "https%3A%2F%2F",
    prefix + manifest.replacingOccurrences(of: "https", with: "http"),
    prefix + manifest.replacingOccurrences(of: "sign.rootios.com", with: "other.invalid"),
    prefix + manifest.replacingOccurrences(of: ".com", with: ".com.evil.invalid"),
    prefix + manifest.replacingOccurrences(of: ".com", with: ".com%3A8443"),
    prefix + manifest.replacingOccurrences(of: "sign.", with: "user%40sign."),
    prefix + manifest + "&url=https://other.invalid",
    "itms-services://other.invalid?action=download-manifest&url=" + manifest
] {
    do {
        _ = try Client.reply(invalid, base: base)
        fail("accepted \(invalid)")
    } catch let error as NSError {
        guard error.domain == "SelfUpdate", error.code == -6 else { fail("unexpected error: \(error)") }
    }
}
print("Self-update install link validation passed")
'''

with tempfile.TemporaryDirectory() as temp:
    main = Path(temp) / "main.swift"
    main.write_text(program)
    subprocess.run(["swiftc", str(main), "-o", str(main.with_suffix(""))], check=True)
    subprocess.run([str(main.with_suffix(""))], check=True)
