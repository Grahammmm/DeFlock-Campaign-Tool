"""DF2007-09 (CON-SITEPR-1): Home and /site-prs over a pinned export with the operator-captured sidecar beside it."""
import inspect
import json
import os
from datetime import timedelta

from campaign_tool.records import site_prs
from tests.records.test_console_export import tree_state
from tests.records.test_console_snapshot import Panel
from tests.records.test_site_prs import NOW, REPO, pr


class SitePrPages(Panel):
    def setUp(self):
        super().setUp()
        self.make_export()
        self.clock = [NOW.timestamp()]
        self.capture = self.tmp / "capture.json"

    def up(self, **kw):
        super().up(access_clock=lambda: self.clock[0], **kw)

    def at(self, delta):
        self.clock[0] = (NOW + delta).timestamp()

    def snapshot(self, rows):
        self.capture.write_text(json.dumps(rows))
        site_prs.snapshot(str(self.capture), str(self.root), REPO, NOW)

    def test_valid_snapshot_renders_escaped_with_ages_and_home_links_it(self):
        self.snapshot([pr(7, title="Add <script>alert(1)</script> page"), pr(9, updatedAt="2026-11-16T00:00:00Z")])
        self.up()
        self.at(timedelta(hours=5, minutes=30))
        page = self.get("/site-prs")
        self.assertIn("2 captured open drafts", page)
        self.assertIn("captured 5 hours ago", page)
        self.assertIn("Add &lt;script&gt;alert(1)&lt;/script&gt; page", page)
        self.assertNotIn("<script>alert", page)
        self.assertIn('href="https://github.com/%s/pull/7"' % REPO, page)
        self.assertIn("29 hours", page)  # captured updatedAt 2026-11-14T12:00Z, now 17:30 the next day
        self.assertIn("update age unknown (clock ahead)", page)
        self.assertNotIn("Stale", page)
        for text in ("<form", "<button", "<input", "<script"):
            self.assertNotIn(text, page)
        home = self.get("/")
        self.assertIn('<a href="/site-prs">Site draft PRs</a>: 2 captured open drafts, captured 5 hours ago.', home)
        self.assertNotIn("CON-SITEPR-1", home)

    def test_stale_banner_appears_only_after_24_hours(self):
        self.snapshot([pr()])
        self.up()
        for delta, stale in ((timedelta(hours=23, minutes=59, seconds=59), False), (timedelta(hours=24), False),
                             (timedelta(hours=24, seconds=1), True), (timedelta(days=3), True)):
            self.at(delta)
            page, home = self.get("/site-prs"), self.get("/")
            self.assertEqual("Stale: this snapshot was captured more than 24 hours ago" in page, stale, delta)
            self.assertEqual("Stale: older than 24 hours." in home, stale, delta)
            self.assertIn('<a href="/site-prs">Site draft PRs</a>', home)  # a stale nonempty snapshot still links

    def test_missing_or_bad_sidecar_is_unavailable_never_zero_and_home_stays_healthy(self):
        self.up()
        for path in ("/site-prs", "/"):
            page = self.get(path)
            self.assertIn("Site draft PR snapshot unavailable", page)
            self.assertNotIn("no open drafts", page)
            self.assertNotIn("0 captured", page)
        self.assertIn("Follow-ups 1", self.get("/"))  # independent healthy sources stay visible
        (self.root / "site-prs.json").write_text("{broken")
        self.assertIn("Site draft PR snapshot unavailable", self.get("/site-prs"))
        (self.root / "site-prs.json").unlink()
        self.snapshot([])
        page = self.get("/site-prs")
        self.assertIn("no open drafts captured", page)
        self.assertNotIn("unavailable", page)
        self.assertIn("captured no open drafts", self.get("/"))

    def test_the_sidecar_never_changes_the_pinned_export_and_the_page_has_no_mutating_route(self):
        self.snapshot([pr()])
        self.up()
        before = tree_state(self.root)
        text = inspect.getsource(site_prs)  # no transport or process machinery exists in the module at all
        for word in ("socket", "subprocess", "urllib.request", "http.client", "os.system"):
            self.assertNotIn(word, text)
        self.get("/site-prs")
        self.get("/")
        self.assertEqual(tree_state(self.root), before)
        self.assertGreaterEqual(self.request("POST", "/site-prs", {"x": "1"})[0], 400)
        self.get("/site-prs?x=1", status=400)
        self.assertEqual(tree_state(self.root), before)
        self.assertFalse(os.path.exists(self.root / "current" / "site-prs.json"))

    def test_a_future_capture_is_unavailable(self):
        self.snapshot([pr()])
        self.up()
        self.at(timedelta(hours=-1))
        self.assertIn("Site draft PR snapshot unavailable", self.get("/site-prs"))
