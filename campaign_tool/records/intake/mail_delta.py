"""Offline, single-receipt mailbox delta into an existing intake v3.2 ledger.

Receipt values are data. This module never scans the mailbox, connects to a
provider, extracts text, or updates the existing inventory run in place.
"""
import argparse
import email.policy
from email.parser import BytesParser
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

from campaign_tool.records.intake import folder

MAX_RECEIPT = 1024 * 1024
MAX_EML = 64 * 1024 * 1024
MAX_ATTACHMENT = 128 * 1024 * 1024
MAX_TOTAL = 512 * 1024 * 1024
MAX_ATTACHMENTS = 100
MAX_MIME_PARTS = 1000


class Rejected(ValueError):
    """Safe, non-narrative failure code."""


def require(test, code):
    if not test:
        raise Rejected(code)


def private_path(path, directory=False):
    """Reject linked, shared, or wrong-owner canonical output paths."""
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path, "unsafe_output_path")
    try:
        mode = path.lstat()
    except FileNotFoundError:
        raise Rejected("missing_output") from None
    require(mode.st_uid == os.getuid() and not (stat.S_IMODE(mode.st_mode) & 0o077),
            "output_not_owner_only")
    require(stat.S_ISDIR(mode.st_mode) if directory else stat.S_ISREG(mode.st_mode),
            "unsafe_output_type")


def source_path(root, literal, out):
    require(isinstance(literal, str) and 0 < len(literal) <= 4096 and "\x00" not in literal,
            "invalid_source_path")
    require(".." not in literal.split("/") and "\\" not in literal,
            "source_path_escape")
    path = Path(literal) if literal.startswith("/") else root / literal
    path = Path(os.path.abspath(path))
    require(os.path.commonpath((root, path)) == str(root) and path != root,
            "source_path_escape")
    require(os.path.commonpath((out, path)) != str(out), "source_is_output")
    return path


def number(value, code, ceiling):
    require(type(value) is int and 0 <= value <= ceiling, code)
    return value


def identity_number(value, code):
    require((type(value) is int and value > 0) or
            (isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value)), code)
    return str(value)


def digest(value):
    require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value),
            "invalid_sha256")
    return value


def short_text(value, code):
    require(isinstance(value, str) and 0 < len(value) <= 256 and "\x00" not in value,
            code)
    return value


def load_receipt(path):
    try:
        with folder.secure_open(path) as stream:
            require(os.fstat(stream.fileno()).st_size <= MAX_RECEIPT,
                    "receipt_size_limit")
            raw = stream.read(MAX_RECEIPT + 1)
        require(len(raw) <= MAX_RECEIPT, "receipt_size_limit")
        receipt = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise Rejected("invalid_receipt_file") from None
    if isinstance(receipt, dict) and receipt.get("schema") == "mail-wire-receipt-v2":
        from .native_receipt import parse
        return parse(receipt)
    require(isinstance(receipt, dict) and receipt.get("complete") is True,
            "incomplete_receipt")
    account = short_text(receipt.get("account_id"), "invalid_account")
    mailbox = short_text(receipt.get("folder"), "invalid_folder")
    uidvalidity = identity_number(receipt.get("uidvalidity"), "invalid_uidvalidity")
    uid = identity_number(receipt.get("uid"), "invalid_uid")
    original = receipt.get("original_eml")
    attachments = receipt.get("attachments")
    require(isinstance(original, dict) and isinstance(attachments, list),
            "invalid_receipt_shape")
    require(len(attachments) <= MAX_ATTACHMENTS, "attachment_count_limit")
    number(receipt.get("bytes"), "invalid_receipt_bytes", MAX_TOTAL)
    original_size = number(original.get("bytes"), "invalid_eml_bytes", MAX_EML)
    require(receipt["bytes"] == original_size, "receipt_bytes_mismatch")
    original_sha = digest(original.get("sha256"))
    require(isinstance(original.get("path"), str), "invalid_source_path")
    items = [dict(part="0", sha=original_sha, size=original_size,
                  path=original["path"], kind="eml", source=original)]
    parts = set()
    total = original_size
    for attachment in attachments:
        require(isinstance(attachment, dict), "invalid_attachment")
        part = attachment.get("part")
        require((isinstance(part, str) and 0 < len(part) <= 256) or
                (type(part) is int and part > 0), "invalid_part")
        raw_part = part
        part = str(part)
        require(part != "0" and part not in parts and "\x00" not in part,
                "duplicate_or_invalid_part")
        parts.add(part)
        size = number(attachment.get("bytes"), "invalid_attachment_bytes",
                      MAX_ATTACHMENT)
        total += size
        require(total <= MAX_TOTAL, "receipt_total_size_limit")
        digest(attachment.get("sha256"))
        require(isinstance(attachment.get("path"), str), "invalid_source_path")
        for field in ("filename", "original_filename", "content_type"):
            value = attachment.get(field)
            require(value is None or (isinstance(value, str) and len(value) <= 4096 and
                                      "\x00" not in value), "invalid_attachment_metadata")
        items.append(dict(part=part, part_value=raw_part,
                          part_is_walk_index=type(raw_part) is int,
                          sha=attachment["sha256"], size=size,
                          path=attachment["path"], kind="attachment", source=attachment))
    if "schema" in receipt:
        from .rfc822_adapter import SCHEMA
        require(receipt["schema"] == SCHEMA, "invalid_receipt_shape")
        for item in items[1:]:
            require(item["source"].get("kind") in {"eml", "mime"}, "invalid_attachment_metadata")
            item["kind"] = "eml" if item["source"]["kind"] == "eml" else "attachment"
    return receipt, (account, mailbox, uidvalidity, uid), items


def stage_source(item, root, out):
    path = source_path(root, item["path"], out)
    folder.check_space(out, item["size"])
    tmp = out / "blobs" / (".pending-" + uuid.uuid4().hex)
    hasher = hashlib.sha256()
    count = 0
    head = b""
    try:
        with folder.secure_open(path) as source, tmp.open("xb") as staged:
            before = os.fstat(source.fileno())
            require(before.st_size == item["size"], "source_size_mismatch")
            while True:
                block = source.read(min(1024 * 1024, item["size"] - count + 1))
                if not block:
                    break
                count += len(block)
                require(count <= item["size"], "source_size_mismatch")
                if not head:
                    head = block[:16384]
                hasher.update(block)
                staged.write(block)
            after = os.fstat(source.fileno())
            staged.flush()
            os.fsync(staged.fileno())
        require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                 before.st_ctime_ns) ==
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                 after.st_ctime_ns), "source_changed")
        require(count == item["size"], "source_size_mismatch")
        require(hasher.hexdigest() == item["sha"], "source_hash_mismatch")
        item["tmp"] = tmp
        item["head"] = head
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def mime_content_type(part):
    """Validate classification headers; an absent header keeps the MIME default."""
    headers = part.get_all("Content-Type", [])
    require(len(headers) <= 1 and not any(header.defects for header in headers),
            "invalid_mime_content_type")
    return part.get_content_type()


def related_root(part, children):
    """Resolve only immediate children; never guess around an invalid start."""
    require(bool(children) and not part.defects, "invalid_related_container")
    headers = part.get_all("Content-Type", [])
    require(len(headers) == 1 and not headers[0].defects,
            "invalid_related_container")
    ids = {}
    for index, child in enumerate(children, 1):
        values = child.get_all("Content-ID", [])
        require(len(values) <= 1, "ambiguous_related_content_id")
        if not values:
            continue
        cid = str(values[0]).strip()
        require(re.fullmatch(r"<[^<>\s]+>", cid) is not None,
                "invalid_related_content_id")
        require(cid not in ids, "ambiguous_related_content_id")
        ids[cid] = index
    start = part.get_param("start")
    root = 1
    if start is not None:
        require(isinstance(start, str) and
                re.fullmatch(r"<[^<>\s]+>", start) is not None,
                "invalid_related_start")
        require(start in ids, "missing_related_root")
        root = ids[start]
    root_type = mime_content_type(children[root - 1])
    declared_type = part.get_param("type")
    # Some exporters omit type. Preserve that compatibility, but never ignore
    # an explicit declaration that contradicts the selected root.
    require(declared_type is None or
            (isinstance(declared_type, str) and
             declared_type.lower() == root_type),
            "related_root_type_mismatch")
    return root


def mime_candidates(eml_path, *, raw=None, allow_rfc822=False, legacy_scalar=False):
    try:
        if raw is not None:
            parser = BytesParser(policy=email.policy.default)
            if legacy_scalar:
                # Match historical scalar export/import hashes, paths and ledger edges.
                from io import BytesIO
                message = parser.parse(BytesIO(raw))
            else:
                message = parser.parsebytes(raw)
        else:
            with folder.secure_open(eml_path) as source:
                message = BytesParser(policy=email.policy.default).parse(source)
    except (OSError, ValueError, RecursionError):
        raise Rejected("invalid_eml") from None
    candidates = []
    stack = [(message, "1", True)]
    visited = 0
    while stack:
        part, locator, body_allowed = stack.pop()
        visited += 1
        require(visited <= MAX_MIME_PARTS, "mime_part_limit")
        walk_index = visited - 1  # Exporter enumerates every msg.walk() node from zero.
        content_type = mime_content_type(part)
        if content_type == "message/rfc822":
            if allow_rfc822:
                continue  # The exact-wire inventory owns this message boundary.
            raise Rejected("unsupported_rfc822_part")
        if content_type.startswith("multipart/"):
            dispositions = part.get_all("Content-Disposition", [])
            require(len(dispositions) <= 1 and
                    not any(header.defects for header in dispositions),
                    "invalid_multipart_disposition")
            # Container bytes are not separately bound by this adapter. Do not
            # descend past an explicitly attached/named original and lose it.
            require(not part.get_filename() and
                    part.get_content_disposition() != "attachment",
                    "unsupported_attached_multipart_part")
            require(part.is_multipart() and not part.defects,
                    "invalid_mime_container")
        if part.is_multipart():
            children = list(part.iter_parts())
            require(visited + len(stack) + len(children) <= MAX_MIME_PARTS,
                    "mime_part_limit")
            root = related_root(part, children) if content_type == "multipart/related" else 1
            # Body permission follows the whole ancestry. A related/alternative
            # wrapper in a non-body slot must not launder an inline text record.
            stack.extend((child, locator + "." + str(n),
                          body_allowed and
                          (content_type == "multipart/alternative" or
                           (content_type in {"multipart/mixed", "multipart/related"} and
                            n == root)))
                         for n, child in reversed(list(enumerate(children, 1))))
            continue
        dispositions = part.get_all("Content-Disposition", [])
        require(len(dispositions) <= 1 and
                not any(header.defects for header in dispositions),
                "invalid_leaf_disposition")
        data = part.get_payload(decode=True) or b""
        require(len(data) <= MAX_ATTACHMENT, "mime_payload_size_limit")
        name = part.get_filename()
        disposition = part.get_content_disposition()
        if not name and disposition != "attachment" and content_type.startswith("text/"):
            if content_type not in {"text/plain", "text/html"}:
                raise Rejected("ambiguous_inline_text_part")
            if not body_allowed:
                raise Rejected("ambiguous_inline_body_part")
        attachable = bool(name or disposition == "attachment" or
                          not content_type.startswith("text/"))
        candidates.append(dict(locator=locator, walk_index=walk_index,
                               sha=hashlib.sha256(data).hexdigest(), size=len(data),
                               name=name, content_type=content_type,
                               attachable=attachable))
    return candidates


def wire_intake_plan(raw):
    """Keep legacy body/related policy, independently for each preserved message."""
    from . import rfc822_adapter as adapter
    plan = adapter.prepare_rfc822_intake(raw)
    choices = {}
    by_source = {}
    for source in plan.inventory.sources:
        by_source[source.part] = {candidate["locator"]: candidate
            for candidate in mime_candidates(None, raw=source.raw_bytes, allow_rfc822=True)}
    for part in plan.inventory.scalar_parts:
        candidate = by_source[part.message_part].get(part.mime)
        require(candidate is not None, "missing_or_mismatched_mime_part")
        choices[part.part] = "attachment" if candidate["attachable"] else "body"
    return adapter.classify_scalar_parts(plan, choices)


def safe_filename(original_filename):
    base = re.split(r"[/\\]", original_filename or "")[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip(".")[:100]
    return cleaned or "attachment.bin"


def prepare_mail(raw, form="eml", **limits):
    from .mail_wire import prepare
    return prepare(raw, form, **limits)


def _native_artifact(root, out, literal, expected, destination):
    from .native_receipt import artifact
    return artifact(root, out, literal, expected, destination)


def bind_parts(items, receipt=None):
    if receipt is not None and receipt.get("schema") == "mail-wire-receipt-v2":
        from .native_receipt import bind
        return bind(items, receipt, items[0]["native_root"], items[0]["native_output"])
    if receipt is not None and "schema" in receipt:
        from . import rfc822_adapter as adapter
        with folder.secure_open(items[0]["tmp"]) as source:
            raw = source.read(MAX_EML + 1)
        plan = wire_intake_plan(raw)
        require(receipt.get("scalar_roles") == plan.as_manifest()["scalar_roles"] and
                receipt.get("budget") == plan.as_manifest()["budget"], "invalid_receipt_shape")
        captures = adapter.bind_rfc822_receipts(plan, [item["source"] for item in items[1:]])
        by_part = {capture.part: capture for capture in captures}
        for item in items[1:]:
            # Binding checks membership; map by part rather than trusting list order.
            capture = by_part[item["part"]]
            require(item["sha"] == capture.sha256 and item["size"] == len(capture.payload),
                    "missing_or_mismatched_mime_part")
            item.update(mime=capture.mime, mime_name=capture.original_filename,
                        parent_sha=capture.parent_sha256, mime_chain=list(capture.mime_chain),
                        wire_schema=adapter.SCHEMA)
        return
    candidates = mime_candidates(items[0]["tmp"])
    required = {c["locator"] for c in candidates if c["attachable"]}
    used = set()
    for item in items[1:]:
        part = item["part"]
        aliases = {part}
        if re.fullmatch(r"[1-9][0-9]*", part):
            aliases.add("1." + part)
        matches = [c for c in candidates if c["attachable"] and
                   (c["walk_index"] == int(part) if item["part_is_walk_index"]
                    else c["locator"] in aliases) and
                   c["sha"] == item["sha"] and c["size"] == item["size"] and
                   c["locator"] not in used]
        require(len(matches) == 1, "missing_or_mismatched_mime_part")
        matched = matches[0]
        original_filename = item["source"].get("original_filename")
        require(original_filename == matched["name"], "mime_filename_mismatch")
        require(item["source"].get("filename") == safe_filename(original_filename),
                "export_filename_mismatch")
        declared_type = item["source"].get("content_type")
        require(declared_type is None or declared_type == matched["content_type"],
                "mime_content_type_mismatch")
        item["mime"] = matched["locator"]
        item["mime_name"] = matched["name"]
        used.add(item["mime"])
    require(used == required, "unlisted_mime_attachment")


def occurrence(item, identity, root, parent_sha):
    account, mailbox, uidvalidity, uid = identity
    oid = folder.hid(folder.js(["mail-receipt-v1", account, mailbox,
                                uidvalidity, uid, item["part"]]))
    locator = {"mail_receipt_part": item.get("part_value", item["part"])}
    if item["part"] != "0":
        locator["mime"] = item["mime"]
    if "mime_chain" in item:
        locator["mime_chain"] = item["mime_chain"]
    metadata = dict(source="mail_delta", account_id=account, folder=mailbox,
                    uidvalidity=uidvalidity, uid=uid,
                    part=item.get("part_value", item["part"]))
    if item["part"] != "0":
        metadata.update(filename=item["source"].get("filename"),
                        original_filename=item["source"].get("original_filename"),
                        content_type=item["source"].get("content_type"))
    return dict(oid=oid, sha=item["sha"], path=item["path"], root=str(root),
                parent=item.get("parent_sha", parent_sha) if item["part"] != "0" else None,
                locator=folder.js(locator), receipt=folder.js(metadata))


def seal_tracked(item, out, created):
    tmp = item["tmp"]
    staged = tmp.stat()
    dest = out / "blobs" / item["sha"]
    try:
        folder.seal_blob(tmp, item["sha"], out)
    except ValueError:
        raise Rejected("existing_blob_invalid") from None
    finally:
        try:
            current = dest.lstat()
        except FileNotFoundError:
            current = None
        if (current is not None and stat.S_ISREG(current.st_mode) and
                (current.st_dev, current.st_ino) == (staged.st_dev, staged.st_ino)):
            created[item["sha"]] = (staged.st_dev, staged.st_ino)


def cleanup_created_blobs(out, created):
    if not created:
        return
    try:
        db = sqlite3.connect(out / "intake.sqlite")
        try:
            removed = False
            for sha, inode in created.items():
                if db.execute("SELECT 1 FROM preservations WHERE sha=?", (sha,)).fetchone():
                    continue
                dest = out / "blobs" / sha
                try:
                    current = dest.lstat()
                except FileNotFoundError:
                    continue
                if (stat.S_ISREG(current.st_mode) and current.st_nlink == 1 and
                        (current.st_dev, current.st_ino) == inode):
                    dest.unlink()
                    removed = True
            if removed:
                fd = os.open(out / "blobs", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        finally:
            db.close()
    except (OSError, sqlite3.Error):
        raise Rejected("new_blob_cleanup_failed") from None


def edge_locator(item):
    """Keep exact-wire relationships distinct from legacy decoded derivations."""
    locator = {"mime": item["mime"]}
    if item.get("wire_schema") == "mail-wire-receipt-v2":
        locator["part"] = item["part"]
    if "wire_schema" in item:
        locator["wire_schema"] = item["wire_schema"]
    return folder.js(locator)


def ledger_import(db, out, root, identity, items, created):
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("BEGIN IMMEDIATE")
    current = db.execute("SELECT value FROM meta WHERE key='inventory_run'").fetchone()
    require(current is not None, "missing_active_inventory")
    prior = current[0]
    active_run = db.execute("SELECT status FROM runs WHERE id=?", (prior,)).fetchone()
    require(active_run is not None, "missing_active_run")
    require(active_run[0] == "complete", "incomplete_active_run")
    blocked = {row[0] for row in db.execute("SELECT sha FROM scope_exclusions WHERE sha!=''")}
    blocked_paths = {
        os.path.normpath(path if os.path.isabs(path) else str(root / path))
        for (path,) in db.execute("SELECT path FROM scope_exclusions") if path
    }
    require(not any(item["sha"] in blocked for item in items), "quarantined_hash")
    require(not any(str(item["resolved"]) in blocked_paths for item in items),
            "quarantined_path")
    records = [occurrence(item, identity, root, items[0]["sha"]) for item in items]
    changed = False
    for item, record in zip(items, records):
        old_doc = db.execute("SELECT bytes FROM docs WHERE sha=?", (item["sha"],)).fetchone()
        require(old_doc is None or old_doc[0] == item["size"], "document_size_conflict")
        old = db.execute("SELECT sha,path,root,parent,locator,receipt FROM occurrences WHERE oid=?",
                         (record["oid"],)).fetchall()
        require(not old or (len(old) == 1 and tuple(old[0]) ==
                tuple(record[key] for key in ("sha", "path", "root", "parent",
                                             "locator", "receipt"))), "receipt_identity_conflict")
        if not old or not db.execute("SELECT 1 FROM seen WHERE run=? AND oid=? AND sha=?",
                                    (prior, record["oid"], record["sha"])).fetchone():
            changed = True
        preservation = db.execute("SELECT bytes FROM preservations WHERE sha=?",
                                  (item["sha"],)).fetchone()
        require(preservation is None or preservation[0] == item["size"],
                "preservation_size_conflict")
    for item in items[1:]:
        parent = item.get("parent_sha", items[0]["sha"])
        loc = edge_locator(item)
        edge = db.execute("SELECT child FROM edges WHERE parent=? AND locator=?",
                          (parent, loc)).fetchone()
        require(edge is None or edge[0] == item["sha"], "parent_edge_conflict")
        changed |= edge is None
    if not changed:
        for item in items:
            try:
                verified = folder.verify_blob(item["sha"], out)
            except (OSError, ValueError):
                raise Rejected("existing_blob_invalid") from None
            require(verified["bytes"] == item["size"], "existing_blob_invalid")
        db.rollback()
        return dict(status="replay", added_occurrences=0, added_edges=0, run_id=prior)
    for item in items:
        seal_tracked(item, out, created)
        item["tmp"] = None
    run = uuid.uuid4().hex
    stamp = folder.now()
    db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?)",
               (run, stamp, stamp, "mail_delta", "complete",
                folder.js({"previous_run": prior, "receipt_count": 1})))
    db.execute("INSERT INTO seen SELECT ?,oid,sha FROM seen WHERE run=?", (run, prior))
    added_occurrences = 0
    added_edges = 0
    for item, record in zip(items, records):
        name = (item["mime_name"] or ("part-" + item["mime"])) if item["kind"] == "attachment" else item["path"]
        form = item.get("verified_format") or ("eml" if item["kind"] == "eml" else folder.fmt(name, item["head"], out / "blobs" / item["sha"]))
        db.execute("INSERT OR IGNORE INTO docs(sha,bytes,format,first_seen) VALUES(?,?,?,?)",
                   (item["sha"], item["size"], form, stamp))
        db.execute("INSERT OR IGNORE INTO preservations VALUES(?,?,?,?)",
                   (item["sha"], str(out / "blobs" / item["sha"]), stamp, item["size"]))
        cursor = db.execute("INSERT OR IGNORE INTO occurrences VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (record["oid"], record["sha"], record["path"], record["root"],
                             record["parent"], record["locator"], "original",
                             record["receipt"], stamp, stamp))
        added_occurrences += cursor.rowcount
        db.execute("INSERT OR IGNORE INTO seen VALUES(?,?,?)",
                   (run, record["oid"], record["sha"]))
        if item["part"] != "0":
            edge_name = item["mime_name"] or name
            cursor = db.execute("INSERT OR IGNORE INTO edges VALUES(?,?,?,?)",
                                (item.get("parent_sha", items[0]["sha"]), edge_locator(item),
                                 item["sha"], edge_name))
            added_edges += cursor.rowcount
    db.execute("UPDATE meta SET value=? WHERE key='inventory_run'", (run,))
    db.commit()
    return dict(status="imported", added_occurrences=added_occurrences,
                added_edges=added_edges, run_id=run)


def import_receipt(receipt_path, mail_root, output):
    root = Path(os.path.abspath(mail_root))
    out = Path(os.path.abspath(output))
    require(root.resolve() == root and root.is_dir(), "unsafe_mail_root")
    private_path(out, directory=True)
    private_path(out / "blobs", directory=True)
    private_path(out / "intake.sqlite")
    receipt, identity, items = load_receipt(receipt_path)
    for item in items:
        item["resolved"] = source_path(root, item["path"], out)
    old_umask = os.umask(0o077)
    lock_fd = None
    created = {}
    committed = False
    try:
        lock_fd = os.open(out / "writer.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                          0o600)
        lock_stat = os.fstat(lock_fd)
        require(stat.S_ISREG(lock_stat.st_mode) and lock_stat.st_uid == os.getuid() and
                not (stat.S_IMODE(lock_stat.st_mode) & 0o077), "unsafe_writer_lock")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for item in items:
            stage_source(item, root, out)
        items[0].update(native_root=root, native_output=out)
        bind_parts(items, receipt)
        db = sqlite3.connect(out / "intake.sqlite")
        try:
            db.execute("PRAGMA busy_timeout=0")
            result = ledger_import(db, out, root, identity, items, created)
        finally:
            db.close()
        committed = True
        return result
    except (OSError, sqlite3.Error):
        raise Rejected("mail_delta_io_or_ledger_error") from None
    finally:
        try:
            for item in items:
                tmp = item.get("tmp")
                if tmp is not None:
                    tmp.unlink(missing_ok=True)
            if not committed:
                cleanup_created_blobs(out, created)
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            os.umask(old_umask)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, help="one exporter JSON receipt")
    parser.add_argument("--mail-root", required=True, help="explicit trusted export root")
    parser.add_argument("--output", required=True, help="existing private intake output")
    args = parser.parse_args()
    try:
        result = import_receipt(args.receipt, args.mail_root, args.output)
    except (Rejected, ValueError) as error:
        code = str(error) if isinstance(error, Rejected) else "invalid_mail_delta"
        parser.exit(2, "mail_delta: rejected (" + code + ")\n")
    print(folder.js(result))


if __name__ == "__main__":
    main()
