"""Portal intake adapter tests: allowlist-gated enrollment and refusal recording.

Synthetic-only: no sockets, no live URLs, no host policy file reads. The
allowlist is parsed directly (``parse_approval``) so tests never need root to
construct the read-only administrator policy the intake adapter consumes.
"""
import contextlib
import hashlib
import io
import json
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from campaign_tool.records.portal import Queue, fetch_queue
from campaign_tool.records.portal.intake import intake_notice, intake_notices
from campaign_tool.records.portal.policy import PortalError, parse_approval
from campaign_tool.records.portal.__main__ import main
from tests.test_records_portal import (
    FakeTransport, HOST, OBJECT, PDF, SOURCE, NOW, URL, policy_data, response,
)

UNAPPROVED = "unapproved.example.test"


def policy():
    return parse_approval(json.dumps(policy_data()), uid=0, mode=stat.S_IFREG | 0o644, now=NOW)


def notice(url, request_id="26-001", source_sha256=SOURCE):
    return {"body": f'<a href="{url}">file</a>', "request_id": request_id,
            "source_sha256": source_sha256}


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class PortalIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "queue"
        self.queue = Queue(self.root)

    def tearDown(self):
        self.queue.close()
        self.temp.cleanup()

    def run_intake(self, notices):
        return intake_notices(self.queue, notices=notices,
                              approval_loader=policy, now=NOW)

    def refusals(self):
        return self.queue.status()["refusals"]

    def test_allowlisted_notice_enrolls_link(self):
        result = intake_notice(self.queue, body=notice(URL)["body"],
                               request_id="26-001", source_sha256=SOURCE,
                               approval_loader=policy, now=NOW)
        self.assertEqual(result, {"enrolled": 1, "refused": 0, "skipped": 0})
        items = self.queue.status()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["host"], HOST)
        self.assertEqual(items[0]["item_id"], "42")

    def test_non_allowlisted_host_is_refused(self):
        evil = f"https://{UNAPPROVED}/documents/7/download?token=synthetic-only"
        result = self.run_intake([notice(evil)])
        self.assertEqual(result, {"enrolled": 0, "refused": 1, "skipped": 0})
        self.assertEqual(self.queue.status()["items"], [])
        refusals = self.refusals()
        self.assertEqual(len(refusals), 1)
        self.assertEqual(refusals[0]["host"], UNAPPROVED)
        self.assertEqual(refusals[0]["reason"], "portal_not_approved")
        self.assertEqual(self.queue.status()["refused_hosts"], 1)

    def test_redirect_host_as_origin_is_refused(self):
        # A redirect-only host (objects.*) is not itself an approved portal, so a
        # /documents/ link pointing straight at it is refused, not enrolled.
        redirect_url = f"https://{OBJECT}/documents/7/download?token=synthetic-only"
        result = self.run_intake([notice(redirect_url)])
        self.assertEqual(result["refused"], 1)
        self.assertEqual(self.refusals()[0]["host"], OBJECT)
        self.assertEqual(self.refusals()[0]["reason"], "portal_not_approved")

    def test_path_out_of_scope_is_refused(self):
        data = policy_data()
        data["path_prefixes"] = {HOST: ["/restricted/"], OBJECT: ["/production/"]}
        restricted = parse_approval(json.dumps(data), uid=0, mode=stat.S_IFREG | 0o644, now=NOW)
        result = intake_notices(self.queue, notices=[notice(URL)],
                                approval_loader=lambda: restricted, now=NOW)
        self.assertEqual(result["refused"], 1)
        self.assertEqual(self.refusals()[0]["host"], HOST)
        self.assertEqual(self.refusals()[0]["reason"], "path_not_approved")

    def test_non_document_url_is_skipped_not_refused(self):
        result = self.run_intake([notice(f"https://{HOST}/about")])
        self.assertEqual(result, {"enrolled": 0, "refused": 0, "skipped": 1})
        self.assertEqual(self.refusals(), [])

    def test_missing_allowlist_fails_closed(self):
        def missing():
            raise PortalError("approval_missing_or_unsafe")
        with self.assertRaisesRegex(PortalError, "approval_missing_or_unsafe"):
            intake_notices(self.queue, notices=[notice(URL)], approval_loader=missing, now=NOW)
        self.assertEqual(self.queue.status()["items"], [])
        self.assertEqual(self.refusals(), [])

    def test_expired_allowlist_fails_closed(self):
        data = policy_data()
        data["expires_utc"] = NOW.isoformat()
        with self.assertRaisesRegex(PortalError, "approval_expired"):
            intake_notices(self.queue, notices=[notice(URL)],
                           approval_loader=lambda: parse_approval(
                               json.dumps(data), uid=0, mode=stat.S_IFREG | 0o644, now=NOW),
                           now=NOW)
        self.assertEqual(self.queue.status()["items"], [])

    def test_batch_populates_queue_and_marks_refusals(self):
        allowlisted = 100
        refused = 23
        notices = [notice(f"https://{HOST}/documents/{n}/download?token=synthetic-only")
                   for n in range(allowlisted)]
        notices += [notice(f"https://{UNAPPROVED}/documents/{n}/download?token=synthetic-only")
                    for n in range(refused)]
        result = self.run_intake(notices)
        self.assertEqual(result["enrolled"] + result["refused"], allowlisted + refused)
        self.assertEqual(result["enrolled"], allowlisted)
        self.assertEqual(result["refused"], refused)
        self.assertEqual(len(self.queue.status()["items"]), allowlisted)
        self.assertEqual(self.queue.status()["refused_hosts"], 1)

    def test_dry_run_never_fetches(self):
        self.run_intake([notice(URL)])
        transport = FakeTransport()
        result = fetch_queue(self.queue, transport=transport)
        self.assertEqual(result["mode"], "inventory_only")
        self.assertEqual(transport.calls, [])

    def test_apply_fetches_only_allowlisted_hosts(self):
        evil = f"https://{UNAPPROVED}/documents/7/download?token=synthetic-only"
        self.run_intake([notice(URL), notice(evil)])
        transport = FakeTransport(response(), response())
        result = fetch_queue(self.queue, apply=True, approval_loader=policy,
                             transport=transport, egress=lambda u: True,
                             wall=lambda: NOW.timestamp())
        self.assertEqual(result["received"], 1)
        self.assertEqual(len(transport.calls), 1)
        hosts = {call[0] for call in transport.calls}
        self.assertEqual(hosts, {URL})
        self.assertFalse(any(UNAPPROVED in url for url in hosts))

    def test_refusal_is_deduped_and_hides_signed_url(self):
        evil = f"https://{UNAPPROVED}/documents/7/download?token=synthetic-only"
        for _ in range(2):
            self.run_intake([notice(evil)])
        refusals = self.refusals()
        self.assertEqual(len(refusals), 1)
        self.assertEqual(refusals[0]["count"], 2)
        self.assertNotIn("token=", json.dumps(refusals))
        self.assertNotIn("synthetic-only", json.dumps(refusals))
        self.assertNotIn("https://", json.dumps(refusals))

    def test_intake_is_idempotent(self):
        for _ in range(2):
            self.run_intake([notice(URL)])
        self.assertEqual(len(self.queue.status()["items"]), 1)

    def test_multiple_links_in_one_body(self):
        body = (f'<a href="{URL}">a</a> '
                f'<a href="https://{UNAPPROVED}/documents/9/download?token=x">b</a>')
        result = intake_notice(self.queue, body=body, request_id="26-001",
                               source_sha256=SOURCE, approval_loader=policy, now=NOW)
        self.assertEqual(result, {"enrolled": 1, "refused": 1, "skipped": 0})

    def test_oversize_notice_rejected(self):
        body = "<a href='" + URL + "'>x</a>" + " " * (2 * 1024 * 1024)
        with self.assertRaisesRegex(PortalError, "notice_oversize"):
            intake_notice(self.queue, body=body, request_id="26-001",
                          source_sha256=SOURCE, approval_loader=policy, now=NOW)

    def test_cli_intake_refuses_without_approval(self):
        input_path = Path(self.temp.name) / "notices.json"
        input_path.write_text(json.dumps([notice(URL)]))
        missing = Path(self.temp.name) / "absent-approval.json"
        with contextlib.redirect_stdout(io.StringIO()) as out:
            status = main(["--root", str(self.root), "intake",
                           "--input", str(input_path), "--approval-file", str(missing)])
        self.assertEqual(status, 2)
        self.assertNotIn("token=", out.getvalue())

    def test_refusal_reason_codes_are_fixed(self):
        evil = f"https://{UNAPPROVED}/documents/7/download?token=synthetic-only"
        self.run_intake([notice(evil)])
        self.assertEqual({r["reason"] for r in self.refusals()},
                         {"portal_not_approved"})


if __name__ == "__main__":
    unittest.main()
