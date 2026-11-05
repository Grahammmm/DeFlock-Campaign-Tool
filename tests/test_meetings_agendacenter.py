"""DF1207-12: CivicPlus AgendaCenter RSS import. Synthetic feed only (invented host example-city.example.org); no network."""
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
from campaign_tool.meetings import AgendaCenterClient, MeetingsError, relevant_events, resolve_zone  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "agendacenter-rss.xml"
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SINCE = date(2026, 12, 1)
HOST = "example-city.example.org"
ZONE = resolve_zone("America/Los_Angeles")
BASE = f"https://{HOST}/AgendaCenter/ViewFile/Agenda/"


def feed(*items):
    body = "".join(f"<item><title>{t}</title><link>{l}</link><pubDate>{p}</pubDate><description>{d}</description></item>"
                   for t, l, p, d in items)
    return f'<?xml version="1.0"?><rss><channel>{body}</channel></rss>'.encode()


def item(title="Council - Regular", link=BASE + "_12082026-11", pub="Mon, 30 Nov 2026 08:00:00 -0800",
         desc="license plate reader contract"):
    return (title, link, pub, desc)


def run(data, all_events=False, since=SINCE):
    calls = []
    client = AgendaCenterClient(HOST, opener=lambda url: calls.append(url) or data)
    return relevant_events(client, since=since, zone=ZONE, all_events=all_events), client, calls


class Import(unittest.TestCase):
    def test_fixture_one_match_legistar_shape(self):
        meetings, _, calls = run(FIXTURE.read_bytes())
        self.assertEqual(len(meetings), 1)
        m = meetings[0]
        self.assertEqual((m["id"], m["source"], m["event_id"], m["status"], m["body"]),
                         ("agendacenter-example-city-301", "agendacenter", 301, "scheduled", "City Council"))
        self.assertEqual((m["starts_at"], m["time_known"]), ("2026-12-08T00:00:00-08:00", False))
        self.assertEqual(m["agenda_url"], BASE + "_12082026-301")
        self.assertEqual(m["relevance"], "alpr_item")
        self.assertEqual(calls, [f"https://{HOST}/RSSFeed.aspx?ModID=65&CID=All-0"])  # the feed once; the PDF never

    def test_link_date_wins_over_pubdate_and_undated_dropped(self):
        meetings, client, _ = run(feed(item()))
        self.assertEqual(meetings[0]["starts_at"][:10], "2026-12-08")  # pubDate says Nov 30
        undated = feed(item(link=BASE + "_99992026-5", pub="not a date"), item(link=BASE + "_12092026-6", pub=""))
        meetings, client, _ = run(undated)
        self.assertEqual([m["event_id"] for m in meetings], [6])
        self.assertEqual(client.dropped_no_date, 1)
        pubonly, _, _ = run(feed(item(link=BASE + "plain-7", pub="Tue, 08 Dec 2026 10:00:00 -0800")))
        self.assertEqual(pubonly[0]["starts_at"][:10], "2026-12-08")

    def test_body_falls_back_to_title_and_id_to_hash(self):
        data = feed(item(title="Harbor Board \u2013 Special", link=f"https://{HOST}/AgendaCenter/x", pub="Tue, 08 Dec 2026 10:00:00 -0800"))
        meetings, _, _ = run(data)
        self.assertEqual(meetings[0]["body"], "Harbor Board")
        again, _, _ = run(data)
        self.assertEqual(meetings[0]["id"], again[0]["id"])
        self.assertRegex(meetings[0]["id"], r"^agendacenter-example-city-\d+$")

    def test_all_events_keeps_not_scanned(self):
        data = feed(item(title="ALPR Review - Regular", desc=""))
        self.assertEqual(run(data)[0], [])
        meetings, _, _ = run(data, all_events=True)
        self.assertEqual(len(meetings), 1)
        self.assertTrue(meetings[0]["not_scanned"])
        self.assertEqual((meetings[0]["matched_items"], meetings[0]["matched_terms"]), (0, []))
        every, _, _ = run(FIXTURE.read_bytes(), all_events=True)
        self.assertEqual([x["event_id"] for x in every], [301, 303])  # the unrelated item with text stays dropped

    def test_bad_hosts_refused_before_any_request(self):
        calls = []
        for host in ("10.0.0.1", f"{HOST}:8080", f"user@{HOST}", "HOST.ORG", f"{HOST}/AgendaCenter", "localhost",
                     "a.b.c.d.e.f.g.org", "", None):
            with self.assertRaises(MeetingsError, msg=str(host)):
                AgendaCenterClient(host, opener=lambda url: calls.append(url))
        self.assertEqual(calls, [])

    def test_xml_refused(self):
        for bad in (b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x "y">]><rss/>', b"<!DOCTYPE rss><rss/>", b'<!ENTITY a "b"><rss/>',
                    b"<rss><channel><item>"):
            with self.assertRaisesRegex(MeetingsError, "DOCTYPE|well-formed"):
                run(bad)

    def test_link_other_host_or_http_dropped_and_never_fetched(self):
        for link in ("https://evil.example.com/AgendaCenter/ViewFile/Agenda/_12082026-1", f"http://{HOST}/AgendaCenter/a-1",
                     f"https://u:p@{HOST}/AgendaCenter/a-1", f"https://{HOST}/Other/_12082026-1", "javascript:x"):
            meetings, _, calls = run(feed(item(link=link, pub="Tue, 08 Dec 2026 10:00:00 -0800")))
            self.assertEqual(len(meetings), 1)
            self.assertIsNone(meetings[0]["agenda_url"])
            self.assertEqual(len(calls), 1)

    def test_cancelled_and_cap(self):
        meetings, _, _ = run(feed(item(title="Council - CANCELLED - Regular")))
        self.assertEqual(meetings[0]["status"], "cancelled")
        many = feed(*[item(link=f"{BASE}_12{1 + i % 28:02d}2026-{1000 + i}") for i in range(65)])
        meetings, client, _ = run(many)
        self.assertEqual((len(meetings), client.cut), (60, 5))


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "c"
        shutil.copytree(EXAMPLE, self.root)

    def meetings(self, *extra, feed_path=FIXTURE, client=HOST):
        argv = ["meetings", "--directory", str(self.root), "--platform", "agendacenter", "--client", client,
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
        self.assertEqual((doc["source"], doc["client"], len(doc["meetings"])), ("agendacenter", HOST, 2))
        report = json.loads(out)
        self.assertEqual((report["platform"], report["not_scanned"], report["cut_by_item_cap"], report["dropped_no_date"]),
                         ("agendacenter", 1, 0, 0))

    def test_refused_inputs_leave_file_untouched(self):
        self.assertEqual(self.meetings()[0], 0)
        path = self.root / "kit" / "meetings.json"
        before = path.read_bytes()
        bad, trunc, big = self.tmp / "bad.xml", self.tmp / "trunc.xml", self.tmp / "big.xml"
        bad.write_bytes(b'<!DOCTYPE rss [<!ENTITY a "b">]><rss/>')
        trunc.write_bytes(b"<rss><channel><item>")
        big.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        link = self.tmp / "link.xml"
        link.symlink_to(FIXTURE)
        for p in (bad, trunc, big, link):
            self.assertEqual(self.meetings(feed_path=p)[0], 1, p.name)
        self.assertEqual(self.meetings(client="10.0.0.1")[0], 1)
        self.assertEqual(path.read_bytes(), before)

    def test_rerun_records_moved_date_and_cancel(self):
        a = self.tmp / "a.xml"
        a.write_bytes(feed(item(link=BASE + "_12082026-11")))
        self.assertEqual(self.meetings(feed_path=a)[0], 0)
        a.write_bytes(feed(item(link=BASE + "_12092026-11")))
        self.assertEqual(self.meetings(feed_path=a)[0], 0)
        self.assertEqual(self.doc()["changes"]["rescheduled"][0]["to"][:10], "2026-12-09")
        a.write_bytes(feed(item(title="Council - CANCELLED", link=BASE + "_12092026-11")))
        self.meetings(feed_path=a)
        self.assertEqual(self.doc()["changes"]["cancelled"], ["agendacenter-example-city-11"])

    def test_option_misuse(self):
        for extra in (["--view-id", "7"], ["--from-json", str(FIXTURE)], ["--online"]):
            argv = ["meetings", "--directory", str(self.root), "--platform", "agendacenter", "--client", HOST,
                    "--from-xml", str(FIXTURE), *extra]
            with mock.patch.object(sys, "argv", ["campaign_tool", *argv]), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
