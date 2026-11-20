"""RyukSign updater; single event loop, one signing slot, no request logs."""
import asyncio
import hashlib
import hmac
import json
import os
import plistlib
import re
import secrets
import shutil
import signal
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from aiohttp import BodyPartReader, ClientSession, ClientTimeout, web

REPO = "oneulddu/RyukSign"
ORIGIN = "https://sign.rootios.com"
MIB = 1024**2
UPLOAD = 2 * MIB
IPA_LIMIT = 96 * MIB
VERSION = re.compile(r"[0-9]{1,5}(?:\.[0-9]{1,5}){2}(?:-r[1-9][0-9]{0,4})?")
LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
BUNDLE = re.compile(fr"{LABEL}(?:\.{LABEL})+")
ALLOW = re.compile(os.environ["BUNDLE_ALLOW"])
PROXY_KEY = Path(os.environ["CREDENTIALS_DIRECTORY"], "proxy-key").read_bytes().strip()
CACHE = Path(os.environ["CACHE_DIRECTORY"])
WORK = Path("/tmp/requests")
JOBS = Path("/tmp/published")
TTL = int(os.getenv("JOB_TTL_SECONDS", "600"))
if not 1 <= TTL <= 1800:
    raise ValueError("invalid job lifetime")
MAX_AGE = min(3 * TTL, 1800)
LIMITS = {"p12": 512 * 1024, "provision": MIB, "version": 64,
          "bundleId": 255, "p12password": 1024}


class Failure(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


async def parse(request):
    if (request.content_type != "multipart/form-data" or not request.content_length
            or request.content_length > UPLOAD or "Content-Encoding" in request.headers):
        raise Failure("bad_request")
    parts = {}
    try:
        async with asyncio.timeout(10):
            reader = await request.multipart()
            async for part in reader:
                if not isinstance(part, BodyPartReader):
                    raise ValueError()
                name = part.name
                if name not in LIMITS or name in parts or any(
                    k in part.headers for k in ("Content-Encoding", "Content-Transfer-Encoding")
                ):
                    raise ValueError()
                data = bytearray()
                while chunk := await part.read_chunk():
                    if len(data) + len(chunk) > LIMITS[name]:
                        raise ValueError()
                    data.extend(chunk)
                parts[name] = bytes(data)
    except (ValueError, KeyError, AssertionError, TimeoutError, RuntimeError):
        raise Failure("bad_request") from None
    version = parts.get("version", b"").decode("ascii", "replace")
    bundle = parts.get("bundleId", b"").decode("ascii", "replace")
    if not VERSION.fullmatch(version):
        raise Failure("bad_version")
    if not BUNDLE.fullmatch(bundle) or not ALLOW.fullmatch(bundle):
        raise Failure("bundle_not_allowed", 403)
    for name in ("p12", "provision"):
        if not parts.get(name):
            raise Failure("missing_" + name)
    return version, bundle, parts


async def release(http, version):
    tag = "v" + version
    async with http.get(f"https://api.github.com/repos/{REPO}/releases/tags/{tag}",
                        allow_redirects=False) as r:
        if r.status == 404:
            raise Failure("unknown_version", 404)
        r.raise_for_status()
        raw = bytearray()
        async for chunk in r.content.iter_chunked(65536):
            raw.extend(chunk)
            if len(raw) > MIB:
                raise Failure("upstream", 502)
        info = json.loads(raw)
    if info["draft"] or info["tag_name"] != tag:
        raise Failure("unknown_version", 404)
    assets = [a for a in info["assets"] if a["name"] == "RyukSign.ipa"
              and a["state"] == "uploaded"]
    if not assets:
        raise Failure("unknown_version", 404)
    asset, = assets
    digest = re.fullmatch(r"sha256:([a-f0-9]{64})", asset.get("digest") or "")
    if not digest or not 0 < asset["size"] <= IPA_LIMIT:
        raise Failure("upstream", 502)
    # Fixed filenames, one verified cache entry. Even hits must match today's metadata.
    cached = CACHE / "release.ipa"
    if cached.exists() and await asyncio.to_thread(file_hash, cached) == digest[1]:
        return cached
    tmp = CACHE / "incoming"
    try:
        h = hashlib.sha256()
        length = 0
        url = f"https://github.com/{REPO}/releases/download/{tag}/RyukSign.ipa"
        async with http.get(url, allow_redirects=False) as r:
            from yarl import URL
            dest = URL(r.headers["Location"])
            if (r.status != 302 or dest.scheme != "https" or dest.user is not None
                    or dest.port != 443 or dest.host != "release-assets.githubusercontent.com"):
                raise Failure("upstream", 502)
        async with http.get(dest, allow_redirects=False) as r:
            if r.status != 200:
                raise Failure("upstream", 502)
            with tmp.open("wb") as f:
                async for chunk in r.content.iter_chunked(128 * 1024):
                    length += len(chunk)
                    if length > asset["size"]:
                        raise Failure("upstream", 502)
                    h.update(chunk)
                    f.write(chunk)
        if length != asset["size"] or h.hexdigest() != digest[1]:
            raise Failure("upstream", 502)
        tmp.replace(cached)
        return cached
    finally:
        tmp.unlink(missing_ok=True)


def file_hash(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


async def run_signer(folder, bundle, parts, ipa):
    for name in ("p12", "provision"):
        (folder / name).write_bytes(parts.pop(name))
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/python3", "-I", "/opt/ryuksign-update/sign.py", bundle, str(ipa),
        cwd=folder, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        async with asyncio.timeout(45):
            await proc.communicate(parts.pop("p12password", b""))
    finally:
        if proc.returncode is None:
            os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
    if proc.returncode == 20:
        raise Failure("bad_cert")
    if proc.returncode == 21:
        raise Failure("bad_provision")
    if proc.returncode:
        raise Failure("sign_failed", 500)


@dataclass
class Job:
    directory: Path
    expires: float
    hard_deadline: float


class Service:
    def __init__(self):
        self.busy = False
        self.jobs = {}

    def expire(self, now):
        for token, job in list(self.jobs.items()):
            if now >= job.expires:
                shutil.rmtree(job.directory)
                del self.jobs[token]

    async def sweep(self):
        while True:
            await asyncio.sleep(10)
            self.expire(time.monotonic())

    async def lifecycle(self, app):
        WORK.mkdir(mode=0o700)
        JOBS.mkdir(mode=0o700)
        (CACHE / "incoming").unlink(missing_ok=True)
        async with ClientSession(timeout=ClientTimeout(total=25, sock_connect=5),
                                 auto_decompress=False,
                                 headers={"Accept-Encoding": "identity",
                                          "User-Agent": "RyukSign-selfupdate"}) as self.http:
            sweep = asyncio.create_task(self.sweep())
            try:
                yield
            finally:
                sweep.cancel()
                await asyncio.gather(sweep, return_exceptions=True)

    async def sign(self, request):
        self.expire(time.monotonic())
        if self.busy or len(self.jobs) >= 8:
            raise Failure("rate_limited", 429)
        self.busy = True
        try:
            version, bundle, parts = await parse(request)
            ipa = await release(self.http, version)
            with tempfile.TemporaryDirectory(dir=WORK) as tmp:
                folder = Path(tmp)
                await run_signer(folder, bundle, parts, ipa)
                signed = folder / "signed.ipa"
                with zipfile.ZipFile(signed) as z:
                    main, = [n for n in z.namelist() if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", n)]
                    info = plistlib.loads(z.read(main))
                if info["CFBundleIdentifier"] != bundle:
                    raise Failure("sign_failed", 500)
                token = secrets.token_urlsafe(32)
                base = f"{ORIGIN}/install/{token}/"
                manifest = {"items": [{"assets": [{"kind": "software-package", "url": base + "signed.ipa"}],
                                       "metadata": {"kind": "software", "title": "RyukSign",
                                                    "bundle-identifier": bundle,
                                                    "bundle-version": info["CFBundleShortVersionString"]}}]}
                published = folder / "output"
                published.mkdir()
                (published / "manifest.plist").write_bytes(plistlib.dumps(manifest))
                signed.rename(published / "signed.ipa")
                dest = JOBS / token
                published.rename(dest)
                now = time.monotonic()
                self.jobs[token] = Job(dest, now + TTL, now + MAX_AGE)
            link = "itms-services://?action=download-manifest&url=" + quote(base + "manifest.plist", safe="")
            return web.json_response({"install": link})
        finally:
            self.busy = False

    async def download(self, request):
        now = time.monotonic()
        self.expire(now)
        job = self.jobs.get(request.match_info["token"])
        if not job:
            raise Failure("not_found", 404)
        job.expires = min(now + TTL, job.hard_deadline)
        # Only the two literal router resources select a filename.
        name = request.match_info.route.name
        return web.FileResponse(job.directory / name, headers={
            "Content-Type": "text/xml" if name == "manifest.plist" else "application/octet-stream"})


@web.middleware
async def boundary(request, handler):
    try:
        if not hmac.compare_digest(request.headers.get("X-Updater-Key", "").encode(), PROXY_KEY):
            raise Failure("forbidden", 403)
        response = await handler(request)
    except Failure as e:
        response = web.json_response({"error": e.code}, status=e.status)
    except web.HTTPException as e:
        response = web.json_response({"error": "bad_request"}, status=e.status)
    except Exception:
        # No traceback: parsers and network exceptions can quote inputs.
        response = web.json_response({"error": "unavailable"}, status=503)
    response.headers["Cache-Control"] = "private, no-store"
    return response


def main():
    service = Service()
    app = web.Application(middlewares=[boundary], client_max_size=UPLOAD,
                          handler_args={"auto_decompress": False})
    app.cleanup_ctx.append(service.lifecycle)
    app.router.add_post("/ryuksign/sign", service.sign)
    for filename in ("signed.ipa", "manifest.plist"):
        app.router.add_get("/install/{token:[A-Za-z0-9_-]+}/" + filename,
                           service.download, name=filename)
    web.run_app(app, host="127.0.0.1", port=18573, access_log=None, print=None)


if __name__ == "__main__":
    main()
