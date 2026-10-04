"""Bounded EML/MSG intake composition; original bytes and derived text stay distinct."""
from dataclasses import dataclass
from email import policy
from email.errors import MultipartInvariantViolationDefect
from email.parser import BytesHeaderParser
import hashlib
import os
from pathlib import Path
import tempfile

from . import folder
from .rfc822_adapter import (
    RFC822AdapterError, RFC822_FAILURE_CODES, _decode_scalar,
    classify_scalar_parts, prepare_rfc822_intake,
)
from .rfc822_inventory import RFC822InventoryError

SCHEMA = "mail-wire-receipt-v2"
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
FAILURE_CODES = RFC822_FAILURE_CODES | frozenset({
    "mail_wire_rejected", "mail_wire_total_byte_limit", "mail_wire_depth_limit",
    "mail_wire_part_limit", "mail_wire_capture_limit", "mail_wire_invalid_format",
    "mail_wire_receipt_mismatch", "msg_decoder_unavailable", "invalid_msg",
    "unsupported_msg_type", "unsupported_msg_attachment", "msg_body_limit",
})


class MailWireError(ValueError):
    """Reviewed literal code only."""


def require(condition, code):
    if not condition:
        raise MailWireError(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Capture:
    part: str
    parent_part: str
    parent_sha256: str
    payload: bytes
    metadata: dict


@dataclass(frozen=True)
class MailPlan:
    raw: bytes
    format: str
    captures: tuple[Capture, ...]
    units: tuple[dict, ...]
    budget: dict
    native_items: tuple = ()

    @property
    def original(self):
        return {"part": "0", "sha256": digest(self.raw), "bytes": len(self.raw),
                "format": self.format, "schema": SCHEMA}

    @property
    def receipts(self):
        return [dict(c.metadata) for c in self.captures]


def scalar_roles(inventory):
    """Apply the frozen PR50 classifier to every validated wire message context."""
    from . import mail_delta
    roles = {}
    for source in inventory.sources:
        parts = [part for part in inventory.parts if part.message_part == source.part]
        headers, children = {}, {}
        for part in parts:
            header = BytesHeaderParser(policy=policy.default).parsebytes(
                source.raw_bytes[part.wire_start:part.body_start])
            header.set_default_type(part.content_type)
            # Boundaries/body mapping were already validated by the wire helper.
            header.defects = [defect for defect in header.defects
                             if not isinstance(defect, MultipartInvariantViolationDefect)]
            headers[part.mime] = header
            children.setdefault(part.parent_mime, []).append(part)
        permissions = {"1": True}
        for part in parts:
            header = headers[part.mime]
            typ = mail_delta.mime_content_type(header)
            disposition = header.get_all("Content-Disposition", [])
            mail_delta.require(len(disposition) <= 1 and not any(h.defects for h in disposition),
                               "invalid_multipart_disposition" if typ.startswith("multipart/")
                               else "invalid_leaf_disposition")
            allowed = permissions.get(part.mime, False)
            if typ.startswith("multipart/"):
                mail_delta.require(not header.get_filename() and
                                   header.get_content_disposition() != "attachment",
                                   "unsupported_attached_multipart_part")
                immediate = children.get(part.mime, [])
                mail_delta.require(bool(immediate), "invalid_mime_container")
                root = mail_delta.related_root(header, [headers[p.mime] for p in immediate]) if typ == "multipart/related" else 1
                for index, child in enumerate(immediate, 1):
                    permissions[child.mime] = allowed and (typ == "multipart/alternative" or
                        (typ in {"multipart/mixed", "multipart/related"} and index == root))
            elif typ != "message/rfc822":
                name = header.get_filename()
                attached = bool(name or header.get_content_disposition() == "attachment" or
                                not typ.startswith("text/"))
                if not attached:
                    mail_delta.require(typ in {"text/plain", "text/html"}, "ambiguous_inline_text_part")
                    mail_delta.require(allowed, "ambiguous_inline_body_part")
                roles[part.part] = "attachment" if attached else "body"
    return roles


def prepare(raw, form="eml", *, max_total_bytes=512*1024**2,
            max_parts=1000, max_captures=100, max_depth=32):
    """One cumulative inventory across RFC822 and genuine byte-valued MSG streams.

    Object-valued embedded MSG attachments are derived items anchored to exact
    outer OLE bytes and exhaustive manifests; exportBytes/as_bytes is never used.
    """
    require(type(raw) is bytes and bool(raw), "mail_wire_rejected")
    require(form in {"eml", "msg"}, "mail_wire_invalid_format")
    for value, ceiling in ((max_total_bytes, 512*1024**2), (max_parts, 1000),
                           (max_captures, 100), (max_depth, 32)):
        require(type(value) is int and 0 < value <= ceiling, "mail_wire_rejected")
    require(len(raw) <= 64*1024**2, "mail_wire_total_byte_limit")
    captures, units, native_items = [], [], []
    usage = {"total_bytes": 0, "mime_parts": 0, "captures": 0, "messages": 0, "max_depth": 0}

    def charge(byte_count=0, parts=0, count=0, depth=1):
        require(usage["total_bytes"] + byte_count <= max_total_bytes, "mail_wire_total_byte_limit")
        require(usage["mime_parts"] + parts <= max_parts, "mail_wire_part_limit")
        require(usage["captures"] + count <= max_captures, "mail_wire_capture_limit")
        require(depth <= max_depth, "mail_wire_depth_limit")
        usage["total_bytes"] += byte_count
        usage["mime_parts"] += parts
        usage["captures"] += count
        usage["max_depth"] = max(usage["max_depth"], depth)

    def nested_form(data, name, content_type):
        if data.startswith(OLE_MAGIC):
            return "msg"
        if content_type == "message/rfc822" or str(name or "").lower().endswith(".eml"):
            return "eml"  # Routing hint only; wire validation must still succeed.
        return None

    def add(metadata, data):
        require(len(metadata["part"]) <= 256, "mail_wire_part_limit")
        captures.append(Capture(metadata["part"], metadata["parent_part"],
                                metadata["parent_sha256"], data, metadata))

    def eml(data, parent_part, depth_offset, counted):
        plan = prepare_rfc822_intake(data)
        roles = scalar_roles(plan.inventory)
        plan = classify_scalar_parts(plan, roles)
        charge(plan.usage.total_bytes - (len(data) if counted else 0),
               plan.usage.mime_parts, len(plan.captures), plan.usage.max_depth + depth_offset)
        usage["messages"] += plan.usage.messages
        names = {"0": parent_part}
        for capture, metadata in zip(plan.captures, plan.receipts):
            if parent_part == "0":
                part = capture.mime if capture.kind == "mime" and capture.parent_part == "0" else capture.part
            else:
                part = parent_part + "|" + capture.part
            names[capture.part] = part
            metadata.update(schema=SCHEMA, part=part, parent_part=names[capture.parent_part],
                            relation="original_rfc822_payload" if capture.kind == "eml" else "decoded_mime_payload")
            subform = "eml" if capture.kind == "eml" else nested_form(capture.payload, capture.original_filename, capture.content_type)
            metadata["format"] = subform
            add(metadata, capture.payload)
        sources = {source.part: source for source in plan.inventory.sources}
        for part in plan.inventory.parts:
            if not part.is_scalar:
                continue
            source = sources[part.message_part]
            unit = {"kind": "mime_part", "locator": {"mime": part.mime, "source_part": names[part.message_part]},
                    "text": "", "data": {"content_type": part.content_type, "filename": part.original_filename,
                                           "source_original_sha256": part.message_sha256}}
            units.append(unit)
            if roles[part.part] == "body":
                body = _decode_scalar(source.raw_bytes[part.body_start:part.wire_end], part.transfer_encoding)
                header = BytesHeaderParser(policy=policy.default).parsebytes(source.raw_bytes[part.wire_start:part.body_start])
                charset = header.get_content_charset() or "utf-8"
                try:
                    text = body.decode(charset)
                except (UnicodeError, LookupError):
                    text = body.decode("utf-8", errors="replace")
                units.append({"kind": "email_body", "locator": unit["locator"], "text": text,
                              "data": {"representation": "derived_decoded_body", "charset": charset,
                                       "source_original_sha256": part.message_sha256}})
        # RFC822 descendants were already inventoried. Only MSG transitions re-enter.
        depths = {part.part: part.depth for part in plan.inventory.parts}
        for capture in plan.captures:
            if capture.kind == "mime":
                subform = nested_form(capture.payload, capture.original_filename, capture.content_type)
                if subform == "msg":
                    msg(capture.payload, names[capture.part], depth_offset + depths[capture.part] + 1, True)
                elif subform == "eml":
                    eml(capture.payload, names[capture.part], depth_offset + depths[capture.part], True)

    def msg(data, parent_part, depth, counted):
        from . import native_msg
        charge(0 if counted else len(data), depth=depth)
        try:
            native = native_msg.inventory(data, max_parts=max_parts-usage["mime_parts"],
                max_captures=max_captures-usage["captures"], max_depth=max_depth-depth+1,
                max_stream_bytes=max_total_bytes-usage["total_bytes"])
        except native_msg.NativeMSGError as error:
            raise MailWireError(error.args[0]) from None
        charge(native.stream_bytes, native.directory_entries, len(native.items)+len(native.attachments),
               depth+native.max_depth-1)
        usage["messages"] += native.messages
        names = {"0": parent_part}
        for item in native.items:
            names[item.part] = parent_part + "|" + item.part
        for item in native.items:
            metadata = {"schema": "native-msg-item-v1", "part": names[item.part],
                "parent_part": names[item.parent_part], "outer_original_part": parent_part,
                "source_original_sha256": digest(data), "ole_storage_locator": list(item.locator),
                "stream_manifest_sha256": digest(item.manifest), "stream_manifest_bytes": len(item.manifest),
                "representation": "derived_embedded_msg_item", "received_standalone_original": False}
            require(len(metadata["part"]) <= 256, "mail_wire_part_limit")
            native_items.append((metadata, item.manifest, item.streams))
        for unit in native.units:
            unit["locator"]["source_part"] = names[unit["locator"]["source_part"]]
            units.append(unit)
        for attachment in native.attachments:
            key = parent_part + "|" + attachment.part
            names[attachment.part] = key
            subform = nested_form(attachment.payload, attachment.name, None)
            metadata = {"schema": SCHEMA, "part": key, "parent_part": names[attachment.parent_part],
                "parent_sha256": digest(data), "sha256": digest(attachment.payload), "bytes": len(attachment.payload),
                "filename": folder_name(attachment.name), "original_filename": attachment.name,
                "content_type": None, "kind": "mime", "format": subform, "relation": "decoded_msg_attachment",
                "ole_stream_locator": list(attachment.locator)}
            add(metadata, attachment.payload)
            charge(len(attachment.payload), depth=depth+attachment.depth-1)
            if subform == "eml":
                eml(attachment.payload, key, depth+attachment.depth-1, True)
            elif subform == "msg":
                msg(attachment.payload, key, depth+attachment.depth, True)

    try:
        if form == "msg":
            msg(raw, "0", 1, False)
        else:
            eml(raw, "0", 0, False)
    except (RFC822AdapterError, RFC822InventoryError) as error:
        code = error.args[0] if len(error.args) == 1 and error.args[0] in RFC822_FAILURE_CODES else "mail_wire_rejected"
        raise MailWireError(code) from None
    return MailPlan(raw, form, tuple(captures), tuple(units), dict(usage), tuple(native_items))


def folder_name(name):
    from .mail_delta import safe_filename
    return safe_filename(name)


def store(plan, root):
    """Durably seal original and every child; never overwrite an invalid existing blob."""
    root = Path(root)
    (root / "blobs").mkdir(mode=0o700, parents=True, exist_ok=True)
    for data in (plan.raw, *(capture.payload for capture in plan.captures)):
        sha = digest(data)
        folder.check_space(root, len(data))
        fd, temporary = tempfile.mkstemp(prefix=".pending-wire-", dir=root / "blobs")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            folder.seal_blob(Path(temporary), sha, root)
        finally:
            Path(temporary).unlink(missing_ok=True)


def store_native(plan, root):
    """Persist verified derived MSG artifacts separately from received originals."""
    from .eml_export import _ensure_private
    root = Path(root)
    for metadata, manifest, streams in plan.native_items:
        directory = root / 'native-items'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _ensure_private(directory / metadata['stream_manifest_sha256'], manifest)
        directory = root / 'native-streams'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        for stream in streams:
            _ensure_private(directory / stream.metadata['sha256'], stream.payload)


def needs_extended(raw, form="eml"):
    """Routing only. Existing EML receipt policy stays unchanged when not needed."""
    if form == "msg":
        return True
    from email.parser import BytesParser
    message = BytesParser(policy=policy.default).parsebytes(raw)
    for index, part in enumerate(message.walk()):
        if index >= 1000:
            return True  # Strict preparation will reject the bound.
        name = str(part.get_filename() or "").lower()
        if name.endswith((".eml", ".msg")) and part.get_content_type() != "message/rfc822":
            return True
        if part.get_content_type() == "application/vnd.ms-outlook":
            return True
        if not part.is_multipart():
            payload = part.get_payload(decode=True)
            if type(payload) is bytes and payload.startswith(OLE_MAGIC):
                return True
    return False


def project_units(plan):
    """Typed catalog locators; preserve the exhaustive raw locator in provenance."""
    originals = {"0": digest(plan.raw)}
    originals.update((capture.part, digest(capture.payload)) for capture in plan.captures)
    native = {metadata["part"]: metadata for metadata, _, _ in plan.native_items}
    require(len(native) == len(plan.native_items), "mail_wire_receipt_mismatch")
    result = []
    for unit in plan.units:
        raw_locator = dict(unit["locator"])
        context = raw_locator["source_part"]
        part = context
        if unit["kind"] == "msg_embedded_item":
            matches = [metadata["part"] for metadata in native.values()
                       if metadata["parent_part"] == context
                       and metadata["ole_storage_locator"] == raw_locator["ole_storage_locator"]
                       and metadata["source_original_sha256"] == unit["data"]["source_original_sha256"]]
            require(len(matches) == 1, "mail_wire_receipt_mismatch")
            part = matches[0]
        elif unit["kind"] == "msg_attachment":
            matches = [capture.part for capture in plan.captures
                       if capture.parent_part == context
                       and capture.metadata.get("ole_stream_locator") == raw_locator["ole_stream_locator"]
                       and digest(capture.payload) == unit["data"]["sha256"]]
            require(len(matches) == 1, "mail_wire_receipt_mismatch")
            part = matches[0]
        require(part in originals or part in native, "mail_wire_receipt_mismatch")
        require(type(part) is str and 0 < len(part) <= 500 and part == part.strip()
                and not any(ord(c) < 32 or ord(c) == 127 for c in part),
                "mail_wire_part_limit")
        locator = {"part": part}
        if "mime" in raw_locator:
            locator["mime"] = raw_locator["mime"]
        data = dict(unit["data"])
        data["wire_provenance"] = {"schema": SCHEMA, "locator": raw_locator}
        result.append({**unit, "locator": locator, "data": data})
    return tuple(result)


def identity_ok(value):
    """Additional typed receipt family, not an exemption from legacy validation."""
    import re
    if value.wire_schema != SCHEMA or value.original_format not in {"eml", "msg"}:
        return False
    def valid_part(part):
        if type(part) is not str or not 0 < len(part) <= 256:
            return False
        pieces = part.split("|")
        if len(pieces) > 32:
            return False
        for index, piece in enumerate(pieces):
            if piece == "0" and index == 0:
                continue
            if re.fullmatch(r"(?:rfc822:|mime:)?1(?:\.[1-9][0-9]*)*(?:/1(?:\.[1-9][0-9]*)*)*", piece):
                continue
            if re.fullmatch(r"(?:native-msg:|msg:)(?:__attach_version1\.0_#[0-9A-Fa-f]{8}|__substg1\.0_3701000D)(?:/(?:__attach_version1\.0_#[0-9A-Fa-f]{8}|__substg1\.0_3701000D))*", piece):
                continue
            return False
        return True
    try:
        attachments = dict(value.attachments)
        if any(type(digest) is not str or re.fullmatch("[0-9a-f]{64}", digest) is None or
               digest not in value.documents for digest in attachments.values()):
            return False
        natives = {item["part"]: item for item in value.native_items}
        if len(attachments) != len(value.attachments) or len(natives) != len(value.native_items):
            return False
        if len(attachments) + len(natives) > 100 or set(attachments) & set(natives):
            return False
        originals = {"0": value.eml_sha256, **attachments}
        parents = dict(value.attachment_parents)
        if len(parents) != len(value.attachment_parents) or set(parents) != set(attachments) | set(natives):
            return False
        if len(value.relationships) != len(attachments) or {row[0] for row in value.relationships} != set(attachments):
            return False
        if any(not valid_part(part) for part in set(attachments) | set(natives)):
            return False
        for part, item in natives.items():
            anchor = item["outer_original_part"]
            locator = item["ole_storage_locator"]
            if (item["schema"] != "native-msg-item-v1" or
                item["representation"] != "derived_embedded_msg_item" or
                item["received_standalone_original"] is not False or
                anchor not in originals or item["source_original_sha256"] != originals[anchor] or
                type(locator) is not list or not locator or
                any(type(c) is not str or not 0 < len(c) <= 31 or "/" in c or "\\" in c for c in locator) or
                part != anchor + "|native-msg:" + "/".join(locator) or
                re.fullmatch("[0-9a-f]{64}", item["stream_manifest_sha256"]) is None or
                type(item["stream_manifest_bytes"]) is not int or not 0 < item["stream_manifest_bytes"] <= 1024**2 or
                parents[part] != (None if item["parent_part"] == "0" else item["parent_part"])):
                return False
        for part, parent, parent_sha in value.relationships:
            expected = originals.get(parent)
            if parent in natives:
                expected = natives[parent]["source_original_sha256"]
            if expected is None or parent_sha != expected or parents[part] != (None if parent == "0" else parent):
                return False
        for part in parents:
            seen = set()
            cursor = part
            while cursor is not None:
                if cursor in seen or cursor not in parents:
                    return False
                seen.add(cursor)
                if len(seen) > 32:
                    return False
                cursor = parents[cursor]
        if len(set(value.eml_parts)) != len(value.eml_parts) or not set(value.eml_parts) <= set(attachments):
            return False
        return True
    except (KeyError, TypeError, ValueError):
        return False
