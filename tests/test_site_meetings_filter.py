"""DF1622-09: meetings.html governing-body filter and meetings/filter.js.

Synthetic content only: temporary copies of the fictional example; no network, no private reads. The behaviour of the
generated script itself runs in tests/site-meetings-filter.test.mjs.
"""
import hashlib
import html as html_lib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import site as site_module
from campaign_tool.labels import DEFAULTS, JS_DEFAULTS, MEETINGS_JS_DEFAULTS
from campaign_tool.site import MEETINGS_FILTER_ASSET, build_site
from tests.test_site_build import EXPECTED_CSP

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 11, 1)
AGENCY = "ca-cedar-city-council"
# Digest of every public file captured from detached main d2470763 (fixed-date fixtures below); mixed drops the two
# files this feature may change. Recapture from a new main, never from this feature's output.
ROOTS = {"empty": "f174f11766b31f1fe42b6a22d9c0d308f75e0df6c38e45a7b1e6f83c8d1dbe30",  # pragma: allowlist secret
         "one-body": "f2a31292279262074fe50e447814b147ea78744d8b310a71165c39e74cdbcfd0",  # pragma: allowlist secret
         "mixed": "0b9321d1445080e45c9887d5af413fe7d7c4da1c352dbba7ada7c615bbbf5292"}  # pragma: allowlist secret
MIXED = (("alpha-up", "Council A", "2026-11-10", None), ("beta-up", "Council B", "2026-11-11", None),
         ("alpha-past", "Council A", "2026-10-01", None), ("beta-cancel", "Council B", "2026-11-12", "cancelled"))


class MeetingsFilterCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-meetings-filter-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.meetings = self.root / "content" / "meetings"
        self.public = self.root / "public"

    def add(self, meeting_id, body, day="2026-11-10", status=None, agency: str | None = AGENCY):
        record = {"id": meeting_id, "body": body, "starts_at": f"{day}T18:00:00-08:00"}
        if agency:
            record["agency_id"] = agency
        if status:
            record["status"] = status
        (self.meetings / f"{meeting_id}.json").write_text(json.dumps(record), encoding="utf-8")

    def clear(self):
        for path in self.meetings.glob("*.json"):
            path.unlink()

    def build(self):
        return build_site(self.root, today=TODAY)

    def page(self, name="meetings.html"):
        return (self.public / name).read_text(encoding="utf-8")

    def enabled(self):
        self.build()
        return (self.public / "meetings" / "filter.js").exists()

    def digest(self, drop=()):
        files = {p.relative_to(self.public).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(self.public.rglob("*")) if p.is_file()}
        for name in drop:
            files.pop(name)
        return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def slugs(self):
        site = site_module.Site.__new__(site_module.Site)
        site.model = {"meetings": [{"body": b} for b in self.bodies]}
        return site.meeting_body_slugs()

    def test_threshold_counts_staged_bodies_only(self):
        self.assertFalse(self.enabled())  # the example's one body
        self.clear()
        self.assertFalse(self.enabled())  # none
        self.add("a", "One body", agency="ca-cedar-police")
        self.add("b", "One body", agency=AGENCY)  # one body across two agencies
        self.assertFalse(self.enabled())
        self.add("c", "Second body", agency=None)  # no agency_id still counts
        self.assertTrue(self.enabled())
        self.clear()
        self.add("a", "Body A", agency=AGENCY)
        self.add("b", "Body B", agency=AGENCY)  # two bodies, one agency
        self.assertTrue(self.enabled())
        self.clear()
        self.add("a", "Body A", day="2026-11-10")
        self.add("b", "Body B", day="2026-11-12", status="cancelled")  # a cancelled-only second body
        self.assertTrue(self.enabled())
        self.clear()
        self.add("a", "Body A", day="2026-11-10")
        self.add("b", "Body B", day="2025-01-01")  # a past-only second body
        self.assertTrue(self.enabled())

    def test_slug_sort_collision_and_escaping(self):
        names = ["Zeta", "alpha", "Alpha", "A!pha", "a-pha", "all", "All", "日本", "Ünï", "R&D \"x\"", "alpha-2"]
        self.bodies = names
        forward = self.slugs()
        self.bodies = list(reversed(names))
        self.assertEqual(self.slugs(), forward)  # input order never matters
        self.assertEqual(len(set(forward.values())), len(names))  # every slug distinct
        self.assertNotIn("all", forward.values())  # reserved for the select's first option
        self.assertTrue(all(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", slug) for slug in forward.values()))
        self.assertEqual(forward["Alpha"], "alpha")
        self.assertEqual(forward["alpha"], "alpha-2")
        self.assertEqual(forward["日本"], "body")
        self.assertEqual(forward["Ünï"], "n")
        self.bodies = ["x", "x", "x"]
        self.assertEqual(self.slugs(), {})  # the same exact name is one body
        self.clear()
        for i, name in enumerate(["R&D \"x\"", "Plain"]):
            self.add(f"m{i}", name)
        self.build()
        page = self.page()
        self.assertIn("R&amp;D &quot;x&quot;", page.split('id="meetings-filter-body"')[1].split("</select>")[0])
        options = re.findall(r'<option value="([^"]*)">([^<]*)</option>', page.split('id="meetings-filter-body"')[1])
        self.assertEqual([html_lib.unescape(t) for _, t in options], ["All governing bodies", "Plain", "R&D \"x\""])
        item_slugs = re.findall(r'<div class="meeting-filter-item" data-body="([^"]*)">', page)
        self.assertEqual(sorted(item_slugs), sorted(v for v, _ in options[1:]))

    def test_complete_nojs_lists_and_unchanged_article_bytes(self):
        self.clear()
        for meeting_id, body, day, status in MIXED:
            self.add(meeting_id, body, day, status)
        self.build()
        page = self.page()
        upcoming = re.search(r'id="meetings-filter-upcoming">(.*?)</div></section>', page, re.S).group(1)
        past = re.search(r'id="meetings-filter-past">(.*?)</div></section>', page, re.S).group(1)
        self.assertEqual(re.findall(r'<article id="meeting-([^"]+)"', upcoming), ["alpha-up", "beta-up"])
        self.assertEqual(re.findall(r'<article id="meeting-([^"]+)"', past), ["beta-cancel", "alpha-past"])
        self.assertIn("Cancelled", past)  # cancelled stays listed and marked
        # every article is the unchanged helper output: stripping the wrappers leaves the single-body markup
        for wrapped in re.findall(r'<div class="meeting-filter-item" data-body="[^"]*">(<article.*?</article>)</div>', page, re.S):
            self.assertIn(wrapped, page)
        self.assertEqual(len(re.findall(r'<div class="meeting-filter-item"', page)), 4)
        self.assertNotIn("style=", page.split('id="meetings-filter"')[1].split("</form>")[0])
        for hidden in ('id="meetings-filter"', 'id="meetings-filter-upcoming-count"', 'id="meetings-filter-past-count"',
                       'id="meetings-filter-upcoming-empty"', 'id="meetings-filter-past-empty"'):
            self.assertRegex(page, hidden + r"[^>]*\shidden>")
        self.assertIn("Showing 2 of 2", page)
        self.assertIn("Showing 2 of 2", page.split('id="meetings-filter-past-count"')[1].split("</p>")[0])
        self.assertNotRegex(re.search(r'<div class="meeting-filter-item"[^>]*>', page).group(0), "hidden")

    def test_regular_meetings_and_no_past_list(self):
        self.clear()
        self.add("up-a", "Body A", "2026-11-10")
        self.add("up-b", "Body B", "2026-11-11")
        self.build()
        page = self.page()
        self.assertNotIn("past-meetings", page)  # no past list is invented
        self.assertNotIn("meetings-filter-past", page)
        self.assertIn("meetings-filter-upcoming", page)
        self.clear()
        self.add("old-a", "Body A", "2020-01-01")
        self.add("old-b", "Body B", "2020-01-02")
        self.build()
        page = self.page()
        self.assertIn("No upcoming", page)  # the existing empty wording stays, inside the list
        self.assertIn("Showing 0 of 0", page)
        self.assertIn("meetings-filter-past", page)

    def test_main_manifest_parity_and_repeat_build(self):
        for kind in ("empty", "one-body", "mixed"):
            with self.subTest(kind):
                shutil.rmtree(self.root / "public", ignore_errors=True)
                self.clear()
                if kind == "one-body":
                    shutil.copy(EXAMPLE / "content" / "meetings" / "2026-10-cedar-council.json", self.meetings)
                if kind == "mixed":
                    shutil.copy(EXAMPLE / "content" / "meetings" / "2026-10-cedar-council.json", self.meetings)
                    for meeting_id, body, day, status in MIXED:
                        self.add(meeting_id, body, day, status)
                self.build()
                drop = ("meetings.html", "meetings/filter.js") if kind == "mixed" else ()
                self.assertEqual((self.public / "meetings" / "filter.js").exists(), kind == "mixed")
                self.assertEqual(self.digest(drop), ROOTS[kind])
                if kind == "mixed":
                    first = self.digest()
                    self.build()
                    self.assertEqual(self.digest(), first)  # repeat build is byte-identical

    def test_conditional_asset_stale_cleanup_and_collision(self):
        self.add("beta", "Second body")
        self.build()
        self.assertEqual((self.public / "meetings" / "filter.js").read_bytes(), MEETINGS_FILTER_ASSET.read_bytes())
        self.assertEqual(self.page().count("/meetings/filter.js"), 1)
        for other in ("index.html", "sources.html", "agencies.html", "meetings/beta.html"):
            self.assertNotIn("meetings/filter.js", self.page(other), other)
        self.assertIn("Content-Security-Policy: " + EXPECTED_CSP + "\n", self.page("_headers"))
        self.assertNotIn("unsafe-inline", self.page("_headers"))
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(self.root), "--check"],
                                capture_output=True, text=True, cwd=REPO_ROOT, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (self.meetings / "beta.json").unlink()  # back below two bodies
        with mock.patch.object(site_module, "MEETINGS_FILTER_ASSET") as asset:
            self.build()
            asset.read_text.assert_not_called()  # the template is only read when enabled
        self.assertFalse((self.public / "meetings" / "filter.js").exists())  # stale script removed
        self.assertTrue((self.public / "meetings" / "cedar-council-2026-10.html").exists() or any((self.public / "meetings").glob("*.html")))
        self.assertNotIn("filter.js", self.page())

    def test_page_local_labels_and_placeholder_validation(self):
        self.assertEqual(set(MEETINGS_JS_DEFAULTS), {"js.meetings_label", "js.meetings_body", "js.meetings_all_bodies",
                                                    "js.meetings_count", "js.meetings_empty"})
        self.assertTrue(set(MEETINGS_JS_DEFAULTS) <= set(DEFAULTS))
        self.assertFalse(set(MEETINGS_JS_DEFAULTS) & set(JS_DEFAULTS))  # no global site-data.js transport
        self.add("beta", "Second body")
        self.build()
        base, base_script = self.page(), self.page("meetings/filter.js")
        before = self.digest(("meetings.html",))
        labels = {"js.meetings_label": "Filtrar", "js.meetings_body": "Órgano", "js.meetings_all_bodies": "Todos",
                  "js.meetings_count": "{total} / {total} $1 \\ \"q\" '", "js.meetings_empty": "Ninguna"}
        (self.root / "content" / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": labels}), encoding="utf-8")
        self.build()
        page = self.page()
        for text in ("Filtrar", "Órgano", "Todos", "Ninguna"):
            self.assertIn(text, page)
        self.assertIn('data-count-template="{total} / {total} $1 \\ &quot;q&quot; \'"', page)
        self.assertNotIn("site-data.js", page)
        self.assertEqual(self.page("meetings/filter.js"), base_script)
        self.assertNotEqual(page, base)
        self.assertEqual(self.digest(("meetings.html",)), before)  # other files keep their bytes under the override
        for bad in ({"js.meetings_count": "{shown} of {nope}"}, {"js.meetings_empty": "{shown}"}):
            (self.root / "content" / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": bad}), encoding="utf-8")
            with self.assertRaises(Exception):
                self.build()
        # below the threshold the overrides change nothing
        (self.meetings / "beta.json").unlink()
        (self.root / "content" / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": labels}), encoding="utf-8")
        self.build()
        self.assertNotIn("Filtrar", self.page())


if __name__ == "__main__":
    unittest.main()
