"""Synthetic agenda bytes, injectable HTTP responses and converter; no live GETs."""
import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
import urllib.error
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from campaign_tool import cli, meeting_pdf_scan as pdf, meetings

URL = "https://alpha.legistar.com/agenda.pdf"
DATA = b"%PDF-1.4 synthetic"
DAY = date(2026, 11, 6)


def opener(data=DATA, code=200, url=URL):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.getcode.return_value = code
    response.geturl.return_value = url
    response.read.return_value = data
    return Mock(open=Mock(return_value=response))


def client(events=None, text="Routine business"):
    events = events or [{"EventId": 1, "EventBodyName": "Alpha Council", "EventDate": DAY.isoformat(),
                         "EventAgendaFile": URL}]
    return SimpleNamespace(source="legistar", client="alpha", events=lambda **kw: events,
                           event_items=lambda n: [{"EventItemTitle": text}])


class PdfScanTests(unittest.TestCase):
    @patch.object(pdf.subprocess, "run", return_value=SimpleNamespace(stdout=b"Budget\fParks\fALPR renewal\f"))
    def test_page_three_memory_only_and_bounded_get(self, convert):
        http = opener()
        with patch("builtins.open", side_effect=AssertionError("no disk writes")):
            result = pdf.scan(URL, http, platform_host_rule=lambda u: True)
        self.assertEqual(result, {"pdf_scan": "ok", "pdf_sha256": hashlib.sha256(DATA).hexdigest(),
                                 "pages": 3, "pages_matched": [3], "matched_terms": ["alpr"]})
        req = http.open.call_args.args[0]
        self.assertEqual((req.full_url, req.get_method(), http.open.call_args.kwargs), (URL, "GET", {"timeout": 20}))
        http.open.return_value.read.assert_called_once_with(pdf.MAX_BYTES + 1)
        convert.assert_called_once_with(["pdftotext", "-layout", "-", "-"], input=DATA,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=True)

    def test_refusals_before_get_and_platform_rules(self):
        http = opener()
        rule = pdf.host_rule(client())
        for url in ("http://alpha.legistar.com/a", "https://elsewhere.invalid/a", "https://user@alpha.legistar.com/a",
                    "https://alpha.legistar.com:443/a", "https://alpha.legistar.com/a\nb", "https://[invalid"):
            with self.subTest(url=url):
                self.assertEqual(pdf.scan(url, http, platform_host_rule=rule)["pdf_scan"], "refused")
        http.open.assert_not_called()
        for platform, host, good, bad in (
                ("civicclerk", "unused", "https://alpha.civicclerk.com/a", "https://civicclerk.com.invalid/a"),
                ("granicus", "alpha.granicus.com", "https://alpha.granicus.com/a", "https://beta.granicus.com/a"),
                ("agendacenter", "alpha.example.test", "https://alpha.example.test/AgendaCenter/a", "https://alpha.example.test/a")):
            allowed = pdf.host_rule(SimpleNamespace(source=platform, host=host))
            self.assertTrue(allowed(good))
            self.assertFalse(allowed(bad))

    @patch.object(pdf.subprocess, "run")
    def test_bad_response_size_redirect_and_timeouts(self, convert):
        for http, status in ((opener(b"x" * (11 * 1024 * 1024)), "too_large"),
                             (opener(b"<html>no pdf"), "not_pdf"), (opener(code=302), "refused"),
                             (opener(url="https://elsewhere.invalid/a"), "refused"),
                             (Mock(open=Mock(side_effect=TimeoutError())), "failed"),
                             (Mock(open=Mock(side_effect=urllib.error.HTTPError(URL, 302, "redirect", {}, None))), "refused")):
            with self.subTest(status=status):
                self.assertEqual(pdf.scan(URL, http, platform_host_rule=lambda u: True)["pdf_scan"], status)
                self.assertEqual(http.open.call_count, 1)
        convert.assert_not_called()
        self.assertIsNone(pdf.NoRedirect().redirect_request(None, None, 302, "redirect", {}, URL))
        for error, status in ((FileNotFoundError(), "unavailable_pdftotext"),
                              (subprocess.TimeoutExpired("pdftotext", 30), "failed"),
                              (subprocess.CalledProcessError(1, "pdftotext"), "failed")):
            convert.side_effect = error
            self.assertEqual(pdf.scan(URL, opener(), platform_host_rule=lambda u: True)["pdf_scan"], status)

    @patch.object(pdf.subprocess, "run", return_value=SimpleNamespace(stdout=b"Parks only\f"))
    def test_no_match_missing_converter_and_default_off(self, convert):
        scans = pdf.AgendaScans(opener())
        self.assertEqual(meetings.relevant_events(client(), pdf_scans=scans), [])
        self.assertEqual(scans.counts, {"ok": 1})
        convert.side_effect = FileNotFoundError()
        scans = pdf.AgendaScans(opener())
        self.assertEqual(meetings.relevant_events(client(), pdf_scans=scans), [])
        self.assertEqual(scans.counts, {"unavailable_pdftotext": 1})
        convert.reset_mock()
        self.assertEqual(meetings.relevant_events(client()), [])
        convert.assert_not_called()

    @patch.object(pdf.subprocess, "run", return_value=SimpleNamespace(stdout=b"One\fTwo\fFlock\f"))
    def test_kept_cancelled_unscanned_feed_hits_and_per_body_cap(self, convert):
        events = [dict(client().events()[0], EventId=n, EventAgendaFile=URL + f"?id={n}") for n in range(21)]
        events[0]["EventAgendaStatusName"] = "Cancelled"
        http = opener()
        http.open.side_effect = lambda req, **kw: opener(url=req.full_url).open.return_value
        scans = pdf.AgendaScans(http)
        c = client(events)
        c.scanned = lambda n: False
        found = meetings.relevant_events(c, pdf_scans=scans, all_events=True)
        self.assertEqual(http.open.call_count, 20)
        self.assertEqual(len(found), 21)  # capped unscanned row still kept with --all-events
        self.assertEqual(found[0]["status"], "cancelled")
        self.assertEqual(found[0]["pdf_pages_matched"], [3])
        self.assertEqual(found[0]["matched_in"], "agenda_pdf")
        self.assertNotIn("not_scanned", found[0])
        self.assertTrue(found[-1]["not_scanned"])
        # Duplicate URL uses its cached lead, not another GET.
        meetings.relevant_events(client(), pdf_scans=pdf.AgendaScans(http))
        duplicate = pdf.AgendaScans(http)
        duplicate.meeting(c, "Other Council", URL)
        duplicate.meeting(c, "Other Council", URL)
        self.assertEqual(duplicate.counts, {"ok": 1})
        before = http.open.call_count
        self.assertEqual(meetings.relevant_events(client(text="ALPR"), pdf_scans=scans),
                         meetings.relevant_events(client(text="ALPR")))
        self.assertEqual(http.open.call_count, before)

    @patch.object(pdf.subprocess, "run", return_value=SimpleNamespace(stdout=b"ALPR\f"))
    def test_multi_stale_preserved_no_refetch(self, convert):
        sources = [{"platform": "legistar", "client": n, "label": n} for n in ("alpha", "beta")]
        previous = {"schema_version": 1, "client": "multi", "clients": ["beta"], "meetings": [
            {"client": "beta", "id": "old", "starts_at": "2026-11-06", "agenda_url": URL}]}
        mapping = {"/v1/alpha/events": client().events(), "/v1/alpha/events/1/eventitems": []}
        scans = pdf.AgendaScans(opener())
        found, report, _ = meetings.scan_sources(sources, lambda c: meetings.fixture_opener(mapping), DAY, None,
                                                 previous, pdf_scans=scans)
        self.assertEqual([r["status"] for r in report], ["ok", "failed"])
        self.assertTrue(next(m for m in found if m["id"] == "old")["stale"])
        self.assertEqual(scans.counts, {"ok": 1})

    @patch.object(pdf.subprocess, "run", return_value=SimpleNamespace(stdout=b"One\fTwo\fALPR\f"))
    def test_single_multi_cli_counts_and_only_kit_written(self, convert):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "campaign.json").write_text(json.dumps({"schema_version": 1, "name": "Synthetic", "county": "Synthetic", "state": "CA", "meeting_sources": [
                {"platform": "legistar", "client": "alpha", "label": "Alpha"}]}))
            mapping = {"/v1/alpha/events": client().events(), "/v1/alpha/events/1/eventitems": []}
            base = dict(matters=False, platform="legistar", tz="UTC", scan_agenda_pdfs=True, online=True,
                        from_json=None, from_xml=None, from_ics=None, feed_url=None, body=None,
                        since=DAY.isoformat(), all_events=False, view_id=None)
            for name in ("alpha", None):
                with patch.object(meetings, "default_opener", meetings.fixture_opener(mapping)), \
                     patch.object(pdf.urllib.request, "build_opener", return_value=opener()), \
                     contextlib.redirect_stdout(io.StringIO()) as out:
                    cli.meetings_cmd(SimpleNamespace(client=name, **base), root)
                self.assertEqual(json.loads(out.getvalue())["pdf_scan"], {"ok": 1})
                doc = json.loads((root / "kit/meetings.json").read_text())
                self.assertEqual(doc["meetings"][0]["pdf_pages_matched"], [3])
            self.assertEqual({p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()},
                             {"campaign.json", "kit/meetings.json"})
            with self.assertRaises(meetings.MeetingsError):
                cli.meetings_cmd(SimpleNamespace(client="alpha", **dict(base, online=False)), root)
