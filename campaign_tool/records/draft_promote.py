"""Local approved review copy -> proposed packet only. No publication or source acquisition."""
import argparse
from contextlib import contextmanager
from datetime import date
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import time

from .. import privacy_patterns
from . import approval_queue as aq, analysis_packets, draft_templates, public_packets as pp, redaction_record
from .run_stages import LedgerBusy

SCHEMA = "records-draft-reviewed-sources-v1"
PLAN = "records-draft-promote-plan-v1"
PROVENANCE = "records-draft-promotion-provenance-v1"
RECEIPT_SCHEMA = "records-draft-promotion-receipt-v1"
RECEIPTS, LOCK = "draft-promotions.jsonl", "draft-promotions.lock"
MAX_INPUT, MAX_RECEIPTS = 1 << 20, 16 << 20
FIELDS = {"schema", "project_id", "queue_id", "queue_revision", "body_sha256", "evidence_sha256", "title", "sources", "correction"}
RECEIPT_FIELDS = {"schema", "source_project_id", "queue_id", "queue_revision", "body_sha256", "evidence_sha256",
                  "provenance_sha256", "reviewed_sources_sha256", "packet_id", "packet_revision", "packet_digest", "timestamp"}


class DraftPromoteError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _need(ok, code):
    if not ok:
        raise DraftPromoteError(code)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _digest(value):
    return _sha(aq._canon(value).encode("utf-8"))


def _hash(value):
    return isinstance(value, str) and pp.SHA.fullmatch(value) is not None


def _manifest(value):
    _need(isinstance(value, dict) and set(value) == FIELDS and value["schema"] == SCHEMA, "manifest_invalid")
    _need(isinstance(value["project_id"], str) and pp.PACKET_ID.fullmatch(value["project_id"])
          and _hash(value["queue_id"]) and type(value["queue_revision"]) is int and value["queue_revision"] >= 0
          and _hash(value["body_sha256"]) and _hash(value["evidence_sha256"]), "manifest_invalid")
    title = value["title"]
    _need(isinstance(title, str) and 0 < len(title) <= pp.MAX_TITLE and "\n" not in title, "manifest_invalid")
    sources = value["sources"]
    _need(isinstance(sources, list) and 1 <= len(sources) <= 64, "manifest_invalid")
    for source in sources:
        _need(isinstance(source, dict) and set(source) == {"citation_index", "original_sha256", "page", "redaction_record_id"},
              "manifest_invalid")
        _need(type(source["citation_index"]) is int and source["citation_index"] >= 0
              and type(source["page"]) is int and source["page"] > 0
              and _hash(source["original_sha256"]) and _hash(source["redaction_record_id"]), "manifest_invalid")
    # Canonical semantic identity: formatting and source-entry order do not create another promotion.
    value = dict(value, sources=sorted(sources, key=lambda s: s["citation_index"]))
    _need(len(aq._canon(value).encode("utf-8")) <= MAX_INPUT, "manifest_invalid")
    return value


def read_reviewed_sources(path):
    parent_fd = leaf_fd = None
    try:
        parent, name = os.path.split(os.path.abspath(path))
        parent_fd = aq._open_chain(parent)
        leaf_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        info = os.fstat(leaf_fd)
        _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= MAX_INPUT, "manifest_invalid")
        raw = aq._read_capped(leaf_fd, MAX_INPUT)
        _need(len(raw) <= MAX_INPUT, "manifest_invalid")
        return _manifest(aq._strict_load(raw, "manifest_invalid"))
    except (OSError, aq.ApprovalQueueError, ValueError, UnicodeError, TypeError, RecursionError):
        raise DraftPromoteError("manifest_invalid") from None
    finally:
        for fd in (leaf_fd, parent_fd):
            if fd is not None:
                os.close(fd)


def _sources(project, row, manifest):
    evidence = row["evidence"]
    citations = evidence.get("citations")
    _need(isinstance(citations, list) and 1 <= len(citations) <= 64, "source_unavailable")
    sources = manifest["sources"]
    _need([s["citation_index"] for s in sources] == list(range(len(citations))), "source_mapping_invalid")
    documents, refs, aliases = {}, [], {}
    for citation, source in zip(citations, sources):
        _need(not isinstance(citation, dict) or citation.get("source_type") != "finding", "review_gate_unavailable")
        _need(isinstance(citation, dict) and citation.get("source_type") == "original"
              and citation.get("verification") == "verified", "source_unavailable")
        # Exact authoritative grammar: outcomes/received_items.py::_resolve_evidence.
        source_id, locator = citation.get("source_id"), citation.get("locator")
        sha = source_id[7:] if isinstance(source_id, str) and source_id.startswith("sha256:") else source_id
        page_match = re.fullmatch(r"page ([1-9][0-9]*)", locator) if isinstance(locator, str) else None
        _need(_hash(sha) and page_match is not None, "unsupported_locator")
        page = int(page_match[1])
        _need(sha == source["original_sha256"] and page == source["page"], "source_binding_invalid")
        _need(sha[:12] not in aliases or aliases[sha[:12]] == sha, "source_binding_invalid")
        aliases[sha[:12]] = sha
        pp._check_original(project, sha)
        item = pp._received_item(project, sha)
        request = "" if row["request_id"] == "agency-wide" else row["request_id"]
        _need(item is not None and item.get("extraction_status") == "current"
              and item.get("agency_id") == row["agency_id"] and (item.get("request_id") or "") == request,
              "source_attribution_unavailable")
        received = item.get("received_at")
        _need(isinstance(received, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", received[:10]), "date_unavailable")
        try:
            date.fromisoformat(received[:10])
        except ValueError:
            raise DraftPromoteError("date_unavailable") from None
        count = analysis_packets._count(project, sha, {})
        _need(count is not None and page <= count, "page_unavailable")
        _, derivative = redaction_record.verified_derivative(project, row["agency_id"], sha, source["redaction_record_id"])
        try:
            _need(not privacy_patterns.categories(derivative.decode("utf-8")), "privacy_lint")
        except UnicodeError:
            raise DraftPromoteError("derivative_not_text") from None
        doc = documents.setdefault(sha, {"sha256": sha, "date_received": received[:10], "source_request": request,
                                         "redaction_ids": [], "use_restriction": False})
        doc["redaction_ids"].append(source["redaction_record_id"])
        refs.append({"ref": f"record {sha[:12]} page {page}", "verified": True})
    return list(documents.values()), refs


def _packet_events(project):
    return pp._reading(project, True, lambda lock: lock.events(), list)


def _correction(project, row, manifest):
    value = manifest["correction"]
    if row["kind"] == "draft-agency-summary":
        _need(value is None, "correction_linkage_unavailable")
        return None
    code = "correction_linkage_unavailable"
    signed = row["evidence"].get("correction")
    _need(isinstance(value, dict) and set(value) == {
        "packet_id", "packet_revision", "text_sha256", "prior_statement", "prior_version", "linkage"}, code)
    ident, revision = value["packet_id"], value["packet_revision"]
    _need(isinstance(ident, str) and pp.PACKET_ID.fullmatch(ident) and type(revision) is int and revision >= 0
          and _hash(value["text_sha256"]) and isinstance(signed, dict)
          and set(signed) == {"prior_statement", "prior_version", "linkage"}, code)
    _need(all(isinstance(signed[k], str) and bool(signed[k]) and value[k] == signed[k] for k in signed)
          and signed["prior_version"] == f"packet:{ident}@{revision}"
          and signed["linkage"] == f"correction-of:{ident}", code)
    try:
        rows = [r for r in pp._written_revisions(_packet_events(project)) if r["id"] == ident and r["revision"] == revision]
        _need(len(rows) == 1, code)
        old = rows[0]
        pp._written_checked(old)
        packet = old["packet"]
        _need(_sha(packet["text"].encode("utf-8")) == value["text_sha256"]
              and signed["prior_statement"] in packet["text"] and packet["agency_key"] == row["agency_id"]
              and packet["request_id"] == ("" if row["request_id"] == "agency-wide" else row["request_id"])
              and packet.get("draft_provenance", {}).get("target") == row["evidence"]["target"], code)
    except (pp.PacketError, KeyError, TypeError, ValueError):
        raise DraftPromoteError(code) from None
    return value


def _prepare(project, ident, manifest, rows):
    row = next((r for r in rows if r["id"] == ident), None)
    _need(row is not None and row["kind"] in draft_templates.REVIEW_COPY_KINDS, "queue_unavailable")
    _need(row["status"] == "approved" and not row.get("approval_unverified")
          and isinstance(row.get("approval_evidence"), dict), "queue_not_verified")
    proof = row["approval_evidence"]
    _need(proof["project"] == manifest["project_id"] == aq._project_id(project)
          and row["id"] == manifest["queue_id"] and row["revision"] == manifest["queue_revision"]
          and _sha(row["body"].encode("utf-8")) == manifest["body_sha256"]
          and _digest(row["evidence"]) == manifest["evidence_sha256"], "stale_binding")
    docs, refs = _sources(project, row, manifest)
    correction = _correction(project, row, manifest)
    evidence = row["evidence"]
    prov = {"schema": PROVENANCE, "source_project_id": proof["project"], "queue_id": ident,
            "queue_revision": row["revision"], "body_sha256": manifest["body_sha256"],
            "evidence_sha256": manifest["evidence_sha256"], "approval_signature_sha256": proof["signature_sha256"],
            "reviewed_sources_sha256": _digest(manifest), "correction": correction,
            **{k: evidence[k] for k in ("target", "citations", "source_attachment_sha256", "source_section", "source_run_id")}}
    candidate = pp.clean({"kind": "summary" if row["kind"] == "draft-agency-summary" else "site_update",
                          "agency_key": row["agency_id"], "request_id": "" if row["request_id"] == "agency-wide" else row["request_id"],
                          "source": "queue-" + ident, "title": manifest["title"], "text": row["body"],
                          "documents": docs, "citations": refs, "draft_provenance": prov})
    _need(candidate["text"].encode("utf-8") == row["body"].encode("utf-8"), "text_mismatch")
    return candidate


def prepare(project, id, reviewed_sources):
    return _prepare(os.path.abspath(project), id, _manifest(reviewed_sources), aq.load_queue(project))


@contextmanager
def _promotion_store(project, apply):
    root = aq._open_chain(project)
    lock = None
    try:
        _need(aq._private(os.fstat(root), True), "project_invalid")
        lock = aq._open_file(root, LOCK, os.O_RDWR if apply else os.O_RDONLY)
        if lock is None and apply:
            try:
                lock = aq._open_file(root, LOCK, os.O_RDWR, create=True)
            except aq.ApprovalQueueError:
                lock = aq._open_file(root, LOCK, os.O_RDWR)  # another promoter created it
            _need(lock is not None, "promotion_busy")
        if lock is not None:
            _need(stat.S_IMODE(os.fstat(lock).st_mode) == 0o600, "receipt_invalid")
            try:
                fcntl.flock(lock, (fcntl.LOCK_EX if apply else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DraftPromoteError("promotion_busy") from None
        else:
            _need(not os.path.lexists(os.path.join(project, RECEIPTS)), "receipt_invalid")
        yield root
    finally:
        if lock is not None:
            os.close(lock)
        os.close(root)


def _receipts(root):
    fd = aq._open_file(root, RECEIPTS, os.O_RDONLY)
    if fd is None:
        return b"", []
    try:
        _need(stat.S_IMODE(os.fstat(fd).st_mode) == 0o600, "receipt_invalid")
        raw = aq._read_capped(fd, MAX_RECEIPTS)
    finally:
        os.close(fd)
    _need(len(raw) <= MAX_RECEIPTS and (not raw or raw.endswith(b"\n")), "receipt_invalid")
    rows = []
    for line in raw.splitlines():
        value = aq._strict_load(line, "receipt_invalid")
        _need(isinstance(value, dict) and set(value) == RECEIPT_FIELDS and value["schema"] == RECEIPT_SCHEMA, "receipt_invalid")
        _need(all(_hash(value[k]) for k in ("queue_id", "body_sha256", "evidence_sha256", "provenance_sha256",
                                          "reviewed_sources_sha256", "packet_digest"))
              and isinstance(value["source_project_id"], str) and pp.PACKET_ID.fullmatch(value["source_project_id"])
              and isinstance(value["packet_id"], str) and pp.PACKET_ID.fullmatch(value["packet_id"])
              and type(value["queue_revision"]) is int and value["queue_revision"] >= 0
              and type(value["packet_revision"]) is int and value["packet_revision"] >= 0
              and type(value["timestamp"]) is int, "receipt_invalid")
        rows.append(value)
    _need(len({(r["source_project_id"], r["queue_id"]) for r in rows}) == len(rows), "receipt_invalid")
    return raw, rows


def _receipt(root, data):
    fd = aq._open_file(root, RECEIPTS, os.O_WRONLY | os.O_APPEND)
    if fd is None:
        fd = aq._open_file(root, RECEIPTS, os.O_WRONLY | os.O_APPEND, create=True)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _existing(project, candidate, now):
    ident = pp.packet_id(candidate)
    row = pp.fold(_packet_events(project)).get(ident)
    if row is not None:
        _need(row["revision"] == 0 and row["status"] == "proposed" and not pp._expired(row, now)
              and row["packet"] == candidate and row["digest"] == _sha(pp._canon(candidate).encode()), "packet_conflict")
    return row


def promote(project, id, reviewed_sources, *, apply=False):
    project, now = os.path.abspath(project), time.time()
    try:
        manifest = _manifest(reviewed_sources)
        # Gate failures do not even create the private serialization lock.
        candidate = prepare(project, id, manifest)
        with aq._Store(project, False) as queue, _promotion_store(project, apply) as root:
            candidate = _prepare(project, id, manifest, list(aq._fold(project, queue.raw()).values()))
            raw, receipts = _receipts(root)
            prov = candidate["draft_provenance"]
            receipt = {"schema": RECEIPT_SCHEMA, "source_project_id": prov["source_project_id"], "queue_id": id,
                       "queue_revision": prov["queue_revision"], "body_sha256": prov["body_sha256"],
                       "evidence_sha256": prov["evidence_sha256"], "provenance_sha256": _digest(prov),
                       "reviewed_sources_sha256": prov["reviewed_sources_sha256"], "packet_id": pp.packet_id(candidate),
                       "packet_revision": 0, "packet_digest": _sha(pp._canon(candidate).encode()), "timestamp": int(now)}
            prior = [r for r in receipts if r["queue_id"] == id]
            _need(not prior or dict(prior[0], timestamp=receipt["timestamp"]) == receipt, "promotion_conflict")
            existing = _existing(project, candidate, now)
            _need(not prior or existing is not None, "promotion_conflict")
            data = (aq._canon(receipt) + "\n").encode("utf-8")
            _need(prior or len(raw) + len(data) <= MAX_RECEIPTS, "receipt_full")
            if apply and raw:
                fd = aq._open_file(root, RECEIPTS, os.O_WRONLY | os.O_APPEND)
                _need(fd is not None, "receipt_invalid")
                os.close(fd)  # preflight existing receipt writability before proposing
            action = "existing" if prior else "would_propose"
            if apply:
                # Fresh source hashes and signatures just before the atomic create-only proposal.
                checked = _prepare(project, id, manifest, list(aq._fold(project, queue.raw()).values()))
                _need(checked == candidate, "stale_binding")
                result = pp.propose(project, candidate, create_only=True)
                _need(result["id"] == receipt["packet_id"] and result["revision"] == 0, "packet_conflict")
                # Read back under the packet lock; a concurrent ordinary decision fails closed.
                _need(_existing(project, candidate, time.time()) is not None, "packet_conflict")
                if not prior:
                    try:
                        _receipt(root, data)
                    except (OSError, aq.ApprovalQueueError):
                        raise DraftPromoteError("receipt_pending") from None
                    action = "existing" if existing is not None else "proposed"
            return {"schema": PLAN, "action": action, "queue_id": id, "queue_revision": prov["queue_revision"],
                    "packet_id": receipt["packet_id"], "packet_revision": 0, "revision_is_prediction": not apply}
    except DraftPromoteError:
        raise
    except LedgerBusy:
        raise DraftPromoteError("ledger_unavailable") from None
    except (aq.ApprovalQueueError, pp.PacketError, redaction_record.RedactionError) as error:
        raise DraftPromoteError(error.code) from None
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise DraftPromoteError("gate_unavailable") from None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records draft-promote", description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--reviewed-sources", required=True)
    parser.add_argument("--apply", action="store_true", help="propose only; separate packet signature still required")
    args = parser.parse_args(argv)
    try:
        result = promote(args.project, args.id, read_reviewed_sources(args.reviewed_sources), apply=args.apply)
    except DraftPromoteError as error:
        print(json.dumps({"schema": PLAN, "error": error.code}, sort_keys=True), file=sys.stderr)
        return 75 if error.code in ("promotion_busy", "journal_busy", "queue_busy") else 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
