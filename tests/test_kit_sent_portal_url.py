"""Portal-filed requests: kit-sent --channel portal --request-url, shown by kit-status/kit-due/--csv and quoted in the
follow-up and denial-reply drafts. Offline, synthetic: no socket or urlopen may be used anywhere in this module."""
import contextlib
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit_track  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402

PORTAL = "https://cityx.nextrequest.com/"
URL = "https://cityx.nextrequest.com/requests/24-118"
SENT_ON = "2026-03-02"
LINE = "Portal request: " + URL
REFUSED = (  # (url, the words of its own message)
    ("https://other.nextrequest.com/requests/1", "host other.nextrequest.com is not the agency's verified portal host "
                                                  "cityx.nextrequest.com"),
    ("http://cityx.nextrequest.com/requests/1", "must start with https://"),
    ("https://u@cityx.nextrequest.com/requests/1", "must not include a login"),
    ("https://u:p@cityx.nextrequest.com/requests/1", "must not include a login"),
    ("https://cityx.nextrequest.com:8443/requests/1", "must not include a port"),
    ("https://cityx.nextrequest.com/requests/1?id=1", "must not include a query"),
    ("https://cityx.nextrequest.com/requests/1#top", "must not include a fragment"),
    ("https://cityx.nextrequest.com/" + "a" * (301 - len(PORTAL)), "at most 300 characters"),
    ("https://cityx.nextrequest.com/requests/ 1", "must not contain spaces or control characters"),
    ("https://cityx.nextrequest.com/requests/1\n", "must not contain spaces or control characters"),
    ("ftp://cityx.nextrequest.com/requests/1", "must start with https://"),
    ("https://cityx.nextrequest.com.evil.example/requests/1", "is not the agency's verified portal host"),
    ("https://evil.example/https://cityx.nextrequest.com/", "is not the agency's verified portal host"),
)


def run(*arguments):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class KitSentPortalUrlTests(unittest.TestCase):
    def setUp(self):
        self.urlopen = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        self.opener = self.urlopen.start()
        self.addCleanup(self.urlopen.stop)
        sock = mock.patch("socket.socket", side_effect=AssertionError("socket opened"))
        sock.start()
        self.addCleanup(sock.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-portal-url-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        self.assertEqual(run("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                             "--state", "CA")[0], 0)
        self.assertEqual(run("kit", "--directory", self.root)[0], 0)
        self.a, self.b = sorted(p.stem for p in (self.root / "kit" / "requests").glob("*.md"))[:2]
        ready_drafts(self.root)  # both agencies verified by email; only a has a portal
        code, _, err = run("kit-verify-agency", "--directory", self.root, "--agency-id", self.a, "--source-url",
                           "https://example.org/portal", "--checked-on", "2026-01-02", "--portal", PORTAL)
        self.assertEqual(code, 0, err)
        self.sent = self.root / "kit" / "sent.json"

    def send(self, agency=None, channel="portal", url: str | None = URL, *extra):
        args = ["kit-sent", "--directory", self.root, "--agency", agency or self.a, "--date", SENT_ON,
                "--channel", channel, *extra]
        return run(*args, *([] if url is None else ["--request-url", url]))

    def ok(self, *args, **kwargs):
        code, out, err = self.send(*args, **kwargs)
        self.assertEqual(code, 0, err)
        return out

    def entry(self, agency=None):
        return json.loads(self.sent.read_text(encoding="utf-8"))["requests"][agency or self.a]

    def refused(self, words, *args, **kwargs):
        before = self.sent.read_bytes() if self.sent.exists() else None
        code, out, err = self.send(*args, **kwargs)
        self.assertEqual(code, 1, out)
        self.assertTrue(err.startswith("Stopped: "), err)
        self.assertIn(words, err)
        self.assertEqual(self.sent.read_bytes() if self.sent.exists() else None, before)  # nothing written

    # 1. stored, and shown once by every reader
    def test_portal_url_is_stored_and_shown_once_by_status_due_csv_followup_and_denial_reply(self):
        out = self.ok(self.a, "portal", URL, "--reference", "24-118")
        self.assertEqual(json.loads(out[:out.rindex("}") + 1])["request_url"], URL)
        self.assertEqual(self.entry()["request_url"], URL)
        self.assertEqual(kit_track.read_sent(self.root)[self.a]["request_url"], URL)
        code, out, err = run("kit-status", "--directory", self.root)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.count(URL), 1)
        self.assertEqual(json.loads(out)["requests"][0]["request_url"], URL)
        self.assertEqual(err.count(LINE), 1)
        code, out, err = run("kit-due", "--directory", self.root, "--as-of", "2026-03-03")
        self.assertEqual((code, out.count(URL)), (0, 1), err)
        self.assertEqual(json.loads(out)["open"][0]["request_url"], URL)
        target = self.tmp / "tracker.csv"
        self.assertEqual(run("kit-status", "--directory", self.root, "--csv", target)[0], 0)
        rows = list(csv.reader(target.read_text(encoding="utf-8").splitlines()))
        self.assertEqual(rows[0][-1], "request_url")
        self.assertEqual(rows[1][-1], URL)
        self.assertEqual(target.read_text(encoding="utf-8").count(URL), 1)
        code, out, err = run("kit-followup", "--directory", self.root, "--agency", self.a, "--as-of", "2026-06-01")
        self.assertEqual(code, 0, err)
        text = (self.root / "kit" / "followups" / f"{self.a}-2026-06-01.md").read_text(encoding="utf-8")
        self.assertEqual(text.count(URL), 1)
        self.assertIn("Regarding your reference 24-118,\n\n" + LINE + "\n\n", text)
        self.assertEqual(run("kit-response", "--directory", self.root, "--agency", self.a, "--date", "2026-03-20",
                             "--kind", "denied")[0], 0)
        code, out, err = run("kit-denial-reply", "--directory", self.root, "--agency", self.a)
        self.assertEqual(code, 0, err)
        text = (self.root / "kit" / "followups" / f"{self.a}-denial-2026-03-20.md").read_text(encoding="utf-8")
        self.assertEqual(text.count(URL), 1)
        self.assertIn("Regarding your reference 24-118,\n\n" + LINE + "\n\nOn 2026-03-02", text)

    def test_drafts_quote_the_link_without_a_reference_and_not_for_a_portal_request_with_no_link(self):
        self.ok()
        self.assertEqual(run("kit-followup", "--directory", self.root, "--agency", self.a, "--as-of", "2026-06-01")[0], 0)
        text = (self.root / "kit" / "followups" / f"{self.a}-2026-06-01.md").read_text(encoding="utf-8")
        self.assertIn("To: ", text)
        self.assertEqual(text.count(LINE), 1)
        self.assertNotIn("reference", text)
        self.send(self.b, "portal", None)
        self.assertEqual(run("kit-followup", "--directory", self.root, "--agency", self.b, "--as-of", "2026-06-01")[0], 0)
        other = (self.root / "kit" / "followups" / f"{self.b}-2026-06-01.md").read_text(encoding="utf-8")
        self.assertNotIn("Portal request", other)

    # 2. each rule refused by its own message, nothing written
    def test_bad_urls_are_each_refused_by_their_own_message_and_nothing_is_written(self):
        for url, words in REFUSED:
            with self.subTest(url=url):
                self.refused(words, self.a, "portal", url)
        self.assertFalse(self.sent.exists())

    def test_refusals_leave_an_existing_record_untouched(self):
        self.ok()
        for url, words in REFUSED[:3]:
            with self.subTest(url=url):
                self.refused(words, self.a, "portal", url, "--replace")

    # 3. email and mail
    def test_request_url_is_refused_with_email_and_mail(self):
        for channel in ("email", "mail"):
            with self.subTest(channel=channel):
                self.refused("--request-url is only for portal or muckrock requests", self.a, channel, URL)
        self.assertEqual(self.send(self.a, "email", None)[0], 0)  # the same request with no link is fine
        self.assertNotIn("request_url", self.entry())

    # 4. no verified portal
    def test_agency_without_a_verified_portal_is_refused_with_the_hint(self):
        hint = "record the agency's portal first (kit-verify-agency --portal)"
        self.refused(hint, self.b, "portal", URL)  # verified by email only: portal url is null
        path = self.root / "kit" / "agencies.json"
        agencies = json.loads(path.read_text(encoding="utf-8"))
        next(e for e in agencies if e["agency_id"] == self.a)["verified"] = False
        path.write_text(json.dumps(agencies), encoding="utf-8")
        self.refused(hint, self.a, "portal", URL)
        next(e for e in agencies if e["agency_id"] == self.a).pop("portal")
        path.write_text(json.dumps(agencies), encoding="utf-8")
        self.refused(hint, self.a, "portal", URL)

    # 5. host compared lower-cased
    def test_upper_case_host_is_accepted(self):
        url = "https://CITYX.NextRequest.com/requests/24-118"
        self.ok(self.a, "portal", url)
        self.assertEqual(self.entry()["request_url"], url)

    def test_path_is_free_but_a_trailing_dot_host_is_not_the_portal_host(self):
        self.ok(self.a, "portal", "https://cityx.nextrequest.com")
        self.assertEqual(self.entry()["request_url"], "https://cityx.nextrequest.com")
        self.refused("is not the agency's verified portal host", self.a, "portal", "https://cityx.nextrequest.com./x",
                     "--replace")

    # 6. --replace
    def test_replace_keeps_the_stored_link_unless_a_new_one_is_given(self):
        self.ok(self.a, "portal", URL, "--reference", "24-118")
        self.ok(self.a, "portal", None, "--replace")
        self.assertEqual(self.entry()["request_url"], URL)
        self.assertEqual(self.entry()["reference"], "24-118")
        new = "https://cityx.nextrequest.com/requests/24-119"
        self.ok(self.a, "portal", new, "--replace")
        self.assertEqual(self.entry()["request_url"], new)
        self.refused("is not the agency's verified portal host", self.a, "portal",
                     "https://other.nextrequest.com/requests/1", "--replace")
        self.assertEqual(self.entry()["request_url"], new)

    def test_replace_to_another_channel_drops_the_link_and_the_file_stays_readable(self):
        self.ok()
        self.ok(self.a, "email", None, "--replace")
        self.assertNotIn("request_url", self.entry())
        self.assertEqual(kit_track.read_sent(self.root)[self.a]["channel"], "email")

    # 7. older files
    def test_older_sent_json_without_request_url_reads_unchanged(self):
        self.assertEqual(self.send(self.a, "portal", None, "--reference", "R-1")[0], 0)
        self.assertEqual(self.send(self.b, "email", None)[0], 0)
        before = self.sent.read_bytes()
        self.assertNotIn(b"request_url", before)
        code, out, err = run("kit-status", "--directory", self.root)
        self.assertEqual((code, err), (0, ""))
        rows = json.loads(out)["requests"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all("request_url" not in row for row in rows))
        code, out, err = run("kit-due", "--directory", self.root, "--as-of", "2026-03-03")
        self.assertEqual(code, 0, err)
        self.assertNotIn("request_url", out)
        self.assertEqual(run("kit-status", "--directory", self.root, "--csv", self.tmp / "t.csv")[0], 0)
        cells = list(csv.reader((self.tmp / "t.csv").read_text(encoding="utf-8").splitlines()))
        self.assertEqual([row[-1] for row in cells[1:]], ["", ""])
        self.assertEqual(run("kit-followup", "--directory", self.root, "--agency", self.a, "--as-of", "2026-06-01")[0], 0)
        text = (self.root / "kit" / "followups" / f"{self.a}-2026-06-01.md").read_text(encoding="utf-8")
        self.assertIn("Regarding your reference R-1,\n\nOn 2026-03-02", text)
        self.assertNotIn("Portal request", text)
        self.assertEqual(self.sent.read_bytes(), before)  # no reader rewrote it

    def test_a_stored_request_url_that_breaks_the_rules_makes_the_file_unreadable(self):
        self.ok()
        document = json.loads(self.sent.read_text(encoding="utf-8"))
        good = document["requests"][self.a]
        for change in ({"request_url": "http://cityx.nextrequest.com/r/1"}, {"request_url": "https://u@x.example/r"},
                       {"request_url": "https://x.example:8443/r"}, {"request_url": URL + "?a=1"},
                       {"request_url": "https://x.example/" + "a" * 300}, {"request_url": 7},
                       {"channel": "email"}, {"channel": "mail"}):
            with self.subTest(change=change):
                document["requests"][self.a] = {**good, **change}
                self.sent.write_text(json.dumps(document), encoding="utf-8")
                code, out, err = run("kit-status", "--directory", self.root)
                self.assertEqual(code, 1, out)
                self.assertIn("kit/sent.json unreadable", err)

    # 8. never fetched
    def test_urlopen_is_never_called(self):
        self.ok()
        self.refused("is not the agency's verified portal host", self.a, "portal", "https://other.nextrequest.com/r/1",
                     "--replace")
        run("kit-status", "--directory", self.root)
        run("kit-due", "--directory", self.root, "--as-of", "2026-03-03")
        self.ok(self.a, "portal", URL + "9", "--replace")
        run("kit-status", "--directory", self.root, "--csv", self.tmp / "n.csv")
        self.assertEqual(self.opener.call_count, 0)


if __name__ == "__main__":
    unittest.main()
