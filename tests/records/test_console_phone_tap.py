"""A6 PR 2: the console's "Approve with phone" tap in journal mode (synthetic passkey only; no browser, no network).

Contract: the review page of a pending row offers a server-built envelope over the pinned export's exact approval payload
(text, hash prefix, expiry, challenge) and one same-origin script; POST /approvals/phone-approve journals one
``phone_approve`` entry only when CSRF, row, revision, envelope binding, validity window and nonce all check out, and
nothing otherwise. The console verifies no signature; ``records decisions-apply`` does. The typed form is unchanged.
"""
import base64
import hashlib
import html
import http.client
import json
import re
import time
import unittest
from unittest import mock

from campaign_tool import phone_approval
from campaign_tool.records import approval_queue as aq, console_journal, console_page_decisions, console_phone, console_snapshot
from campaign_tool.records.outcomes import unmatched
from tests.phone_signing import Phone
from tests.records.test_console_apply import Apply
from tests.records.test_console_model import RUN

ROUTE = console_phone.ROUTE


class PhoneTap(Apply):
    def setUp(self):
        super().setUp()
        self.phone = Phone()
        signers = self.tmp / "signers-with-phone"
        signers.write_text(self.signer.signers.read_text() + self.phone.signers_line())
        trust = self.signer.trust(signers)  # the typed key and the enrolled phone, one trust file
        trust.__enter__()
        self.addCleanup(trust.__exit__, None, None, None)
        self.other_id = aq.add_draft(self.live, item_id="9" * 64, kind="reply-extension", agency_id="caltrans",
                                     request_id="req-2", body="Another draft.", evidence={"source": "test"},
                                     source_run_id=RUN)["id"]

    def fetch(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:
            connection.close()

    def offered(self, path):
        """(envelope, challenge) the page offers for signing, or None when it offers nothing signable by phone."""
        page = self.get(path)
        envelope = re.search(r'name="envelope" value="([^"]*)"', page)
        challenge = re.search(r'data-challenge="([^"]*)"', page)
        self.assertEqual(envelope is None, challenge is None)
        return None if envelope is None else (html.unescape(envelope.group(1)), html.unescape(challenge.group(1)))

    def tap(self, ident, envelope, revision="0", **extra):
        fields = {"id": ident, "expected_revision": revision, **self.phone.tap(envelope), **extra}
        return self.decide(ROUTE, **fields)

    def envelope(self, ident, issued=None, nonce="ab" * 16, choice=None):
        payload = aq.approval_payload(self.live, ident, choice=choice)  # equal to the exported bytes (test_console_model)
        return phone_approval.envelope(ident, payload, int(time.time()) if issued is None else issued, nonce)

    # Bug caught: a page that offers no binding (or another text) to sign, an inline script the CSP would have to allow,
    # or a signable envelope on a row that is not pending.
    def test_page_shows_envelope_text_hash_prefix_and_challenge_for_a_pending_row_only(self):
        self.make_export()
        self.up()
        payload = aq.approval_payload(self.live, self.pending_id)
        status, headers, page = self.fetch("/review?id=" + self.pending_id)
        self.assertEqual(status, 200)
        envelope, challenge = self.offered("/review?id=" + self.pending_id)
        parsed = phone_approval.parse_envelope(envelope)
        self.assertEqual((parsed["decision"], parsed["payload_sha256"]), (self.pending_id, hashlib.sha256(payload).hexdigest()))
        self.assertEqual(parsed["expires"] - parsed["issued"], phone_approval.DEFAULT_TTL)
        self.assertEqual(challenge, base64.urlsafe_b64encode(phone_approval.challenge(envelope)).rstrip(b"=").decode())
        text = html.unescape(page)
        self.assertIn(payload.decode(), text)  # the full text being approved
        self.assertIn('id="phone-payload-sha256">%s<' % hashlib.sha256(payload).hexdigest()[:12], page)
        self.assertRegex(page, r'id="phone-expires">\d{4}-\d\d-\d\d \d\d:\d\d P[SD]T<')
        self.assertNotEqual(self.offered("/review?id=" + self.pending_id)[0], envelope)  # a fresh nonce per page
        self.assertIn('<script src="/phone-approve.js"></script>', page)
        self.assertEqual(page.count("<script"), 1)  # no inline script
        self.assertTrue(headers["Content-Security-Policy"].endswith("; script-src 'self'"))
        status, headers, script = self.fetch(console_phone.SCRIPT)
        self.assertEqual((status, headers["Content-Type"], headers["X-Content-Type-Options"]),
                         (200, "text/javascript; charset=utf-8", "nosniff"))
        for needle in ("navigator.credentials.get", 'userVerification: "required"', "timeout: 120000",
                       "rpId: window.location.hostname"):
            self.assertIn(needle, script)
        _, headers, approved = self.fetch("/review?id=" + self.approved_id)
        self.assertIsNone(self.offered("/review?id=" + self.approved_id))
        self.assertNotIn("<script", approved)
        self.assertNotIn("script-src", headers["Content-Security-Policy"])
        self.assertEqual(self.lines(), [])

    # Bug caught: a good tap not journaled (or journaled twice), the CRLF a browser form sends breaking the envelope,
    # the console touching the live project, or a journaled tap that decisions-apply cannot apply.
    def test_a_good_tap_is_journaled_once_and_then_applies_through_decisions_apply(self):
        self.make_export()
        self.up()
        envelope, _ = self.offered("/review?id=" + self.pending_id)
        tap = self.phone.tap(envelope)
        posted = {**tap, "envelope": envelope.replace("\n", "\r\n")}
        status, _, page = self.decide(ROUTE, id=self.pending_id, expected_revision="0", **posted)
        self.assertEqual(status, 200)
        self.assertIn(console_phone.RECORDED, page)
        (entry,) = self.lines()
        self.assertEqual((entry["action"], entry["target_id"], entry["revision_seen"], entry["value"], entry["route"]),
                         ("phone_approve", self.pending_id, 0, console_journal._canon(tap), ROUTE))
        self.assertNotIn("choice", entry)
        self.assertEqual(self.row(self.pending_id)["status"], "pending")  # journaled, not applied
        self.assertIn("pending apply: phone_approve", self.get("/review?id=" + self.pending_id))
        status, _, page = self.decide(ROUTE, id=self.pending_id, expected_revision="0", **tap)
        self.assertEqual(status, 409)  # the same nonce again
        self.assertIn("phone_replay", page)
        self.assertEqual(len(self.lines()), 1)
        self.assertEqual(self.apply(), ["APPLIED"])
        row = self.row(self.pending_id)
        self.assertEqual((row["status"], row["approved_by"]), ("approved", self.phone.principal))
        self.assertEqual(console_page_decisions.ACTIONS["phone_approve"][0], "Approve draft (phone)")

    # Bug caught: a tap accepted for another row, an older revision, outside its window, with a used nonce, without the
    # CSRF token or without an assertion; or a refusal that still journals something.
    def test_refused_taps_journal_nothing(self):
        self.make_export()
        self.up()
        mine = self.envelope(self.pending_id)
        now = int(time.time())
        cases = (
            ("another row", self.tap(self.other_id, mine), 409, "phone_mismatch"),
            ("another text", self.tap(self.pending_id, phone_approval.envelope(self.pending_id, b"other", now, "cd" * 16)),
             409, "phone_mismatch"),
            ("stale revision", self.tap(self.pending_id, mine, revision="1"), 409, "Draft changed"),
            ("expired", self.tap(self.pending_id, self.envelope(self.pending_id, issued=now - 2 * 86400)), 409, "phone_expired"),
            ("future", self.tap(self.pending_id, self.envelope(self.pending_id, issued=now + 3600)), 409, "phone_expired"),
            ("approved row", self.tap(self.approved_id, self.envelope(self.approved_id)), 409, "Conflict"),
            ("bad envelope", self.tap(self.pending_id, mine.replace("nonce", "nonse")), 400, "Bad request"),
            ("no assertion", self.decide(ROUTE, id=self.pending_id, expected_revision="0", envelope=mine,
                                         authenticator_data="", client_data="", signature=""), 400, "Bad request"),
            ("wrong csrf", self.tap(self.pending_id, mine, csrf_token="x" * 43), 403, "Refused"),
        )
        for name, (status, _, page), code, text in cases:
            self.assertEqual(status, code, name)
            self.assertIn(text, page, name)
        fields = {"id": self.pending_id, "expected_revision": "0", **self.phone.tap(mine)}
        self.assertEqual(self.request("POST", ROUTE, fields)[0], 403)  # no CSRF token at all
        self.assertEqual(self.lines(), [])
        console_journal.append(str(self.j), console_snapshot.Pinner(str(self.root)).pin(), "operator", ROUTE, "phone_approve",
                               self.other_id, 0,
                               console_journal._canon(self.phone.tap(self.envelope(self.other_id, nonce="ee" * 16))))
        status, _, page = self.tap(self.pending_id, self.envelope(self.pending_id, nonce="ee" * 16))
        self.assertEqual((status, "phone_replay" in page), (409, True))  # a nonce journaled for any row approves once
        self.assertEqual(len(self.lines()), 1)
        with mock.patch.object(console_phone, "_now", return_value=now + phone_approval.DEFAULT_TTL + 1):
            self.assertEqual(self.tap(self.pending_id, mine)[0], 409)  # the page's own envelope, after it expired
        self.assertEqual(len(self.lines()), 1)

    # Bug caught: offering or taking a tap over text that an edit waiting in the journal is about to replace.
    def test_an_edit_waiting_to_apply_withholds_the_tap(self):
        self.make_export()
        self.up()
        self.assertEqual(self.decide("/approvals/edit", id=self.pending_id, body="new words", expected_revision="0")[0], 303)
        self.assertIsNone(self.offered("/review?id=" + self.pending_id))
        status, _, page = self.tap(self.pending_id, self.envelope(self.pending_id))
        self.assertEqual(status, 409)
        self.assertIn("approve_edit_pending", page)
        self.assertEqual([e["action"] for e in self.lines()], ["edit"])

    # Bug caught: the phone path breaking or replacing the typed (pasted SSH signature) path beside it.
    def test_the_typed_form_still_journals_approve(self):
        self.make_export()
        self.up()
        page = self.get("/review?id=" + self.pending_id)
        self.assertIn('formaction="/approvals/approve"', page)
        self.assertIn('<textarea id="signature" name="signature">', page)
        signature = self.sign(aq.approval_payload(self.live, self.pending_id).decode())
        self.assertEqual(self.decide("/approvals/approve", id=self.pending_id, signature=signature)[0], 303)
        (entry,) = self.lines()
        self.assertEqual((entry["action"], entry["value"]), ("approve", signature))
        self.assertEqual(self.apply(), ["APPLIED"])
        self.assertEqual(self.row(self.pending_id)["approved_by"], self.signer.principal)

    # Bug caught: a tie tap that does not carry (or bind) the chosen request, or links another one.
    def test_a_message_tie_tap_binds_the_chosen_request(self):
        sha = "ab" * 32
        options = [{"request_id": "req-b", "agency_id": "caltrans"}, {"request_id": "req-a", "agency_id": "caltrans"}]
        aq.queue_message_links(self.live, RUN, [unmatched._draft(
            {"message_sha256": sha, "received_date": "2026-11-09", "sender_domain": "caltrans.example",
             "attachment_count": 1, "candidates": options, "agency_id": "caltrans"})])
        ident = aq.draft_id(sha, "message_link")
        self.make_export()
        self.up()
        self.assertIsNone(self.offered("/review?id=" + ident))  # nothing signable before a selection
        envelope, _ = self.offered("/review?id=%s&choice=req-a" % ident)
        self.assertEqual(phone_approval.parse_envelope(envelope)["payload_sha256"],
                         hashlib.sha256(aq.approval_payload(self.live, ident, choice="req-a")).hexdigest())
        self.assertEqual(self.tap(ident, envelope, choice="req-b")[0], 409)  # signed for req-a, posted as req-b
        self.assertEqual(self.tap(ident, envelope)[0], 400)  # a tie needs its choice
        self.assertEqual(self.lines(), [])
        self.assertEqual(self.tap(ident, envelope, choice="req-a")[0], 200)
        (entry,) = self.lines()
        self.assertEqual((entry["action"], entry["choice"]), ("phone_approve", "req-a"))
        self.assertEqual(self.apply(), ["APPLIED"])
        (line,) = (self.live / "message-links.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(line)["request_id"], "req-a")


if __name__ == "__main__":
    unittest.main()
