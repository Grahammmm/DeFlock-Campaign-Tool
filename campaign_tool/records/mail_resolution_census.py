"""Private deterministic inventory, not a policy, approval or apply planner."""
from collections import Counter
import contextlib
from email import policy
from email.parser import BytesParser
from email.message import MIMEPart
import hashlib
import os
from pathlib import Path
import stat

from . import mail_adoption as adoption
from .intake import mail_delta
from .ledger import original_resolution as resolution, original_resolution_mail as mail, store
from .ledger.mail_resolution_policy import decode, read_regular
from .mail_resolution_config import load_settings
from .runner.core import hid


def _source(entry, mail_root, output):
    path = mail_delta.source_path(mail_root, entry["path"], output)
    raw = read_regular(path, entry["size"])
    if len(raw) != entry["size"] or hashlib.sha256(raw).hexdigest() != entry["sha"]:
        raise adoption.AdoptionError("resolution_census_source_mismatch")
    return raw


def receipt_occurrences(item, mail_root, output):
    """Compute only genuine ids; no intake/CAS writes or reserialized messages.

    Legacy scalar indexes are mapped through the complete MIME tree, even when
    a message/rfc822 boundary makes the whole receipt ineligible. Unknown or
    mismatched parts are refused rather than guessed from an integer locator.
    """
    receipt_raw = read_regular(item["path"], mail_delta.MAX_RECEIPT)
    if (hashlib.sha256(receipt_raw).hexdigest() != item["receipt_sha256"]
            or decode(receipt_raw) != item["declaration"]):
        raise adoption.AdoptionError("resolution_census_receipt_changed")
    mid = adoption.message_id(item["identity"])
    raw = _source(item["items"][0], mail_root, output)
    records = [(mid, item["items"][0]["sha"], "mail", len(raw))]
    declaration = item["declaration"]
    if "schema" in declaration:
        # These receipt formats already use exact canonical part locators.
        if declaration["schema"] == "mail-wire-receipt-v2":
            from .intake import mail_wire
            plan = mail_wire.prepare(raw, declaration["original_eml"]["format"])
            wanted = {c.part: c for c in plan.captures}
            if (set(wanted) != {e["part"] for e in item["items"][1:]}
                    or declaration["budget"] != plan.budget
                    or {k: v for k, v in declaration["original_eml"].items() if k != "path"} != plan.original):
                raise adoption.AdoptionError("resolution_census_occurrence_mismatch")
            for entry in item["items"][1:]:
                capture = wanted[entry["part"]]
                if ({k: v for k, v in entry["source"].items() if k != "path"} != capture.metadata
                        or _source(entry, mail_root, output) != capture.payload):
                    raise adoption.AdoptionError("resolution_census_occurrence_mismatch")
            native = {e["part"]: e for e in declaration["native_items"]}
            if set(native) != {metadata["part"] for metadata, _, _ in plan.native_items}:
                raise adoption.AdoptionError("resolution_census_occurrence_mismatch")
            for metadata, manifest, streams in plan.native_items:
                given = native[metadata["part"]]
                if {k: v for k, v in given.items() if k not in {"manifest_path", "streams"}} != metadata:
                    raise adoption.AdoptionError("resolution_census_occurrence_mismatch")
                from .intake.native_receipt import artifact
                artifact(mail_root, output, given["manifest_path"], manifest, None)
                if len(given["streams"]) != len(streams):
                    raise adoption.AdoptionError("resolution_census_occurrence_mismatch")
                for supplied, stream in zip(given["streams"], streams):
                    if {k: v for k, v in supplied.items() if k != "path"} != stream.metadata:
                        raise adoption.AdoptionError("resolution_census_occurrence_mismatch")
                    artifact(mail_root, output, supplied["path"], stream.payload, None)
                # Embedded native items refer to their genuine source original,
                # never a derived standalone original.
                subject = metadata["source_original_sha256"]
                sizes = {sha: size for _, sha, _, size in records}
                sizes.update({e["sha"]: e["size"] for e in item["items"]})
                if subject not in sizes:
                    raise adoption.AdoptionError("resolution_census_occurrence_mismatch")
                records.append((hid(["native-msg-item", mid, metadata["part"]]), subject, "attachment", sizes[subject]))
        else:
            from .intake.rfc822_adapter import bind_rfc822_receipts
            plan = mail_delta.wire_intake_plan(raw)
            if declaration["scalar_roles"] != plan.as_manifest()["scalar_roles"] or declaration["budget"] != plan.as_manifest()["budget"]:
                raise adoption.AdoptionError("resolution_census_occurrence_mismatch")
            captures = {c.part: c for c in bind_rfc822_receipts(plan, [e["source"] for e in item["items"][1:]])}
            for entry in item["items"][1:]:
                if _source(entry, mail_root, output) != captures[entry["part"]].payload:
                    raise adoption.AdoptionError("resolution_census_occurrence_mismatch")
        for entry in item["items"][1:]:
            records.append((hid(["attachment", mid, entry["part"]]), entry["sha"], "attachment", entry["size"]))
        return records

    message = BytesParser(policy=policy.default).parsebytes(raw)
    stack: list[tuple[MIMEPart, str]] = [(message, "1")]
    leaves, visited = [], 0
    while stack:
        part, loc = stack.pop()
        index = visited
        visited += 1
        if visited > mail_delta.MAX_MIME_PARTS:
            raise adoption.AdoptionError("resolution_census_part_bound")
        if part.is_multipart():
            stack.extend((child, loc + "." + str(n)) for n, child in
                         reversed(list(enumerate(part.iter_parts(), 1))))
        else:
            data = part.get_payload(decode=True) or b""
            leaves.append((index, loc, data, part))
    used = set()
    for entry in item["items"][1:]:
        data = _source(entry, mail_root, output)
        aliases = {entry["part"], "1." + entry["part"]}
        matches = [(loc, part) for index, loc, payload, part in leaves
                   if (index == int(entry["part"]) if entry["part_is_walk_index"] else loc in aliases)
                   and payload == data and loc not in used]
        if len(matches) != 1:
            raise adoption.AdoptionError("resolution_census_occurrence_mismatch")
        loc, part = matches[0]
        if (part.get_filename() != entry["source"].get("original_filename")
                or entry["source"].get("filename") != mail_delta.safe_filename(part.get_filename())
                or entry["source"].get("content_type") not in (None, part.get_content_type())):
            raise adoption.AdoptionError("resolution_census_occurrence_mismatch")
        used.add(loc)
        records.append((hid(["attachment", mid, loc]), entry["sha"], "attachment", entry["size"]))
    return records


def build_inventory(root, mail_root, config_path=None):
    """Read every discovered receipt, independent of its adoption state."""
    root, mail_root = Path(os.path.abspath(root)), Path(os.path.abspath(mail_root))
    if root.resolve() != root or mail_root.resolve() != mail_root:
        raise adoption.AdoptionError("symlinked_path")
    if mail_root.is_relative_to(root):
        raise adoption.AdoptionError("export_inside_records_root")
    settings = load_settings(root, config_path)
    receipts, gaps = adoption.discover(mail_root, root / "intake")
    containing = {}
    with contextlib.ExitStack() as stack:
        con = stack.enter_context(adoption._snapshot_ledger(root / "ledger.sqlite"))
        intake_path = root / "intake" / "intake.sqlite"
        if os.path.lexists(intake_path):
            stack.enter_context(adoption._snapshot_db(intake_path, "intake"))
        installed = resolution.installed(con)
        for item in receipts:
            code = adoption.mime_precheck(item, mail_root, root / "intake")
            grouped = {}
            for oid, subject, kind, size in receipt_occurrences(item, mail_root, root / "intake"):
                grouped.setdefault(subject, []).append((oid, kind, size))
            for subject, records in grouped.items():
                containing.setdefault(subject, []).append({"receipt_sha256": item["receipt_sha256"],
                    "mime_precheck": code, "occurrence_ids": sorted({r[0] for r in records}),
                    "mail_occurrence_ids": sorted({r[0] for r in records if r[1] == "mail"}),
                    "sizes": sorted({r[2] for r in records}), "kinds": sorted({r[1] for r in records})})
        entries = {}
        for subject, members in sorted(containing.items()):
            original = con.execute("SELECT * FROM originals WHERE sha256=?", (subject,)).fetchone()
            if original is None:
                continue
            row = con.execute("SELECT * FROM original_resolutions WHERE original_sha256=?", (subject,)).fetchone() if installed else None
            if row is not None:
                # Every existing row goes through the real fail-closed reader,
                # including unknown payloads and local-capture resolutions.
                settings.context.effective_original(con, subject)
            payload = decode(row["payload_json"]) if row is not None else None
            resolved = payload is not None and payload.get("schema") == mail.MAIL_SCHEMA
            if not resolved and (original["scope"] != "unresolved" or row is not None):
                continue
            members.sort(key=lambda m: m["receipt_sha256"])
            code, legacy = None, None
            try:
                provenance = decode(original["provenance_json"])
                legacy_row = con.execute("SELECT payload_sha256 FROM legacy_rows WHERE import_id=? AND source_table='docs' AND source_rowid=?",
                    (provenance.get("import_id"), provenance.get("source_rowid"))).fetchone()
                legacy = legacy_row[0] if legacy_row else None
                mail._legacy_binding(con, subject, original, {"legacy_binding_sha256": legacy})
                if any(m["sizes"] != [original["bytes"]] for m in members):
                    raise ValueError("original_bytes_conflict")
                if resolved:
                    settings.context.effective_original(con, subject)
                else:
                    # Check every role in which these bytes occur, not just the
                    # first receipt encountered (a shared EML/attachment).
                    for kind in sorted({k for m in members for k in m["kinds"]}):
                        mail.eligible(con, subject, original, {"legacy_binding_sha256": legacy}, kind)
            except (ValueError, KeyError, TypeError) as error:
                if resolved:
                    raise
                safe = str(error)
                code = safe if safe.startswith("original_") else "original_resolution_import_binding_mismatch"
            eligible = [m for m in members if m["mime_precheck"] is None]
            if resolved:
                assert row is not None and payload is not None
                proposal = {"state": "fixed", "verification_receipt_sha256": row["capture_receipt_sha256"],
                    "binding_occurrence_id": row["capture_occurrence_id"], "legacy_binding_sha256": payload["legacy_binding_sha256"],
                    "dependent_receipts": sorted(m["receipt_sha256"] for m in members if m["receipt_sha256"] != row["capture_receipt_sha256"])}
            elif not eligible:
                proposal = {"state": "held", "code": "resolution_no_eligible_binding_receipt"}
            else:
                binding = eligible[0]
                proposal = {"state": "proposed", "verification_receipt_sha256": binding["receipt_sha256"],
                    "binding_occurrence_id": (binding["mail_occurrence_ids"] or binding["occurrence_ids"])[0],
                    "dependent_receipts": [m["receipt_sha256"] for m in members if m is not binding]}
            entries[subject] = {"bytes": original["bytes"], "legacy_format": original["legacy_format"],
                "kinds": sorted({k for m in members for k in m["kinds"]}), "legacy_binding_sha256": legacy,
                "state": "resolved" if resolved else "unresolved", "eligibility_code": code,
                "receipts": [{k: v for k, v in m.items() if k not in {"sizes", "kinds", "mail_occurrence_ids"}} for m in members],
                "proposal": proposal, "warnings": []}
    held_receipts = {m["receipt_sha256"] for entry in entries.values() if entry["proposal"]["state"] == "held" for m in entry["receipts"]}
    for entry in entries.values():
        if entry["proposal"].get("verification_receipt_sha256") in held_receipts:
            entry["warnings"].append("resolution_binding_receipt_contains_held_original")
    counts = {"receipts": len(receipts), "originals": len(entries),
        "shared_originals": sum(len(e["receipts"]) > 1 for e in entries.values()),
        "repeated_originals": sum(any(len(m["occurrence_ids"]) > 1 for m in e["receipts"]) for e in entries.values()),
        "proposed_bindings": sum(e["proposal"]["state"] == "proposed" for e in entries.values()),
        "fixed_bindings": sum(e["proposal"]["state"] == "fixed" for e in entries.values()),
        "proposed_held": dict(sorted(Counter(e["proposal"]["code"] for e in entries.values() if e["proposal"]["state"] == "held").items())),
        "discovery_gaps": dict(sorted(gaps.items()))}
    return {"schema": "legacy-mail-resolution-inventory-v1", "counts": counts, "originals": entries}


def inventory(root, mail_root, output_path, *, config_path=None):
    """Create a new private regular file outside source trees and Git; no overwrite."""
    target = Path(os.path.abspath(output_path))
    sources = [Path(os.path.abspath(p)) for p in (root, mail_root)]
    if (target.resolve() != target or any(target.is_relative_to(p) for p in sources)
            or any(os.path.lexists(p / ".git") for p in target.parents)):
        raise adoption.AdoptionError("resolution_inventory_output_unsafe")
    # Walk with directory fds so replacing a component cannot redirect output.
    directory = os.open(target.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in target.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise adoption.AdoptionError("resolution_inventory_output_unsafe")
        if os.path.lexists(target):
            raise adoption.AdoptionError("resolution_inventory_output_exists")
        value = build_inventory(root, mail_root, config_path)
        raw = store.canonical(value).encode()
        fd = os.open(target.name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            os.unlink(target.name, dir_fd=directory)
            raise
    finally:
        os.close(directory)
    return value["counts"]
