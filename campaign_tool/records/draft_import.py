"""Import one operator-checked correspondence draft; dry-run by default, never send."""
import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import date
from email.headerregistry import Address

from . import approval_queue as aq, draft_templates

SCHEMA = "records-console-draft-input-v1"
KIND = "correspondence-draft"
MAX_INPUT = 128 * 1024
FIELDS = {"schema", "draft_key", "kind", "agency_id", "request_id", "body", "why", "source_run_id",
          "source_attachment_sha256", "source_section", "citations", "envelope"}
SHA = re.compile(r"[a-f0-9]{64}")
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")
ADDRESS = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")


def _need(condition, code):
    if not condition:
        raise aq.ApprovalQueueError(code)


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _bounded_text(value, limit):
    if not _text(value):
        return False
    try:
        return len(value.encode("utf-8")) <= limit
    except UnicodeEncodeError:
        return False


def _validate_citations(citations):
    _need(isinstance(citations, list) and len(citations) <= 64, "input_citations")
    required = {"source_type", "source_id", "locator", "as_of", "verification"}
    optional = {"original_sha256", "page", "limits"}
    for citation in citations:
        _need(isinstance(citation, dict) and required <= set(citation)
              and set(citation) <= required | optional, "input_citations")
        _need(citation["source_type"] in ("original", "tracker", "timeline", "public", "missing-original")
              and citation["verification"] in ("verified", "unverified", "missing-original")
              and all(_bounded_text(citation[k], 1024) for k in ("source_id", "locator")), "input_citations")
        as_of = citation["as_of"]
        if as_of is not None:
            _need(isinstance(as_of, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", as_of),
                  "input_citations")
            try:
                date.fromisoformat(as_of)
            except ValueError:
                raise aq.ApprovalQueueError("input_citations") from None
        if "original_sha256" in citation:
            _need(isinstance(citation["original_sha256"], str)
                  and SHA.fullmatch(citation["original_sha256"]), "input_citations")
        if "page" in citation:
            _need(type(citation["page"]) is int and citation["page"] > 0, "input_citations")
        if "limits" in citation:
            _need(citation["limits"] == "" or _bounded_text(citation["limits"], 4000), "input_citations")
        if citation["source_type"] == "original":
            _need("original_sha256" in citation, "input_citations")
        if citation["source_type"] == "missing-original":
            _need(citation["verification"] != "verified" and not {"original_sha256", "page"} & set(citation),
                  "input_citations")


def validate_envelope(envelope):
    """One literal address and subject, with an explicit thread-or-new-recipient basis."""
    _need(isinstance(envelope, dict) and set(envelope) == {"to", "subject", "thread_item_id", "recipient_basis"},
          "envelope_invalid")
    _need(_text(envelope["to"]) and _bounded_text(envelope["subject"], 998), "envelope_invalid")
    basis = envelope["recipient_basis"]
    _need(isinstance(basis, dict) and set(basis) == {"source_type", "source_id", "locator", "checked"},
          "thread_provenance_invalid")
    _need(basis["source_type"] in ("custodian", "source-header") and basis["checked"] is True
          and all(_bounded_text(basis[k], 1024) for k in ("source_id", "locator")), "thread_provenance_invalid")
    _need(ADDRESS.fullmatch(envelope["to"]) is not None, "recipient_invalid")
    try:
        address = Address(addr_spec=envelope["to"])
    except ValueError:
        raise aq.ApprovalQueueError("recipient_invalid") from None
    _need(address.addr_spec == envelope["to"], "recipient_invalid")
    ident = envelope["thread_item_id"]
    if ident is None:
        _need(basis["source_type"] == "custodian", "thread_provenance_invalid")
    else:
        _need(isinstance(ident, str) and SHA.fullmatch(ident) is not None, "thread_provenance_invalid")
        if basis["source_type"] == "source-header":
            _need(basis["source_id"] == ident and basis["locator"] in ("Reply-To", "From"),
                  "thread_provenance_invalid")
    return envelope


def identity(doc):
    """Stable section + request + operator draft key, never the body hash."""
    return hashlib.sha256(aq._canon([doc["source_section"], doc["request_id"], doc["draft_key"]]).encode()).hexdigest()


def validate(doc):
    """Validate one v1 instance and return add_draft arguments; no filesystem or network."""
    _need(isinstance(doc, dict) and set(doc) == FIELDS, "input_fields")
    _need(doc["schema"] == SCHEMA and doc["kind"] == KIND, "input_schema")
    for key in ("draft_key", "agency_id", "request_id", "source_run_id", "source_section"):
        _need(isinstance(doc[key], str) and ID.fullmatch(doc[key]) is not None, "input_" + key)
    _need(isinstance(doc["source_attachment_sha256"], str)
          and SHA.fullmatch(doc["source_attachment_sha256"]) is not None, "input_sha256")
    _need(_text(doc["why"]), "input_why")
    body = doc["body"]
    _need(isinstance(body, str) and bool(body.strip()) and "\x00" not in body, "input_body")
    try:
        data = body.encode("utf-8")
        aq._canon(doc).encode("utf-8")
    except (UnicodeEncodeError, ValueError, TypeError, RecursionError):
        raise aq.ApprovalQueueError("input_utf8") from None
    _need(len(data) <= draft_templates.MAX_BODY_BYTES, "body_bound")
    _validate_citations(doc["citations"])
    validate_envelope(doc["envelope"])
    # Bind original body and run too: the queue compares evidence, not body, on re-import.
    evidence = {k: doc[k] for k in ("schema", "draft_key", "why", "source_run_id", "source_attachment_sha256",
                                   "source_section", "citations", "envelope")}
    evidence.update(source="terra-import", body_sha256=hashlib.sha256(data).hexdigest())
    draft = {k: doc[k] for k in ("kind", "agency_id", "request_id", "body", "source_run_id")}
    draft.update(item_id=identity(doc), evidence=evidence)
    event = aq._event("draft", aq.draft_id(draft["item_id"], KIND), 0, aq.PIPELINE_ACTOR, snapshot=draft)
    _need(len(aq._canon(event).encode("utf-8")) <= aq.MAX_EVENT, "input_bound")
    return draft


def read_input(path):
    """Read one capped single-link regular leaf beneath no-follow directory descriptors."""
    parent_fd = leaf_fd = None
    try:
        parent, name = os.path.split(os.path.abspath(path))
        parent_fd = aq._open_chain(parent)
        leaf_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        info = os.fstat(leaf_fd)
        _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= MAX_INPUT,
              "source_invalid")
        raw = aq._read_capped(leaf_fd, MAX_INPUT)
        _need(len(raw) <= MAX_INPUT, "source_invalid")
        return raw
    except (OSError, aq.ApprovalQueueError):
        raise aq.ApprovalQueueError("source_invalid") from None
    finally:
        if leaf_fd is not None:
            os.close(leaf_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def import_draft(project, path, *, apply=False):
    """Read a bounded no-follow regular file. Apply only adds a pending queue row."""
    from . import mail_drafts
    doc = aq._strict_load(read_input(path), "input_json")
    draft = validate(doc)
    try:
        mail_drafts.correspondence_envelope(project, draft)  # read-only proof, also on dry-run
    except mail_drafts.MailDraftError as error:
        raise aq.ApprovalQueueError(error.code) from None
    ident = aq.draft_id(draft["item_id"], KIND)
    existing = next((r for r in aq.load_queue(project) if r["id"] == ident), None)
    if existing is not None:
        _need(existing["agency_id"] == draft["agency_id"] and existing["request_id"] == draft["request_id"]
              and aq._canon(existing["evidence"]) == aq._canon(draft["evidence"]), "source_conflict")
    row = aq.add_draft(project, **draft) if apply else existing
    return {"id": ident, "action": "existing" if existing else "queued" if apply else "would_queue",
            "status": row["status"] if row else "pending", "applied": apply}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records draft-import", description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--apply", action="store_true", help="queue only; never approve, place or send")
    args = parser.parse_args(argv)
    try:
        result = import_draft(args.project, args.file, apply=args.apply)
    except aq.ApprovalQueueError as error:
        print("draft-import: " + error.code, file=sys.stderr)
        return 3 if error.code == "source_conflict" else 2
    except (OSError, ValueError):
        print("draft-import: input_invalid", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
