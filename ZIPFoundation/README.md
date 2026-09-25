# ZIPFoundation

Vendored from https://github.com/weichsel/ZIPFoundation at `22787ffb59de99e5dc1fbfe80b19c97a904ad48d` under the
included MIT license, by way of [KorSign a1dbd06](https://github.com/korboybeats/KorSign/tree/a1dbd06f7d43f12cd54334a4717fc7be98646a02/ZIPFoundation).
KorSign added an opt-in system-zlib DEFLATE decoder for IPA imports. The default
decoder and archive writing remain unchanged, so RyukSign's ZIPFoundation packaging
engine produces the same output as before. Local changes are in Archive+Reading.swift,
Archive+Helpers.swift, Data+ZlibImport.swift, and Entry.swift (preserving valid
zero-valued ZIP64 fields). File extraction accepts an optional throwing checkpoint
before each output chunk.

RyukSign additionally bounds the zlib decoder by the central-directory uncompressed
size, the same record that already supplies the compressed size. A ZIP64-format data
descriptor behind a non-ZIP64 central record (as written by streaming Python 3.9
`zipfile` with `force_zip64`) was otherwise misread as zero bytes and rejected.

Run `python3 tests/test_archive_extraction.py --checkouts <SourcePackages/checkouts>`
to verify archive boundaries and the import decoder.

## Maintaining the local changes

`UPSTREAM_REVISION` records the source revision. Preserve the upstream license and
source notices. When updating, review the decoder hook in both extraction overloads,
its propagation through `readCompressed`, and `Data+ZlibImport.swift`. Keep system
zlib opt-in and archive writing unchanged. Keep the privacy manifest bundled.

The import decoder uses bounded input/output chunks, checks declared input and
output lengths, and rejects incomplete or trailing streams. The extraction caller
must compare the returned CRC with the archive checksum (`ArchiveExtraction` does).
Archive handles are not shared across simultaneous imports. Synthetic ZIP64 fixtures
exercise format handling without allocating multi-gigabyte outputs.
