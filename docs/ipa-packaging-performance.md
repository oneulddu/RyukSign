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
