#!/usr/bin/env python3
"""Inject one T12 harness fault into a SANDBOX records project (T12s).

This is test tooling for ``scripts/run_t11i_test_harness.py``. It never runs
against TEST or live. It runs inside the Flock container (``docker exec`` as the
ledger owner, engine on ``PYTHONPATH``) or locally in synthetic tests. The
fault goes in through the engine's own adapters, and the records run that
follows has to detect it through the real ``run_stages`` adapters. Nothing here
writes a stage result or a run report.

Faults:

* ``portal-404``: one synthetic document link on an allowlisted portal host
  goes through the real portal intake (``intake_notices``). The host is the
  first portal host of the read-only allowlist, and the allowlist is never
  edited. Then the engine's own fetch attempt state machine
  (``portal.engine._attempt_item``) runs on that one item. Its transport never
  opens a socket and always answers HTTP 404, and the egress check allows only
  the synthetic URL. The item ends ``blocked`` with HTTP 404 evidence, and the
  next run's portal stage has to report it.
* ``corrupt-pdf``: one synthetic e-mail with a damaged PDF attachment goes
  through the engine's intake (``Pipeline.ingest_inbox``) under the
  records-root lock. The next run's extract stage has to code it
  ``CORRUPT_DOCUMENT``.

Guard: the project must hold the sandbox marker ``SANDBOX_MARKER``, owned by
the caller and private (0600). Only the harness (on its fresh sandbox copy) or
a synthetic test writes that marker.

Prints exactly one JSON line ``{"fault", "status", "code", ...counts}``. It
holds no paths, URLs, hosts or record text. Exit 0 when the fault was
injected, 1 when it was refused.
"""
import argparse
from email.message import EmailMessage
from email.policy import SMTP
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time

SANDBOX_MARKER = ".t12-sandbox-v1"
FAULT_REQUEST_ID = "t12-fault-portal-404"
FAULT_ITEM_ID = "t12-synthetic-404"
NOTICE_SOURCE = hashlib.sha256(b"t12 synthetic portal_404 notice").hexdigest()


def corrupt_pdf_bytes():
    """A PDF header followed by damaged bytes: no objects, no xref, no trailer."""
    return b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + bytes(range(256)) * 4 + b"\n(t12 truncated transfer)"


class Refused(Exception):
    pass


def check_sandbox(project):
    project = Path(os.path.abspath(project))
    marker = project / SANDBOX_MARKER
    try:
        info = os.lstat(marker)
    except OSError:
        raise Refused("sandbox_marker_missing") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise Refused("sandbox_marker_unsafe")
    for name in ("mail-history", "processing-v1", "portal-queue-v1"):
        if not (project / name).is_dir():
            raise Refused("sandbox_layout_invalid")
    return project


class NotFoundTransport:
    """No network: every open answers HTTP 404 without a body."""

    def __init__(self):
        self.calls = 0

    def open(self, url, *, timeout):
        from campaign_tool.records.portal.engine import Response
        self.calls += 1
        return Response(status=404, headers={"Content-Type": "text/html"}, chunks=())


def portal_404(project, approval_file):
    from campaign_tool.records.portal import engine, policy
    from campaign_tool.records.portal.intake import intake_notices
    from campaign_tool.records.portal.store import Queue

    loader = lambda: policy.load_approval(approval_file)  # read only, never edited
    try:
        approval = loader()
    except policy.PortalError:
        raise Refused("allowlist_unloadable") from None
    path = f"/documents/{FAULT_ITEM_ID}"
    hosts = [host for host in sorted(approval.portals)
             if any(path.startswith(prefix) for prefix in approval.scopes.get(host, ()))]
    if not hosts:
        raise Refused("allowlist_has_no_document_scope")
    url = f"https://{hosts[0]}{path}"
    queue = Queue(str(project / "portal-queue-v1"))
    try:
        totals = intake_notices(queue, notices=[{"body": f'<a href="{url}">synthetic</a>',
                                                 "request_id": FAULT_REQUEST_ID,
                                                 "source_sha256": NOTICE_SOURCE}],
                                approval_loader=loader)
        transport = NotFoundTransport()
        outcome = {"attempted": 0, "received": 0, "blocked": 0, "retry": 0}
        with queue.lock():
            item = queue.db.execute("SELECT * FROM portal_items WHERE host=? AND item_id=?",
                                    (hosts[0], FAULT_ITEM_ID)).fetchone()
            if item is None:
                raise Refused("synthetic_item_not_enrolled")
            if item["state"] in ("pending", "retry"):
                engine._attempt_item(queue, item, approval_loader=loader,
                                     egress=lambda candidate: candidate == url,
                                     transport=transport, limits=engine.Limits(), wall=time.time,
                                     mono=time.monotonic, fault=None, outcome=outcome)
            item = queue.db.execute("SELECT state,reason FROM portal_items WHERE id=?",
                                    (item["id"],)).fetchone()
            last = queue.db.execute("SELECT a.evidence_json FROM portal_attempts a JOIN portal_items i "
                                    "ON i.id=a.item WHERE i.host=? AND i.item_id=? ORDER BY a.id DESC LIMIT 1",
                                    (hosts[0], FAULT_ITEM_ID)).fetchone()
    finally:
        queue.close()
    statuses = [entry.get("http_status") for entry in json.loads(last[0])] if last else []
    if item["state"] != "blocked" or 404 not in statuses:
        raise Refused("synthetic_item_not_blocked")
    return {"enrolled": totals["enrolled"], "attempted": outcome["attempted"],
            "transport_calls": transport.calls, "item_state": item["state"],
            "item_reason": item["reason"], "http_status": 404}


def synthetic_corrupt_email():
    msg = EmailMessage()
    msg["From"] = "records@t12-fault.example.invalid"
    msg["To"] = "organizer@t12-fault.example.invalid"
    msg["Subject"] = "T12 synthetic corrupt PDF fault"
    msg["Message-ID"] = "<t12-corrupt-pdf@t12-fault.example.invalid>"
    msg["Date"] = "Fri, 02 Jan 2026 03:04:05 +0000"
    msg.set_content("Synthetic fault-injection message. The attached PDF is deliberately damaged.\n")
    msg.add_attachment(corrupt_pdf_bytes(), maintype="application", subtype="pdf",
                       filename="t12-corrupt.pdf")
    for index, part in enumerate(msg.walk()):
        if part.is_multipart():
            part.set_boundary(f"t12-corrupt-pdf-boundary-{index}")
    return msg.as_bytes(policy=SMTP)


def corrupt_pdf(project):
    from campaign_tool.records.run import Pipeline, RunError, root_lock

    pipeline = Pipeline(project / "processing-v1")
    inbox = Path(tempfile.mkdtemp(prefix=".t12-inbox-", dir=str(project)))
    eml = inbox / "t12-corrupt-pdf.eml"
    try:
        fd = os.open(eml, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(synthetic_corrupt_email())
        try:
            with root_lock(pipeline.root.path):
                report = pipeline.ingest_inbox(inbox)
        except RunError:
            raise Refused("root_locked") from None
    finally:
        eml.unlink(missing_ok=True)
        inbox.rmdir()
    documents = sum(len(entry.get("documents") or ()) for entry in report["preserved"] + report["replayed"])
    if report["failures"] or not documents:
        raise Refused("corrupt_pdf_not_preserved")
    return {"messages": report["messages"], "preserved": len(report["preserved"]),
            "replayed": len(report["replayed"]), "documents": documents}


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Inject one T12 harness fault into a sandbox records project.")
    parser.add_argument("fault", choices=("portal-404", "corrupt-pdf"))
    parser.add_argument("--project", required=True, help="sandbox records project (holds the marker)")
    parser.add_argument("--approval-file", help="portal-404: root-owned allowlist (read only)")
    args = parser.parse_args(argv)
    name = args.fault.replace("-", "_")
    try:
        project = check_sandbox(args.project)
        if args.fault == "portal-404":
            if not args.approval_file:
                raise Refused("allowlist_missing")
            detail = portal_404(project, args.approval_file)
        else:
            detail = corrupt_pdf(project)
    except Refused as error:
        print(json.dumps({"fault": name, "status": "refused", "code": str(error)}, sort_keys=True))
        return 1
    except Exception:  # noqa: BLE001 - never print exception text (may hold private values)
        print(json.dumps({"fault": name, "status": "refused", "code": "injector_internal_error"}, sort_keys=True))
        return 1
    print(json.dumps({"fault": name, "status": "injected", "code": None, **detail}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
