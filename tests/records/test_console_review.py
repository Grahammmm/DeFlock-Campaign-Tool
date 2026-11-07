"""Synthetic HTTP acceptance for the single-draft editor and existing decisions."""
import hashlib
import re
from html import unescape
from unittest import mock

from campaign_tool.records import admin_panel as ap, approval_queue as aq, mail_preview
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
        self.assertIn("/review?id=" + self.draft_row["id"], self.get("/")[2])
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
        status, headers, _ = self.post("edit", body=new_body)
        self.assertEqual((status, headers["location"]), (303, "/review?id=" + self.draft_row["id"]))
        page = self.get()[2]
        digest = hashlib.sha256(new_body.encode()).hexdigest()
        for text in ("Pending", "1 edits", "Last by operator-1", "--- original", "+Edited &lt;b&gt;&amp;", digest):
            self.assertIn(text, page)
        before = journal(self.project)
        self.assertEqual(self.post("approve", signature=old_signature)[0], 403)
        self.assertEqual(journal(self.project), before)
        self.assertEqual(self.row()["status"], "pending")

    def test_current_signed_approval_terminal_and_duplicate(self):
        signature = self.signer.sign(aq.approval_payload(self.project, self.draft_row["id"]))
        self.assertEqual(self.post("approve", signature=signature)[0], 303)
        page = self.get()[2]
        self.assertIn("Approved (not sent)", page)
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
        self.assertEqual(self.post("edit", body="x", **{"return": "/elsewhere"})[0], 400)
        self.assertEqual(self.post("edit", body="x", csrf_token="")[0], 403)
        self.assertEqual(self.post("edit", body="x", csrf_token="old-start-token")[0], 403)
        self.assertEqual(self.http.post("/approvals/edit", {"id": self.draft_row["id"], "body": "x",
                                                          "return": "review"})[0], 400)
        self.assertEqual(self.http.post("/approvals/edit", {"csrf_token": self.tok, "id": self.draft_row["id"],
                            "body": "x", "return": "review"}, headers={"Origin": "http://foreign.invalid"})[0], 403)
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
