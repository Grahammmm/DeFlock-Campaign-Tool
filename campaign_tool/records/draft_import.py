"""Import one operator-checked correspondence draft; dry-run by default, never send."""
import argparse
import hashlib
import json
import re
import sys
from email.headerregistry import Address

from . import approval_queue as aq, draft_templates

SCHEMA = "records-console-draft-input-v1"
KIND = "correspondence-draft"
MAX_INPUT = draft_templates.MAX_BODY_BYTES * 2 + 64 * 1024
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


def validate_envelope(envelope):
    """One literal address and subject, with an explicit thread-or-new-recipient basis."""
    _need(isinstance(envelope, dict) and set(envelope) == {"to", "subject", "thread_item_id", "recipient_basis"},
          "envelope_invalid")
    _need(all(_text(envelope[k]) for k in ("to", "subject", "recipient_basis")), "envelope_invalid")
    _need(ADDRESS.fullmatch(envelope["to"]) is not None, "recipient_invalid")
    try:
        address = Address(addr_spec=envelope["to"])
    except ValueError:
        raise aq.ApprovalQueueError("recipient_invalid") from None
    _need(address.addr_spec == envelope["to"], "recipient_invalid")
    ident = envelope["thread_item_id"]
    if ident is None:
        _need(envelope["recipient_basis"] in ("custodian-fact", "source-header"), "thread_provenance_invalid")
    else:
        _need(isinstance(ident, str) and SHA.fullmatch(ident) is not None
              and envelope["recipient_basis"] == "thread", "thread_provenance_invalid")
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
    _need(isinstance(body, str) and bool(body.strip()), "input_body")
    try:
        data = body.encode("utf-8")
        aq._canon(doc).encode("utf-8")
    except (UnicodeEncodeError, ValueError, TypeError, RecursionError):
        raise aq.ApprovalQueueError("input_utf8") from None
    _need(len(data) <= draft_templates.MAX_BODY_BYTES, "body_bound")
    citations = doc["citations"]
    _need(isinstance(citations, list) and bool(citations), "input_citations")
    for citation in citations:
        _need(isinstance(citation, dict) and set(citation) == {"page", "quote"}
              and type(citation["page"]) is int and citation["page"] >= 1
              and isinstance(citation["quote"], str) and bool(citation["quote"].strip()), "input_citations")
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


def import_draft(project, path, *, apply=False):
    """Read a bounded no-follow regular file. Apply only adds a pending queue row."""
    from . import mail_drafts
    doc = aq._strict_load(aq._read_input(path, MAX_INPUT), "input_json")
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
