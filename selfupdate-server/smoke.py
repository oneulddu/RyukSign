"""Live, synthetic macOS acceptance test. No device, Apple identity or admin access."""
import argparse
import datetime as dt
import hashlib
import io
import json
import os
import plistlib
import re
import secrets
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

HOST = "https://sign.rootios.com"
BUNDLE = "com.example.RyukUpdateTest"
http = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def openssl(*args, data=None):
    return subprocess.run(["openssl", *args], input=data, capture_output=True, check=True).stdout


def fixture(version, *, wildcard):
    # Only the X.509 issuer *name* selects Zsign's built-in chain. This CA key
    # and leaf are disposable; the resulting chain is NOT trusted by Apple.
    issuer = "/CN=Apple Worldwide Developer Relations Certification Authority/OU=G3/O=Apple Inc./C=US"
    openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", issuer, "-keyout", "ca.key", "-out", "ca.pem")
    openssl("req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=Synthetic updater test",
            "-keyout", "leaf.key", "-out", "leaf.csr")
    openssl("x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
            "-set_serial", "42", "-days", "1", "-out", "leaf.pem")
    password = secrets.token_hex(24).encode()
    p12 = openssl("pkcs12", "-export", "-inkey", "leaf.key", "-in", "leaf.pem", "-passout", "stdin",
                  data=password + b"\n")
    der = openssl("x509", "-in", "leaf.pem", "-outform", "DER")
    team = "SYNTH12345"
    # The explicit profile names another App ID, like per-device reseller profiles.
    entitlements = {"application-identifier": team + "." + ("com.example.*" if wildcard else "app.example.unrelated"),
                    "com.apple.developer.team-identifier": team,
                    "keychain-access-groups": [team + ".*"]}
    profile = plistlib.dumps({"Name": "Synthetic only", "UUID": str(uuid.uuid4()),
                              "DeveloperCertificates": [der], "TeamIdentifier": [team],
                              "ApplicationIdentifierPrefix": [team],
                              "ExpirationDate": dt.datetime.utcnow() + dt.timedelta(days=1),
                              "Entitlements": entitlements})
    provision = openssl("cms", "-sign", "-binary", "-nodetach", "-signer", "leaf.pem",
                        "-inkey", "leaf.key", "-outform", "DER", data=profile)
    return {"version": version.encode(), "bundleId": BUNDLE.encode(), "p12password": password,
            "p12": p12, "provision": provision}, der, entitlements


def post(fields):
    boundary = "RyukSign-" + str(uuid.uuid4()).upper()
    body = bytearray()
    for name, value in fields.items():
        header = f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
        if name in ("p12", "provision"):
            filename = "certificate.p12" if name == "p12" else "profile.mobileprovision"
            header += f'; filename="{filename}"\r\nContent-Type: application/octet-stream'
        body += header.encode() + b"\r\n\r\n" + value + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(HOST + "/ryuksign/sign", data=bytes(body), headers={
        "Content-Type": "multipart/form-data; boundary=" + boundary})
    try:
        with http.open(req, timeout=110) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def verify_signature(executable, expected, bundle, entitlements):
    # LC_CODE_SIGNATURE -> SuperBlob -> detached CMS over the primary CodeDirectory.
    # Verify the signature itself, not just that the IPA contains a certificate.
    if executable[:4] != b"\xcf\xfa\xed\xfe":
        Path("main").write_bytes(executable)
        subprocess.run(["lipo", "main", "-thin", "arm64", "-output", "thin"], check=True)
        executable = Path("thin").read_bytes()
    off = 32
    for _ in range(struct.unpack_from("<I", executable, 16)[0]):
        cmd, size = struct.unpack_from("<II", executable, off)
        if cmd == 0x1d:
            start, length = struct.unpack_from("<II", executable, off + 8)
            superblob = executable[start:start + length]
            break
        off += size
    else:
        raise AssertionError("no Mach-O code signature")
    slots = {}
    for i in range(struct.unpack_from(">I", superblob, 8)[0]):
        slot, offset = struct.unpack_from(">II", superblob, 12 + i * 8)
        size = struct.unpack_from(">I", superblob, offset + 4)[0]
        slots[slot] = superblob[offset:offset + size]
    Path("directory").write_bytes(slots[0])
    openssl("cms", "-verify", "-binary", "-noverify", "-inform", "DER", "-content", "directory",
            "-signer", "actual.pem", "-out", os.devnull, data=slots[0x10000][8:])
    assert openssl("x509", "-in", "actual.pem", "-outform", "DER") == expected, "wrong signing identity"
    ident_offset, = struct.unpack_from(">I", slots[0], 20)
    assert slots[0][ident_offset:].split(b"\0", 1)[0].decode() == bundle, "wrong signed identifier"
    assert plistlib.loads(slots[5][8:]) == entitlements, "profile entitlements changed"


def main_plist(z):
    main, = [p for p in z.namelist() if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", p)]
    return main, plistlib.loads(z.read(main))


def version_fields(info):
    return info["CFBundleShortVersionString"], info["CFBundleVersion"]


def success(fields, cert, entitlements, *, wildcard):
    tag = "v" + fields["version"].decode()
    source = f"https://github.com/oneulddu/RyukSign/releases/download/{tag}/RyukSign.ipa"
    with http.open(source, timeout=30) as r, zipfile.ZipFile(io.BytesIO(r.read())) as z:
        _, original = main_plist(z)
        expected = version_fields(original)
    status, result = post(fields)
    received = time.monotonic()
    assert status == 200, (status, result.get("error"))
    link = urllib.parse.urlsplit(result["install"])
    q = urllib.parse.parse_qs(link.query)
    assert link.scheme == "itms-services" and q["action"] == ["download-manifest"]
    manifest, = q["url"]
    assert re.fullmatch(re.escape(HOST) + r"/install/[A-Za-z0-9_-]{43}/manifest\.plist", manifest)
    with http.open(manifest, timeout=15) as r:
        assert r.headers.get_content_type() == "text/xml", r.headers.get("Content-Type")
        item, = plistlib.loads(r.read())["items"]
    assert item["metadata"]["bundle-identifier"] == BUNDLE
    asset, = item["assets"]
    ipa = manifest.rsplit("/", 1)[0] + "/signed.ipa"
    assert asset == {"kind": "software-package", "url": ipa}
    with http.open(ipa, timeout=30) as r, zipfile.ZipFile(io.BytesIO(r.read())) as z:
        main, info = main_plist(z)
        extensions = [p for p in z.namelist() if ".appex/" in p]
        if wildcard:
            widget, = [p for p in extensions if p.endswith(".appex/Info.plist")]
            wi = plistlib.loads(z.read(widget))
            assert wi["NSExtension"]["NSExtensionPointIdentifier"] == "com.apple.widgetkit-extension"
            widget_id = BUNDLE + ".FeatherWidgetExtension"
            assert wi["CFBundleIdentifier"] == widget_id
            verify_signature(z.read(widget.rsplit("/", 1)[0] + "/" + wi["CFBundleExecutable"]),
                             cert, widget_id, entitlements)
        else:
            assert not extensions, "explicit profile left an extension in the IPA"
        assert version_fields(info) == expected, (tag, "source", expected, "signed", version_fields(info))
        assert info["CFBundleIdentifier"] == BUNDLE
        assert item["metadata"]["bundle-version"] == info["CFBundleShortVersionString"]
        app = main.rsplit("/", 1)[0] + "/"
        assert z.read(app + "embedded.mobileprovision") == fields["provision"]
        verify_signature(z.read(app + info["CFBundleExecutable"]), cert, BUNDLE, entitlements)
    print("PASS", tag, "source/signed (short, build):", expected,
          "wildcard: signed widget" if wildcard else "explicit: no widget",
          "— TLS OTA, version, embedded profile, signed identifiers, entitlements and CMS; signer SHA256",
          hashlib.sha256(cert).hexdigest(), flush=True)
    return manifest, ipa, received


def check_lifetime(jobs, idle):
    cold, active = jobs
    manifest, ipa, received = active

    def at(intervals):
        time.sleep(max(0, received + intervals * idle - time.monotonic()))

    def expired(url):
        try:
            http.open(urllib.request.Request(url, method="HEAD"), timeout=5).close()
        except urllib.error.HTTPError as e:
            assert e.code == 404, e.code
        else:
            raise AssertionError("expired URL still available")

    def head():
        with http.open(urllib.request.Request(manifest, method="HEAD"), timeout=5) as r:
            assert r.status == 200

    def resume():
        req = urllib.request.Request(ipa, headers={"Range": "bytes=1-1"})
        with http.open(req, timeout=5) as r:
            assert r.status == 206 and len(r.read()) == 1

    at(0.6)
    head()
    at(1.25)
    resume()  # Past the initial idle deadline, due to HEAD.
    for url in cold[:2]:
        expired(url)
    at(1.95)
    head()  # Past the first HEAD's deadline, due to the range GET.
    at(2.65)
    resume()
    at(3.05)
    for url in active[:2]:
        expired(url)  # Less than one idle window since the last GET.
    print(f"PASS idle expiry, HEAD/range GET renewal and {3 * idle}s publication cap")


def check_host_origin():
    # Execute the requests on the signing host; the Mac is an allowed peer.
    codes = subprocess.check_output(["ssh", "-o", "BatchMode=yes", "ubuntu", """
        set -eu
        for addr in "$(tailscale ip -4)" "[$(tailscale ip -6)]"; do
            curl --noproxy '*' -sS --max-time 5 --resolve "sign.rootios.com:443:$addr" \
                -d '' -o /dev/null -w '%{http_code}\\n' https://sign.rootios.com/ryuksign/sign
        done"""], text=True, timeout=20)
    assert codes.splitlines() == ["403", "403"], codes
    print("PASS host-origin POST refused over both Tailscale addresses")


def failures(fields):
    for change, status, code in [
        ({"version": b"../"}, 400, "bad_version"),
        ({"version": b"0.0.0-r99999"}, 404, "unknown_version"),
        ({"bundleId": b"com/example.invalid"}, 403, "bundle_not_allowed"),
        ({"p12": None}, 400, "missing_p12"),
        ({"provision": None}, 400, "missing_provision"),
        ({"p12password": b"definitely incorrect"}, 400, "bad_cert"),
        ({"provision": b"not a CMS profile"}, 400, "bad_provision"),
    ]:
        sent = {k: v for k, v in {**fields, **change}.items() if v is not None}
        actual = post(sent)
        assert actual == (status, {"error": code}), (code, actual[0], actual[1].get("error"))
        print("PASS", code, flush=True)
    # Fill this client's nginx burst with cheap invalid requests (never signs).
    for _ in range(14):
        actual = post({"version": b"invalid"})
        if actual == (429, {"error": "rate_limited"}):
            print("PASS rate_limited", flush=True)
            return
        assert actual == (400, {"error": "bad_version"})
    raise AssertionError("per-IP burst not limited")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="override the default plain/rebuild tag pair for both profiles")
    parser.add_argument("--success-only", action="store_true")
    parser.add_argument("--expiry-seconds", type=int, help="exercise renewal/cap; match a short deployed TTL")
    parser.add_argument("--check-host", action="store_true", help="check both local-origin denials via ssh ubuntu")
    args = parser.parse_args()
    if args.check_host:
        check_host_origin()
    with tempfile.TemporaryDirectory() as tmp:
        original = Path.cwd()
        os.chdir(tmp)
        try:
            jobs = []
            for wildcard, version in ((False, "4.1.0"), (True, "4.1.0-r1")):
                fields, cert, entitlements = fixture(args.version or version, wildcard=wildcard)
                jobs.append(success(fields, cert, entitlements, wildcard=wildcard))
            if args.expiry_seconds:
                check_lifetime(jobs, args.expiry_seconds)
            if not args.success_only:
                failures(fields)
        finally:
            os.chdir(original)


if __name__ == "__main__":
    main()
