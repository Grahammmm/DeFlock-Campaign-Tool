"""DF0137-10: Granicus ViewPublisher RSS import. Synthetic feed only (invented host examplecity.granicus.com); no network."""
import contextlib
import io
import json
import shutil
import socket
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli  # noqa: E402
from campaign_tool.meetings import GranicusClient, MeetingsError, read_feed_file, relevant_events, resolve_zone  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "granicus-feed.xml"
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SINCE = date(2026, 12, 1)
HOST = "examplecity.granicus.com"
ZONE = resolve_zone("America/Los_Angeles")


def feed(*items):
    body = "".join(f"<item><title>{t}</title><link>{l}</link><guid>{g}</guid><description>{d}</description></item>"
                   for t, l, g, d in items)
    return f'<?xml version="1.0"?><rss><channel>{body}</channel></rss>'.encode()


def run(data, all_events=False, since=SINCE, view_id=7):
    client = GranicusClient(HOST, view_id, opener=lambda url: data)
    return relevant_events(client, since=since, zone=ZONE, all_events=all_events), client


def item(title="Council - Dec 8, 2026", link=f"https://{HOST}/AgendaViewer.php?clip_id=1", guid="1",
         desc="license plate reader contract"):
    return (title, link, guid, desc)


class Import(unittest.TestCase):
    def test_fixture_one_match_and_all_events(self):
        data = FIXTURE.read_bytes()
        meetings, _ = run(data)
        self.assertEqual(len(meetings), 1)
        m = meetings[0]
        self.assertEqual((m["id"], m["source"], m["event_id"], m["status"]), ("granicus-examplecity-4101", "granicus", 4101, "scheduled"))
        self.assertEqual((m["starts_at"], m["time_known"]), ("2026-12-08T00:00:00-08:00", False))
        self.assertEqual(m["agenda_url"], f"https://{HOST}/AgendaViewer.php?view_id=7&clip_id=4101")
        self.assertNotIn("not_scanned", m)
        every, _ = run(data, all_events=True)
        self.assertEqual([x["event_id"] for x in every], [4101, 4103])  # unrelated item with text stays dropped
        self.assertTrue(every[1]["not_scanned"])
        self.assertEqual(every[1]["matched_items"], 0)

    def test_urls_refused_before_any_request(self):
        calls = []
        for host, view in (("evil.example.com", 7), ("examplecity.granicus.com.evil.net", 7), (HOST, 0), (HOST, 10000), (HOST, "7")):
            with self.assertRaises(MeetingsError):
                GranicusClient(host, view, opener=lambda url: calls.append(url))
        self.assertEqual(calls, [])

    def test_xml_refused(self):
        for bad in (b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x "y">]><rss/>', b"<!DOCTYPE rss><rss/>", b'<!ENTITY a "b"><rss/>'):
            with self.assertRaisesRegex(MeetingsError, "DOCTYPE|well-formed"):
                run(bad)
        utf16 = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE rss [<!ENTITY x "y">]><rss/>'.encode("utf-16")
        with self.assertRaisesRegex(MeetingsError, "DOCTYPE"):
            run(utf16)
        client = GranicusClient(HOST, 7, opener=lambda url: b"<rss><channel><item>", label="feed.xml")
        with self.assertRaisesRegex(MeetingsError, "feed.xml"):
            client.events()

    def test_link_other_host_or_http_dropped(self):
        for link in ("https://evil.example.com/a?clip_id=1", f"http://{HOST}/a", f"https://u:p@{HOST}/a", "javascript:x"):
            meetings, _ = run(feed(item(link=link)))
            self.assertEqual(len(meetings), 1)
            self.assertIsNone(meetings[0]["agenda_url"])

    def test_event_id_from_query_and_bad_ids_skipped(self):
        meetings, _ = run(feed(item(guid="not-a-number", link=f"https://{HOST}/a?clip_id=55")))
        self.assertEqual(meetings[0]["event_id"], 55)
        self.assertEqual(run(feed(item(guid="x", link=f"https://{HOST}/a")))[0], [])

    def test_cancelled_and_cap(self):
        meetings, _ = run(feed(item(title="Council - CANCELLED - Dec 8, 2026")))
        self.assertEqual(meetings[0]["status"], "cancelled")
        many = feed(*[item(guid=str(1000 + i), title=f"Council - Dec {1 + i % 28}, 2026") for i in range(65)])
        meetings, client = run(many)
        self.assertEqual((len(meetings), client.cut), (60, 5))

    def test_dst_week(self):
        meetings, _ = run(feed(item(title="A - Oct 31, 2026", guid="1"), item(title="B - Nov 2, 2026", guid="2")), since=date(2026, 10, 1))
        self.assertEqual([m["starts_at"] for m in meetings], ["2026-10-31T00:00:00-07:00", "2026-11-02T00:00:00-08:00"])

    def test_pubdate_fallback(self):
        data = b"<rss><channel><item><title>Council</title><guid>9</guid><pubDate>Tue, 08 Dec 2026 10:00:00 -0800</pubDate>" \
               b"<description>ALPR</description></item></channel></rss>"
        self.assertEqual(run(data)[0][0]["starts_at"][:10], "2026-12-08")


    def test_pubdate_wins_over_title_date(self):
        data = b"<rss><channel><item><title>Council - Dec 9, 2026</title><guid>9</guid><pubDate>Tue, 08 Dec 2026 10:00:00 -0800</pubDate>" \
               b"<description>ALPR</description></item></channel></rss>"
        self.assertEqual(run(data)[0][0]["starts_at"][:10], "2026-12-08")
        bad = data.replace(b"Tue, 08 Dec 2026 10:00:00 -0800", b"not a date")
        self.assertEqual(run(bad)[0][0]["starts_at"][:10], "2026-12-09")

    def test_since_defaults_to_today(self):
        data = feed(item(title="Old - Jan 5, 2020", guid="1"), item(title="Future - Jan 5, 2999", guid="2"))
        meetings, _ = run(data, since=None)
        self.assertEqual([m["event_id"] for m in meetings], [2])

    def test_title_alone_is_not_scanned(self):
        data = feed(item(title="ALPR Review - Dec 8, 2026", desc=""))
        self.assertEqual(run(data)[0], [])
        meetings, _ = run(data, all_events=True)
        self.assertEqual(len(meetings), 1)
        self.assertTrue(meetings[0]["not_scanned"])
        self.assertEqual((meetings[0]["matched_items"], meetings[0]["matched_terms"]), (0, []))


class FeedFile(unittest.TestCase):
    def test_symlink_and_oversize_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            link, big = Path(tmp, "link.xml"), Path(tmp, "big.xml")
            link.symlink_to(FIXTURE)
            big.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
            for path in (link, big, Path(tmp)):
                with self.assertRaises(MeetingsError):
                    read_feed_file(str(path))
            self.assertEqual(read_feed_file(str(FIXTURE)), FIXTURE.read_bytes())


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "c"
        shutil.copytree(EXAMPLE, self.root)

    def meetings(self, *extra, feed_path=FIXTURE):
        argv = ["meetings", "--directory", str(self.root), "--platform", "granicus", "--client", HOST, "--view-id", "7",
                "--from-xml", str(feed_path), "--since", "2026-12-01", *extra]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sys, "argv", ["campaign_tool", *argv]):
            try:
                cli.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
            except ValueError as exc:
                code = str(exc)
        return code, out.getvalue()

    def doc(self):
        return json.loads((self.root / "kit" / "meetings.json").read_text(encoding="utf-8"))

    def test_writes_document_and_report(self):
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network")):
            code, out = self.meetings("--all-events")
        self.assertEqual(code, 0)
        doc = self.doc()
        self.assertEqual((doc["source"], doc["client"], len(doc["meetings"])), ("granicus", HOST, 2))
        report = json.loads(out)
        self.assertEqual((report["platform"], report["not_scanned"], report["cut_by_item_cap"]), ("granicus", 1, 0))

    def test_refused_inputs_leave_file_untouched(self):
        self.assertEqual(self.meetings()[0], 0)
        before = (self.root / "kit" / "meetings.json").read_bytes()
        bad = self.tmp / "bad.xml"
        bad.write_bytes(b"<!DOCTYPE rss><rss/>")
        trunc = self.tmp / "trunc.xml"
        trunc.write_bytes(b"<rss><channel><item>")
        for path in (bad, trunc):
            self.assertEqual(self.meetings(feed_path=path)[0], 1)
        self.assertEqual((self.root / "kit" / "meetings.json").read_bytes(), before)

    def test_rerun_records_moved_time(self):
        a = self.tmp / "a.xml"
        a.write_bytes(feed(item(title="Council - Dec 8, 2026")))
        self.assertEqual(self.meetings(feed_path=a)[0], 0)
        a.write_bytes(feed(item(title="Council - Dec 9, 2026")))
        self.assertEqual(self.meetings(feed_path=a)[0], 0)
        self.assertEqual(self.doc()["changes"]["rescheduled"][0]["to"][:10], "2026-12-09")
        a.write_bytes(feed(item(title="Council - CANCELLED - Dec 9, 2026")))
        self.meetings(feed_path=a)
        self.assertEqual(self.doc()["changes"]["cancelled"], ["granicus-examplecity-1"])

    def test_option_misuse(self):
        argv = ["meetings", "--directory", str(self.root), "--client", "x", "--from-xml", str(FIXTURE)]
        with mock.patch.object(sys, "argv", ["campaign_tool", *argv]), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as cm:
            cli.main()
        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
