"""Phone fixes for the records console (DFB-10): layout rules, plain error pages, Home never reads "all clear"
when a source is missing, and identical decision rows tell themselves apart. Synthetic data only."""
import re
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import admin_panel as ap, console_home as home, console_layout as layout
from tests.records.test_console_home import NOW, RUN, fixture


class PhoneLayout(unittest.TestCase):
    def css(self):
        return re.search(r'<style media="\(max-width:699px\)">(.*?)</style>', layout.start("t"), re.S).group(1)

    def test_phone_rules_cover_touch_targets_tables_and_nav(self):
        css = self.css()
        self.assertIn("main a", css)
        self.assertRegex(css, r"main a[^}]*min-height:44px")  # links in the page body, not only the menu
        self.assertRegex(css, r"summary\{min-height:44px")
        self.assertIn("main table{display:block;overflow-x:auto", css)  # a wide table scrolls itself, not the page
        self.assertIn("th,td{overflow-wrap:break-word}", css)  # columns are not squeezed to one letter
        self.assertIn("flex-wrap:wrap", css)  # the menu is a few rows, not seven full-width lines
        self.assertIn("overflow-wrap:anywhere", layout.STYLE)  # a long id wraps instead of widening the page

    def test_every_source_key_has_a_plain_name_and_every_code_a_sentence(self):
        for key in ("timeline", "queue", "received", "links", "proposals", "response", "textstate", "tiers", "aliases"):
            self.assertIn(key, layout.PLAIN)
        self.assertNotIn("textstate", " ".join(layout.plain(["textstate"])))
        self.assertIn("(odd_code)", layout.reason("odd_code"))  # an unknown code is still named for the operator
        self.assertNotIn("(missing)", layout.unavailable_note("X", "missing"))


class _Stub:
    def __init__(self):
        self.sent = []

    def _send(self, status, body, **kw):
        self.sent.append((status, body))


class ErrorPages(unittest.TestCase):
    def test_every_refusal_says_what_happened_and_how_to_go_back(self):
        for status in ap.REASONS:
            stub = _Stub()
            ap._Handler._fail(stub, status)
            body = stub.sent[0][1]
            self.assertIn(ap.REASONS[status], body)
            self.assertIn(ap.DETAIL.get(status, ap.NOTHING), body)
            self.assertIn('href="/"', body)

    def test_named_refusals_and_empty_dashboard_field_get_plain_detail(self):
        for code in ("read_only_export", "export_unavailable", "journal_unavailable"):
            stub = _Stub()
            ap._Handler._fail(stub, 503, code)
            self.assertIn(ap.DETAIL[code], stub.sent[0][1])
        stub = _Stub()
        ap._Handler._fail(stub, 400, detail="%s. %s" % (ap.EMPTY_FIELD["reject"], ap.NOTHING))
        self.assertIn("Say why you are rejecting. Nothing was saved or decided.", stub.sent[0][1])


class HomeNeverReadsAllClear(unittest.TestCase):
    def test_nothing_loaded_means_unknown_everywhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            project.chmod(0o700)
            page = home.render_home(home.load(project, RUN, now=NOW), "")
        self.assertIn("Some information could not be loaded", page)
        self.assertIn("unknown due in the next 7 days", page)
        self.assertIn("Next 7 days: unknown", page)
        for all_clear in ("Nothing due in the next 7 days", "No requests waiting on agencies", "No agency events recorded"):
            self.assertNotIn(all_clear, page)
        for raw in ("(missing)", "(board_unreadable)", "(no_response_items)", "(timeline)"):
            self.assertNotIn(raw, page)

    def test_identical_status_lines_are_numbered(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            project.chmod(0o700)
            fixture(project)
            page = home.render_home(home.load(project, RUN, now=NOW), "")
        self.assertIn("Public status line (L1, 2026-11-04) (item 1 of 2)", page)
        self.assertIn("Public status line (L1, 2026-11-04) (item 2 of 2)", page)
        self.assertNotIn("Reply about fees (item", page)  # a row that is not repeated stays as it was


if __name__ == "__main__":
    unittest.main()
