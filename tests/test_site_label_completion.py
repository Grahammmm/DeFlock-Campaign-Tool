"""DF1109-16: the remaining fixed page strings of site.py and timeline.py go through labels; English is unchanged.

The English digests in tests/fixtures/site-label-completion/english-digests.json were captured from main before the
sweep (see its provenance block) and are compared with the current build; they are never regenerated from this code."""
import hashlib
import json
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from campaign_tool.content import ContentError, load_content
from campaign_tool.labels import DEFAULTS, PLACEHOLDER
from campaign_tool.site import build_site, meeting_outcome_html
from campaign_tool.timeline import render_timeline, timeline_events
from tests.test_site_labels import _write, bare, enrich, tree

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "fictional-campaign"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "site-label-completion" / "english-digests.json"
SOON, LATE = date(2026, 10, 1), date(2027, 1, 1)
MONTHS = "January February March April May June July August September October November December"


def extras(root):
    """A newsletter issue and two findings carrying a rule version, a duty and list/text exceptions."""
    content = root / "content"
    _write(content / "newsletters" / "2026-09-20.json", {"schema_version": 1, "sent_on": "2026-09-20", "title": "Zq issue",
                                                         "body_md": "Zq newsletter body."})
    base = json.loads((content / "findings" / "cedar-policy-posted.json").read_text(encoding="utf-8"))
    for n, exceptions in ((7, ["Zq exception one", "Zq exception two"]), (8, "Zq exception text")):
        sources = [dict(base["sources"][0], rule_id="ca-civ-1798.90.5-definitions")] if n == 7 else base["sources"]
        _write(content / "findings" / f"zq-law-{n}.json", dict(
            base, id=f"fnd_000000000000000{n}", slug=f"zq-law-{n}", title=f"Zq law {n}", summary=f"Zq law summary {n}",
            rule_version="zq-rule-v1", duty="Zq duty", exceptions=exceptions, corrections=[], limitations=[], sources=sources))


CASES = {"fictional": (lambda root: None, SOON), "rich": (lambda root: (enrich(root), extras(root)), SOON),
         "late": (lambda root: enrich(root, maplibre=True), LATE), "bare": (bare, SOON)}
# Keys added by DF1109-16 (every key of these families is new; months are month.01 .. month.12).
NEW_PREFIXES = ("timeline.", "month.", "methods.", "packet.", "newsletters.", "cameras.page_", "legal.")
NEW_KEYS = sorted([key for key in DEFAULTS if key.startswith(NEW_PREFIXES)]
                  + ["nav.timeline", "title.timeline", "title.timeline_description", "finding.share",
                     "footer.cameras", "footer.newsletters", "footer.methods", "outcome.official_record"])


def build(tmp, case, labels=None):
    setup, today = CASES[case]
    root = tmp / f"{case}-{len(list(tmp.iterdir()))}"
    shutil.copytree(EXAMPLE, root)
    setup(root)
    if labels is not None:
        _write(root / "content" / "labels.json", {"schema_version": 1, "labels": labels})
    build_site(root, today=today)
    return root


def digests(public):
    return {path: hashlib.sha256(data).hexdigest() for path, data in tree(public).items()}


def capture(tmp):
    """The fixture body: every case's whole-tree digests (used once, on main before the sweep)."""
    return {case: digests(build(tmp, case) / "public") for case in CASES}


class TmpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)


class EnglishTreeTest(TmpTest):
    def test_english_tree_matches_presweep_base(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(sorted(fixture["cases"]), sorted(CASES))
        for case in CASES:
            for labels in (None, {}, {key: DEFAULTS[key] for key in NEW_KEYS}):
                with self.subTest(case=case, labels=None if labels is None else len(labels)):
                    got = digests(build(self.tmp, case, labels) / "public")
                    self.assertEqual(sorted(got), sorted(fixture["cases"][case]))
                    self.assertEqual([p for p in got if got[p] != fixture["cases"][case][p]], [])


class NewLabelsTest(TmpTest):
    def test_new_keys_are_scalar_english_text(self):
        self.assertGreaterEqual(len(NEW_KEYS), 85)
        self.assertTrue(all(isinstance(DEFAULTS[key], str) and DEFAULTS[key].strip() for key in NEW_KEYS))
        self.assertEqual(" ".join(DEFAULTS[f"month.{n:02}"] for n in range(1, 13)), MONTHS)

    def test_every_new_key_reaches_its_owned_ui_nodes(self):
        markers = {key: f"X{key}X" + "".join(" {" + n + "}" for n in PLACEHOLDER.findall(DEFAULTS[key])) for key in NEW_KEYS}
        seen, pages = set(), {}
        for case in ("rich", "late", "bare"):
            public = build(self.tmp, case, markers) / "public"
            for path, data in tree(public).items():
                if path.endswith(".html"):
                    page = data.decode()
                    pages[f"{case}:{path}"] = page
                    seen |= {key for key in NEW_KEYS if f"X{key}X" in page}
        # timeline.*/month.* keys the fixture's dates and kinds do not reach: test_timeline_all_kinds_* below observes them.
        elsewhere = {"outcome.official_record"} | {key for key in NEW_KEYS if key.startswith(("timeline.", "month."))}
        self.assertEqual(sorted(set(NEW_KEYS) - seen - elsewhere), [])
        self.assertIn("packet.source_rule", seen)
        # No English default of a new key is left on any page; one-word defaults by their exact markup.
        markup = ['>Timeline</a>', '<title>Timeline', '>Mapped points<', '>Exceptions:', '<h3>Confidence</h3>',
                  '<h3>Classifications</h3>', '>Share this finding<'] + [f'">{month} 20' for month in MONTHS.split()]
        for name, page in pages.items():
            for key in NEW_KEYS:
                for chunk in PLACEHOLDER.split(DEFAULTS[key]):
                    if len(chunk.split()) >= 3:
                        self.assertFalse(chunk.strip() in page, f"{name}: {key}: {chunk.strip()}")
            for needle in markup:
                self.assertFalse(needle in page, f"{name}: {needle}")
        timeline = pages["rich:timeline.html"]
        self.assertRegex(timeline, r'<h3><time datetime="2026-09">Xtimeline\.month_yearX Xmonth\.09X 2026</time></h3>')
        self.assertIn("Xtimeline.event_finding_publishedX Zq law 7", timeline)
        self.assertIn("Xtimeline.event_coverageX Zq outlet news", timeline)
        self.assertNotIn("In the news:", timeline)

    def test_outcome_official_record_label(self):
        meeting = {"outcome": {"result": "approved", "on": "2026-09-01", "source_url": "https://example.invalid/m",
                               "verified_by": "Zq"}}
        english = meeting_outcome_html(meeting)
        self.assertIn('rel="noopener noreferrer">Official record</a>', english)
        labels = dict(DEFAULTS, **{"outcome.official_record": "Acta & oficial"})
        translated = meeting_outcome_html(meeting, lambda key, **values: labels[key].replace("&", "&amp;").format(**values))
        self.assertEqual(translated, english.replace(">Official record<", ">Acta &amp; oficial<"))

    def test_months_all_twelve_and_locale_unreachable(self):
        labels = dict(DEFAULTS, **{f"month.{n:02}": f"M{n}" for n in range(1, 13)}, **{"timeline.month_year": "{year} {month}"})
        events = [{"date": f"20{25 + n % 2}-{n:02}-15", "kind": "meeting", "text": f"E{n}", "href": "/m.html"}
                  for n in range(1, 13)]
        events.sort(key=lambda event: event["date"], reverse=True)
        refuse = mock.Mock(side_effect=AssertionError("locale-dependent formatting called"))
        with mock.patch("time.strftime", refuse), mock.patch("locale.setlocale", refuse):
            html = render_timeline(SimpleNamespace(labels=labels), events)
            english = render_timeline(SimpleNamespace(labels=DEFAULTS), events)
        refuse.assert_not_called()
        for n in range(1, 13):
            year = 2025 + n % 2
            self.assertIn(f'<h3><time datetime="{year}-{n:02}">{year} M{n}</time></h3>', html)
            self.assertIn(f'<h3><time datetime="{year}-{n:02}">{MONTHS.split()[n - 1]} {year}</time></h3>', english)
        self.assertEqual(html.count("<h3>"), 12)

    def test_timeline_all_kinds_and_conditional_lead_matrix(self):
        today = date(2026, 10, 1)
        model = {
            "agencies": [{"agency_id": "a1", "name": "Zq {agency} & co", "slug": "zq",
                          "actions": [{"on": "2026-01-05", "kind": "policy_adopted", "as_stated": "Zq stated {x}",
                                       "source_url": "https://example.invalid/a"}],
                          "asked": [{"asked_on": "2026-02-01", "topic": "Zq topic", "updated_on": "2026-03-01",
                                     "state": "acknowledged"}]}],
            "sources": [{"received_at": "2026-04-01T00:00:00Z", "agency_id": "gone", "title": "Zq record", "fragment": "f1"}],
            "findings": [{"slug": "zq", "published_at": "2026-05-01T00:00:00Z", "title": "Zq finding",
                          "corrections": [{"date": "2026-06-01"}, {"date": "2026-11-01"}], "updates": [{"date": "2026-07-01"}]}],
            "meetings": [{"id": "m1", "starts_at": "2026-08-01T18:00:00-07:00", "body": "Zq board",
                          "outcome": {"on": "2026-08-02", "result": "approved", "source_url": "https://example.invalid/m"}},
                         {"id": "m2", "starts_at": "2026-12-01T18:00:00-07:00", "body": "Zq later"},
                         {"id": "m3", "starts_at": "2026-09-09T18:00:00-07:00", "body": "Zq gone", "status": "cancelled"}],
            "statements": [{"agency_id": "a1", "said_on": "2026-09-01", "official": "Zq official",
                            "source_url": "https://example.invalid/s"}],
            "coverage": [{"published_on": "2026-10-01", "outlet": "Zq outlet"}]}
        keys = [key for key in NEW_KEYS if key.startswith(("timeline.", "month."))]
        markers = dict(DEFAULTS, **{key: f"X{key}X" + "".join(" {" + n + "}" for n in PLACEHOLDER.findall(DEFAULTS[key]))
                                    for key in keys})
        english_events = timeline_events(model, today)
        english = render_timeline(SimpleNamespace(labels=DEFAULTS), english_events)
        marked_events = timeline_events(dict(model, labels=markers), today)
        self.assertEqual([(ev["date"], ev["kind"], ev["href"]) for ev in marked_events],
                         [(ev["date"], ev["kind"], ev["href"]) for ev in english_events])  # same events, same order
        site = SimpleNamespace(labels=markers)
        two = [ev for ev in marked_events if ev["kind"] != "coverage"]
        html = render_timeline(site, marked_events) + render_timeline(site, two) + render_timeline(site, [])
        self.assertEqual([key for key in keys if f"X{key}X" not in html], [])
        self.assertNotIn("Zq gone", html)
        self.assertIn("Xtimeline.event_agency_actionX Zq {agency} &amp; co", html)  # content braces inserted once
        self.assertIn("Zq stated {x}", html)
        for key in keys:
            for chunk in PLACEHOLDER.split(DEFAULTS[key]):
                if len(chunk.split()) >= 2:
                    self.assertNotIn(chunk.strip(), html, key)
        self.assertIn("<p>Dates from published sources, findings, corrections, records requests, meetings and recorded "
                      "decisions. Agency actions are transcribed from their cited official sources. Also shown: finding "
                      "updates, public statements and press coverage.</p>", english)
        self.assertIn("Also shown: finding updates and public statements.",
                      render_timeline(SimpleNamespace(labels=DEFAULTS), [ev for ev in english_events if ev["kind"] != "coverage"]))
        self.assertIn("Records received from an agency: Zq record", english)
        self.assertIn("Scheduled meeting: Zq later", english)
        self.assertIn("Decision at Zq board: Approved", english)

    def test_labels_escape_once_and_do_not_translate_content(self):
        labels = {"timeline.heading": 'Fish & "Chips" it\'s', "timeline.event_finding_published": "Hallazgo {title} & co",
                  "packet.intro": "Al {date}: {count} & más", "footer.methods": "Cómo & leer"}
        root = build(self.tmp, "rich", labels)
        timeline = (root / "public" / "timeline.html").read_text(encoding="utf-8")
        self.assertIn("<h2>Fish &amp; &quot;Chips&quot; it's</h2>", timeline)
        self.assertIn("Hallazgo Zq law 7 &amp; co", timeline)
        self.assertNotIn("&amp;amp;", timeline)
        packet = (root / "public" / "findings" / "packet-all.html").read_text(encoding="utf-8")
        self.assertRegex(packet, r"<p>Al 2026-10-01: \d+ &amp; más</p>")
        self.assertIn('<a href="/methods.html">Cómo &amp; leer</a>', packet)
        self.assertIn("<li>Zq exception one</li>", (root / "public" / "findings" / "zq-law-7.html").read_text(encoding="utf-8"))

    def test_new_label_refusal_is_atomic(self):
        root = build(self.tmp, "rich")
        before = tree(root / "public")
        for labels, needle in (({"timeline.month_year": "{month} {day}"}, "placeholder {day} is not allowed"),
                               ({"month.01": "<b>enero</b>"}, "must not contain <"),
                               ({"month.13": "x"}, "unknown label"), ({"packet.intro": "{date"}, "unpaired brace")):
            with self.subTest(labels=labels):
                _write(root / "content" / "labels.json", {"schema_version": 1, "labels": labels})
                with self.assertRaises(ContentError) as caught:
                    build_site(root, today=SOON)
                self.assertIn(needle, str(caught.exception))
                self.assertEqual(tree(root / "public"), before)
        with self.assertRaises(ContentError):
            load_content(root)


if __name__ == "__main__":
    unittest.main()
