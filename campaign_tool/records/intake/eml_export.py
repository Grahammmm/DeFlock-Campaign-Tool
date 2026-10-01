"""Write a mail-delta export receipt for one RFC 822 message held as bytes.

This is the bridge between any message source (a local inbox directory, an
IMAP fetch) and the existing exact-hash receipt importer in ``mail_delta``.
It writes the original EML and every attachable MIME part under
``mail_root`` and emits the ``complete`` receipt that ``mail_delta.load_receipt``
and ``CanonicalMailBackend.preserve`` already verify byte for byte. Nothing
here parses records for content or touches the ledger.
"""
import hashlib
import json
import os
from pathlib import Path

from . import folder
from . import mail_delta

MAX_MESSAGE = mail_delta.MAX_EML


def local_uid(sha256):
    """Stable positive UID for a message that has no provider UID (local inbox)."""
    return int(sha256[:14], 16) + 1


def _write_private(path, data):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def export_message(raw, *, mail_root, account, mailbox, uidvalidity, uid):
    """Persist ``raw`` and its attachable parts; return the receipt path.

    Layout under ``mail_root``: ``<sha>/message.eml``, ``<sha>/<part>-<name>``
    and ``<sha>/receipt.json``. Re-exporting identical bytes is a no-op that
    returns the same receipt; different bytes never collide because the
    directory is the content hash.
    """
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise mail_delta.Rejected("empty_message")
    if len(raw) > MAX_MESSAGE:
        raise mail_delta.Rejected("message_size_limit")
    if type(uidvalidity) is not int or type(uid) is not int or uidvalidity <= 0 or uid <= 0:
        raise mail_delta.Rejected("invalid_identity_numbers")
    root = Path(mail_root)
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    sha = hashlib.sha256(raw).hexdigest()
    base = root / sha
    receipt_path = base / "receipt.json"
    if receipt_path.exists():
        existing, identity, _ = mail_delta.load_receipt(receipt_path)
        if identity != (account, mailbox, str(uidvalidity), str(uid)):
            raise mail_delta.Rejected("export_identity_conflict")
        return receipt_path
    eml_path = base / "message.eml"
    if not eml_path.exists():
        _write_private(eml_path, bytes(raw))
    candidates = mail_delta.mime_candidates(eml_path)
    attachments = []
    for candidate in candidates:
        if not candidate["attachable"]:
            continue
        payload = _part_payload(eml_path, candidate["locator"])
        if hashlib.sha256(payload).hexdigest() != candidate["sha"]:
            raise mail_delta.Rejected("part_payload_mismatch")
        name = mail_delta.safe_filename(candidate["name"])
        part_path = base / (candidate["locator"] + "-" + name)
        if not part_path.exists():
            _write_private(part_path, payload)
        attachments.append({
            "part": candidate["locator"], "path": str(part_path.relative_to(root)),
            "bytes": candidate["size"], "sha256": candidate["sha"],
            "filename": name, "original_filename": candidate["name"],
            "content_type": candidate["content_type"]})
    receipt = {
        "complete": True, "account_id": account, "folder": mailbox,
        "uidvalidity": uidvalidity, "uid": uid, "bytes": len(raw),
        "original_eml": {"path": str(eml_path.relative_to(root)), "bytes": len(raw), "sha256": sha},
        "attachments": attachments,
    }
    _write_private(receipt_path, (json.dumps(receipt, sort_keys=True, indent=1) + "\n").encode())
    mail_delta.load_receipt(receipt_path)
    return receipt_path


def _part_payload(eml_path, locator):
    """Return the decoded payload of the MIME part at ``locator`` ("1.2.1")."""
    from email import policy
    from email.parser import BytesParser
    with folder.secure_open(eml_path) as source:
        message = BytesParser(policy=policy.default).parse(source)
    part = message
    for index in locator.split(".")[1:]:
        parts = list(part.iter_parts())
        part = parts[int(index) - 1]
    return part.get_payload(decode=True) or b""


def attachment_forms(receipt_path):
    """Map attachment sha256 -> detected format name, from the export receipt."""
    receipt, _, items = mail_delta.load_receipt(receipt_path)
    root = receipt_path.parent.parent
    forms = {}
    for item in items[1:]:
        path = root / item["path"]
        with folder.secure_open(path) as source:
            head = source.read(16384)
        forms[item["sha"]] = folder.fmt(item["source"].get("original_filename") or item["path"], head)
    return forms
