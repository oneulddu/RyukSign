"""Check real macOS ATS decisions using the app's transport policy; no iOS Simulator."""
from pathlib import Path
import argparse
import copy
import json
import plistlib
import subprocess
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('--plist', type=Path, default=Path(__file__).resolve().parents[1] / 'RyukSign/Resources/Info.plist')
args = parser.parse_args()
policy = plistlib.loads(args.plist.read_bytes())['NSAppTransportSecurity']

# These are non-self addresses: a server's own interface IP can bypass ATS classification.
# An allowed request may time out or be refused; this checks ATS, not server availability.
allowed = ['100.64.0.1', '10.255.255.254', '172.31.255.254', '192.168.255.254']
blocked = ['100.63.255.254', '100.128.0.1', '11.0.0.1', '172.32.0.1', '192.169.0.1', '198.51.100.1']
swift = r'''
import Foundation
let done = DispatchSemaphore(value: 0)
let configuration = URLSessionConfiguration.ephemeral
configuration.timeoutIntervalForRequest = 1
configuration.timeoutIntervalForResource = 2
let session = URLSession(configuration: configuration)
var request = URLRequest(url: URL(string: CommandLine.arguments[1])!)
request.httpMethod = "HEAD"
session.dataTask(with: request) { _, response, error in
    let error = error as NSError?
    let result: [String: Any] = ["status": (response as? HTTPURLResponse)?.statusCode ?? 0,
                               "domain": error?.domain ?? "", "error": error?.code ?? 0]
    let json = try! JSONSerialization.data(withJSONObject: result, options: .sortedKeys)
    print(String(data: json, encoding: .utf8)!)
    done.signal()
}.resume()
if done.wait(timeout: .now() + 5) == .timedOut { exit(2) }
session.invalidateAndCancel()
'''

with tempfile.TemporaryDirectory(prefix='ryuksign-ats-check-') as directory:
    root = Path(directory)
    source = root / 'Probe.swift'; source.write_text(swift)
    def build(name, ats):
        app = root / (name + '.app')
        (app / 'Contents/MacOS').mkdir(parents=True)
        info = {'CFBundleExecutable': 'Probe', 'CFBundleIdentifier': 'local.ryuksign.ats.' + name,
                'CFBundlePackageType': 'APPL', 'CFBundleVersion': '1', 'NSAppTransportSecurity': ats}
        plist = app / 'Contents/Info.plist'; plist.write_bytes(plistlib.dumps(info))
        binary = app / 'Contents/MacOS/Probe'
        subprocess.run(['swiftc', str(source), '-o', str(binary), '-Xlinker', '-sectcreate',
                        '-Xlinker', '__TEXT', '-Xlinker', '__info_plist', '-Xlinker', str(plist)], check=True)
        return binary
    def denied(binary, host):
        process = subprocess.run([str(binary), f'http://{host}:8765/ats-probe'],
                                 check=True, capture_output=True, text=True, timeout=8)
        result = json.loads(process.stdout)
        return result['domain'] == 'NSURLErrorDomain' and result['error'] == -1022, result
    # Validate that this environment actually enforces ATS for a non-self Tailscale IP.
    original = copy.deepcopy(policy); original.pop('NSExceptionDomains', None)
    baseline = build('baseline', original)
    is_denied, result = denied(baseline, allowed[0])
    assert is_denied, f'ATS baseline did not reject the non-self address: {result}'
    print('PASS baseline: non-self Tailscale HTTP rejected by ATS', flush=True)
    current = build('current', policy)
    for host in allowed:
        is_denied, result = denied(current, host)
        assert not is_denied, f'{host} still blocked by ATS: {result}'
        print(f'PASS HTTP exception {host}: ATS permits connection (result {result["error"]})', flush=True)
    for host in blocked:
        is_denied, result = denied(current, host)
        assert is_denied, f'Public/out-of-range HTTP unexpectedly permitted: {host}: {result}'
        print(f'PASS public/out-of-range {host}: ATS still blocks HTTP', flush=True)
