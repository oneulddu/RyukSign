# KorSign reliability backports

Reference: [korboybeats/KorSign a1dbd06](https://github.com/korboybeats/KorSign/tree/a1dbd06f7d43f12cd54334a4717fc7be98646a02).
The implementation and executable fixtures adapt its compatible fixes while keeping
RyukSign's identity, settings, compression dependencies, and existing Zip output.

## Imported and adapted

- Wait for actual extraction completion instead of racing an uncancellable ZIP
  operation against a five-minute timeout. Run it at user-initiated QoS and gate
  progress before dispatching UI work. The pinned Zip extractor is retained;
  KorSign's vendored decoder and import controls are not required for this fix.
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
verification state machine, and custom ZIPFoundation decoder are not included.

## Verification and limits

See [focused regression checks](../tests/README.md). These exercise production methods
with disposable fixtures, plus real Core Data and Vapor loopback integration where
appropriate. The app is also built as an unsigned iOS Release.

The original four-level ZIP output comparison remains applicable and is rerun after
the archive ownership change. This change does not claim a measured device speedup,
repair damaged databases, add cancellability to Zip, or prove physical iPhone behavior.
Backup restore is not an all-or-nothing transaction across every selected component;
earlier successfully restored items remain if a later item fails.
