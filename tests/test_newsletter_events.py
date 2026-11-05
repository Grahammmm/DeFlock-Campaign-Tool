"""DF1107-09: the 'Campaign events' section of newsletter drafts. Synthetic data; nothing is sent."""
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.newsletter import build_draft  # noqa: E402
from campaign_tool.newsletter.draft import DraftError  # noqa: E402
from campaign_tool.newsletter.from_content import manifest_from_content  # noqa: E402
from campaign_tool.newsletter.site_links import site_links  # noqa: E402

NOW = datetime(2027, 3, 1, 12, 0, tzinfo=timezone.utc)
BASE = "https://synthetic.example.invalid"


def event(ident, starts, venue="Synthetic Hall", title=None, hours=2, **extra):
    start = datetime.fromisoformat(starts)
    row = {"id": ident, "title": title or "Event " + ident, "starts_at": starts,
           "ends_at": (start + timedelta(hours=hours)).isoformat(), "path": f"/events.html#event-{ident}", **extra}
    if venue is not None:
        row["venue"] = venue
    return row


def manifest(events=None, base_url=BASE, since=None):
    data = {"schema_version": 1, "campaign": {"name": "Synthetic", "base_url": base_url, "county_name": "Synthetic"},
            "since": since, "findings": [], "meetings": []}
    if events is not None:
        data["events"] = events
    return data


def section(draft):
    return draft["text"].split("## Campaign events\n")[1].split("\n\n")[0].splitlines()


class EventsSectionTests(unittest.TestCase):
    def test_selection_order_offset_and_links(self):
        draft = build_draft(manifest([
            event("zeta", "2027-03-10T19:00:00-08:00"),
            event("online", "2027-03-05T09:30:00+05:30", venue=None, online=True),
            event("alpha", "2027-03-10T19:00:00-08:00"),
            event("past", "2027-02-28T10:00:00Z"),
            event("started", "2027-03-01T11:59:59Z", hours=48),
            event("far", "2027-03-31T12:00:01Z"),
        ]), NOW)
        lines = section(draft)
        self.assertEqual(lines, [
            "- **Event online**, Fri Mar 05, 2027 09:30 UTC+05:30, Online [Event details](https://synthetic.example.invalid/events.html#event-online)",
            "- **Event alpha**, Wed Mar 10, 2027 19:00 UTC-08:00, Synthetic Hall [Event details](https://synthetic.example.invalid/events.html#event-alpha)",
            "- **Event zeta**, Wed Mar 10, 2027 19:00 UTC-08:00, Synthetic Hall [Event details](https://synthetic.example.invalid/events.html#event-zeta)"])
        self.assertEqual(draft["counts"]["events"], 3)
        self.assertEqual(draft["excluded"]["events_outside_window"], 3)
        self.assertEqual(draft["subject"], build_draft(manifest(), NOW)["subject"])
        for ident in ("online", "alpha", "zeta"):
            self.assertIn(f'href="{BASE}/events.html#event-{ident}"', draft["html"])
        for ident in ("past", "started", "far"):
            self.assertNotIn(f"event-{ident}", draft["html"] + draft["text"])
        order = [draft["html"].index(f"event-{i}") for i in ("online", "alpha", "zeta")]
        self.assertEqual(order, sorted(order))

    def test_boundaries_inclusive_and_since(self):
        edge = [event("at-now", "2027-03-01T12:00:00Z"), event("at-horizon", "2027-03-31T12:00:00Z"),
                event("before-now", "2027-03-01T11:59:59Z"), event("after-horizon", "2027-03-31T12:00:01Z")]
        draft = build_draft(manifest(edge), NOW)
        self.assertEqual(draft["counts"]["events"], 2)
        self.assertIn("event-at-now", draft["text"])
        self.assertIn("event-at-horizon", draft["text"])
        self.assertEqual(draft["excluded"]["events_outside_window"], 2)
        later = build_draft(manifest(edge, since="2027-03-10T00:00:00+00:00"), NOW)
        self.assertEqual(later["counts"]["events"], 1)
        self.assertNotIn("event-at-now", later["text"])
        beyond = build_draft(manifest(edge, since="2027-05-01T00:00:00+00:00"), NOW)
        self.assertNotIn("Campaign events", beyond["text"])
        self.assertEqual(beyond["excluded"]["events_outside_window"], 4)

    def test_utc_horizon_crossed_while_local_date_differs(self):
        edge = [event("late", "2027-03-30T23:00:00-14:00"), event("leap", "2028-02-29T00:30:00+14:00")]
        draft = build_draft(manifest(edge), datetime(2028, 2, 10, 0, 0, tzinfo=timezone.utc))
        self.assertIn("Tue Feb 29, 2028 00:30 UTC+14:00", draft["text"])
        self.assertEqual(draft["counts"]["events"], 1)

    def test_no_section_and_byte_identical_when_nothing_upcoming(self):
        plain = build_draft(manifest(), NOW)
        for events in ([], [event("old", "2027-01-01T10:00:00Z")]):
            draft = build_draft(manifest(events), NOW)
            self.assertEqual((draft["html"], draft["text"], draft["subject"]), (plain["html"], plain["text"], plain["subject"]))
            self.assertEqual(draft["counts"], plain["counts"])
            self.assertEqual(draft["excluded"], plain["excluded"])
            self.assertEqual(set(draft), set(plain))

    def test_future_events_all_outside_window_keep_body_and_report_count(self):
        plain = build_draft(manifest(), NOW)
        draft = build_draft(manifest([event("later", "2027-06-01T10:00:00Z"), event("old", "2027-01-01T10:00:00Z")]), NOW)
        self.assertEqual((draft["html"], draft["text"]), (plain["html"], plain["text"]))
        self.assertNotIn("events", draft["counts"])
        self.assertEqual(draft["excluded"], {**plain["excluded"], "events_outside_window": 2})

    def test_no_base_url_uses_plain_path_and_site_links_sees_fragment(self):
        draft = build_draft(manifest([event("one", "2027-03-02T10:00:00Z")], base_url=None), NOW)
        self.assertEqual(section(draft), ["- **Event one**, Tue Mar 02, 2027 10:00 UTC+00:00, Synthetic Hall /events.html#event-one"])
        self.assertIn("/events.html#event-one", site_links(draft, None))

    def test_fifty_events_all_listed_and_html_text_agree(self):
        rows = [event(f"e{i:02d}", (NOW + timedelta(days=1, minutes=i)).isoformat()) for i in range(50)]
        draft = build_draft(manifest(rows), NOW)
        self.assertEqual(len(section(draft)), 50)
        self.assertEqual(draft["html"].count("Event details"), 50)

    def test_markdown_and_unicode_stay_literal(self):
        title = "Fish & chips [click](https://evil.example.invalid) **bold** — café"
        draft = build_draft(manifest([event("lit", "2027-03-02T10:00:00Z", title=title, venue="Hall *A* [x](https://evil.example.invalid)")]), NOW)
        self.assertNotIn("evil.example.invalid\"", draft["html"])
        self.assertNotIn("<strong>bold", draft["html"])
        self.assertIn("café", draft["html"])
        self.assertIn(f'href="{BASE}/events.html#event-lit"', draft["html"])

    def test_invalid_events_name_the_field_not_the_value(self):
        good = event("ok", "2027-03-02T10:00:00Z")
        bad = {
            "events": ("not-a-list", "events"),
            "dup": ([good, dict(good)], "events[1].id"),
            "naive": ([{**good, "starts_at": "2027-03-02T10:00:00"}], "events[0].starts_at"),
            "end": ([{**good, "ends_at": "2027-03-02T09:00:00Z"}], "events[0].ends_at"),
            "member": (["x"], "events[0]"),
            "path": ([{**good, "path": "/events.html#event-other"}], "events[0].path"),
            "path2": ([{**good, "path": "https://evil.example.invalid/"}], "events[0].path"),
            "id": ([{**good, "id": "Bad Id"}], "events[0].id"),
            "mode": ([{**good, "online": True}], "events[0].venue"),
            "novenue": ([{k: v for k, v in good.items() if k != "venue"}], "events[0].venue"),
            "many": ([good] * 51, "events"),
            "far-bad": ([good, {**event("far", "2030-01-01T10:00:00Z"), "ends_at": "nope"}], "events[1].ends_at"),
        }
        for name, (events, field) in bad.items():
            with self.subTest(name):
                with self.assertRaises(DraftError) as ctx:
                    build_draft(manifest(events), NOW)
                self.assertIn(field, str(ctx.exception))

    def test_privacy_categories_refuse_without_echo(self):
        for value in ("call 415-555-0134 now", "mail someone@example.invalid"):
            with self.subTest(value):
                with self.assertRaises(DraftError) as ctx:
                    build_draft(manifest([event("p", "2027-03-02T10:00:00Z", title=value)]), NOW)
                self.assertNotIn(value, str(ctx.exception))
                self.assertNotIn("555", str(ctx.exception))


class FromContentEventsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-newsletter-events-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "init", "--directory", str(self.root), "--name",
                        "Synthetic", "--county", "Synthetic", "--state", "CA"], cwd=REPO_ROOT, check=True,
                       capture_output=True, timeout=30)
        content = self.root / "content"
        (content / "findings").mkdir(parents=True, exist_ok=True)
        (content / "site.json").write_text(json.dumps({"schema_version": 1, "tagline": "Synthetic.", "about_md": "Synthetic.",
                                                      "base_url": BASE}))

    def write_events(self, rows):
        (self.root / "content" / "events.json").write_text(json.dumps(rows))

    def cli(self, *extra):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool.newsletter", "--directory", str(self.root),
                               "--from-content", *extra], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)

    def raw(self, ident, days, **extra):
        start = (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0)
        return {"id": ident, "title": "Synthetic " + ident, "starts_at": start.isoformat(), "ends_at": (start + timedelta(hours=1)).isoformat(),
                "description_md": "Private description text.", "venue": "Synthetic Hall", "url": "https://secret.example.invalid/reg",
                "accessibility_note": "Private accessibility note.", **extra}

    def test_manifest_projects_only_public_fields(self):
        self.write_events([self.raw("a", 3), self.raw("b", 4, online=True, venue=None)][:1])
        data = manifest_from_content(self.root, {"name": "S", "county": "S"})
        self.assertEqual(set(data["events"][0]), {"id", "title", "starts_at", "ends_at", "venue", "path"})
        self.assertEqual(data["events"][0]["path"], "/events.html#event-a")

    def test_no_events_file_has_no_manifest_key(self):
        self.assertNotIn("events", manifest_from_content(self.root, {"name": "S", "county": "S"}))

    def test_cli_section_private_fields_absent_and_empty_branch_identical(self):
        before = self.cli()
        self.assertEqual(before.returncode, 0, before.stderr)
        plain_html = (self.root / "kit" / "newsletter-draft.html").read_text()
        self.write_events([self.raw("soon", 3), self.raw("later", 90)])
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["counts"]["events"], 1)
        self.assertEqual(out["excluded"]["events_outside_window"], 1)
        html = (self.root / "kit" / "newsletter-draft.html").read_text()
        text = (self.root / "kit" / "newsletter-draft.txt").read_text()
        archive = json.loads((self.root / "kit" / "newsletter-draft.json").read_text())["public_archive"]["body_md"]
        for blob in (html, text, archive):
            self.assertIn("event-soon", blob)
            self.assertNotIn("event-later", blob)
            for secret in ("Private description", "secret.example", "Private accessibility"):
                self.assertNotIn(secret, blob)
        self.assertIn("## Campaign events", archive)
        self.write_events([self.raw("old", -5)])
        self.assertEqual(self.cli().returncode, 0)
        self.assertEqual((self.root / "kit" / "newsletter-draft.html").read_text(), plain_html)

    def test_site_check_names_missing_fragment(self):
        self.write_events([self.raw("soon", 3)])
        public = self.root / "public"
        public.mkdir()
        (public / "index.html").write_text("<html></html>")
        (public / "events.html").write_text('<html><article id="event-other"></article></html>')
        result = self.cli("--site", str(public))
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("/events.html#event-soon", result.stderr)
        self.assertIn("no_fragment", result.stderr)
        (public / "events.html").write_text('<html><article id="event-soon"></article></html>')
        self.assertEqual(self.cli("--site", str(public)).returncode, 0)


if __name__ == "__main__":
    unittest.main()
