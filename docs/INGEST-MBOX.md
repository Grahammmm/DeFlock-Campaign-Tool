# Ingest a mailbox export (`--mbox`)

`ingest --mbox` stores one local mbox export, then every message in it as `ingest --eml` would. It is offline: it
reads the file you name and sends nothing. See [preserving a document](../README.md#preserve-a-document) and
[INGEST-MAIL.md](INGEST-MAIL.md) for the shared store, receipts and attachment handling.

```text
python3 -m campaign_tool ingest --directory CAMPAIGN --mbox replies.mbox --source-id SOURCE --dry-run
python3 -m campaign_tool ingest --directory CAMPAIGN --mbox replies.mbox --source-id SOURCE
```

`--dry-run` writes nothing and reports the same message hashes and skips as the real import.

## Original mailbox versus derived messages

The whole mailbox is stored first, byte for byte, and is never changed. Each message is then *derived* by cutting
the mailbox bytes (no text decoding, no line-ending conversion) and stored like `--eml`, with a `contains` row from
the mailbox and a receipt `<mailbox>/message-<n>.eml`. Attachments are linked to their derived message.

## LF and CRLF framing

A message starts at a line beginning `From ` at offset 0 or right after a line feed. That separator line is not
part of the message, whether it ends in LF or CRLF. The exporter adds one blank framing line after each message;
exactly one framing line ending is removed from the end of each message: a complete CRLF, else one LF, else
nothing. A bare CR is never removed, and earlier trailing blank lines stay in the message.

```text
LF:    From a@example.org Tue\n  Subject: x\n  \n  body\n  \n   ->  "Subject: x\n\nbody\n"
CRLF:  From a@example.org Tue\r\n  Subject: x\r\n  \r\n  body\r\n  \r\n   ->  "Subject: x\r\n\r\nbody\r\n"
```

Message-internal line endings are untouched, mixed LF/CRLF included. The rule is the same whatever the separator
line's own ending. A last message with no framing newline is kept as it is. A last message that ends in a single
CRLF has that ending treated as framing, because the tool cannot tell whether the exporter left out its blank line;
a pre-export original is not recovered.

## Empty, too large, refused

The empty and size checks apply after framing is removed: framing alone after a separator is `empty` (not stored),
and the 50 MiB per-message limit counts only the derived message. The 200 MiB mailbox limit counts every raw byte.
More than 500 messages, or a file that does not start with `From `, is refused whole before anything is written.
Skipped messages (`empty`, `too_large`, `malformed`) are listed by number; the import still succeeds.

## Limits

- mboxo: a body line that begins `From ` and was not escaped by the exporter starts a new message. `>From ` lines
  are kept as they are (no mboxrd unescaping), and no `Content-Length` header is trusted.
- Importing the same mailbox again is idempotent: every object is reported `present`.
- An earlier version cut CRLF messages one byte short, leaving a lone CR. Objects it already stored stay as they
  are, with their receipts and relationships. This is a splitter correction, not a reconciliation tool; importing
  the same CRLF mailbox with this version can add the correctly cut message objects beside the old ones.
