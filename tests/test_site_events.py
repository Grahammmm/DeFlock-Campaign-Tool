"""DF1107-08: optional content/events.json -> /events.html, a next-event home notice, footer link and sitemap entry.

Builds temporary copies of the fictional example with synthetic events; sockets are blocked, nothing is fetched.
"""
import json
import os
import re
import shutil
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from campaign_tool.content import STARTER_SLUGS, ContentError, load_content
from campaign_tool.content_check import content_problems, problem_record
from campaign_tool.content_events import build_cutoff, load_events, parse_moment, partition, read_events
from campaign_tool.site import GENERATED_TOP_LEVEL, build_site
from campaign_tool.site_check import check_accessibility, check_links

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 10, 1)  # the cutoff is 2026-10-01T00:00:00Z
NO_SOCKETS = mock.patch("socket.socket", side_effect=AssertionError("network access attempted"))
FOOTER_EVENTS = '<p><a href="/events.html">Campaign events</a></p>'


def event(number=1, **changes):
    base = {"id": f"forum-{number}", "title": f"Teach-in {number}", "starts_at": "2026-10-10T18:00:00-07:00",
            "ends_at": "2026-10-10T20:00:00-07:00", "venue": f"Cedar Library room {number}",
            "description_md": f"Synthetic gathering {number}."}
    base.update(changes)
    return {key: value for key, value in base.items() if value is not None}


class EventsCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-events-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content, self.public = self.root / "content", self.root / "public"
        self.path = self.content / "events.json"
        NO_SOCKETS.start()
        self.addCleanup(NO_SOCKETS.stop)

    def put(self, value, name="events.json"):
        path = self.content / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8"))
        return path

    def tree(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in sorted(self.public.rglob("*")) if p.is_file()}

    def build(self):
        build_site(self.root, today=TODAY)
        return self.tree()

    def refused(self, value, pattern):
        """build refuses with ``pattern`` and public/ keeps its bytes; content-check lists the same line."""
        before = self.tree() if self.public.exists() else self.build()
        self.put(value)
        with self.assertRaisesRegex(ContentError, pattern) as caught:
            build_site(self.root, today=TODAY)
        self.assertEqual(self.tree(), before, "a refused build changed public/")
        self.assertIn(str(caught.exception), content_problems(self.root))
        return str(caught.exception)


class RenderingTests(EventsCase):
    def test_partition_order_anchors_home_footer_sitemap(self):
        past = event(1, id="past-b", starts_at="2026-09-01T10:00:00+02:00", ends_at="2026-09-01T11:00:00+02:00")
        past2 = event(2, id="past-a", starts_at="2026-09-20T10:00:00Z", ends_at="2026-09-20T11:00:00Z")
        later = event(3, id="later", starts_at="2026-10-20T09:00:00-07:00", ends_at="2026-10-20T10:00:00-07:00")
        online = event(4, id="online-first", venue=None, online=True, starts_at="2026-10-05T12:00:00Z",
                       ends_at="2026-10-05T13:30:00Z", url="https://example.com/register?x=1&y=2",
                       accessibility_note="Live captions provided.",
                       description_md="Hello **world** and [a link](https://example.com/a).\n\n- one\n- two")
        self.put([past, later, past2, online])
        files = self.build()
        page = files["events.html"].decode()
        self.assertEqual(re.findall(r'<article id="(event-[^"]+)"', page), ["event-online-first", "event-later", "event-past-a", "event-past-b"])
        self.assertLess(page.index('id="upcoming-events"'), page.index('id="past-events"'))
        self.assertLess(page.index("event-later"), page.index('id="past-events"'))
        self.assertGreater(page.index("event-past-a"), page.index('id="past-events"'))
        self.assertIn("<title>Campaign events", page)
        self.assertEqual(len(re.findall(r"<h1", page)), 1)
        ids = re.findall(r'\bid="([^"]+)"', page)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn('<time datetime="2026-09-01T10:00:00+02:00">2026-09-01T10:00:00+02:00</time>', page)  # original offset kept
        self.assertIn("Online", page)
        self.assertNotIn("Cedar Library room 1<", page.split('id="past-events"')[0])  # past-b's venue only in the past section
        self.assertIn("<strong>world</strong>", page)
        self.assertIn('href="https://example.com/register?x=1&amp;y=2" rel="noopener noreferrer">Details and registration</a>', page)
        self.assertIn("Accessibility: Live captions provided.", page)
        self.assertIn("not governing-body meetings", page)
        self.assertIn("current as of 2026-10-01", page)
        for forbidden in ("comment", "outcome", "kit", "<script"):
            self.assertNotIn(forbidden, page.lower().replace("noopener noreferrer", ""), forbidden)
        home = files["index.html"].decode()
        notice = re.findall(r'<section class="section" id="next-event">.*?</section>', home, re.S)
        self.assertEqual(len(notice), 1)
        self.assertEqual(notice[0].count('<a href="/events.html#event-'), 1)
        self.assertIn('<a href="/events.html#event-online-first">Teach-in 4</a>', home)
        self.assertNotIn("event-later", home)
        self.assertEqual(home.count("2026-10-05T12:00:00Z"), 2)  # start and end of the one event shown, both times
        for name, data in files.items():
            if name.endswith(".html"):
                footer = re.search(r'<footer class="site-footer">(.*?)</footer>', data.decode(), re.S)
                self.assertTrue(footer and footer.group(1).endswith(FOOTER_EVENTS), name)
        sitemap = files["sitemap.xml"].decode()
        self.assertEqual(re.findall(r"<loc>([^<]*events[^<]*)</loc>", sitemap), ["https://cedar-records.example.invalid/events.html"])
        self.assertNotIn("#event-", sitemap)
        self.assertEqual(check_links(self.public), [])
        self.assertEqual(check_accessibility(self.public), [])
        self.assertFalse((self.public / "events.json").exists())
        self.assertEqual(self.build(), files)  # deterministic

    def test_events_stay_apart_from_governing_body_meetings(self):
        without = self.build()
        self.put([event()])
        files = self.build()
        for name in ("meetings.ics", "meetings.xml", "data/meetings.json", "data/meetings.csv"):
            self.assertEqual(files[name], without[name], name)
        meetings = files["meetings.html"].decode()
        self.assertNotIn("Teach-in", meetings)
        self.assertNotIn("event-forum-1", meetings)
        self.assertNotIn("Teach-in", files["meetings.ics"].decode())
        self.assertNotIn("Teach-in", files["meetings.xml"].decode())
        self.assertFalse([name for name in files if name.startswith("meetings/") and "forum-1" in name])

    def test_cutoff_is_the_build_day_in_utc_and_start_based(self):
        exact = event(1, id="exact", starts_at="2026-10-01T00:00:00Z", ends_at="2026-10-01T01:00:00Z")
        zoned = event(2, id="zoned", starts_at="2026-09-30T20:00:00-04:00", ends_at="2026-09-30T21:00:00-04:00")  # same instant
        before = event(3, id="before", starts_at="2026-09-30T23:59:59Z", ends_at="2026-10-02T00:00:00Z")  # still underway
        after = event(4, id="after", starts_at="2026-10-01T00:00:01Z", ends_at="2026-10-01T00:00:01Z")  # equal endpoints
        self.put([before, after, zoned, exact])
        page = self.build()["events.html"].decode()
        upcoming, past = page.split('id="past-events"')
        self.assertEqual(re.findall(r'<article id="event-([^"]+)"', upcoming), ["exact", "zoned", "after"])  # tie: id order
        self.assertEqual(re.findall(r'<article id="event-([^"]+)"', past), ["before"])
        self.assertEqual(re.findall(r'href="/events.html#event-([^"]+)"', self.tree()["index.html"].decode()), ["exact"])

    def test_past_only_has_page_footer_sitemap_but_no_home_notice(self):
        self.put([event(1, starts_at="2026-01-10T18:00:00Z", ends_at="2026-01-10T19:00:00Z")])
        files = self.build()
        self.assertIn("No upcoming events are listed.", files["events.html"].decode())
        self.assertNotIn("next-event", files["index.html"].decode())
        self.assertIn(FOOTER_EVENTS, files["index.html"].decode())
        self.assertIn("/events.html</loc>", files["sitemap.xml"].decode())

    def test_upcoming_only_shows_an_empty_past_notice(self):
        self.put([event(1)])
        self.assertIn("No past events are listed.", self.build()["events.html"].decode())

    def test_no_base_url_means_no_sitemap_but_the_page_and_link_work(self):
        site = json.loads((self.content / "site.json").read_text(encoding="utf-8"))
        site.pop("base_url")
        self.put(site, "site.json")
        self.put([event()])
        files = self.build()
        self.assertNotIn("sitemap.xml", files)
        self.assertIn(FOOTER_EVENTS, files["index.html"].decode())
        self.assertNotIn("canonical", files["events.html"].decode())

    def test_text_is_escaped_and_unsafe_markdown_stays_inert(self):
        self.put([event(1, title="Fish & Chips \"night\"", venue="Hall A & B",
                        description_md="[bad](javascript:alert(1)) and [ok](https://example.com/x) *em*",
                        accessibility_note="Ramp & lift")])
        page = self.build()["events.html"].decode()
        self.assertIn("Fish &amp; Chips", page)
        self.assertIn("Hall A &amp; B", page)
        self.assertIn("Ramp &amp; lift", page)
        self.assertNotIn('href="javascript', page)
        self.assertIn('href="https://example.com/x"', page)

    def test_labels_come_from_the_label_table(self):
        self.put({"schema_version": 1, "labels": {"events.upcoming": "Coming up", "footer.events": "Gatherings"}}, "labels.json")
        self.put([event()])
        files = self.build()
        self.assertIn("<h2>Coming up</h2>", files["events.html"].decode())
        self.assertIn('<a href="/events.html">Gatherings</a>', files["index.html"].decode())


class AbsenceTests(EventsCase):
    def test_no_file_and_empty_array_are_byte_identical_and_removal_restores_bytes(self):
        absent = self.build()
        self.assertNotIn("events.html", absent)
        self.assertNotIn("/events.html", b"".join(absent.values()).decode("utf-8", "replace"))
        self.put([])
        self.assertEqual(content_problems(self.root), [])
        self.assertEqual(self.build(), absent)
        self.put([event()])
        enabled = self.build()
        self.assertIn("events.html", enabled)
        self.assertNotEqual(enabled["index.html"], absent["index.html"])
        self.path.unlink()
        self.assertEqual(self.build(), absent)  # stale page, footer, home notice and sitemap entry all gone
        self.assertFalse((self.public / "events.html").exists())

    def test_reader_returns_none_for_no_file_and_empty_list_for_empty_file(self):
        self.assertIsNone(read_events(self.root))
        self.put([])
        self.assertEqual(read_events(self.root), [])
        self.put([event(1), event(2)])
        self.assertEqual([item["id"] for item in read_events(self.root)], ["forum-1", "forum-2"])
        self.put([])
        self.assertNotIn("events", load_content(self.root, TODAY))

    def test_reserved_names_hold_without_a_file(self):
        self.assertIn("events", STARTER_SLUGS)
        self.assertIn("events.html", GENERATED_TOP_LEVEL)
        self.put({"title": "Mine", "body_md": "Text"}, "pages/events.json")
        with self.assertRaisesRegex(ContentError, "events"):
            build_site(self.root, today=TODAY)
        self.assertTrue(any("events" in problem for problem in content_problems(self.root)))
        (self.content / "pages" / "events.json").unlink()
        self.assertNotIn("events.html", self.build())


class RefusalTests(EventsCase):
    def test_duplicate_id_names_the_id_and_both_positions(self):
        message = self.refused([event(1), event(2), event(3, id="forum-1")], r"events\.json\[2\]\.id: duplicates events\[0\]\.id 'forum-1'")
        self.assertIn("forum-1", message)

    def test_ends_before_starts_is_refused_by_instant_not_text(self):
        self.refused([event(1, starts_at="2026-10-10T09:00:00+09:00", ends_at="2026-10-09T23:30:00Z")], r"events\.json\[0\]\.ends_at: is before starts_at")
        # lexically later text but an earlier instant is also refused; the reverse order of text and instant is accepted
        self.refused([event(1, starts_at="2026-10-09T23:00:00Z", ends_at="2026-10-10T01:00:00+05:00")], r"ends_at: is before starts_at")
        self.put([event(1, starts_at="2026-10-10T00:30:00+02:00", ends_at="2026-10-09T23:00:00-02:00")])
        self.assertEqual(content_problems(self.root), [])  # 22:30Z <= 01:00Z next day

    def test_non_https_urls_are_refused_by_name(self):
        for url in ("http://example.com/x", "ftp://example.com", "javascript:alert(1)", "https://user:pw@example.com/", "https://", "", None, 5):
            with self.subTest(url=url):
                self.refused([event(1, url=url) if url is not None else dict(event(1), url=None)], r"events\.json\[0\]\.url: must be an https:// URL")

    def test_timestamps_need_a_zone_and_a_real_moment(self):
        for bad in ("2026-10-10T18:00:00", "2026-10-10", "2026-10-10T18:00:00.5Z", "2026-10-10T18:00:00+15:00", "2026-10-10T18:00:00+14:30",
                    "2026-10-10T18:00:00+24:00", "2026-13-10T18:00:00Z", "2026-02-30T18:00:00Z", "2026-10-10T18:00:60Z",
                    "2026-10-10T25:00:00Z", "2026-10-10T18:00:00+0700", "2026-10-10 18:00:00Z", "", None, 5):
            with self.subTest(bad=bad):
                self.refused([event(1, starts_at=bad, ends_at="2027-01-01T00:00:00Z") if bad is not None else dict(event(1), starts_at=None)],
                             r"events\.json\[0\]\.starts_at: must be YYYY-MM-DDTHH:MM:SS")
        for good in ("2026-10-10T18:00:00+14:00", "2026-10-10T18:00:00-12:00", "2026-12-31T23:59:59-11:30", "2026-10-10T18:00:00Z"):
            self.assertIsNotNone(parse_moment(good), good)

    def test_location_has_exactly_two_modes(self):
        for changes in ({"online": True}, {"venue": None}, {"venue": ""}, {"online": 1}, {"online": "true"}, {"venue": None, "online": False},
                        {"venue": None, "online": 0}):
            with self.subTest(changes=changes):
                self.refused([event(1, **changes)], r"events\.json\[0\]\.(venue|online): ")
        self.refused([dict(event(1), venue=None)], r"events\.json\[0\]\.venue: must be trimmed plain text")
        self.put([event(1, venue=None, online=True), event(2, online=False)])
        self.assertEqual(content_problems(self.root), [])

    def test_counts_and_shapes(self):
        self.put([event(n, id=f"e-{n}") for n in range(1, 51)])
        self.assertEqual(content_problems(self.root), [])
        self.refused([event(n, id=f"e-{n}") for n in range(1, 52)], r"events\.json: must hold at most 50 events")
        for bad in (None, {}, "x", 3, [None], [[]], ["x"], [True]):
            with self.subTest(bad=bad):
                self.refused(bad, r"events\.json(\[0\])?: (must be a JSON array|expected an object)")
        self.refused([dict(event(1), surprise="x")], r"events\.json\[0\]: unknown field surprise")
        self.refused([dict(event(1), **{"a<b": "x"})], r"unknown field 3 chars")
        self.refused([{"id": "x"}], r"events\.json\[0\]\.description_md: missing required field")

    def test_text_limits_and_characters(self):
        self.refused([event(1, id="Bad_ID")], r"\.id: must be a lowercase id")
        self.refused([event(1, id="a" * 65)], r"\.id: must be a lowercase id")
        self.refused([event(1, id=5)], r"\.id: must be a lowercase id")
        for title in ("", " ", " pad", "x" * 121, "a<b", "line\nbreak", "tab\there", "zero\u200bwidth", "bidi\u202e", 5, None):
            with self.subTest(title=title):
                self.refused([event(1, title=title) if title is not None else dict(event(1), title=None)], r"\.title: must be trimmed plain text of 1 to 120")
        self.put([event(1, title="x" * 120, venue="v" * 160, accessibility_note="n" * 400, description_md="d" * 1200)])
        self.assertEqual(content_problems(self.root), [])
        self.refused([event(1, venue="v" * 161)], r"\.venue: must be trimmed plain text of 1 to 160")
        self.refused([event(1, accessibility_note="n" * 401)], r"\.accessibility_note: must be trimmed plain text of 1 to 400")
        self.refused([event(1, accessibility_note="")], r"\.accessibility_note: must be trimmed plain text")
        for description in ("", "   \n", "d" * 1201, "<b>x</b>", "![x](/a.png)", "a\rb", "x\u202ey", 5):
            with self.subTest(description=description):
                self.refused([event(1, description_md=description)], r"\.description_md: must be non-blank text of at most 1200")
        self.put([event(1, description_md="line one\n\n\tindented line two")])
        self.assertEqual(content_problems(self.root), [])

    def test_privacy_lint_covers_every_published_text_field_with_parity(self):
        email, phone, plate = "jane.doe@example.com", "(805) 555-1234", "7ABC123"
        cases = [("title", {"title": f"Contact {email}"}, "email"), ("venue", {"venue": f"Call {phone}"}, "phone"),
                 ("description_md", {"description_md": f"Bring {plate} today"}, "plate"),
                 ("description_md", {"description_md": f"[write](https://example.com/?to={email})"}, "email"),
                 ("description_md", {"description_md": "mail jane@example.com"}, "email"),
                 ("accessibility_note", {"accessibility_note": f"Ask {phone}"}, "phone"),
                 ("url", {"url": "https://example.com/805-555-1234"}, "phone"),
                 ("venue", {"venue": "jdoe [at] example [dot] gov"}, "email")]
        for field, changes, category in cases:
            with self.subTest(field=field, category=category):
                message = self.refused([event(1, **changes)], rf"events\.json\[0\]\.{field}: privacy lint: {category}")
                for secret in (email, phone, plate, "jane", "555"):
                    self.assertNotIn(secret, message)
        raw = '[{"id":"e1","title":"T","starts_at":"2026-10-10T18:00:00Z","ends_at":"2026-10-10T19:00:00Z","venue":"Hall","description_md":"mail jane\\u0040example.com"}]'
        message = self.refused(raw.encode("utf-8"), r"events\.json\[0\]\.description_md: privacy lint: email")  # JSON-escaped @
        self.assertNotIn("jane", message)
        self.refused([event(1, venue="Hall (805) 555-1234")], r"\.venue: privacy lint: phone")  # no venue exception

    def test_privacy_lint_runs_on_the_shared_gate_too(self):
        parsed = load_events([event(1)])
        parsed[0]["venue"] = "Call 805-555-1234"  # bypasses the reader, as a programmatic model could
        model = load_content(self.root, TODAY)
        model["events"] = parsed
        from campaign_tool.site import _privacy_gate
        with self.assertRaisesRegex(ContentError, r"privacy lint: events\[0\]\.venue: phone"):
            _privacy_gate(model)

    def test_every_independent_problem_is_listed_and_attributed_to_the_file(self):
        self.put([event(1, url="http://x.example"), event(2, id="forum-1", ends_at="2020-01-01T00:00:00Z"), 7])
        problems = content_problems(self.root)
        mine = [line for line in problems if line.startswith("events.json")]
        self.assertEqual(len(mine), 4, mine)
        self.assertTrue(all((problem_record(line)["file"] or "").startswith("events.json") for line in mine), mine)


class FileKindTests(EventsCase):
    def refused_file(self, pattern):
        with self.assertRaisesRegex(ContentError, pattern):
            build_site(self.root, today=TODAY)
        self.assertEqual(self.tree(), self.before)
        self.assertTrue(any(re.search(pattern, line) for line in content_problems(self.root)))

    def test_symlink_dangling_symlink_directory_and_fifo_are_refused_unread(self):
        self.before = self.build()
        target = self.root / "elsewhere.json"
        target.write_text("[]", encoding="utf-8")
        os.symlink(target, self.path)
        self.refused_file(r"events\.json: symlinks are not read")
        self.path.unlink()
        os.symlink(self.root / "missing.json", self.path)
        self.refused_file(r"events\.json: symlinks are not read")
        self.path.unlink()
        self.path.mkdir()
        self.refused_file(r"events\.json: must be a regular file")
        self.path.rmdir()
        os.mkfifo(self.path)  # opening it would block forever: it must be refused before any open
        self.refused_file(r"events\.json: must be a regular file")

    def test_malformed_oversized_deep_and_non_utf8_json_are_named(self):
        self.before = self.build()
        for payload, pattern in ((b"[", r"events\.json: not valid UTF-8 JSON"), (b"\xff\xfe[]", r"events\.json: not valid UTF-8 JSON"),
                                 (b"[" * 100000 + b"]" * 100000, r"events\.json: not valid UTF-8 JSON"),
                                 (b"[" + b" " * (2 * 1024 * 1024) + b"]", r"events\.json: file too large")):
            with self.subTest(pattern=pattern, size=len(payload)):
                self.put(payload)
                self.refused_file(pattern)


class HelperTests(unittest.TestCase):
    def test_partition_sorts_copies_and_never_reorders_the_input(self):
        rows = [event(1, id="b", starts_at="2026-10-02T00:00:00Z"), event(2, id="a", starts_at="2026-10-02T00:00:00Z"),
                event(3, id="p2", starts_at="2026-09-02T00:00:00Z"), event(4, id="p1", starts_at="2026-09-03T00:00:00Z"),
                event(5, id="p0", starts_at="2026-09-03T00:00:00Z"), event(6, id="first", starts_at="2026-10-01T03:00:00+05:00")]
        original = [dict(row) for row in rows]
        upcoming, past = partition(rows, build_cutoff(TODAY))
        self.assertEqual([row["id"] for row in upcoming], ["a", "b"])  # same instant: id order
        self.assertEqual([row["id"] for row in past], ["first", "p0", "p1", "p2"])  # newest start first, ties by id
        self.assertEqual(rows, original)
        self.assertEqual(build_cutoff(TODAY), datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertIsNot(upcoming, rows)


if __name__ == "__main__":
    unittest.main()
