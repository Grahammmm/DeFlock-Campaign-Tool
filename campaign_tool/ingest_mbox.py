"""`ingest --mbox`: one local mbox export, each message stored exactly as `ingest --eml` stores one message.

The mailbox bytes are read once (no link followed), stored first as `--zip` stores its archive, and split in
memory (never with `mailbox.mbox`, which opens by path). A message starts at a line beginning `From ` at offset 0
or right after `\\n`; that separator line is not stored and the message runs to the byte before the next separator
(one framing line ending dropped: a complete CRLF, else one LF, else nothing; see docs/INGEST-MBOX.md). `>From ` lines are kept as they are (no mboxrd unescape). Each kept message `n` gets a
receipt `<mailbox>/message-<n>.eml` and a `contains` row to the mailbox; its attachments are linked to it as with
`--eml`. Every message is checked before anything is written. No header value is printed, logged or named.

mboxo ambiguity: a body line that itself begins with `From ` (unescaped by the exporter) starts a new message;
only a line-start `From ` splits, never one later in a line."""
import json
import os
import stat

from .cli import MAX_BYTES, _ledger_lookup, _store_bytes, database
from .ingest_eml import _extract, _store_message
from .ingest_zip import _previewer

MAX_MBOX_BYTES = 4 * MAX_BYTES  # 200 MiB
MAX_MESSAGES = 500
_SEPARATOR = b"From "


def _read_mbox(path):
    """The mailbox bytes: one regular, non-symlink file of at most 200 MiB."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise ValueError("--mbox must be a regular, non-symlink file") from None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("--mbox must be a regular, non-symlink file")
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(MAX_MBOX_BYTES + 1)
    if len(data) > MAX_MBOX_BYTES:
        raise ValueError("Alpha limit: 200 MiB per mailbox; nothing was imported")
    return data


def _require_receipt_name(name):
    """The mailbox basename prefixes every receipt name the ledger stores, so it must be strict UTF-8 text.
    A raw POSIX byte name (surrogate-escaped) cannot be persisted; refusing here, before any object is
    installed, leaves no orphan. Only the basename is checked: undecodable ancestor directories stay supported."""
    try:
        name.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ValueError("Mailbox filename cannot be represented as UTF-8; nothing was imported") from None


def _split(data, name):
    """(start, end) of each message body in `data`; refused whole if not an mbox or over 500 messages."""
    if not data.startswith(_SEPARATOR):
        raise ValueError("not an mbox file (no From line at the start); nothing was imported")
    starts, at = [0], data.find(b"\n" + _SEPARATOR)
    while at != -1:
        starts.append(at + 1)
        if len(starts) > MAX_MESSAGES:
            raise ValueError(f"{name}: more than {MAX_MESSAGES} messages; nothing was imported")
        at = data.find(b"\n" + _SEPARATOR, at + 1)
    spans = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(data)
        line_end = data.find(b"\n", start, end)
        body = end if line_end == -1 else line_end + 1  # separator line is never stored
        if end - body >= 2 and data[end - 2:end] == b"\r\n":
            end -= 2  # one complete CRLF framing line ending
        elif end > body and data[end - 1:end] == b"\n":
            end -= 1  # one LF framing line ending; a bare CR is never removed
        spans.append((body, end))
    return spans


def ingest_mbox(args, root):
    """Store the mailbox, then each message and its attachments; print the listing (dry run: write nothing)."""
    data = _read_mbox(args.mbox)
    name = os.path.basename(os.path.abspath(args.mbox))
    _require_receipt_name(name)  # RF0345-2: same refusal for preview and apply, before split, lookup or any store
    kept, skipped = [], []
    spans = _split(data, name)
    for number, (start, end) in enumerate(spans, 1):
        code = "empty" if end == start else "too_large" if end - start > MAX_BYTES else None
        if code is None:
            blob = data[start:end]
            try:
                kept.append((number, blob, _extract(blob, f"message-{number}.eml")[0]))
            except ValueError:
                code = "malformed"
        if code:
            skipped.append({"number": number, "code": code})
    dry = getattr(args, "dry_run", False)
    if dry:
        known, close = _ledger_lookup(root)
        store = _previewer(known, args.source_id)
    else:
        db = database(root)
        close = db.close

        def store(blob, receipt_name, contained_in=None):
            return _store_bytes(db, root, blob, args.source_id, receipt_name, contained_in)
    try:
        container = store(data, name)
        messages = []
        for number, blob, found in kept:
            label = f"message-{number}.eml"
            result = _store_message(store, blob, f"{name}/{label}", found, (container["sha256"], label))
            messages.append({"number": number, "sha256": result["message"]["sha256"],
                             "outcome": result["message"]["outcome"], "attachments": result["attachments"],
                             "skipped": result["skipped"]})
    finally:
        close()
    report = {"mailbox": {k: container[k] for k in ("sha256", "receipt_id", "outcome")}, "messages": messages,
              "skipped": skipped, "count": len(spans)}
    print(json.dumps({**report, **({"dry_run": True} if dry else {})}, indent=2))
    if dry:
        print("Dry run: nothing was written.")
