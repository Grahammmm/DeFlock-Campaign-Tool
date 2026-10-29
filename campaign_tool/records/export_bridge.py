"""Credential-free, single-receipt enrollment through the existing mail backend.

No exporter, mailbox, configuration loader, network transport or stage substitute.
The caller owns the canonical root lock and has started the supplied backend run.
"""
import hashlib
import os
from pathlib import Path

from .intake import folder, mail_delta
from .runner.contracts import Folder


def enroll_export(receipt_path, *, export_root, backend, inventory):
    """Preserve exact declared bytes/occurrence, then discover verified body links.

    backend must be CanonicalMailBackend bound to export_root and the selected
    canonical root. Its existing importer checks all original/attachment bytes,
    MIME membership, immutable receipt identity and canonical occurrence joins.
    Replays use the original receipt, including its real UIDVALIDITY and UID.
    """
    from .run import require_root_lock
    require_root_lock(Path(backend.database).parent)
    root = Path(os.path.abspath(export_root))
    if Path(backend.mail_root) != root:
        raise mail_delta.Rejected("export_root_binding_mismatch")
    path = mail_delta.source_path(root, str(receipt_path), backend.output)
    declaration, identity, _ = mail_delta.load_receipt(path)
    account, mailbox, validity, uid = identity
    value = backend.preserve(path, account, Folder(mailbox, int(validity)), int(uid))
    original = backend.output / "blobs" / value.eml_sha256
    with folder.secure_open(original) as stream:
        raw = stream.read(mail_delta.MAX_EML + 1)
    if len(raw) > mail_delta.MAX_EML or hashlib.sha256(raw).hexdigest() != value.eml_sha256:
        raise mail_delta.Rejected("preserved_message_changed")
    # Enrollment records the identity/receipt binding before discovery, so a
    # parser failure remains a durable incomplete event and can never look empty.
    inventory.register(value, identity)
    try:
        if value.original_format != "eml" or declaration.get("schema"):
            raise mail_delta.Rejected("body_discovery_format_unqualified")
        candidates = mail_delta.mime_candidates(None, raw=raw)
        from email import policy
        from email.parser import BytesParser
        message = BytesParser(policy=policy.default).parsebytes(raw)
        bodies = []
        for candidate in candidates:
            if candidate["attachable"]:
                continue
            part = message
            for index in candidate["locator"].split(".")[1:]:
                part = list(part.iter_parts())[int(index) - 1]
            payload = part.get_payload(decode=True) or b""
            if hashlib.sha256(payload).hexdigest() != candidate["sha"]:
                raise mail_delta.Rejected("body_payload_mismatch")
            # No lossy replacement: undecodable body means incomplete discovery.
            text = payload.decode(part.get_content_charset() or "ascii", errors="strict")
            bodies.append((candidate["locator"], text, candidate["content_type"]))
        inventory.discover(value.message_id, bodies)
    except Exception as error:
        from .portal.policy import PortalError
        # A contradictory replay must not erase a completed enumeration: an
        # empty one has no saved link rows to distinguish it from pending work.
        if not (isinstance(error, PortalError) and str(error) == "body_discovery_conflict"):
            inventory.discovery_failed(value.message_id)
        raise
    return value
