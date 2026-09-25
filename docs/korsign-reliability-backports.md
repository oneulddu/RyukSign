# KorSign reliability backports

Reference: [korboybeats/KorSign a1dbd06](https://github.com/korboybeats/KorSign/tree/a1dbd06f7d43f12cd54334a4717fc7be98646a02).
The implementation and executable fixtures adapt its compatible fixes while keeping
RyukSign's identity, settings, compression dependencies, and existing Zip output.

## Imported and adapted

- Wait for actual extraction completion instead of racing an uncancellable ZIP
  operation against a five-minute timeout. Run it at user-initiated QoS and gate
  progress before dispatching UI work.
- Extract imports with KorSign's `ArchiveExtraction` and vendored ZIPFoundation
  (256 KB buffers, opt-in system zlib) instead of Zip's 4 KB minizip loop. On a
  756 MB IPA (994 MB unpacked, 3,200 files) CPU time fell from about 2.4 s to 1.6 s
  and wall time from about 2.5 s to 1.7 s on a Mac (three runs each). File contents
  and permissions match Zip's output. Every entry path is validated, destinations are
  resolved against symlinks before writing, and each CRC is checked. Symlink entries are
  now restored as links (Zip wrote a text file holding the target); links leaving the
  destination are rejected. Tweak extraction from IPAs and backup restore use the same
  extractor. Import pause/resume controls are not included.
- Parse `.deb` AR members with bounds, numeric, and padding checks, and validate tar
  entry paths before writing. Truncated or crafted packages now fail with an error
  instead of crashing or writing outside the extraction directory.
- Use KorSign's bounded Mach-O patchers for the Liquid Glass SDK patch and the ARM64e
  slice fix. Every slice and load command is validated before any byte is written.
  Unlike KorSign, a failed patch still lets signing continue as before, and the reason
  is logged.
- Throw `infoPlistNotFound` for a missing or unreadable Info.plist during signing
  instead of force-unwrapping it.
- Stage each finished download in its own directory. Two downloads with the same
  server filename previously shared `downloadStaging/<name>`, and the second deleted
  the first before its import had copied it. Staged files are removed when the import
  finishes or the download is cancelled instead of lingering up to a day, which matters
  for multi-GB IPAs.
- Remove the per-signing tweak staging directory after injection finishes.
- Wait for the actual library-save result. A timer cannot cancel Core Data, so
  premature import cleanup must not remove a payload before a late successful save.
- Give temporary installation archives an explicit owner. Pairing calls, the OTA
  server, outstanding HTTP responses, and server shutdown retain it until their
  final read. The last release removes only that job's directory on a utility queue.
  Exported IPAs have already moved outside that directory and survive cleanup.
- Treat HEAD as a probe, disable HTTP recompression of already-compressed IPAs,
  and prevent repeated manifests or late callbacks from regressing terminal status.
  Snapshot manifest metadata on the Core Data context's main queue.
  A conditional GET returning 304 starts install progress tracking for cached bytes;
  a conditional HEAD remains a probe.
- Propagate export errors and avoid overwriting same-name files, directories, or
  symlinks. Normal exports keep `name_version_timestamp.ipa`; collision suffixes
  and bounded, sanitized metadata cover previously failing inputs.
- Preserve existing databases, apps, and certificates when a store cannot open.
  Report save failures, roll back failed changes, and delete owned files only
  after the database deletion is saved. Migration flags require a successful save.
- Treat native signing return values and callback failures as failures before
  publishing output. Retain the current Zsign submodule and callback signature.
- Propagate certificate import and backup restore failures. Validate certificate
  identifiers before restore writes, and preserve unregistered recovery files.
  Certificate updates stage replacement files and retain originals for rollback
  until both file replacement and metadata saving succeed.
- Prevent general cleanup from treating an unavailable database as an empty library.
  Protect certificate recovery directories even when the database is healthy, and
  recheck protections before deleting paths from an earlier scan.

The broader fork's branding, UI preferences, updater/release changes, installation
verification state machine, and import pause/resume controls are not included.
RyukSign's own post-install cleanup (stage, then flush after the install UI closes)
is kept. KorSign's source-loading generations, branded log export, and temporary
export ownership were reviewed and left out: they depend on excluded features or
showed no user-visible defect in RyukSign.

## Verification and limits

See [focused regression checks](../tests/README.md). These exercise production methods
with disposable fixtures, plus real Core Data and Vapor loopback integration where
appropriate. The app is also built as an unsigned iOS Release.

The original four-level ZIP output comparison remains applicable and is rerun after
the archive ownership change. This change does not claim a measured device speedup,
repair damaged databases, add cancellability to Zip, or prove physical iPhone behavior.
Backup restore is not an all-or-nothing transaction across every selected component;
earlier successfully restored items remain if a later item fails.
