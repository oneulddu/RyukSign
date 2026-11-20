# RyukSign self-update service

`POST https://sign.rootios.com/ryuksign/sign` is the interface in
`SelfUpdateManager.requestServerSign`. The phone must be on the tailnet for
**all three** requests: sign, fetch manifest, fetch IPA. The 256-bit random
download URL is a bearer credential; don't paste it into tickets.

The service accepts only the public release `v<version>` in `oneulddu/RyukSign`
and its uploaded `RyukSign.ipa`. Tags are three decimal components optionally
followed by `-rN`. It consults GitHub even on a cache hit and checks SHA-256
against GitHub's asset metadata. This trusts that repository and GitHub, not
user-supplied URLs or IPAs. A cache hit does not avoid GitHub API rate limits.

One bounded request at a time may sign; downloads remain available. nginx
allows 6 sign requests/min per source IP with an 11-request burst. Uploads
are limited to 2 MiB, release assets to 96 MiB, signing to 45 seconds.
Subprocess output, access logs and vhost error logs are disabled deliberately.
nginx streams fixed-length requests. A root-only generated proxy header
prevents other loopback clients from bypassing nginx.
The vhost admits remote tailnet peers, **not the signer's own Tailscale
addresses**. If the host is renumbered, update both the source exclusions and
the destination map before reloading.

Credentials never go into argv. The password goes down a pipe; PKCS#12,
the derived PEM key and the profile are used in a mode-0700 request directory
inside the unit's private size-limited, non-swappable tmpfs. Normal completion
removes it, including on errors. systemd destroys the mount and kills the
cgroup on stop/crash. Only the unsigned release cache is persistent.
**The signed IPA necessarily contains the public signing certificate and
embedded provisioning profile** (which can include device IDs); the private
key, password and PKCS#12 are not published. The output and manifest also
live only in that tmpfs and disappear on restart. The initial **idle window
is 10 minutes** (`JOB_TTL_SECONDS=600`). A GET or HEAD on either artifact,
including a range request to resume an IPA, renews the whole job for another
idle window, but never beyond **30 minutes from publication** (the smaller of
three idle windows and 1800 seconds). After either deadline new requests get
404; a transfer already admitted may finish. The idle sweeper unlinks within
another 10 seconds. There is no user/account history or recovery of old links.

The app sends just one profile. The server matches the uploaded identity
against that profile, checks expiry, team/prefix and the bundle pattern, and
passes **no entitlement override**. The pinned signer takes the profile's
`Entitlements` dictionary as-is for the main and nested executables; it does
not specialize wildcard application identifiers or keychain groups. Zsign
embeds the profile in the main app and replaces the original bundle ID in the
widget ID. The requested version selects the GitHub release only. Signing
preserves both `CFBundleShortVersionString` and `CFBundleVersion` from the
release IPA, including for rebuild tags such as `v4.1.0-r1`. The OTA manifest's
`bundle-version` uses the signed IPA's `CFBundleShortVersionString`, as in the
app's local install manifest, not the Git build SHA or release tag. nginx
proxies the manifest with its `text/xml` media type.

A wildcard covering the requested main ID also covers its suffixed widget
ID: Zsign keeps and recursively signs the widget with the same identity. An
explicit main App ID cannot cover the widget; **only then** does the server
use `-E` and omit extensions. No separate widget profile is synthesized.
This comparison is to on-device signing with an empty entitlements path
and **Remove Provisioning disabled**. `installViaIDevice` uses the phone's
saved options, not a fixed preset (`Options.defaultOptions.removeProvisioning`
is `true`); the server does not reproduce arbitrary local settings. Signing
parity is not byte-for-byte IPA equality or proof of device installability.

## Install (Ubuntu 24.04, ARM64)

1. Check the two Tailscale interface addresses in `zz-ryuksign-update`, the
   DNS A record, the existing wildcard certificate, that `127.0.0.1:18573` is
   unused, and that the account, paths and unit below aren't already owned
   by anything else. Capture response statuses of the existing sites. Do **not**
   dump `nginx -T`: once installed, its output includes the proxy header.
2. Required packages: `g++`, `make`, `pkg-config`, `libssl-dev`, `openssl`,
   `python3`, `python3-cryptography`, `python3-aiohttp`, nginx. Reuse the
   existing nginx and certificate. On this host only the aiohttp package and
   its five dependencies needed installation. Simulate any package install
   first; use `--no-install-recommends --no-upgrade` and
   `NEEDRESTART_MODE=l` to avoid restarting unrelated services.
3. From the Mac, at repository root (no Git state changes):

   ```bash
   build=$(ssh ubuntu 'mktemp -d /tmp/ryuksign-build.XXXXXXXX')
   pin=$(git ls-tree HEAD Zsign | cut -d' ' -f3 | cut -f1)
   git -C Zsign archive "$pin" build/linux/Makefile src |
     ssh ubuntu "tar xf - -C '$build'"
   scp selfupdate-server/{server.py,sign.py,settings,ryuksign-update.service,zz-ryuksign-update} ubuntu:"$build/"
   ssh ubuntu "make -C '$build/build/linux' -j2 VERSION=$pin"
   ```

4. On Ubuntu, in **that build directory**, create the account and install:

   ```bash
   sudo useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin ryuksign-update
   sudo install -d -m 0755 /opt/ryuksign-update
   sudo install -d -m 0700 /etc/ryuksign-update
   sudo install -m 0755 bin/zsign /opt/ryuksign-update/
   sudo install -m 0644 server.py sign.py /opt/ryuksign-update/
   sudo install -m 0600 settings /etc/ryuksign-update/
   sudo python3 - <<'PY'
   import os, secrets
   from pathlib import Path
   os.umask(0o077)
   conf = Path("/etc/ryuksign-update")
   key = secrets.token_hex(32)
   with (conf / "proxy-key").open("x") as f: print(key, file=f)
   with (conf / "proxy-header").open("x") as f: print("proxy_set_header X-Updater-Key " + key + ";", file=f)
   PY
   sudo install -m 0644 ryuksign-update.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now ryuksign-update
   sudo install -m 0644 zz-ryuksign-update /etc/nginx/sites-available/
   sudo ln -s /etc/nginx/sites-available/zz-ryuksign-update /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   ```

   If validation fails, remove **only the new symlink**, don't reload and
   investigate. Verify the unit, tailnet TLS, public refusal and other sites;
   remove the temporary build directory. The unit creates
   `/var/cache/ryuksign-update` (0700); only a verified `release.ipa` is
   retained. No cron job, database, firewall rule, DNS hook or cert hook.

## Redeploy / verify

Repeat step 3 after a pin change. Install changed files as in step 4, keeping
the existing proxy secret and site-specific settings. `systemctl daemon-reload`
after unit edits; `systemctl restart ryuksign-update` for code/settings/binary
changes; `nginx -t && systemctl reload nginx` only for nginx changes. Schedule
service restarts between installs because outstanding links are invalidated.

From the Mac, run `python3 selfupdate-server/smoke.py` to verify the wildcard
widget signature/ID and the explicit-profile IPA without a widget, profile
entitlements, and all eight error codes with disposable, **untrusted** credentials.
By default it compares both main version keys with independently downloaded
`v4.1.0` and `v4.1.0-r1` release IPAs and checks each manifest against its signed IPA.
It needs `openssl`, macOS `lipo` for fat binaries, network access
and an unused rate-limit burst. Do not use personal certificates.
For host-side inspection, root can count (without reading) entries below
`/proc/$(systemctl show -p MainPID --value ryuksign-update)/root/tmp/{requests,published}`.
Check this after a successful and failed request. Check expiry after the
idle or hard deadline plus one sweep, without restarting. For a timed live
check, temporarily set `JOB_TTL_SECONDS=16`, restart **only** `ryuksign-update`,
and run `python3 selfupdate-server/smoke.py --check-host --expiry-seconds 16`
from the Mac. This checks both host-local denials, idle expiry, HEAD and ranged
GET renewal, and refusal at the 48-second cap. Restore 600 and restart the
unit afterwards. A public-IP
`curl --resolve sign.rootios.com:443:PUBLIC_IP https://sign.rootios.com/ryuksign/sign`
must get **403**, not a redirect or a backend response.

## Before making it public

Keep it private unless explicitly needed. Tailnet clients are trusted to
submit profiles; CMS integrity is checked but **Apple chain trust is not
required** in the private configuration. For public use, first set
`BUNDLE_ALLOW` in `/etc/ryuksign-update/settings` to a narrow full-match regex
(e.g. `com\.example\.RyukSign`), and set `PROFILE_CA_FILE` to a root-owned
readable bundle of the appropriate Apple profile-signing CA roots, outside
the inaccessible `/etc/ryuksign-update` directory. Confirm with a real valid
profile and confirm the synthetic one is refused. Certificate expiry,
revocation, device eligibility and entitlement policy also need operational
review; a bundle name alone is **not** client authentication. Restrict clients
at a trusted gateway/ACL compatible with the iOS client, budget GitHub rate
limits, review abuse limits, patch dependencies and
test cold-cache latency with a phone before accepting untrusted traffic.

- **Isolate the native signer** under a separate unprivileged account with
  no network, no proxy key, and access only to its own job. The networked API
  should fetch verified releases and publish outputs, not share its privileges
  or other jobs with the process parsing uploaded profiles and keys.
- **The iOS client** (`SelfUpdateManager`) already requires an HTTPS
  `ServerBaseURL`, refuses redirects on the credential upload, and opens only
  an `itms-services` download-manifest link whose manifest is HTTPS on that
  same host and port (`tests/test_self_update_install.py`). Still open: it
  writes the first 300 bytes of a non-2xx reply to the app's file log, and it
  cannot tell a compromised server's IPA from a genuine one.

Only then change the two nginx `geo`/`map` gates deliberately (both source
and destination are checked), test and reload, and change DNS. Keep TLS,
proxy authentication, bounded signing, log suppression and expiring links.
Installing a CDN changes peer identity and may cache credentials/artifacts:
don't merely switch on proxying or trust arbitrary forwarded headers.

## Remove

Remove `/etc/nginx/sites-enabled/zz-ryuksign-update`; run `sudo nginx -t &&
sudo systemctl reload nginx`. Disable and stop **only** `ryuksign-update.service`.
Remove its site in `/etc/nginx/sites-available`, unit in `/etc/systemd/system`,
and its dedicated `/opt/ryuksign-update`, `/etc/ryuksign-update` and
`/var/cache/ryuksign-update` directories. Run `sudo systemctl daemon-reload`,
then `sudo userdel ryuksign-update` and remove its same-name group if still
present. Only if no other software now needs them, simulate removal of
`python3-aiohttp python3-aiosignal python3-async-timeout python3-frozenlist
python3-multidict python3-yarl` before removing those six packages. Don't
autoremove, change other sites, or remove nginx, OpenSSL, DNS or the wildcard
certificate as part of service removal.
