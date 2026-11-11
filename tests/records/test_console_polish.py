"""DFGL-14: plain wording, refused review forms that keep the typed text, landmarks, 404s and small console fixes."""
import re
from datetime import date, datetime, timezone
from unittest import mock

from campaign_tool.records import approval_queue as aq, console_calendar as cc, console_layout as layout
from tests.records.test_admin_panel import ServerCase, journal, tree_state

TYPED = '<b>typed</b> & "quoted"'


class RefusedReviewFormTests(ServerCase):
    def post(self, action, **fields):
        base = {"csrf_token": self.tok, "id": self.draft_row["id"], "return": "review"}
        base.update(fields)
        if base["csrf_token"] is None:
            del base["csrf_token"]
        return self.http.post("/approvals/" + action, base)

    def refused(self, response, message, kept=None):
        status, headers, page = response
        self.assertEqual(status, 400)
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertIn(message, page)
        self.assertIn('role="alert"', page)
        self.assertEqual(page.count("<main"), 1)
        self.assertIn("<form", page)  # the review page again, not a bare error
        if kept:
            self.assertIn(kept, page)
        self.assertNotIn("<b>typed</b>", page)  # echoed escaped only

    def tree(self):
        return {k: v for k, v in tree_state(self.project).items() if not k.endswith(("-shm", "-wal"))}

    def assert_untouched(self, before_tree, before_journal):
        self.assertEqual(self.tree(), before_tree)
        self.assertEqual(journal(self.project), before_journal)
        self.assertEqual(self.row()["status"], "pending")

    def test_empty_and_whitespace_reason_keep_typed_text_and_change_nothing(self):
        tree, log = self.tree(), journal(self.project)
        self.refused(self.post("reject", reason=""), "Say why you are rejecting")
        self.refused(self.post("reject", reason="   \r\n "), "Say why you are rejecting")
        self.assert_untouched(tree, log)

    def test_empty_edit_body_is_refused_and_current_draft_kept(self):
        tree, log = self.tree(), journal(self.project)
        self.refused(self.post("edit", body="  \n", expected_revision="0"), "The edited text is empty")
        self.assert_untouched(tree, log)

    def test_missing_csrf_gets_a_plain_message_and_no_echo(self):
        tree, log = self.tree(), journal(self.project)
        status, _, page = self.post("edit", csrf_token=None, body=TYPED, expected_revision="0")
        self.assertEqual(status, 400)
        self.assertIn("The form expired; reload the page and try again.", page)
        self.assertNotIn("&lt;b&gt;typed", page)
        self.assertNotIn("<b>typed", page)
        self.assert_untouched(tree, log)

    def test_dashboard_forms_still_get_a_shared_layout_error(self):
        status, _, page = self.http.post("/approvals/reject", {"csrf_token": self.tok, "id": self.draft_row["id"],
                                                               "reason": " "})
        self.assertEqual(status, 400)
        self.assertEqual(page.count("<main"), 1)
        self.assertNotIn("role=\"alert\"", page)

    def test_typed_text_is_echoed_escaped_in_the_reason_field(self):
        # whitespace-only reasons are refused; the refused field carries the typed whitespace back, escaped
        status, _, page = self.post("reject", reason="  ")
        self.assertEqual(status, 400)
        self.assertRegex(page, r'<input id="reason" name="reason" required maxlength="2000" value="  ">')
        self.assertEqual(aq.load_queue(self.project)[0]["status"], "pending")


class LandmarkAndNavTests(ServerCase):
    def get(self, path):
        return self.http.send("GET", path, headers={"Content-Length": None, "Content-Type": None})

    def test_banner_is_inside_header_and_skip_link_is_first(self):
        for path in ("/", "/settings", "/dashboard", "/nope"):
            _, _, page = self.get(path)
            body = page.split("<body>", 1)[1]
            self.assertTrue(body.startswith('<a class="skip" href="#content">Skip to content</a><header>'), path)
            self.assertRegex(body, r"<header><p class=\"notice\">PRIVATE[^<]*</p></header>")
            self.assertEqual(page.count("<main"), 1, path)

    def test_error_page_uses_the_shared_layout_with_main(self):
        status, _, page = self.get("/nope")
        self.assertEqual(status, 404)
        self.assertIn("<main", page)
        self.assertIn('aria-label="Sections"', page)

    def test_phone_nav_targets_are_44px_and_dashboard_tables_scroll(self):
        _, _, page = self.get("/")
        self.assertRegex(page, r"max-width:699px\)\">[^<]*\.side a[^<]*min-height:44px")
        _, _, dash = self.get("/dashboard")
        self.assertIn(".scroll{overflow-x:auto", dash)
        self.assertRegex(dash, r'<div class="scroll"[^>]*><table>')
        self.assertEqual(dash.count("<table"), dash.count('<div class="scroll"'))

    def test_unknown_agency_is_404(self):
        status, _, page = self.get("/documents?agency=nope")
        self.assertEqual(status, 404)
        self.assertIn("Unknown agency", page)
        self.assertEqual(self.get("/documents")[0], 200)


class SourceWordingTests(ServerCase):
    def test_codes_have_plain_sentences_and_unknown_codes_fall_back(self):
        for code in ("missing", "run_mismatch", "shape_invalid", "register_missing"):
            self.assertNotIn("_", layout.reason(code))
        self.assertIn("could not be read", layout.reason("never_heard_of_it"))
        note = layout.unavailable_note("received documents", "missing")
        self.assertNotIn("(missing)", note)
        self.assertIn("not been created", note)


class CalendarSummaryTests(ServerCase):
    def test_drafting_off_agencies_are_one_summary_line_plus_details(self):
        notes = ["follow-up drafting is off for this agency: %s" % n for n in ("ALPHA", "BETA", "GAMMA")]
        with mock.patch.object(cc, "build", return_value=([], notes + ["something else"])):
            page = cc.render({"run_id": "r"}, date(2026, 11, 5), date(2026, 11, 1))
        self.assertNotIn("follow-up drafting is off for this agency", page)
        self.assertEqual(page.count("Follow-up drafting is off for 3 agencies"), 1)
        details = re.search(r"<details[^>]*><summary>[^<]*</summary><ul>(.*?)</ul></details>", page, re.S).group(1)
        self.assertEqual(re.findall(r"<li>(.*?)</li>", details), ["ALPHA", "BETA", "GAMMA"])
        self.assertIn("something else", page)


class StatusLineRowTests(ServerCase):
    def test_two_status_lines_show_template_and_date(self):
        from campaign_tool.records import console_home as ch
        model = {"now": datetime(2026, 11, 5, tzinfo=timezone.utc),
                 "queue": {"rows": []}, "sends": {"rows": []}, "packets": {"rows": []},
                 "public": {"switch": "OFF", "lines": [
                     {"id": "a", "state": "proposed", "agency_key": "x", "request_id": "R1", "template": "t-ack",
                      "event": {"type": "ack", "date": "2026-11-01"}, "proposed_at": None},
                     {"id": "b", "state": "proposed", "agency_key": "x", "request_id": "R1", "template": "t-closed",
                      "event": {"type": "closed", "date": "2026-11-03"}, "proposed_at": None}]}}
        with mock.patch.object(ch, "_overdue", return_value=(None, None, None)):
            rows = ch.decision_rows(model, date(2026, 11, 5))
        what = sorted(r["what"] for r in rows)
        self.assertEqual(what, ["Public status line (t-ack, 2026-11-01)", "Public status line (t-closed, 2026-11-03)"])
