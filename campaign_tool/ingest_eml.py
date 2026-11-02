"""`ingest --eml`: one emailed production stored as is, plus each attachment as its own object.

The message bytes are read once (no link followed), parsed in memory with the stdlib `email` package and
stored first. Every non-multipart part with a filename is an attachment: its decoded payload is stored with a
receipt `<message>/<sanitized filename>` and linked to the message by one ledger `contains` row. Body text and
inline parts without a filename stay only inside the stored message. A forwarded `message/rfc822` part is one
`.eml` member and is never opened. No header value (Subject, From, To, Date, ...) is printed or logged."""
import io
import json
import os
import stat
import unicodedata
from email import errors, policy
from email.generator import BytesGenerator
from email.parser import BytesParser

from .cli import MAX_BYTES, _ledger_lookup, _store_bytes, database
from .ingest_zip import _previewer

MAX_PARTS = 200
MAX_PART_BYTES = MAX_BYTES  # decoded attachment cap
MAX_NAME = 120
_FATAL = (errors.NoBoundaryInMultipartDefect, errors.StartBoundaryNotFoundDefect,
          errors.MultipartInvariantViolationDefect)


def _read_message(path):
    """The message bytes: one regular, non-symlink file of at most 50 MiB."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise ValueError("--eml must be a regular, non-symlink file") from None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("--eml must be a regular, non-symlink file")
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Alpha limit: 50 MiB per message; nothing was imported")
    return data


def _parse(data, name):
    """Parsed message, refused before any write when its structure is unusable or it has over 200 parts."""
    message = BytesParser(policy=policy.default).parsebytes(data)
    parts = list(message.walk())
    for part in parts:
        for defect in part.defects:
            if isinstance(defect, _FATAL):
                raise ValueError(f"malformed MIME ({type(defect).__name__}); nothing was imported")
    if len(parts) > MAX_PARTS:
        raise ValueError(f"{name}: more than {MAX_PARTS} MIME parts; nothing was imported")
    return message, len(parts)


def sanitize(raw, number):
    """Basename only (`/` and `\\` both separators), control characters dropped, NFC, at most 120 characters;
    an empty, `.` or `..` name becomes `attachment-<number>`."""
    text = unicodedata.normalize("NFC", (raw or "").replace("\\", "/").split("/")[-1])
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cc").strip()
    if len(text) > MAX_NAME:
        stem, ext = os.path.splitext(text)
        text = stem[:MAX_NAME - len(ext)] + ext if len(ext) <= 16 else text[:MAX_NAME]
    return f"attachment-{number}" if text in ("", ".", "..") else text


def _unique(name, taken):
    """`scan.pdf`, then `scan-2.pdf`, `scan-3.pdf`, ... for later parts with the same name."""
    stem, ext = os.path.splitext(name)
    candidate, n = name, 1
    while candidate in taken:
        n += 1
        candidate = f"{stem}-{n}{ext}"
    taken.add(candidate)
    return candidate


def _attachments(message):
    """(raw filename or None, part) for each attachment, in message order; message/rfc822 never recursed."""
    stack = [message]
    while stack:
        part = stack.pop()
        if part.get_content_type() == "message/rfc822" and part is not message:
            yield part.get_filename(), part
        elif part.is_multipart():
            stack.extend(reversed(part.get_payload()))
        elif part is not message and part.get_filename() is not None:
            yield part.get_filename(), part


def _payload(part):
    """Decoded attachment bytes; a forwarded message is serialized whole, its own parts untouched."""
    if part.get_content_type() == "message/rfc822":
        inner, buffer = part.get_payload(0), io.BytesIO()
        BytesGenerator(buffer, mangle_from_=False, policy=policy.default.clone(refold_source="none")).flatten(inner)
        return buffer.getvalue()
    return part.get_payload(decode=True) or b""


def ingest_eml(args, root):
    """Store the message, then each attachment; print the listing (dry run: write nothing)."""
    data = _read_message(args.eml)
    name = os.path.basename(os.path.abspath(args.eml))
    try:
        message, count = _parse(data, name)
        found, taken = [], set()
        for number, (raw, part) in enumerate(_attachments(message), 1):
            label = sanitize(raw, number)
            if part.get_content_type() == "message/rfc822" and raw is None:
                label += ".eml"
            found.append((_unique(label, taken), _payload(part)))
    except (errors.MessageError, LookupError, TypeError, IndexError, AttributeError) as exc:
        raise ValueError(f"malformed MIME ({type(exc).__name__}); nothing was imported") from None
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
        attachments, skipped = [], []
        for label, blob in found:
            code = "empty" if not blob else "too_large" if len(blob) > MAX_PART_BYTES else None
            if code:
                skipped.append({"name": label, "code": code})
                continue
            result = store(blob, f"{name}/{label}", (container["sha256"], label))
            attachments.append({"name": label, "sha256": result["sha256"], "outcome": result["outcome"]})
    finally:
        close()
    report = {"message": {k: container[k] for k in ("sha256", "receipt_id", "outcome")},
              "attachments": attachments, "skipped": skipped, "parts": count}
    print(json.dumps({**report, **({"dry_run": True} if dry else {})}, indent=2))
    if dry:
        print("Dry run: nothing was written.")
