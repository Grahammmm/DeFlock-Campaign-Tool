"""Non-string agenda PDF links are refused before cache hashing and URL parsing; synthetic, offline."""
import contextlib
import copy
import hashlib
import io
import json
import socket
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from campaign_tool import cli, meeting_pdf_scan as pdf, meetings

URL = "https://alpha.legistar.com/agenda.pdf"
URL2 = "https://alpha.legistar.com/other.pdf"
PAGE = "https://alpha.legistar.com/page"
DATA = b"%PDF-1.4 synthetic"
DAY = date(2026, 11, 6)
CANARY = "CANARY-PRIVATE-VALUE"
EMPTY = {"pdf_scan": "refused", "pdf_sha256": None, "pages": 0, "pages_matched": [], "matched_terms": []}


def bad_values():
    """Fresh malformed values each call so mutation checks are meaningful."""
    return [None, True, False, 0, 7, 1.5, [], [CANARY], {}, {"u": CANARY}, CANARY.encode(), bytearray(CANARY.encode())]


def routing_opener():
    """Fake urllib opener answering any requested URL with a valid PDF response."""
    def answer(req, **kw):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.getcode.return_value = 200
        response.geturl.return_value = req.full_url
        response.read.return_value = DATA
        response.getheader.return_value = None
        return response
    return Mock(open=Mock(side_effect=answer))


def client(events, texts=None):
    texts = texts or {}
    return SimpleNamespace(source="legistar", client="alpha", events=lambda **kw: events,
                           event_items=lambda n: [{"EventItemTitle": texts.get(n, "Routine business")}])


def event(n, pdf_field, page=PAGE):
    return {"EventId": n, "EventBodyName": "Alpha Council", "EventDate": DAY.isoformat(),
            "EventInSiteURL": page, "EventAgendaFile": pdf_field}


class NoNetwork(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket.socket, "connect", side_effect=AssertionError("no network"))
        guard.start()
        self.addCleanup(guard.stop)
        converter = patch.object(pdf, "_convert", return_value=b"One\fTwo\fALPR\f")
        self.convert = converter.start()
        self.addCleanup(converter.stop)


class DirectScanTests(NoNetwork):
    def test_scan_nonstring_urls_return_empty_refusal_without_effects(self):
        for value in bad_values():
            with self.subTest(value=type(value).__name__ + repr(value)[:8]):
                before = copy.deepcopy(value)
                http, rule = Mock(), Mock(side_effect=AssertionError("host rule"))
                self.assertEqual(pdf.scan(value, http, platform_host_rule=rule), EMPTY)
                self.assertEqual(value, before)
                rule.assert_not_called()
                http.open.assert_not_called()
                self.convert.assert_not_called()

    def test_malformed_string_urls_keep_existing_refusal(self):
        for url in ("https://[invalid", "http://alpha.legistar.com/a", "https://alpha.legistar.com/a\nb"):
            http = Mock()
            self.assertEqual(pdf.scan(url, http, platform_host_rule=lambda u: True), EMPTY)
            http.open.assert_not_called()


class AgendaScansTests(NoNetwork):
    def test_meeting_admits_url_type_before_cache_hashing(self):
        c = client([])
        for value in bad_values():
            with self.subTest(value=type(value).__name__ + repr(value)[:8]):
                before = copy.deepcopy(value)
                scans = pdf.AgendaScans(routing_opener())
                with patch.object(pdf, "scan", side_effect=AssertionError("scan")):
                    self.assertEqual(scans.meeting(c, "Alpha Council", value), {})
                self.assertEqual(value, before)
                self.assertEqual(dict(scans.counts), {"refused": 1})
                self.assertEqual(sum(scans.bodies.values()), 1)
                self.assertEqual(scans.cache, {})
                scans.opener.open.assert_not_called()
        scans = pdf.AgendaScans(routing_opener())
        first = scans.meeting(c, "Alpha Council", URL)
        for value in bad_values():
            self.assertEqual(scans.meeting(c, "Alpha Council", value), {})
        self.assertEqual(scans.meeting(c, "Alpha Council", URL), first)
        self.assertEqual(scans.counts["ok"], 1)
        self.assertEqual(scans.counts["refused"], len(bad_values()))
        self.assertEqual(list(scans.cache), [(("legistar", "alpha", "Alpha Council"), URL)])
        self.assertEqual(scans.opener.open.call_count, 1)

    def test_repeated_malformed_urls_obey_body_cap_without_cache_retention(self):
        c = client([])
        scans = pdf.AgendaScans(routing_opener())
        for n in range(21):
            self.assertEqual(scans.meeting(c, "Alpha Council", [f"{CANARY}-{n}"]), {})
        self.assertEqual(dict(scans.counts), {"refused": 20})
        self.assertEqual(scans.bodies[("legistar", "alpha", "Alpha Council")], 20)
        self.assertEqual(scans.cache, {})
        scans.opener.open.assert_not_called()
        # 19 malformed attempts, then one valid fetch; the next uncached URL is over the cap.
        scans = pdf.AgendaScans(routing_opener())
        for n in range(19):
            scans.meeting(c, "Alpha Council", {"n": n})
        lead = scans.meeting(c, "Alpha Council", URL)
        self.assertEqual(lead["pdf_pages_matched"], [3])
        self.assertEqual(scans.meeting(c, "Alpha Council", URL2), {})
        self.assertEqual(scans.opener.open.call_count, 1)
        self.assertEqual(scans.counts, {"refused": 19, "ok": 1})
        # A cached valid result is still reusable after the cap; another body has its own budget.
        self.assertEqual(scans.meeting(c, "Alpha Council", URL), lead)
        self.assertEqual(scans.opener.open.call_count, 1)
        self.assertEqual(scans.meeting(c, "Beta Board", URL2)["pdf_pages_matched"], [3])
        self.assertEqual(scans.opener.open.call_count, 2)

    def test_valid_string_cache_result_and_transport_contracts_are_unchanged(self):
        c = client([])
        scans = pdf.AgendaScans(routing_opener())
        first = scans.meeting(c, "Alpha Council", URL)
        self.assertEqual(first, {"relevance": "alpr_item", "matched_in": "agenda_pdf",
                                 "pdf_pages_matched": [3], "pdf_sha256": hashlib.sha256(DATA).hexdigest(),
                                 "matched_terms": ["alpr"]})
        self.assertEqual(scans.meeting(c, "Alpha Council", URL), first)
        self.assertEqual((scans.opener.open.call_count, dict(scans.counts)), (1, {"ok": 1}))
        # A refused string URL is cached too and not rescanned.
        bad = "http://alpha.legistar.com/a"
        self.assertEqual(scans.meeting(c, "Alpha Council", bad), {})
        self.assertEqual(scans.meeting(c, "Alpha Council", bad), {})
        self.assertEqual(dict(scans.counts), {"ok": 1, "refused": 1})


class CallerTests(NoNetwork):
    def test_real_event_handoff_refuses_bad_pdf_field_and_continues(self):
        for value in ([CANARY], {"u": CANARY}, 7, 1.5, True):
            with self.subTest(value=repr(value)[:10]):
                events = [event(1, value), event(2, URL2)]
                before = copy.deepcopy(events)
                scans = pdf.AgendaScans(routing_opener())
                found = meetings.relevant_events(client(events), since=DAY, pdf_scans=scans)
                self.assertEqual([m["event_id"] for m in found], [2])
                self.assertEqual(found[0]["pdf_pages_matched"], [3])
                self.assertEqual(dict(scans.counts), {"refused": 1, "ok": 1})
                self.assertEqual(scans.opener.open.call_count, 1)
                self.assertEqual(events, before)
                self.assertEqual(scans.cache.keys(), {(("legistar", "alpha", "Alpha Council"), URL2)})

    def test_feed_hits_falsey_fallback_and_unscanned_retention_are_unchanged(self):
        # A feed-text hit never reaches the PDF scanner, even with a malformed PDF field.
        scans = pdf.AgendaScans(routing_opener())
        found = meetings.relevant_events(client([event(1, [CANARY])], {1: "ALPR contract"}), since=DAY, pdf_scans=scans)
        self.assertEqual(len(found), 1)
        self.assertNotIn("pdf_pages_matched", found[0])
        self.assertEqual(dict(scans.counts), {})
        # A falsey PDF field keeps the existing fallback to the valid page URL.
        for value in (None, "", 0, False, [], {}):
            with self.subTest(value=repr(value)):
                scans = pdf.AgendaScans(routing_opener())
                found = meetings.relevant_events(client([event(1, value, URL)]), since=DAY, pdf_scans=scans)
                self.assertEqual([m["pdf_pages_matched"] for m in found], [[3]])
                self.assertEqual(dict(scans.counts), {"ok": 1})
        # A truthy malformed field does not fall back to the page URL; --all-events keeps the row unscanned.
        c = client([event(1, [CANARY])])
        c.scanned = lambda n: False
        scans = pdf.AgendaScans(routing_opener())
        found = meetings.relevant_events(c, since=DAY, pdf_scans=scans, all_events=True)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["not_scanned"])
        self.assertNotIn("pdf_pages_matched", found[0])
        self.assertEqual(dict(scans.counts), {"refused": 1})
        scans.opener.open.assert_not_called()


class CliTests(NoNetwork):
    def test_single_and_multi_cli_report_refusal_and_keep_later_valid_lead(self):
        for value in ([CANARY], {"u": CANARY}, 7):
            with self.subTest(value=repr(value)[:10]), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "campaign.json").write_text(json.dumps({
                    "schema_version": 1, "name": "Synthetic", "county": "Synthetic", "state": "CA",
                    "meeting_sources": [{"platform": "legistar", "client": "alpha", "label": "Alpha"}]}))
                events = [event(1, value), event(2, URL2)]
                mapping = {"/v1/alpha/events": events, "/v1/alpha/events/1/eventitems": [],
                           "/v1/alpha/events/2/eventitems": []}
                base = dict(matters=False, platform="legistar", tz="UTC", scan_agenda_pdfs=True, online=True,
                            from_json=None, from_xml=None, from_ics=None, feed_url=None, body=None,
                            since=DAY.isoformat(), all_events=False, view_id=None)
                for name in ("alpha", None):
                    with patch.object(meetings, "default_opener", meetings.fixture_opener(mapping)), \
                         patch.object(pdf.urllib.request, "build_opener", return_value=routing_opener()), \
                         contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
                        cli.meetings_cmd(SimpleNamespace(client=name, **base), root)
                    summary = json.loads(out.getvalue())
                    self.assertEqual(summary["pdf_scan"], {"refused": 1, "ok": 1})
                    kit = (root / "kit/meetings.json").read_text()
                    doc = json.loads(kit)
                    self.assertEqual([m["id"] for m in doc["meetings"]], ["legistar-alpha-2"])
                    self.assertEqual(doc["meetings"][0]["pdf_pages_matched"], [3])
                    for text in (out.getvalue(), err.getvalue(), kit):
                        self.assertNotIn(CANARY, text)
                        self.assertNotIn("Traceback", text)
                self.assertEqual({p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()},
                                 {"campaign.json", "kit/meetings.json"})


if __name__ == "__main__":
    unittest.main()
