# Focused regression checks

These macOS fixtures compile production Swift code with isolated filesystem data
and platform-service stubs. They do not use installed apps or real certificates.
Several are adapted from [KorSign a1dbd06](https://github.com/korboybeats/KorSign/tree/a1dbd06f7d43f12cd54334a4717fc7be98646a02/tests).

```sh
python3 tests/test_import_extraction_lifecycle.py
python3 tests/test_import_database_lifecycle.py
python3 tests/test_installation_archive.py
python3 tests/test_storage.py
python3 tests/test_signing_failure.py
python3 tests/test_certificate_persistence.py
python3 tests/test_backup_restore.py
python3 tests/test_storage_recovery_cleanup.py
python3 tests/test_afc_upload_streaming.py
python3 tests/test_malformed_inputs.py
python3 tests/test_macho_patching.py
python3 tests/test_archive_extraction.py --checkouts /path/to/SourcePackages/checkouts
```

- Extraction: slow successful work, error forwarding, execution QoS, main-queue
  responsiveness, throttled progress, IPA/TIPA handling, and safe completion before cleanup.
- Import database: delayed save completion, error propagation, and cleanup preserving
  registered payloads and the original input.
- Installation archive: producer/consumer lifetime, packaging failure, delayed pairing
  reads, stopped jobs, outstanding HTTP readers, asynchronous shutdown, export collisions,
  unsafe/long metadata, and export failure propagation. ZIP/HTTP/pairing are stubs.
- Storage: the production Core Data model with temporary stores and injected failures;
  unreadable-store preservation, save rollback, failed deletion, and durable reopening.
- Signing: native signing failures and success through stubs, with no real signing.
- Certificates: import error propagation and replacement rollback using disposable
  synthetic files, without real credentials.
- Backup restore: invalid/duplicate identifiers rejected before writes, failed saves
  reported, and existing or unregistered certificate files preserved.
- Recovery cleanup: unreadable-store files and certificate recovery directories
  survive cleanup, including removal requests from stale scan results.
- Pairing upload: the AFC streamer's bytes, chunk sizes, progress, error propagation,
  and bounded memory on a 768 MB file.
- Malformed inputs: truncated or invalid DEB/AR headers and unreadable Info.plist
  files fail without crashing or modifying the input.
- Mach-O patching: native patchers under ASan/UBSan with synthetic thin/fat binaries,
  and the Swift callers that log failures and continue signing.
- Archive extraction: ZIP/TAR/DEB path containment, symlinks, collisions, CRC,
  progress, ZIP64 variants, and both decoders at several buffer sizes. It compiles
  the vendored ZIPFoundation and the pinned SWCompression/BitByteData checkouts from
  an app build, so pass that build's `SourcePackages/checkouts`.

For real Vapor HTTP encoding and streaming over loopback, reuse the dependency
checkouts from an app build:

```sh
RYUKSIGN_VAPOR_PATH=/path/to/SourcePackages/checkouts/vapor zsh tests/test_server_install_http.sh
```

This check may fetch pinned SwiftPM dependencies. It covers HEAD response size,
disabled IPA response compression, conditional 304 responses, full/ranged GET bytes, manifest retries, and
missing-file errors. The temporary SwiftPM build cache is retained for reuse.

Physical iPhone installation, background expiration, third-party share destinations,
and large-app timing still require device validation.
