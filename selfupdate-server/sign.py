"""Short-lived, time-limited child. Its cwd is a fresh private tmpfs directory."""
import datetime
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.serialization.pkcs12 import load_key_and_certificates


def identity():
    try:
        key, cert, _ = load_key_and_certificates(Path("p12").read_bytes(), sys.stdin.buffer.read())
        if key is None or cert is None:
            raise ValueError()
        Path("key").write_bytes(key.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8,
                                                  ser.NoEncryption()))
        Path("cert").write_bytes(cert.public_bytes(ser.Encoding.PEM))
        return cert.public_bytes(ser.Encoding.DER)
    except Exception:
        sys.exit(20)


def profile(bundle, cert):
    ca = os.environ.get("PROFILE_CA_FILE")
    trust = ["-CAfile", ca, "-purpose", "any", "-no-CApath", "-no-CAstore"] if ca else ["-noverify"]
    try:
        content = subprocess.check_output(["/usr/bin/openssl", "cms", "-verify", "-binary",
                                           "-inform", "DER", "-in", "provision", *trust], timeout=5)
        p = plistlib.loads(content)
        prefix, pattern = p["Entitlements"]["application-identifier"].split(".", 1)
        if (cert not in p["DeveloperCertificates"] or prefix not in p["ApplicationIdentifierPrefix"]
                or not re.fullmatch("[A-Z0-9]{10}", p["TeamIdentifier"][0])
                or not re.fullmatch("[A-Z0-9]{10}", prefix)
                or p["ExpirationDate"] <= datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)):
            raise ValueError()
        # A terminal wildcard covering the main ID also covers its suffixed widget ID.
        # Any other App ID still signs the installed bundle ID unchanged, as the app's
        # own on-device signing does: per-device reseller profiles carry an unrelated
        # explicit App ID, and the in-place update must keep the installed bundle ID.
        return pattern.endswith("*") and bundle.startswith(pattern[:-1])
    except Exception:
        sys.exit(21)


if __name__ == "__main__":
    bundle, ipa = sys.argv[1:]
    wildcard = profile(bundle, identity())
    # No -e or -R: the same ZSignAsset/SignFolder path as the Swift wrapper.
    os.execv("/opt/ryuksign-update/zsign", ["zsign", "-q", "-f", *([] if wildcard else ["-E"]),
             "-k", "key", "-c", "cert", "-m", "provision", "-b", bundle, "-U",
             "-t", ".", "-z", "5", "-o", "signed.ipa", ipa])
