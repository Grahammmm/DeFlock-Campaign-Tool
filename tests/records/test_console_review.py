"""Synthetic HTTP acceptance for the single-draft editor and existing decisions."""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
import re
from html import unescape
from unittest import mock

from campaign_tool.records import admin_panel as ap, approval_queue as aq, mail_preview, mail_send as ms, mail_drafts as md, draft_templates
from tests.records.test_admin_panel import ServerCase, RUN, journal, tree_state


class ReviewTests(ServerCase):
    def draft(self, n=1, body="\nTo: inside\nSubject: body only\n[EDIT ME] <b>&\n</textarea><script>x</script>"):
        return aq.add_draft(self.project, item_id=f"{n:064x}", kind="reply-extension", agency_id="alpha-pd",
                            request_id="req-1", body=body, evidence={"why": "<why>&", "limits": ["<limit>"]},
                            source_run_id=RUN)

    def get(self, path=None):
        return self.http.send("GET", path or "/review?id=" + self.draft_row["id"])

    def post(self, action, **fields):
        return self.http.post("/approvals/" + action, {"csrf_token": self.tok, "id": self.draft_row["id"],
                                                       "return": "review", **fields})

    def test_full_body_headers_payload_warnings_and_read_only_get(self):
        before = tree_state(self.project)
        with mock.patch.object(mail_preview.mail_drafts, "_thread_for", return_value=(
                {"reply_to": "clerk@example.test", "subject": "Synthetic subject"}, "ok")), \
                mock.patch.object(mail_preview.mail_drafts, "load_config", return_value={"from_addr": "owner@example.test"}):
            self.server.mail_drafts_config = self.project / "synthetic-drafts.json"
            status, headers, page = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(headers["cache-control"], "no-store")
        body = re.search(r'<textarea id="body"[^>]*>\n(.*?)</textarea>', page, re.S)[1]
        self.assertEqual(unescape(body), self.row()["body"])
        payload = re.search(r'<pre id="approval-payload">(.*?)</pre>', page, re.S)[1]
        self.assertEqual(unescape(payload).encode(), aq.approval_payload(self.project, self.draft_row["id"]))
        for text in ("clerk@example.test", "owner@example.test", "Synthetic subject", "<dd>yes</dd>",
                     "Attachments</dt><dd>None", "[EDIT ME] placeholder left", "Lines 2, 3:",
                     "&lt;why&gt;&amp;", "&lt;limit&gt;", "No edits", 'href="/">Back', 'rows="28"'):
            self.assertIn(text, page)
        self.assertNotIn("<script>", page)
        self.assertEqual(page.count('name="return" value="review"'), 3)
        self.assertEqual(tree_state(self.project), before)
        self.assertIn('>Review</a>', self.get("/dashboard")[2])

    def test_bad_unknown_duplicate_ids_and_unreadable_queue(self):
        before = journal(self.project)
        for path, expected in (("/review", 400), ("/review?id=../x", 400),
                               ("/review?id=" + "f" * 64, 404),
                               ("/review?id=" + self.draft_row["id"] + "&id=" + self.draft_row["id"], 400)):
            self.assertEqual(self.get(path)[0], expected)
        with mock.patch.object(aq, "load_queue", side_effect=aq.ApprovalQueueError("queue_io")):
            self.assertEqual(self.get()[0], 503)
        self.assertEqual(journal(self.project), before)

    def test_save_history_hashes_diff_and_stale_signature(self):
        old_signature = self.signer.sign(aq.approval_payload(self.project, self.draft_row["id"]))
        new_body = "Edited <b>&\ncomplete body"
        revision = self.form_revision(path="/review?id=" + self.draft_row["id"])
        status, headers, _ = self.post("edit", body=new_body, expected_revision=revision)
        self.assertEqual((status, headers["location"]), (303, "/review?id=" + self.draft_row["id"]))
        page = self.get()[2]
        digest = hashlib.sha256(new_body.encode()).hexdigest()
        for text in ("Pending", "1 edits", "Last by operator-1", "--- original", "+Edited &lt;b&gt;&amp;", digest):
            self.assertIn(text, page)
        before = journal(self.project)
        self.assertEqual(self.post("approve", signature=old_signature)[0], 403)
        self.assertEqual(journal(self.project), before)
        self.assertEqual(self.row()["status"], "pending")

    def test_newline_only_history_diff_is_visible_and_escaped(self):
        self.draft_row = self.draft(2, body="<script>&\nSecond\n")
        aq.edit(self.project, self.draft_row["id"], "operator-1", "<script>&\r\nSecond")
        history = aq.history(self.project, self.draft_row["id"])
        self.assertTrue(history["changed"] and history["diff"])
        before = journal(self.project)
        status, _, page = self.get()
        self.assertEqual(journal(self.project), before)
        self.assertEqual(status, 200)
        shown = unescape(re.search(r"<h2>Edit history</h2>.*?<pre>(.*?)</pre>", page, re.S)[1])
        self.assertEqual(shown.splitlines(), history["diff"])
        for text in ("-&lt;script&gt;&amp;", "+&lt;script&gt;&amp;", "\\ Line ending: CRLF",
                     "\\ No newline at end of file"):
            self.assertIn(text, page)
        self.assertNotIn("<script>&", page)
        self.assertNotIn("\r", page.split("<h2>Edit history</h2>")[1].split("</pre>")[0])
        body = re.search(r'<textarea id="body"[^>]*>\n(.*?)</textarea>', page, re.S)[1]
        self.assertEqual(unescape(body), "<script>&\r\nSecond")
        self.assertEqual(self.row()["body"], "<script>&\r\nSecond")
        # an edit back to the original bytes leaves nothing to show
        aq.edit(self.project, self.draft_row["id"], "operator-1", "<script>&\nSecond\n")
        self.assertEqual(aq.history(self.project, self.draft_row["id"])["diff"], [])
        self.assertNotIn("Line ending", self.get()[2])

    def test_current_signed_approval_terminal_and_duplicate(self):
        signature = self.signer.sign(aq.approval_payload(self.project, self.draft_row["id"]))
        self.assertEqual(self.post("approve", signature=signature)[0], 303)
        page = self.get()[2]
        self.assertIn("Approved, not sent. Not in the Drafts mailbox yet: not_placed.", page)
        self.assertIn(" readonly", page)
        self.assertNotIn("<form", page)
        before = journal(self.project)
        self.assertEqual(self.post("approve", signature=signature)[0], 303)
        self.assertEqual(self.post("reject", reason="late")[0], 409)
        self.assertEqual(journal(self.project), before)

    def test_reject_with_reason_and_cancelled_edit(self):
        before = journal(self.project)
        self.get()  # opening the page then Back/cancelling submits nothing
        self.get("/")
        self.assertEqual(journal(self.project), before)
        self.assertEqual(self.post("reject", reason=" ")[0], 400)
        status, headers, _ = self.post("reject", reason="Not needed <reason>&")
        self.assertEqual((status, headers["location"]), (303, "/review?id=" + self.draft_row["id"]))
        page = self.get()[2]
        self.assertIn("Rejected: Not needed &lt;reason&gt;&amp;", page)
        self.assertNotIn("<form", page)
        self.assertIn(" readonly", page)
        before = journal(self.project)
        self.assertEqual(self.post("reject", reason="late")[0], 409)
        self.assertEqual(journal(self.project), before)

    def test_redirect_csrf_origin_and_expired_token_refused(self):
        before = journal(self.project)
        self.assertEqual(self.post("edit", body="x", expected_revision="0", **{"return": "/elsewhere"})[0], 400)
        self.assertEqual(self.post("edit", body="x", expected_revision="0", csrf_token="")[0], 403)
        self.assertEqual(self.post("edit", body="x", expected_revision="0", csrf_token="old-start-token")[0], 403)
        self.assertEqual(self.http.post("/approvals/edit", {"id": self.draft_row["id"], "body": "x",
                                                          "expected_revision": "0", "return": "review"})[0], 400)
        self.assertEqual(self.http.post("/approvals/edit", {"csrf_token": self.tok, "id": self.draft_row["id"],
                            "body": "x", "expected_revision": "0", "return": "review"},
                                        headers={"Origin": "http://foreign.invalid"})[0], 403)
        self.assertEqual(journal(self.project), before)

    def test_static_render_unchanged(self):
        model = self.load()
        before = ap.render_dashboard(model)
        self.get()
        self.assertEqual(ap.render_dashboard(model), before)
        self.assertNotIn('href="/review?', before)
        self.assertNotIn("<form", before)

    def test_attachment_names_and_unthreaded_unknown_headers(self):
        row = aq.add_draft(self.project, item_id="a" * 64, kind="reply-extension", agency_id="alpha-pd",
                           request_id="req-2", body="Clean body", evidence={"clock_kind": "DATED", "clock_date": "2026-11-06",
                           "attachments": [{"name": "<report>.pdf"}]}, source_run_id=RUN)
        page = self.get("/review?id=" + row["id"])[2]
        for text in ("&lt;report&gt;.pdf", "<dd>Unknown</dd>", "<dd>no</dd>", "DATED 2026-11-06"):
            self.assertIn(text, page)
        self.assertNotIn("<h2>Warnings</h2>", page)


    def configure_send(self, **changes):
        path = self.project / "synthetic-send.json"
        data = {"enabled": True, "live": True, "daily_cap": 1, "smtp_host": "smtp.example.test",
                "smtp_user": "synthetic", "smtp_password_file": str(self.project / "unused-secret"),
                "imap_config": str(self.project / "unused-imap.json"),
                "outbox_path": str(self.project / "synthetic-outbox.sqlite")}
        data.update(changes)
        path.write_text(json.dumps(data))
        path.chmod(0o600)
        self.server.options["send_config"] = path
        return data

    def write_mail_rows(self, filename, schema, rows):
        base = {"schema": schema, "queue_id": self.draft_row["id"], "revision": self.row()["revision"],
                "at": "2026-11-06T20:00:00Z"}
        (self.project / "approvals" / filename).write_text("".join(
            json.dumps({**base, **row}) + "\n" for row in rows))

    def approve_review(self):
        signature = self.signer.sign(aq.approval_payload(self.project, self.draft_row["id"]))
        self.assertEqual(self.post("approve", signature=signature)[0], 303)

    def test_button_tracks_real_switches_and_cap_without_writes(self):
        self.assertIn(ms.REASONS["not_configured"], self.get()[2])
        self.configure_send(enabled=False)
        self.assertIn(ms.REASONS["send_disabled"], self.get()[2])
        config = self.configure_send()
        stop = self.project / "approvals" / ms.KILL
        stop.touch()
        self.assertIn(ms.REASONS["kill_switch"], self.get()[2])
        stop.unlink()
        self.assertIn(">Approve and send</button>", self.get()[2])
        with sqlite3.connect(config["outbox_path"]) as con:
            con.execute("CREATE TABLE outbox(kind, state, draft_json, sent_at, sending_at)")
            con.execute("INSERT INTO outbox VALUES(?,?,?,?,?)", ("send_followup", "sent",
                        '{"intent_id":"recq-synthetic"}', datetime.now(timezone.utc).isoformat(), None))
        before = tree_state(self.project)
        self.assertIn(ms.REASONS["daily_cap_reached"], self.get()[2])
        self.assertEqual(tree_state(self.project), before)

    def test_next_step_records_only_even_when_sending_enabled(self):
        self.configure_send()
        for n, kind in enumerate(sorted(draft_templates.NEXT_STEP_KINDS), 50):
            self.draft_row = aq.add_draft(self.project, item_id=f"{n:064x}", kind=kind, agency_id="alpha-pd",
                                          request_id="req-1", body="Synthetic next step", evidence={}, source_run_id=RUN)
            self.assertIn("Approve (records the decision only; nothing is posted or sent)", self.get()[2])
            self.approve_review()
            self.assertIn("Approved; records the decision only; nothing is posted or sent.", self.get()[2])
            self.assertNotIn('href="/send?', self.get()[2])

    def test_approved_placement_and_send_results_are_revision_specific(self):
        self.approve_review()
        self.configure_send()
        self.write_mail_rows(md.JOURNAL, md.SCHEMA, [{"state": "failed", "code": "mailbox_unavailable"}])
        self.assertIn("Not in the Drafts mailbox yet: mailbox_unavailable.", self.get()[2])
        self.write_mail_rows(md.JOURNAL, md.SCHEMA, [{"state": "placed", "revision": self.row()["revision"] - 1}])
        self.assertIn("Not in the Drafts mailbox yet: not_placed.", self.get()[2])
        for state in ("placed", "found_existing"):
            self.write_mail_rows(md.JOURNAL, md.SCHEMA, [{"state": state}])
            self.assertIn('href="/send?id=' + self.draft_row["id"] + '">open the send check</a>', self.get()[2])
        self.configure_send(enabled=False)
        self.assertIn("Approved, not sent. Sending is off: " + ms.REASONS["send_disabled"], self.get()[2])
        for state in ("attempt", "unknown"):
            self.write_mail_rows(ms.RESULTS, ms.SCHEMA, [{"state": state}])
            page = self.get()[2]
            self.assertIn("Delivery unresolved; reconcile in the outbox before any retry", page)
            self.assertNotIn('href="/send?', page)
        self.write_mail_rows(ms.RESULTS, ms.SCHEMA, [{"state": "unknown"}, {"state": "sent",
                             "message_id": "<synthetic-message@example.test>"}])
        page = self.get()[2]
        self.assertIn("Approved and sent on 2026-11-06 12:00 PST.", page)
        self.assertIn("Message id &lt;synthetic-m", page)
        self.assertNotIn("Delivery unresolved", page)
        self.write_mail_rows(ms.RESULTS, ms.SCHEMA, [{"state": "sent", "revision": self.row()["revision"] - 1}])
        self.assertNotIn("Approved and sent", self.get()[2])
        (self.project / "approvals" / ms.RESULTS).write_text("broken journal\n")
        self.assertEqual(self.get()[0], 503)

    def test_reason_text_escaped_and_test_mode_reason_when_available(self):
        codes = ["<unknown>&"] + (["test_mode_recipient"] if "test_mode_recipient" in ms.REASONS else [])
        self.approve_review()
        self.write_mail_rows(md.JOURNAL, md.SCHEMA, [{"state": "placed"}])
        for code in codes:
            with mock.patch.object(ms, "switch_state", return_value={"can_send": False, "reason": code}):
                page = self.get()[2]
                self.assertIn(ms.REASONS.get(code, "&lt;unknown&gt;&amp;"), page)
                self.assertNotIn("<unknown>", page)

    def test_review_get_and_approval_never_use_send_hooks(self):
        self.configure_send()
        self.server.send_hooks = {k: mock.Mock(side_effect=AssertionError(k))
                                  for k in ("imap_factory", "smtp_factory")}
        with mock.patch.object(ms, "send_one", side_effect=AssertionError("send")), \
                mock.patch.object(md, "place", side_effect=AssertionError("place")):
            self.assertEqual(self.get()[0], 200)
            self.approve_review()
            self.assertEqual(self.get()[0], 200)
        for hook in self.server.send_hooks.values():
            hook.assert_not_called()


class RefusalReview(ServerCase):
    get = ReviewTests.get
    def test_refusal_context_warnings_checklist_and_no_writes(self):
        from campaign_tool.records import console_review as review, denial_log
        from tests.records.test_denial_log import fixtures
        states, doc = fixtures()
        refusals = denial_log.entries(states, doc, [])[:2]
        for r in refusals:
            r["request_id"] = "req-1"
        refusals[0]["agency_words"]["quote"] = "<script>withheld</script> https://example.invalid"
        register = {"requests": [{"agency_key": "beta-pd", "agency": "Beta Police", "request_id": "other"},
                                 {"agency_key": "alpha-pd", "agency": "Alpha Police", "request_id": "req-1"}]}
        self.get()  # Open the fixture's read-only ledger before comparing its tree.
        for n, kind in enumerate(("appeal-denial", "followup-playbook"), 80):
            row = aq.add_draft(self.project, item_id=f"{n:064x}", kind=kind, agency_id="alpha-pd",
                               request_id="req-1", body="Gov. Code § 9999. Beta Police.", evidence={}, source_run_id=RUN)
            before = tree_state(self.project)
            with mock.patch.object(review.denial_log, "load", return_value={"state": "available", "entries": refusals}), \
                    mock.patch.object(review.followup_clock, "read_register", return_value=register):
                status, _, page = self.get("/review?id=" + row["id"])
            self.assertEqual(status, 200)
            for text in ("Refusal being answered", "Item 1:", "Item 2:", "page 1", "page 2", "doc bbbbbbbbbbbb",
                         "2026-11-04", "UNVERIFIED by us", "&lt;script&gt;withheld&lt;/script&gt;",
                         "needs counsel clearance", "another agency is named", "By-hand checklist (not machine-checked)",
                         "names and titles", "Every withheld item", "please do not resend"):
                self.assertIn(text, page)
            self.assertNotIn('<a href="https://example.invalid', page)
            self.assertNotIn('<script>', page)
            self.assertEqual(tree_state(self.project), before)
        self.assertNotIn('Refusal being answered', self.get()[2])

    def test_missing_stale_context_and_unrelated_request(self):
        from campaign_tool.records import console_review as review
        row = {"kind": "followup-playbook", "agency_id": "alpha-pd", "request_id": "r1", "id": "q1", "body": "reply"}
        for code in ("denial_log_source_missing", "denial_log_invalid_source"):
            with mock.patch.object(review.denial_log, "load", return_value={"state": "unavailable", "code": code}):
                self.assertIn('context unavailable', review.refusal_panel(self.project, row, RUN))
        entry = {"agency_key": "beta-pd", "request_id": "r1"}
        with mock.patch.object(review.denial_log, "load", return_value={"state": "available", "entries": [entry]}), \
                mock.patch.object(review.followup_clock, "read_register", return_value=None):
            self.assertEqual(review.refusal_panel(self.project, row, RUN), "")
