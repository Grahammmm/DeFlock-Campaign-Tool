#!/usr/bin/env python3
"""Itemize the documents the last extract run could not extract (G D4-A, 2026-10-20).

Read-only test tooling for ``scripts/run_t11i_test_harness.py``. The harness declares
the exact extraction gaps of a baseline (``--expect-extract-failures``); G asked that
each one is recorded well enough to re-acquire it from the original mailbox or export
(follow-up T4C), without any record content.

Input: the newest private per-document list ``processing-v1/runs/extract-*.json``
(``native_extract.write_private_list``) and the ledger, both opened read-only. For
every document whose outcome is an extraction failure (``native_extract.FAILED``) it
prints:

* ``original_sha256``, ``code``, ``form`` and the document's occurrence kinds;
* for its mail message (itself, or the mail an attachment came from):
  ``mail_message_id`` (the ledger's own hashed identity), ``uidvalidity``/``uid``,
  ``folder_sha256`` and ``message_id_sha256`` (SHA-256 of the folder name and of the
  Message-ID header, never the values), the export ``receipt_sha256``, the ``Date``
  header as a UTC calendar date, and the ``From`` domain as a fixed class
  (``SENDER_CLASSES``), never the address or the domain.

No subjects, names, addresses, domains, paths or text. Exactly one JSON line; exit 0
when itemized, 1 when refused.
"""
import argparse
from datetime import timezone
from email.utils import parseaddr, parsedate_to_datetime
import glob
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

FAILED = ("CORRUPT_DOCUMENT", "EXTRACT_INCOMPLETE", "EXTRACT_TIMEOUT", "EXTRACT_NOT_ACCEPTED", "EXTRACT_ERROR")
SENDER_CLASSES = ("government", "records_portal", "flock_vendor", "webmail", "organization", "commercial",
                  "other", "unparseable", "absent")
PORTAL_DOMAINS = ("nextrequest.com", "govqa.us", "mycusthelp.com", "mycusthelp.net", "justfoia.com",
                  "civicplus.com", "granicus.com", "coplogic.com")
WEBMAIL_DOMAINS = ("gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com",
                   "msn.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "pm.me",
                   "gmx.com", "fastmail.com")


class Refused(Exception):
    pass


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _within(domain, suffixes):
    return any(domain == s or domain.endswith("." + s) for s in suffixes)


def sender_class(from_value):
    """A fixed class for the From domain; the address and the domain are never returned."""
    if from_value is None:
        return "absent"
    address = parseaddr(from_value)[1]
    if "@" not in address:
        return "unparseable"
    domain = address.rsplit("@", 1)[1].strip().strip(".").lower()
    if not domain or "." not in domain:
        return "unparseable"
    if _within(domain, PORTAL_DOMAINS):
        return "records_portal"
    if _within(domain, ("flocksafety.com",)):
        return "flock_vendor"
    if _within(domain, WEBMAIL_DOMAINS):
        return "webmail"
    if _within(domain, ("gov", "mil", "us")) or ".gov." in domain + ".":
        return "government"
    if _within(domain, ("org", "edu", "net")):
        return "organization"
    if _within(domain, ("com", "io", "co")):
        return "commercial"
    return "other"


def utc_date(date_value):
    if date_value is None:
        return None
    try:
        parsed = parsedate_to_datetime(date_value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).date().isoformat()


def header(fields, name):
    """The single value of header ``name`` (case-insensitive), None when absent or repeated."""
    values = [pair[1] for pair in fields if isinstance(pair, (list, tuple)) and len(pair) == 2
              and isinstance(pair[0], str) and pair[0].lower() == name]
    return values[0] if len(values) == 1 and isinstance(values[0], str) else None


def mail_of(con, sha):
    """The mail_messages row of ``sha``'s message: itself, or the mail its attachment came from."""
    row = con.execute("SELECT * FROM mail_messages WHERE eml_sha256=? ORDER BY id LIMIT 1", (sha,)).fetchone()
    if row is not None:
        return row, "self"
    for (parent,) in con.execute("SELECT parent_occurrence_id FROM occurrences WHERE original_sha256=? "
                                 "AND parent_occurrence_id IS NOT NULL ORDER BY id", (sha,)).fetchall():
        seen = set()
        while parent and parent not in seen:
            seen.add(parent)
            occ = con.execute("SELECT kind, original_sha256, parent_occurrence_id FROM occurrences WHERE id=?",
                              (parent,)).fetchone()
            if occ is None:
                break
            if occ["kind"] == "mail":
                row = con.execute("SELECT * FROM mail_messages WHERE eml_sha256=? ORDER BY id LIMIT 1",
                                  (occ["original_sha256"],)).fetchone()
                if row is not None:
                    return row, "parent_mail"
            parent = occ["parent_occurrence_id"]
    return None, None


def receipt_sha256(con, sha):
    values = set()
    for (evidence,) in con.execute("SELECT evidence FROM occurrences WHERE original_sha256=? "
                                   "AND acquisition_method='verified_mail_export'", (sha,)):
        try:
            value = json.loads(evidence).get("receipt_sha256")
        except (ValueError, AttributeError):
            continue
        if isinstance(value, str):
            values.add(value)
    return sorted(values)[0] if len(values) == 1 else None


def itemize(project):
    project = Path(os.path.abspath(project))
    ledger = project / "processing-v1" / "ledger.sqlite"
    lists = sorted(glob.glob(str(project / "processing-v1" / "runs" / "extract-*.json")),
                   key=lambda p: (os.path.getmtime(p), p))
    if not ledger.is_file():
        raise Refused("ledger_missing")
    if not lists:
        raise Refused("extract_list_missing")
    newest = lists[-1]
    with open(newest, encoding="utf-8") as stream:
        body = json.load(stream)
    documents = body.get("documents") if isinstance(body, dict) else None
    if not isinstance(documents, list):
        raise Refused("extract_list_invalid")
    con = sqlite3.connect(f"file:{ledger}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    try:
        items = []
        for doc in documents:
            if not isinstance(doc, dict) or doc.get("outcome") not in FAILED:
                continue
            sha = doc.get("original_sha256")
            kinds = sorted({r[0] for r in con.execute("SELECT kind FROM occurrences WHERE original_sha256=?",
                                                      (sha,))})
            item = {"original_sha256": sha, "code": doc["outcome"], "form": doc.get("form"),
                    "occurrence_kinds": kinds, "mail": None}
            row, via = mail_of(con, sha)
            if row is not None:
                try:
                    fields = json.loads(row["headers"]).get("fields") or []
                except (ValueError, AttributeError):
                    fields = []
                item["mail"] = {
                    "via": via, "mail_message_id": row["id"],
                    "uidvalidity": row["uidvalidity"], "uid": row["uid"],
                    "folder_sha256": sha256_text(row["folder"]),
                    "message_id_sha256": sha256_text(row["message_id"]) if row["message_id"] else None,
                    "receipt_sha256": receipt_sha256(con, row["eml_sha256"]),
                    "date_utc": utc_date(header(fields, "date")),
                    "received_at_date": (row["received_at"] or "")[:10] or None,
                    "sender_domain_class": sender_class(header(fields, "from"))}
            items.append(item)
    finally:
        con.close()
    items.sort(key=lambda item: (item["code"], item["original_sha256"] or ""))
    by_code = {}
    for item in items:
        by_code[item["code"]] = by_code.get(item["code"], 0) + 1
    return {"status": "ok", "extract_list_sha256": sha256_file(newest), "documents_listed": len(documents),
            "by_code": by_code, "documents": items}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--project", required=True, help="records project (only read)")
    args = p.parse_args(argv)
    try:
        line, code = itemize(args.project), 0
    except Refused as refusal:
        line, code = {"status": "refused", "code": str(refusal)}, 1
    except (OSError, sqlite3.Error, ValueError, KeyError):
        line, code = {"status": "refused", "code": "manifest_error"}, 1
    print(json.dumps(line, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
