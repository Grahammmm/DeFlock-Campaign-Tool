"""Versioned extended mail receipts. Only received bytes enter originals."""
import hashlib
import json
import os
from pathlib import Path
from . import folder, mail_wire


def parse(receipt):
    from . import mail_delta as delta
    need = delta.require
    need(type(receipt) is dict and receipt.get("complete") is True
         and receipt.get("schema") == mail_wire.SCHEMA, "invalid_receipt_shape")
    identity = (delta.short_text(receipt.get("account_id"), "invalid_account"),
                delta.short_text(receipt.get("folder"), "invalid_folder"),
                delta.identity_number(receipt.get("uidvalidity"), "invalid_uidvalidity"),
                delta.identity_number(receipt.get("uid"), "invalid_uid"))
    original = receipt.get("original_eml")
    attachments = receipt.get("attachments")
    native = receipt.get("native_items")
    need(type(original) is dict and original.get("format") in {"eml", "msg"}
         and type(attachments) is list and type(native) is list
         and len(attachments) + len(native) <= 100, "invalid_receipt_shape")
    size = delta.number(original.get("bytes"), "invalid_eml_bytes", delta.MAX_EML)
    need(receipt.get("bytes") == size and type(original.get("path")) is str,
         "receipt_bytes_mismatch")
    items = [dict(part="0", sha=delta.digest(original.get("sha256")), size=size,
                  path=original["path"], kind=original["format"], source=original)]
    used = {"0"}
    total = size
    for attachment in attachments:
        need(type(attachment) is dict, "invalid_attachment")
        part = delta.short_text(attachment.get("part"), "invalid_part")
        need(part not in used, "duplicate_or_invalid_part")
        used.add(part)
        length = delta.number(attachment.get("bytes"), "invalid_attachment_bytes", delta.MAX_ATTACHMENT)
        total += length
        need(total <= delta.MAX_TOTAL and type(attachment.get("path")) is str,
             "receipt_total_size_limit")
        need(attachment.get("format") in {None, "eml", "msg"}, "invalid_attachment_metadata")
        items.append(dict(part=part, part_value=part, part_is_walk_index=False,
                          sha=delta.digest(attachment.get("sha256")), size=length,
                          path=attachment["path"], kind="eml" if attachment["format"] == "eml" else "attachment",
                          source=attachment))
    for item in native:
        need(type(item) is dict, "invalid_receipt_shape")
        part = delta.short_text(item.get("part"), "invalid_part")
        need(part not in used, "duplicate_or_invalid_part")
        used.add(part)
    return receipt, identity, items


def export(raw, *, mail_root, account, mailbox, uidvalidity, uid, original_format):
    from . import eml_export, mail_delta as delta
    delta.require(type(uidvalidity) is int and type(uid) is int and uidvalidity > 0 and uid > 0,
                  "invalid_identity_numbers")
    delta.short_text(account, "invalid_account")
    delta.short_text(mailbox, "invalid_folder")
    plan = mail_wire.prepare(bytes(raw), original_format)
    root = Path(mail_root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    delta.private_path(root, directory=True)
    base = root / mail_wire.digest(raw)
    base.mkdir(mode=0o700, exist_ok=True)
    delta.private_path(base, directory=True)
    path = base / "receipt.json"
    wanted = (account, mailbox, str(uidvalidity), str(uid))
    if path.exists():
        _, identity, _ = delta.load_receipt(path)
        if identity != wanted:
            path = base / ("receipt-" + hashlib.sha256("\0".join(wanted).encode()).hexdigest()[:16] + ".json")
    original_path = base / ("message." + original_format)
    eml_export._ensure_private(original_path, bytes(raw))
    attachments = []
    for capture in plan.captures:
        destination = base / (mail_wire.digest(capture.part.encode())[:24] + "-" + delta.safe_filename(capture.metadata.get("original_filename")))
        eml_export._ensure_private(destination, capture.payload)
        attachments.append({**capture.metadata, "path": str(destination.relative_to(root))})
    native = []
    for metadata, manifest, streams in plan.native_items:
        destination = base / ("native-" + metadata["stream_manifest_sha256"])
        eml_export._ensure_private(destination, manifest)
        stored = []
        for stream in streams:
            stream_path = base / ("stream-" + stream.metadata["sha256"])
            eml_export._ensure_private(stream_path, stream.payload)
            stored.append({**stream.metadata, "path": str(stream_path.relative_to(root))})
        native.append({**metadata, "manifest_path": str(destination.relative_to(root)), "streams": stored})
    receipt = {"complete": True, "schema": mail_wire.SCHEMA, "account_id": account,
               "folder": mailbox, "uidvalidity": uidvalidity, "uid": uid,
               "bytes": len(raw), "original_eml": {**plan.original, "path": str(original_path.relative_to(root))},
               "attachments": attachments, "native_items": native, "budget": plan.budget}
    eml_export._ensure_private(path, (json.dumps(receipt, sort_keys=True, indent=1) + "\n").encode())
    delta.load_receipt(path)
    return path


def bind(items, receipt, root, out):
    """Recompute exhaustive physical and derived inventories before ledger writes."""
    from . import mail_delta as delta
    with folder.secure_open(items[0]["tmp"]) as stream:
        raw = stream.read(delta.MAX_EML + 1)
    plan = mail_wire.prepare(raw, receipt["original_eml"]["format"])
    delta.require(receipt["budget"] == plan.budget, "mail_wire_receipt_mismatch")
    original = {key: value for key, value in receipt["original_eml"].items() if key != "path"}
    delta.require(original == plan.original, "mail_wire_receipt_mismatch")
    wanted = {capture.part: capture for capture in plan.captures}
    delta.require({item["part"] for item in items[1:]} == set(wanted), "mail_wire_receipt_mismatch")
    items[0]["verified_format"] = plan.format
    for item in items[1:]:
        capture = wanted[item["part"]]
        declaration = {key: value for key, value in item["source"].items() if key != "path"}
        delta.require(declaration == capture.metadata and item["sha"] == mail_wire.digest(capture.payload)
                      and item["size"] == len(capture.payload), "mail_wire_receipt_mismatch")
        item.update(mime=capture.metadata.get("mime", capture.part),
                    mime_name=capture.metadata.get("original_filename"),
                    parent_sha=capture.parent_sha256, wire_schema=mail_wire.SCHEMA,
                    verified_format=capture.metadata.get("format") or folder.fmt(
                        capture.metadata.get("original_filename") or "", capture.payload[:16384], capture.payload))
    natives = {item["part"]: item for item in receipt["native_items"]}
    delta.require(set(natives) == {metadata["part"] for metadata, _, _ in plan.native_items},
                  "mail_wire_receipt_mismatch")
    artifacts = []
    for metadata, manifest, streams in plan.native_items:
        item = natives[metadata["part"]]
        declaration = {key: value for key, value in item.items() if key not in {"manifest_path", "streams"}}
        delta.require(declaration == metadata and type(item.get("streams")) is list
                      and len(item["streams"]) == len(streams), "mail_wire_receipt_mismatch")
        artifacts.append((item["manifest_path"], manifest, out / "native-items" / metadata["stream_manifest_sha256"]))
        for given, stream in zip(item["streams"], streams):
            delta.require(type(given) is dict and
                          {key: value for key, value in given.items() if key != "path"} == stream.metadata,
                          "mail_wire_receipt_mismatch")
            artifacts.append((given["path"], stream.payload, out / "native-streams" / stream.metadata["sha256"]))
    # Verify every artifact before materializing any derived proof.
    for literal, expected, destination in artifacts:
        delta._native_artifact(root, out, literal, expected, None)
    for literal, expected, destination in artifacts:
        delta._native_artifact(root, out, literal, expected, destination)
    return plan


def artifact(root, out, literal, expected, destination):
    from . import eml_export, mail_delta as delta
    path = delta.source_path(root, literal, out)
    with folder.secure_open(path) as stream:
        before = os.fstat(stream.fileno())
        data = stream.read(len(expected) + 1)
        after = os.fstat(stream.fileno())
    fields = lambda stat: (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    delta.require(fields(before) == fields(after) == fields(path.stat(follow_symlinks=False))
                  and data == expected, "mail_wire_receipt_mismatch")
    if destination is not None:
        eml_export._ensure_private(destination, data)
