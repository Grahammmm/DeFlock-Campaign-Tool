"""A console edit saved from an older page is refused (409) under the writer lock; nothing is overwritten or reopened."""
from unittest import mock

from campaign_tool.records import admin_panel as ap, approval_queue as aq
from tests.records.test_admin_panel import ServerCase, journal, sha

STALE_MARK = "STALE-TAB-SENTINEL"
FIELD = 'name="expected_revision"'


class EditConflictTests(ServerCase):
    def save(self, revision, body, ident=None, review=True, **extra):
        fields = {"csrf_token": self.tok, "id": ident or self.draft_row["id"], "body": body,
                  "expected_revision": revision, **extra}
        if review:
            fields["return"] = "review"
        return self.http.post("/approvals/edit", fields)

    def review(self, ident=None):
        return "/review?id=" + (ident or self.draft_row["id"])

    def assert_conflict(self, response, ident=None):
        status, headers, page = response
        self.assertEqual(status, 409)
        self.assertNotIn("location", headers)
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertIn("default-src 'none'", headers["content-security-policy"])
        self.assertIn('<a href="%s">Reload the draft</a>' % self.review(ident), page)
        self.assertIn("not saved", page)
        for leak in (STALE_MARK, self.tok, "<form"):
            self.assertNotIn(leak, page)

    def test_two_captured_pages_second_save_conflicts(self):
        tab_a = self.form_revision(path=self.review())
        tab_b = self.form_revision()  # the dashboard, captured before either save
        self.assertEqual((tab_a, tab_b), ("0", "0"))
        status, headers, _ = self.save(tab_a, "Operator A winning text")
        self.assertEqual((status, headers["location"]), (303, self.review()))
        winner, before = self.row(), journal(self.project)
        self.assertEqual((winner["revision"], winner["body"]), (1, "Operator A winning text"))
        for _ in range(2):  # the duplicate stale retry is refused the same way
            self.assert_conflict(self.save(tab_b, STALE_MARK + " Operator B text", review=False))
            self.assertEqual(journal(self.project), before)
            self.assertEqual(self.row(), winner)

    def test_old_pending_page_cannot_reopen_decided_draft(self):
        other = self.draft(2, body="second draft")
        approved_tab = self.form_revision(path=self.review())
        rejected_tab = self.form_revision(other["id"])
        signature = self.signer.sign(aq.approval_payload(self.project, self.draft_row["id"]))
        self.assertEqual(self.http.post("/approvals/approve", {"csrf_token": self.tok, "id": self.draft_row["id"],
                                                                "signature": signature})[0], 303)
        self.assertEqual(self.http.post("/approvals/reject", {"csrf_token": self.tok, "id": other["id"],
                                                               "reason": "not needed"})[0], 303)
        approved, rejected, before = self.row(), self.row(other["id"]), journal(self.project)
        self.assertEqual((approved["status"], rejected["status"]), ("approved", "rejected"))
        payload = aq.approval_payload(self.project, self.draft_row["id"])
        with mock.patch.object(ap, "_place_after_approve") as place, mock.patch.object(ap.mail_send, "send_one") as send:
            self.assert_conflict(self.save(approved_tab, STALE_MARK + " reopen approved"))
            self.assert_conflict(self.save(rejected_tab, STALE_MARK + " reopen rejected", ident=other["id"],
                                           review=False), other["id"])
        place.assert_not_called()
        send.assert_not_called()
        self.assertEqual(journal(self.project), before)
        self.assertEqual((self.row(), self.row(other["id"])), (approved, rejected))
        self.assertEqual(approved["approval_evidence"]["signed_revision"], 0)
        self.assertEqual(rejected["reason"], "not needed")
        self.assertEqual(aq.approval_payload(self.project, self.draft_row["id"]), payload)

    def test_revision_zero_and_fresh_reload_save(self):
        first = self.form_revision(path=self.review())
        self.assertEqual(first, "0")  # zero is a real precondition, not an omission
        self.assertEqual(self.save(first, "First saved text")[0], 303)
        fresh = self.form_revision(path=self.review())
        self.assertEqual(fresh, "1")
        self.assertEqual(self.save(fresh, "Second saved text")[0], 303)
        self.assertEqual((self.row()["revision"], self.row()["body"]), (2, "Second saved text"))
        before = journal(self.project)
        # the current body resubmitted from the older page still conflicts: revision binding, not body equality
        self.assert_conflict(self.save(fresh, "Second saved text"))
        self.assertEqual(journal(self.project), before)

    def test_edit_revision_admission(self):
        before = journal(self.project)
        bad = ("", " ", "-1", "+1", "01", "00", "1.0", "0 ", " 0", "0x1", "1e3", "\u0661", "\uff11",
               "1" + "0" * 20)
        for value in bad:
            with self.subTest(value=value):
                self.assertEqual(self.save(value, STALE_MARK)[0], 400)
        missing = {"csrf_token": self.tok, "id": self.draft_row["id"], "body": STALE_MARK, "return": "review"}
        self.assertEqual(self.http.post("/approvals/edit", missing)[0], 400)
        duplicate = [*missing.items(), ("expected_revision", "0"), ("expected_revision", "0")]
        self.assertEqual(self.http.post("/approvals/edit", duplicate)[0], 400)
        self.assertEqual(journal(self.project), before)
        # twenty digits is the admitted bound: well formed, and simply stale
        self.assert_conflict(self.save("1" + "0" * 19, STALE_MARK))
        unknown = self.save("0", STALE_MARK, ident=sha(77))
        self.assertEqual(unknown[0], 404)
        self.assertEqual(self.save("0", STALE_MARK, csrf_token="wrong")[0], 403)
        foreign = self.http.post("/approvals/edit", {**missing, "expected_revision": "0"},
                                 headers={"Origin": "http://foreign.invalid"})
        self.assertEqual(foreign[0], 403)
        for _, _, page in (unknown, foreign):
            self.assertNotIn(STALE_MARK, page)
        self.assertEqual(journal(self.project), before)
        self.assertEqual(self.row()["body"], self.draft_row["body"])

    def test_guard_is_checked_under_writer_lock(self):
        ident, real_lock, calls = self.draft_row["id"], aq._lock, []

        def lock(dir_fd, exclusive):
            # The winning save lands exactly when the guarded caller asks for the writer lock, so any revision
            # it read earlier (unlocked or under a separate shared lock) is already stale.
            if exclusive and not calls:
                calls.append("winner")
                aq.edit(self.project, ident, "winner", "Winning text", expected_revision=0)
            return real_lock(dir_fd, exclusive)

        with mock.patch.object(aq, "_lock", side_effect=lock):
            with self.assertRaises(aq.ApprovalQueueError) as caught:
                aq.edit(self.project, ident, "stale", STALE_MARK, expected_revision=0)
        self.assertEqual((caught.exception.code, calls), ("stale_revision", ["winner"]))
        row = self.row()
        self.assertEqual((row["revision"], row["body"], row["actor"]), (1, "Winning text", "winner"))
        self.assertNotIn(STALE_MARK.encode(), journal(self.project))

    def test_guarded_api_and_legacy_compatibility(self):
        ident = self.draft_row["id"]
        lines = journal(self.project).count(b"\n")
        self.assertEqual(aq.edit(self.project, ident, "op", "Guarded", expected_revision=0)["revision"], 1)
        self.assertEqual(journal(self.project).count(b"\n"), lines + 1)
        before = journal(self.project)
        for value, code in ((0, "stale_revision"), (2, "stale_revision"), (-1, "invalid_draft"),
                            (True, "invalid_draft"), (False, "invalid_draft"), ("1", "invalid_draft"),
                            (1.0, "invalid_draft")):
            with self.subTest(value=value):
                with self.assertRaises(aq.ApprovalQueueError) as caught:
                    aq.edit(self.project, ident, "op", "Refused", expected_revision=value)
                self.assertEqual(caught.exception.code, code)
        self.assertEqual(journal(self.project), before)
        old_signature = self.signer.sign(aq.approval_payload(self.project, ident))
        self.assertEqual(aq.approve(self.project, ident, {"kind": "ssh", "signature": old_signature})["status"],
                         "approved")
        # omitted keyword: the intentional unconditional edit still reopens a decided draft
        reopened = aq.edit(self.project, ident, "op", "Reopened")
        self.assertEqual((reopened["status"], reopened["revision"]), ("pending", 3))
        self.assertNotIn("approval_evidence", reopened)
        with self.assertRaises(aq.ApprovalQueueError) as caught:  # the old signature no longer approves
            aq.approve(self.project, ident, {"kind": "ssh", "signature": old_signature})
        self.assertEqual(caught.exception.code, "approval_required")

    def test_both_form_surfaces_bind_same_rendered_revision(self):
        aq.edit(self.project, self.draft_row["id"], "op", "Edited once")
        for path in (self.review(), "/dashboard"):
            with self.subTest(path=path):
                self.assertEqual(self.form_revision(path=path), "1")
                page = self.http.send("GET", path)[2]
                self.assertEqual(page.count(FIELD), 1)  # only the one pending edit form carries it
                for form in page.split("<form")[1:]:
                    if 'action="/approvals/edit"' not in form:
                        self.assertNotIn(FIELD, form)
        self.assertNotIn(FIELD, self.http.send("GET", "/next-steps")[2])
        self.assertNotIn(FIELD, ap.render_dashboard(self.load()))  # static snapshot stays form-free
