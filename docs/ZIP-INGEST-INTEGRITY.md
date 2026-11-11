# ZIP intake: local/central header integrity

`ingest --zip` (campaign_tool/ingest_zip.py) keeps the **original archive** byte-for-byte in `private/objects`
whatever happens to its members. A member only becomes an object, receipt and `contains` row when the archive's
two descriptions of it agree and its payload lies inside its own record. Anything else is listed in `skipped`
with the existing code `unreadable`; the other members of the same archive are still imported, and `--dry-run`
makes the same decisions without writing.

## What is checked, per eligible member
- The central-directory prechecks (directory, unsafe name, duplicate, encrypted, symlink, size, ratio) come
  first and keep their codes.
- The local header has the `PK\x03\x04` signature and the same compression method and general-purpose flags as
  the central entry. A local-only encryption bit is therefore refused. Neither copy is repaired.
- The local file name (UTF-8 when bit 11 is set, CP437 otherwise) equals the central original name exactly: no
  case folding or path normalisation.
- Local CRC and sizes equal the central values. ZIP64 size sentinels (`0xFFFFFFFF`) are resolved from the local
  ZIP64 extra field (uncompressed size, then compressed size); a missing or short value, or a malformed extra
  field, refuses the member. Small members written with `force_zip64` are accepted.
- **Data descriptor** (flag bit 3, streaming writers): local CRC and sizes may be zero or ZIP64 placeholders, or
  the true values; any other value is refused. The central sizes bound the payload and the measured output
  length, CRC and end-of-stream stay authoritative. The descriptor itself is not read or stored.
- The header, name, extra field and payload fit before the next distinct local-header offset (or the central
  directory start), so no span reaches into another record. Two central entries pointing at one local header are
  both refused. Archives with a prefix (self-extracting) work because offsets are the ones `zipfile` resolved.
- A compressed member (deflate, bzip2, ZIP-LZMA) must be a **single stream** that consumes its entire
  declared compressed payload: any unused bytes after the end of that stream inside the declared range (junk,
  zeros or a second stream) make the member `unreadable`, so it gets no derived object, receipt or `contains`
  row, while the original archive and the healthy members are still kept. Stored members are plain content
  and are never scanned for markers. Bytes outside the declared slice (descriptors, headers, gaps) are not
  decoder input, and descriptor contents are still not authenticated. This applies to new ingests only; it is
  not a migration and removes nothing recorded by an earlier import.

## Archives the runtime cannot open
If the central directory itself cannot be opened (for example an entry demands an extraction version the
running `zipfile` does not support), the whole archive is refused with `Stopped: not a zip archive` before the
ledger is opened or the container is written, in preview and apply alike; no earlier entry is partially stored.
This differs from a parseable archive, which is retained once it passes container-level admission: there a member with an unsupported
compression method or other unreadable content is skipped alone with the `unreadable` code.

## Archive filenames that are not valid UTF-8
Container-level admission also checks the archive's own file name (its last path component only). If that name
cannot be represented as strict UTF-8 (for example a file name containing a raw byte such as 0xFF), the whole
archive is refused with `Stopped: Archive filename cannot be represented as UTF-8; nothing was imported`, in
preview and apply alike, before the ledger is opened and before the container or any member is preserved. This is
a whole-container refusal, not a member skip. Ordinary Unicode names (accented, combining, CJK, emoji) are kept
exactly, unnormalized, and a valid name under a parent directory whose own name is not valid UTF-8 is still
accepted. To import a refused archive, rename the file yourself and run the intake again.

## Limits
These are **structural** checks: they show the archive does not contradict itself, not that the contents are
genuine or what they mean. Output caps, ratio limit and nested-archive handling are unchanged.
