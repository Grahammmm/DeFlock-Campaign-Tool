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

## Limits
These are **structural** checks: they show the archive does not contradict itself, not that the contents are
genuine or what they mean. Output caps, ratio limit and nested-archive handling are unchanged.
