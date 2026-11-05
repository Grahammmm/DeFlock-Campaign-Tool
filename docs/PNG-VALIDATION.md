# PNG container validation

Public-asset PNGs are checked for container integrity before metadata is scanned, stripped, measured or
published. The same bounded reader (`campaign_tool/image_meta.py`) serves the build and `content-check` gate,
`content strip-metadata` and image dimensions. See the [public-assets reference](SITE-CONTENT.md).

## What is checked

- The 8-byte PNG signature, then chunks of length, four-letter type, body and CRC32.
- Every chunk's CRC32 (over type and body), including text/EXIF chunks that stripping would remove. A checksum
  is never repaired and a malformed chunk is never dropped to make a file look clean.
- Chunk types are four ASCII letters with the reserved bit (third letter uppercase) clear. An unknown critical
  chunk is refused; an unknown well-formed ancillary chunk is accepted and kept.
- Order: first chunk is IHDR (13 bytes) and the only IHDR; at least one IDAT, all in one contiguous run; an
  optional PLTE (1-256 three-byte entries) once, before IDAT; the last chunk is an empty IEND.
- Bounds: the existing per-file size cap, at most 10,000 chunks, chunk length at most 2^31-1 and within the file.
- Bytes after the first valid IEND stay "trailing data": the asset gate refuses them and strip removes them.

A failing file is `unreadable image`: the build and `content-check` name its path and that category (never chunk
bytes), dimensions are unavailable, and `content strip-metadata` writes nothing and leaves the source untouched.
A previously built public tree is not changed by a refused build.

## Preservation

For an accepted PNG, strip removes only the existing metadata and trailing-data ranges and keeps every other
chunk byte and CRC exactly. Stripping twice gives identical bytes. JPEG, WebP and SVG behaviour is unchanged.

## Limits

This is container integrity only. IHDR field values, bit depth and IDAT decompression are not checked, no image
library is used, and a pass does not mean the pixels render, the image is authentic or it is free of private
information. Other malformed images may still pass.
