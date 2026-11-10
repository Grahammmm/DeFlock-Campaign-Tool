"""Declared Content-Length completeness for agenda PDF fetches, over real HTTPResponse."""
import contextlib
import hashlib
import http.client
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from campaign_tool import cli, meeting_pdf_scan as pdf, meetings

URL = "https://alpha.legistar.com/agenda.pdf"
DATA = b"%PDF-1.4 synthetic agenda body"
DAY = date(2026, 11, 6)
CONVERTED = b"Budget\fALPR renewal\f"


class _Sock:
    def __init__(self, raw):
        self.raw = raw

    def makefile(self, *a, **k):
        return io.BytesIO(self.raw)


def http_response(body, headers=("Content-Length: %d",), chunked=False):
    head = "HTTP/1.1 200 OK\r\n" + "".join(
        (h % len(body) if "%d" in h else h) + "\r\n" for h in headers) + "\r\n"
    raw = head.encode() + body
    response = http.client.HTTPResponse(_Sock(raw))
    response.begin()
    return response


def opener_for(response, url=URL):
    response.geturl = lambda: url
    return Mock(open=Mock(return_value=response))


def scan(response):
    return pdf.scan(URL, opener_for(response), platform_host_rule=lambda u: True)


def fake(header, data=DATA):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.getcode.return_value = 200
    response.geturl.return_value = URL
    response.read.return_value = data
    response.getheader.return_value = header
    return response


class DeclaredLengthTests(unittest.TestCase):
    @patch.object(pdf, "_convert", return_value=CONVERTED)
    def test_real_httpresponse_short_capped_read_refuses_before_conversion(self, convert):
        short = http_response(DATA, headers=("Content-Length: %d",))
        short.headers.replace_header("Content-Length", str(len(DATA) + 100))
        result = scan(short)
        self.assertEqual(result, {"pdf_scan": "failed", "pdf_sha256": None, "pages": 0,
                                  "pages_matched": [], "matched_terms": []})
        convert.assert_not_called()
        exact = scan(http_response(DATA))
        self.assertEqual(exact["pdf_scan"], "ok")
        self.assertEqual(exact["pdf_sha256"], hashlib.sha256(DATA).hexdigest())
        convert.assert_called_once_with(DATA)

    @patch.object(pdf, "_convert", return_value=CONVERTED)
    def test_declared_length_syntax_and_numeric_bounds(self, convert):
        n = str(len(DATA))
        for ok in (None, n, " " + n + "\t", "000" + n):
            self.assertEqual(pdf.scan(URL, Mock(open=Mock(return_value=fake(ok))),
                                      platform_host_rule=lambda u: True)["pdf_scan"], "ok", ok)
        for bad in ("", "  ", "+" + n, "-" + n, n + "," + n, "1e2", n + ".0", "\u0661\u0662",
                    n + " x", 12, b"12"):
            response = fake(bad)
            result = pdf.scan(URL, Mock(open=Mock(return_value=response)), platform_host_rule=lambda u: True)
            self.assertEqual(result["pdf_scan"], "failed", repr(bad))
            self.assertIsNone(result["pdf_sha256"])
            response.read.assert_not_called()
            response.__exit__.assert_called()
        convert.reset_mock()
        for huge in ("0" * 10 + str(pdf.MAX_BYTES + 1), "9" * 10000):
            response = fake(huge)
            result = pdf.scan(URL, Mock(open=Mock(return_value=response)), platform_host_rule=lambda u: True)
            self.assertEqual(result["pdf_scan"], "too_large")
            response.read.assert_not_called()
        convert.assert_not_called()

    @patch.object(pdf, "_convert", return_value=CONVERTED)
    def test_actual_cap_is_enforced_independently_of_header(self, convert):
        big = b"%PDF-" + b"x" * (pdf.MAX_BYTES - 5)
        self.assertEqual(len(big), pdf.MAX_BYTES)
        run = lambda header, data: pdf.scan(URL, Mock(open=Mock(return_value=fake(header, data))),
                                            platform_host_rule=lambda u: True)["pdf_scan"]
        self.assertEqual(run(str(pdf.MAX_BYTES), big), "ok")
        self.assertEqual(run(None, big + b"x"), "too_large")
        self.assertEqual(run("10", big + b"x"), "too_large")
        self.assertEqual(run(str(pdf.MAX_BYTES + 1), big), "too_large")
        self.assertEqual(run(str(len(DATA) + 1), DATA), "failed")
        self.assertEqual(run(str(len(DATA) - 1), DATA), "failed")

    @patch.object(pdf, "_convert", return_value=CONVERTED)
    def test_complete_unknown_length_and_chunked_responses_still_work(self, convert):
        close_delimited = http_response(DATA, headers=("Connection: close",))
        self.assertEqual(scan(close_delimited)["pdf_scan"], "ok")
        chunks = b"%x\r\n%s\r\n0\r\n\r\n" % (len(DATA), DATA)
        chunked = http_response(chunks, headers=("Transfer-Encoding: chunked",))
        self.assertEqual(scan(chunked)["pdf_scan"], "ok")
        broken = http_response(b"ff\r\n" + DATA, headers=("Transfer-Encoding: chunked",))
        self.assertEqual(scan(broken)["pdf_scan"], "failed")

    @patch.object(pdf, "_convert", return_value=CONVERTED)
    def test_transfer_refusal_is_cached_once_without_keyword_lead(self, convert):
        response = http_response(DATA)
        response.headers.replace_header("Content-Length", str(len(DATA) + 100))
        http = opener_for(response)
        scans = pdf.AgendaScans(http)
        c = SimpleNamespace(source="legistar", client="alpha")
        for _ in range(2):
            self.assertEqual(scans.meeting(c, "Alpha Council", URL), {})
        self.assertEqual(http.open.call_count, 1)
        self.assertEqual(scans.counts, {"failed": 1})
        self.assertEqual(scans.cache[(("legistar", "alpha", "Alpha Council"), URL)]["pdf_scan"], "failed")
        events = [{"EventId": 1, "EventBodyName": "Alpha Council", "EventDate": DAY.isoformat(),
                   "EventAgendaFile": URL}]
        client = SimpleNamespace(source="legistar", client="alpha", events=lambda **kw: events,
                                 event_items=lambda n: [{"EventItemTitle": "Routine business"}])
        found = meetings.relevant_events(client, since=DAY, pdf_scans=pdf.AgendaScans(opener_for(
            self._short())))
        self.assertEqual(found, [])

    def _short(self):
        response = http_response(DATA)
        response.headers.replace_header("Content-Length", str(len(DATA) + 100))
        return response

    @patch.object(pdf, "_convert", return_value=CONVERTED)
    def test_cli_reports_failed_transfer_without_persisting_false_pdf_evidence(self, convert):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "campaign.json").write_text(json.dumps({
                "schema_version": 1, "name": "Synthetic", "county": "Synthetic", "state": "CA",
                "meeting_sources": [{"platform": "legistar", "client": "alpha", "label": "Alpha"}]}))
            events = [{"EventId": 1, "EventBodyName": "Alpha Council", "EventDate": DAY.isoformat(),
                       "EventAgendaFile": URL}]
            mapping = {"/v1/alpha/events": events, "/v1/alpha/events/1/eventitems": []}
            base = dict(matters=False, platform="legistar", tz="UTC", scan_agenda_pdfs=True, online=True,
                        from_json=None, from_xml=None, from_ics=None, feed_url=None, body=None,
                        since=DAY.isoformat(), all_events=False, view_id=None)
            for response, expected, matched in ((self._short(), {"failed": 1}, False),
                                                (http_response(DATA), {"ok": 1}, True)):
                with patch.object(meetings, "default_opener", meetings.fixture_opener(mapping)), \
                     patch.object(pdf.urllib.request, "build_opener", return_value=opener_for(response)), \
                     contextlib.redirect_stdout(io.StringIO()) as out:
                    cli.meetings_cmd(SimpleNamespace(client="alpha", **base), root)
                self.assertEqual(json.loads(out.getvalue())["pdf_scan"], expected)
                text = (root / "kit/meetings.json").read_text() if (root / "kit/meetings.json").exists() else ""
                self.assertEqual("pdf_sha256" in text, matched)
                self.assertEqual("agenda_pdf" in text, matched)
            self.assertEqual({p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()},
                             {"campaign.json", "kit/meetings.json"})


if __name__ == "__main__":
    unittest.main()
