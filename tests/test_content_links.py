"""DF1107-11: `content link-check` inventory and opt-in availability check. Synthetic content, injected opener, no network."""
import contextlib
import http.client
import hashlib
import io
import json
import shutil
import socket
import sys
import tempfile
import unittest
import urllib.error
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import cli, content_links

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 11, 5)
WB = "https://web.archive.org/web/20260101000000/https://county.example.org/policy.pdf"
CONTRACT_URL = "https://county.example.org/contract.pdf"
POLICY_URL = "https://county.example.org/policy.pdf"
SENTINEL = "https://private-sentinel.example.org/never"


def agencies_document():
    return [{
        "agency_id": "ca-cedar-sheriff", "name": "Cedar Sheriff (fictional)", "kind": "sheriff", "jurisdiction_name": "Cedar",
        "records_url": "https://county.example.org/records", "records_email": None,
        "portal": {"vendor": "nextrequest", "url": "https://cedar.nextrequest.example.org/"},
        "verified": True, "flock_transparency_slug": "cedar-sheriff-ca",
        "verification": {"source_url": "https://county.example.org/contact", "checked_on": "2026-10-01", "fields": []},
        "profile": {
            "alpr_policy": {"url": POLICY_URL, "source_url": "https://county.example.org/policies", "checked_on": "2026-10-30",
                            "archived_url": WB, "archived_on": "2026-01-01"},
            "contract": {"vendor": "Synthetic Vendor", "start": "2025-01-01", "end": "2027-01-01", "source_url": CONTRACT_URL,
                         "checked_on": "2026-10-01"},
            "transparency": [{"checked_on": "2026-10-02", "source_url": "https://transparency.flocksafety.com/cedar-sheriff-ca"}],
            "funding": [{"kind": "grant", "name": "Synthetic grant", "as_stated_in": "Grant notice",
                         "source_url": "https://state.example.org/grant", "checked_on": "2026-10-03"}],
            "reports": [{"title": "Annual report", "kind": "annual_report", "period_start": "2025-01-01",
                         "period_end": "2025-12-31", "source_url": "https://county.example.org/report",
                         "checked_on": "2026-10-03"}],
            "pre_implementation_meeting": {"status": "found", "checked_on": "2026-10-04", "meeting_on": "2024-05-01",
                                           "body": "Cedar Council", "source_url": "https://county.example.org/minutes-2024"},
        },
    }, {
        "agency_id": "ca-cedar-police", "slug": "cedar", "name": "Cedar Police (fictional)", "kind": "police", "jurisdiction_name": "Cedar",
        "records_url": "https://county.example.org/records", "records_email": None, "portal": {"vendor": "none", "url": None},
        "verified": False,
        "profile": {"alpr_policy": {"url": "https://county.example.org/police-policy", "source_url": "https://county.example.org/policies",
                                    "checked_on": "2026-10-30"}},
    }]


def meeting_document():
    return {
        "id": "cedar-council-2026-10", "body": "Cedar City Council (fictional)", "starts_at": "2026-10-14T18:00:00-07:00",
        "agenda_url": "https://council.example.org/agenda", "agenda_item": "Item 7", "location": "Cedar Civic Center",
        "comment_kit_md": "Read [the agenda packet](https://council.example.org/packet#p2) first.",
        "agency_id": "ca-cedar-police", "status": "scheduled",
        "participation": {"in_person": True, "remote": None, "written_comment": None, "speaker_minutes": 2,
                          "source_url": "https://council.example.org/how-to-comment", "verified_by": "Organizer",
                          "verified_at": "2026-10-01"},
        "outcome": {"result": "approved", "on": "2026-10-14", "source_url": "https://council.example.org/result",
                    "verified_by": "Organizer", "verified_at": "2026-10-15",
                    "recording_url": "https://video.example.org/watch?v=1", "recording_start": "0:12:00",
                    "minutes_url": "https://council.example.org/minutes", "minutes_approved_on": "2026-10-28"},
    }


def sha_hex(text):
    return hashlib.sha256(text.encode()).hexdigest()


class FakeResponse:
    def __init__(self, status=200, location=None, body=b"x" * 10, read_error=None, log=(), name=""):
        self.status, self.headers = status, ({"Location": location} if location is not None else {})
        self.body, self.read_error, self.log, self.name, self.closed = body, read_error, log, name, False

    def read(self, size=-1):
        self.log.append(("read", self.name, size))
        if self.read_error:
            raise self.read_error
        return self.body[:size]

    def close(self):
        self.closed = True
        self.log.append(("close", self.name))


class FakeOpener:
    """Scripted opener: url -> FakeResponse spec (dict), an exception to raise, or a callable."""
    def __init__(self, script):
        self.script, self.log, self.requests, self.responses = script, [], [], []

    def open(self, request, timeout=None):
        url = request.full_url
        self.requests.append((request.get_method(), url, timeout, dict(request.header_items())))
        self.log.append(("open", url))
        assert not [r for r in self.responses if not r.closed], "a previous response is still open"
        spec = self.script.get(url, {"status": 200})
        if isinstance(spec, BaseException):
            raise spec
        response = FakeResponse(log=self.log, name=url, **spec)
        self.responses.append(response)
        return response


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() if p.is_file() else None for p in sorted(Path(root).rglob("*"))}


class LinkCheckBase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-link-check-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root)
        content = self.root / "content"
        (content / "agencies.json").write_text(json.dumps(agencies_document()), encoding="utf-8")
        (content / "meetings" / "2026-10-cedar-council.json").write_text(json.dumps(meeting_document()), encoding="utf-8")
        (content / "sources.json").write_text(json.dumps([
            {"sha256": sha_hex("a"), "title": "Remote copy", "agency_id": "ca-cedar-police", "received_at": "2026-09-01T00:00:00Z",
             "public_url": "https://records.example.org/doc.pdf", "fragment": "remote"},
            {"sha256": sha_hex("b"), "title": "Local copy", "agency_id": "ca-cedar-police", "received_at": "2026-09-01T00:00:00Z",
             "public_url": None, "fragment": "local"}]), encoding="utf-8")
        (content / "officials.json").write_text(json.dumps({"schema_version": 1, "bodies": [{
            "agency_id": "ca-cedar-police", "officials": [{
                "name": "Pat Example", "role": "Chief", "profile_url": "https://county.example.org/chief",
                "source_url": "https://county.example.org/staff", "checked_on": "2026-10-20"}]}]}), encoding="utf-8")
        (content / "pages").mkdir(exist_ok=True)
        (content / "pages" / "links.json").write_text(json.dumps({
            "title": "Links", "body_md": "See [a page-only source](https://pages.example.org/only) and "
                                         "[again](https://pages.example.org/only). [Same site](https://cedar-records.example.invalid/x). "
                                         "[http](http://plain.example.org/no) <https://raw.example.org/no>"}), encoding="utf-8")
        # private material that must never be listed
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "sent").mkdir(exist_ok=True)
        (self.root / "kit" / "sent" / "note.md").write_text(SENTINEL, encoding="utf-8")
        (self.root / "kit" / "drafts.json").write_text(json.dumps({"url": SENTINEL}), encoding="utf-8")

    def groups(self):
        return content_links.collect_links(self.root, today=TODAY)[0]

    def by_url(self):
        return {g["url"]: g for g in self.groups()}


class InventoryTests(LinkCheckBase):
    def test_offline_never_constructs_opener_or_opens_socket(self):
        before = snapshot(self.root)
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("socket opened")), \
                mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("dns")), \
                mock.patch.object(content_links, "real_opener", side_effect=AssertionError("opener built")):
            text, code = content_links.run_cli(self.root, today=TODAY)
        self.assertEqual(code, 0)
        self.assertTrue(text.startswith(content_links.OFFLINE_LINE + "\n"))
        self.assertEqual(snapshot(self.root), before)  # no file created or changed
        urls = self.by_url()
        for expected, field in (
                (POLICY_URL, "content/agencies.json:ca-cedar-sheriff.profile.alpr_policy.url"),
                ("https://county.example.org/policies", "content/agencies.json:ca-cedar-sheriff.profile.alpr_policy.source_url"),
                (CONTRACT_URL, "content/agencies.json:ca-cedar-sheriff.profile.contract.source_url"),
                ("https://transparency.flocksafety.com/cedar-sheriff-ca", "profile.transparency[0].source_url"),
                ("https://state.example.org/grant", "profile.funding[0].source_url"),
                ("https://county.example.org/report", "profile.reports[0].source_url"),
                ("https://county.example.org/minutes-2024", "profile.pre_implementation_meeting.source_url"),
                ("https://county.example.org/records", "ca-cedar-sheriff.records_url"),
                ("https://cedar.nextrequest.example.org/", "portal.url"),
                ("https://county.example.org/contact", "verification.source_url"),
                ("https://records.example.org/doc.pdf", "content/sources.json[0].public_url"),
                ("https://council.example.org/agenda", "content/meetings/2026-10-cedar-council.json:agenda_url"),
                ("https://council.example.org/how-to-comment", "participation.source_url"),
                ("https://council.example.org/result", "outcome.source_url"),
                ("https://council.example.org/minutes", "outcome.minutes_url"),
                ("https://video.example.org/watch?v=1", "outcome.recording_url"),
                ("https://county.example.org/chief", "content/officials.json:ca-cedar-police.officials[0].profile_url"),
                ("https://county.example.org/staff", "officials[0].source_url"),
                ("https://pages.example.org/only", "content/pages/links.json:body_md[anchor 1]"),
                ("https://council.example.org/packet#p2", "comment_kit_md[anchor 1]")):
            self.assertIn(expected, urls)
            self.assertTrue(any(field in c for c in urls[expected]["citations"]), (expected, field, urls[expected]["citations"]))
        # repeated uses of one URL keep every locator, in stable order
        self.assertEqual(urls["https://pages.example.org/only"]["citations"],
                         ["content/pages/links.json:body_md[anchor 1]", "content/pages/links.json:body_md[anchor 2]"])
        self.assertEqual(len(urls["https://county.example.org/records"]["citations"]), 2)
        listed = " ".join(urls)
        for absent in ("/library/local.pdf", "http://plain.example.org", "raw.example.org", "cedar-records.example.invalid", "private-sentinel"):
            self.assertNotIn(absent, listed)
        self.assertNotIn(SENTINEL, text)
        self.assertEqual(text, content_links.run_cli(self.root, today=TODAY)[0])  # byte-deterministic

    def test_press_newsletter_coverage_and_comment_source_are_listed_and_fetched_online_only(self):
        content = self.root / "content"
        site = json.loads((content / "site.json").read_text(encoding="utf-8"))
        site["press"] = {"contact_role": "Press", "contact_email": "press@example.org",
                         "about_md": "See [press source](https://press-only.example.org/p)."}
        (content / "site.json").write_text(json.dumps(site), encoding="utf-8")
        (content / "newsletters").mkdir(exist_ok=True)
        (content / "newsletters" / "2026-10-20.json").write_text(json.dumps({
            "schema_version": 1, "sent_on": "2026-10-20", "title": "Issue",
            "body_md": "Read [letter source](https://news-only.example.org/n)."}), encoding="utf-8")
        (content / "coverage.json").write_text(json.dumps({"schema_version": 1, "entries": [{
            "outlet": "Daily", "headline": "Story", "url": "https://coverage-only.example.org/c",
            "published_on": "2026-10-21"}]}), encoding="utf-8")
        meeting = meeting_document()
        meeting["outcome"]["spoke"] = 3
        meeting["outcome"]["comment_source_url"] = "https://comment-only.example.org/c"
        (content / "meetings" / "2026-10-cedar-council.json").write_text(json.dumps(meeting), encoding="utf-8")
        urls = self.by_url()
        for url, locator in (("https://press-only.example.org/p", "content/site.json:press.about_md[anchor 1]"),
                             ("https://news-only.example.org/n", "content/newsletters/2026-10-20.json:body_md[anchor 1]"),
                             ("https://coverage-only.example.org/c", "content/coverage.json:entries[0].url"),
                             ("https://comment-only.example.org/c",
                              "content/meetings/2026-10-cedar-council.json:outcome.comment_source_url")):
            self.assertEqual(urls[url]["citations"], [locator])
        with mock.patch.object(content_links, "real_opener", side_effect=AssertionError("opener built")):
            content_links.run_cli(self.root, today=TODAY)  # offline: no opener
        opener = FakeOpener({})
        content_links.run_cli(self.root, True, opener=opener, today=TODAY)
        fetched = [url for _, url, _, _ in opener.requests]
        for url in ("https://press-only.example.org/p", "https://news-only.example.org/n",
                    "https://coverage-only.example.org/c", "https://comment-only.example.org/c"):
            self.assertEqual(fetched.count(url), 1)

    def test_published_only_and_archive_provenance(self):
        urls = self.by_url()
        self.assertEqual(urls[POLICY_URL]["archives"], [{
            "locator": "content/agencies.json:ca-cedar-sheriff.profile.alpr_policy.url", "archived_url": WB, "archived_on": "2026-01-01"}])
        self.assertIn(WB, urls)  # a published archive link is itself an inventory entry
        self.assertTrue(urls[WB]["citations"][0].endswith("alpr_policy.archived_url"))
        self.assertEqual(urls["https://county.example.org/policies"]["archives"], [])
        # a failed original stays gone even though its archive is ok
        opener = FakeOpener({POLICY_URL: {"status": 404}})
        text, code = content_links.run_cli(self.root, True, opener=opener, today=TODAY)
        self.assertEqual(code, 3)
        block = text.split(POLICY_URL + "\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn("result: gone (HTTP 404)", block)
        self.assertIn(WB, block)
        self.assertNotIn(SENTINEL, " ".join(url for _, url, _, _ in opener.requests))
        self.assertEqual([url for _, url, _, _ in opener.requests].count(WB), 1)

    def test_no_content_or_invalid_content_refuses_before_any_opener_call(self):
        bare = Path(tempfile.mkdtemp(prefix="synthetic-link-bare-"))
        self.addCleanup(shutil.rmtree, bare, ignore_errors=True)
        opener = FakeOpener({})
        with self.assertRaises(content_links.LinkCheckError):
            content_links.run_cli(bare, True, opener=opener)
        self.assertEqual((opener.requests, list(bare.iterdir())), ([], []))  # content/ not scaffolded
        (self.root / "content" / "sources.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            content_links.run_cli(self.root, True, opener=opener, today=TODAY)
        self.assertEqual(opener.requests, [])

    def test_cli_default_is_offline_and_exit_codes(self):
        out, err = io.StringIO(), io.StringIO()
        argv = ["campaign", "content", "link-check", "--directory", str(self.root)]
        before = snapshot(self.root)
        with mock.patch.object(sys, "argv", argv), mock.patch.object(content_links, "real_opener", side_effect=AssertionError), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cli.main()
        self.assertIn(content_links.OFFLINE_LINE, out.getvalue())
        self.assertEqual(snapshot(self.root), before)
        empty = Path(tempfile.mkdtemp(prefix="synthetic-link-empty-"))
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        with mock.patch.object(sys, "argv", ["campaign", "content", "link-check", "--directory", str(empty)]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as raised:
            cli.main()
        self.assertEqual(raised.exception.code, 1)


class TransportTests(LinkCheckBase):
    def entries(self, *urls):
        return [{"url": url, "citations": ["x"], "archives": []} for url in urls]

    def test_get_limits_headers_and_serial_close(self):
        opener = FakeOpener({"https://a.example.org/big": {"status": 200, "body": b"y" * 500000}})
        results = content_links.check_links(self.entries(
            "https://a.example.org/big", "https://a.example.org/big#one", "https://a.example.org/big#two",
            "https://a.example.org/big"), opener=opener)
        self.assertEqual([r["status"] for r in results], ["ok"] * 4)
        self.assertEqual(len(opener.requests), 1)  # fragments and duplicates share one GET
        method, url, timeout, headers = opener.requests[0]
        self.assertEqual((method, url, timeout), ("GET", "https://a.example.org/big", 15))
        self.assertEqual({k.lower(): v for k, v in headers.items()}, {"user-agent": "DeFlock-Campaign-Tool/link-check"})
        self.assertEqual([e for e in opener.log if e[0] == "read"], [("read", "https://a.example.org/big", 65536)])
        self.assertTrue(all(r.closed for r in opener.responses))
        text = content_links.render(self.entries("https://a.example.org/big"), "n", results[:1])
        self.assertNotIn("yyyy", text)

    def test_read_failure_is_reported_and_response_closed(self):
        opener = FakeOpener({"https://a.example.org/r": {"read_error": TimeoutError()},
                             "https://a.example.org/s": {"read_error": ConnectionResetError()}})
        results = content_links.check_links(self.entries("https://a.example.org/r", "https://a.example.org/s"), opener=opener)
        self.assertEqual([r["status"] for r in results], ["timeout", "error"])
        self.assertTrue(all(r.closed for r in opener.responses))

    def test_redirects_bound_and_scheme(self):
        for code in (301, 302, 303, 307, 308):
            opener = FakeOpener({"https://a.example.org/1": {"status": code, "location": "/2"},
                                 "https://a.example.org/2": {"status": code, "location": "https://b.example.org/3"}})
            [result] = content_links.check_links(self.entries("https://a.example.org/1"), opener=opener)
            self.assertEqual((result["status"], result["chain"]), ("moved", ["https://a.example.org/2", "https://b.example.org/3"]))
        # exactly five followed, the sixth refused (and shown)
        script = {f"https://a.example.org/{n}": {"status": 302, "location": f"/{n + 1}"} for n in range(1, 8)}
        opener = FakeOpener(script)
        [result] = content_links.check_links(self.entries("https://a.example.org/1"), opener=opener)
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(opener.requests), 6)
        self.assertEqual(result["chain"][-1], "https://a.example.org/7")
        script = {f"https://a.example.org/{n}": {"status": 302, "location": f"/{n + 1}"} for n in range(1, 5)}
        opener = FakeOpener(script)  # five redirects then a 200 is still followed
        script["https://a.example.org/5"] = {"status": 302, "location": "/6"}
        [result] = content_links.check_links(self.entries("https://a.example.org/1"), opener=opener)
        self.assertEqual((result["status"], len(result["chain"])), ("moved", 5))
        # cycle, absent and bad locations
        opener = FakeOpener({"https://a.example.org/a": {"status": 302, "location": "/b"},
                             "https://a.example.org/b": {"status": 302, "location": "/a"},
                             "https://a.example.org/n": {"status": 302},
                             "https://a.example.org/e": {"status": 302, "location": ""}})
        results = content_links.check_links(self.entries("https://a.example.org/a", "https://a.example.org/n",
                                                         "https://a.example.org/e"), opener=opener)
        self.assertEqual([r["status"] for r in results], ["error"] * 3)
        self.assertIn("loop", results[0]["detail"])
        for bad in ("http://a.example.org/x", "file:///etc/passwd", "data:text/plain,hi", "javascript:alert(1)",
                    "https://" + "user" + ":" + "pw" + "@a.example.org/x", "https://a.example.org/x\x1b[31m", "ftp://a.example.org/x"):
            opener = FakeOpener({"https://a.example.org/1": {"status": 302, "location": bad}})
            [result] = content_links.check_links(self.entries("https://a.example.org/1"), opener=opener)
            self.assertEqual(result["status"], "error", bad)
            self.assertEqual(len(opener.requests), 1, bad)  # the refused target is never opened
            text = content_links.render(self.entries("https://a.example.org/1"), "n", [result])
            self.assertIn("redirect ->", text)
            self.assertNotIn("\x1b", text)
        # a redirect to a 404 is gone
        opener = FakeOpener({"https://a.example.org/1": {"status": 301, "location": "/2"}, "https://a.example.org/2": {"status": 410}})
        [result] = content_links.check_links(self.entries("https://a.example.org/1"), opener=opener)
        self.assertEqual((result["status"], result["chain"]), ("gone", ["https://a.example.org/2"]))

    def test_malformed_and_control_redirects_never_abort_or_fetch(self):
        base = "https://a.example.org/1"
        for bad in ("https://[", "\nhttps://a.example.org/ok", "https://a.example.org/o\tk", "https://a.example.org/o\r\nk",
                    "https://a.example.org/ok\n", " https://a.example.org/ok", "https://a.example.org:99999999/x"):
            opener = FakeOpener({base: {"status": 302, "location": bad}})
            results = content_links.check_links(self.entries(base, "https://a.example.org/after"), opener=opener)
            self.assertEqual([r["status"] for r in results], ["error", "ok"], repr(bad))
            self.assertEqual([url for _, url, _, _ in opener.requests], [base, "https://a.example.org/after"], repr(bad))
            text = content_links.render(self.entries(base, "https://a.example.org/after"), "n", results)
            self.assertNotIn("\n\t", text)
            self.assertNotIn("\r", text)
            self.assertTrue(all(r.closed for r in opener.responses))

    def test_malformed_open_and_truncated_read_are_per_url_errors(self):
        opener = FakeOpener({"https://a.example.org/p": ValueError("bad port"),
                             "https://a.example.org/t": {"read_error": http.client.IncompleteRead(b"x")},
                             "https://a.example.org/h": http.client.BadStatusLine("x")})
        results = content_links.check_links(self.entries("https://a.example.org/p", "https://a.example.org/t",
                                                         "https://a.example.org/h", "https://a.example.org/ok"), opener=opener)
        self.assertEqual([r["status"] for r in results], ["error", "error", "error", "ok"])
        self.assertTrue(all(r.closed for r in opener.responses))

    def test_shared_redirect_targets_are_fetched_once(self):
        opener = FakeOpener({"https://a.example.org/1": {"status": 301, "location": "/z"},
                             "https://a.example.org/2": {"status": 301, "location": "/z"}})
        results = content_links.check_links(self.entries("https://a.example.org/1", "https://a.example.org/2", "https://a.example.org/z"),
                                            opener=opener)
        self.assertEqual([r["status"] for r in results], ["moved", "moved", "ok"])
        self.assertEqual(sorted(url for _, url, _, _ in opener.requests).count("https://a.example.org/z"), 1)
        self.assertEqual([r["chain"] for r in results[:2]], [["https://a.example.org/z"]] * 2)

    def test_terminal_errors_do_not_short_circuit(self):
        def http_error(code):
            return urllib.error.HTTPError("https://x", code, "m", {}, io.BytesIO(b""))
        opener = FakeOpener({
            "https://e.example.org/404": {"status": 404}, "https://e.example.org/410": {"status": 410},
            "https://e.example.org/403": {"status": 403}, "https://e.example.org/429": {"status": 429},
            "https://e.example.org/500": {"status": 500},
            "https://e.example.org/tls": urllib.error.URLError(__import__("ssl").SSLCertVerificationError("bad")),
            "https://e.example.org/dns": urllib.error.URLError(socket.gaierror(-2, "x")),
            "https://e.example.org/refused": urllib.error.URLError(ConnectionRefusedError()),
            "https://e.example.org/t1": TimeoutError(), "https://e.example.org/t2": urllib.error.URLError(socket.timeout()),
            "https://e.example.org/herr": http_error(404),
            "https://e.example.org/ok": {"status": 200}})
        names = ["404", "410", "403", "429", "500", "tls", "dns", "refused", "t1", "t2", "herr", "ok"]
        results = content_links.check_links(self.entries(*[f"https://e.example.org/{n}" for n in names]), opener=opener)
        self.assertEqual([r["status"] for r in results],
                         ["gone", "gone", "error", "error", "error", "error", "error", "error", "timeout", "timeout", "gone", "ok"])
        self.assertEqual(results[5]["detail"], "TLS error")
        self.assertEqual(len(opener.requests), len(names))
        self.assertTrue(all(r.closed for r in opener.responses))
        # exit codes: only ok/moved (and an empty inventory) exit 0
        for script, expected in (({}, 0), ({POLICY_URL: {"status": 301, "location": "https://county.example.org/new"}}, 0),
                                 ({CONTRACT_URL: {"status": 500}}, 3), ({CONTRACT_URL: TimeoutError()}, 3)):
            text, code = content_links.run_cli(self.root, True, opener=FakeOpener(script), today=TODAY)
            self.assertEqual(code, expected, text)
        with mock.patch.object(content_links, "collect_links", return_value=([], "n")):
            self.assertEqual(content_links.run_cli(self.root, True, opener=FakeOpener({}))[1], 0)
        # remaining URLs were still processed after the failure
        opener = FakeOpener({CONTRACT_URL: {"status": 500}})
        content_links.run_cli(self.root, True, opener=opener, today=TODAY)
        self.assertEqual(len(opener.requests), len(self.groups()))


if __name__ == "__main__":
    unittest.main()
