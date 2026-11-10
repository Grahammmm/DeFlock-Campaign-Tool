# Importing saved mail (`ingest --eml`, `ingest --mbox`)

This page describes how the offline importer treats attachment transfer encodings. Every example is synthetic and
local; nothing here sends, fetches or publishes anything.

```sh
python3 -m campaign_tool ingest --eml saved.eml --source-id example-1 --dry-run
python3 -m campaign_tool ingest --eml saved.eml --source-id example-1
python3 -m campaign_tool ingest --mbox saved.mbox --source-id example-1
```

The whole message (or mailbox) is always stored byte for byte first; each named attachment is then stored as its own
object, linked to the message. An attachment is stored only if its bytes were decoded exactly as the message said.

## Accepted encodings

A named attachment must carry exactly one `Content-Transfer-Encoding` header, or none (read as `7bit`). The value is
compared case-insensitively after trimming surrounding whitespace. Supported values:

| Value | Rule |
| --- | --- |
| `7bit`, `8bit`, `binary`, none | The bytes are stored as they are. There is no content audit. |
| `base64` | ASCII only. Only space, tab, CR and LF may be folded between characters. The text must be canonical: only the base64 alphabet, correct trailing padding (none inside), a possible length, and zero pad bits. An empty payload is valid and is skipped as `empty`. |
| `quoted-printable` | Every `=` must start two hexadecimal digits (either case) or a soft line break (`=` then LF or CRLF). Text is otherwise not checked or converted. |

An empty, repeated (even if equal), malformed or unknown value is refused. The importer never repairs padding,
guesses an encoding or stores encoded text as if it were decoded.

### Forwarded messages

A forwarded `message/rfc822` part, named or not and at any multipart depth, is checked first, and on its own
`Content-Transfer-Encoding` header only. It follows the same rule (exactly one header, or none; trimmed,
case-insensitive; empty, repeated, malformed or unknown is refused) but accepts identity encodings only:
`7bit`, `8bit`, `binary` or none. An explicit `base64` or `quoted-printable` wrapper is refused even if it is
well formed: the importer has no decoder for an encoded forwarded message, never salvages it and never stores
encoded text as if it were a message. Accepted forwards are serialized exactly as before, so their hashes, names
and links do not change. This is the importer's established serialization of the forwarded message, not the
byte-exact wire capture of the separate records intake.

## What is checked

Only named attachments, plus the transfer encoding header of each forwarded message wrapper. Message bodies and
inline parts without a filename are not newly decoded or validated. A forwarded `message/rfc822` part stays one
opaque `.eml` member: its inner headers and its own attachments (including their encodings) are not opened or
checked.

## Refusal

- **`--eml`**: every attachment and forwarded wrapper is checked before anything is written, so one malformed
  attachment or forward refuses the whole message, in a real run and in `--dry-run` alike. Nothing in the campaign
  changes, and the input file is untouched.
  The output is exactly `Stopped: malformed MIME (InvalidAttachmentEncoding); nothing was imported` and exit status 1.
- **`--mbox`**: the malformed message alone is listed in the report's `skipped` as
  `{"number": N, "code": "malformed"}`, with no objects, receipts or links of its own. The other messages import
  normally, the complete original mailbox is stored unchanged, and the exit status stays 0. `--dry-run` reports the
  same skipped numbers and hashes without writing.

A message whose MIME parts are nested more deeply than Python's email parser can follow (for example a
multipart inside a multipart, a thousand levels down) is refused the same way, as a structural problem rather than
an encoding one: `--eml` stops before anything is written with exactly
`Stopped: malformed MIME (RecursionError); nothing was imported` and exit status 1, and `--mbox` lists only that
message as `{"number": N, "code": "malformed"}` while the other messages, their attachments and the original
mailbox import as above. No part of the deep message is stored on its own; keep it with `ingest --file` (below).

The saved file's own name must be valid UTF-8 text, because it prefixes every receipt name the ledger stores. A
`--eml` file whose name contains bytes that are not valid UTF-8 (a raw byte such as `FF` on Linux) is refused, in a
real run and in `--dry-run` alike, before the message is read for MIME, looked up or stored: the output is exactly
`Stopped: Message filename cannot be represented as UTF-8; nothing was imported` and exit status 1, and no object,
receipt or link is created. Only the file's own name is checked; Unicode names (accents, CJK, emoji) and a
directory path containing undecodable bytes are accepted unchanged. To import such a message, rename the file to a
valid UTF-8 name and run the command again; this prevents new unsupported-name imports and does not clean up
anything stored earlier.

The message never repeats header values, body data, filenames or decoder text, so it is safe to paste into a ticket.

## Recovery

1. Export the mail again, or ask for a corrected copy of the message from the sender; then import that file.
2. To keep the raw message as it is without claiming its attachments were decoded, store it with the existing
   `ingest --file saved.eml --source-id example-1`. That keeps the original bytes only; no attachment (or forwarded
   message) is extracted.
