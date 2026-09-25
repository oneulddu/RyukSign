# IPA packaging execution

`AppInstaller._package()` owns the `.userInitiated` task for copying and packaging.
`ArchiveHandler.archive()` runs directly in that task instead of starting a nested
`.background` task. With the app target's current Swift 5 settings (without
`NonisolatedNonsendingByDefault`), its nonisolated async method also leaves the main
actor when called by the self-updater. Preserve this executor behavior when changing
Swift concurrency settings.

The install and self-update pipelines already keep their background managers alive
across packaging, including errors. ArchiveHandler no longer starts a second manager.
ProgressGate still limits UI updates. AppArchiver, both compression engines, compression
levels, Payload layout, export filenames, and install/share destinations are unchanged.

The later [reliability backports](korsign-reliability-backports.md) add explicit
temporary-archive ownership and safe export error/collision handling. Ordinary
export names and ZIP contents remain compatible.

## Pairing upload and the pre-packaging copy

Pairing installs previously read the whole IPA with `Data(contentsOf:)` before
writing it to AFC, so a 4 GB app needed about 4 GB of memory before the first byte
was sent, and the write loop used a pointer that escaped `withUnsafeBytes`. The
IDeviceKitten `InstallationProxy` now streams the file through one reused 64 MB
buffer (`ChunkedFileStreamer`). AFC still receives full 64 MB writes and the same
per-chunk upload progress; a failed write or read now also closes the remote file.
A 768 MB fixture streamed with 64 MB peak memory growth.

IDeviceKitten is a submodule of claration/IDeviceKit. The change is committed on
the local submodule branch `ryuksign/stream-afc-upload` and the parent repository
points at that commit. Push it to a reachable IDeviceKit fork (and point
`.gitmodules` there) before pushing this repository, or CI checkouts with
`submodules: true` cannot fetch it.

`ArchiveHandler.move()` copies the signed `.app` into `Payload/`. On APFS within one
volume, `FileManager.copyItem` clones: copying a 994 MB, 3,200-file bundle took
0.25–0.58 s and consumed about 1 MB. That is small next to compression, so the copy
was kept.

## References

- [Feather 2.9.0](https://github.com/claration/Feather/blob/v2.9.0/Feather/Utilities/Handlers/ArchiveHandler.swift)
  also uses a background detached task, but lacks RyukSign's duplicate keep-alive and
  progress gate. This comparison alone does not establish why RyukSign is slower.
- [KorSign ArchiveHandler](https://github.com/korboybeats/KorSign/blob/a1dbd06f7d43f12cd54334a4717fc7be98646a02/KorSign/Utilities/Handlers/ArchiveHandler.swift)
  supplies the adopted pattern: snapshot Core Data-backed app path/name/version on
  the main actor before passing the handler to packaging. Its background task wrapper
  has the same issue as RyukSign's original. Its extraction optimizations concern
  imports; export renaming and archive ownership changes are outside this change.

## Validation

- Unsigned iOS Release build with Xcode 26.6 and the existing package versions.
- A macOS harness using the production archive body, AppArchiver, Zip engine, and
  pinned Zip 2.1.2, with iOS UI/storage/keep-alive stubs and a ZIP-entry scheduling probe.
  All four compression levels passed from both a `.userInitiated` detached task and
  the main actor: byte-identical output versus direct `Zip.zipFiles`, extracted file
  contents, export moves, final/throttled progress, and missing-payload errors.
- The probe observed thread QoS change from background (9) to user-initiated (25),
  with ZIP work off the main thread. App metadata getters additionally asserted main
  actor isolation. This supports removal of scheduling throttling, not a measured
  end-to-end speedup percentage.

Physical-device 2–5 GB packaging benchmarks, background expiration, and actual
installation/share UI still need device validation. Compare the same app, compression
engine/level, device temperature, and foreground/background state when timing them.
