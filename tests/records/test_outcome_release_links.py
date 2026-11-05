"""Release-link register (synthetic only): lists portal links with expiry; never fetches."""
import contextlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
import socket
import stat
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import run_stages
from campaign_tool.records.ledger import store
from campaign_tool.records.outcomes import release_links as rl
from tests.records import test_outcome_received_items as _ri

ReceivedItemsTests = _ri.ReceivedItemsTests
del _ri

NEXT = "https://slo.nextrequest.com/documents/9001?token=SECRETVALUE123"
BODY = ("Your records are ready.\n<a href=\"%s\">Download</a> This link expires on November 17, 2026.\n"
        "Other files: https://example.sharepoint.com/:f:/s/clerk/EaBcDeFgHiJkLmNo12345?e=ABC999" % NEXT)


class ReleaseLinkTests(unittest.TestCase):
    private, document, enroll, tree = (ReceivedItemsTests.private, ReceivedItemsTests.document,
                                       ReceivedItemsTests.enroll, ReceivedItemsTests.tree)
    setUp = ReceivedItemsTests.setUp
    mail = ReceivedItemsTests.mail

    def collect(self):
        with run_stages.snapshot_ro(self.database) as con:
            return rl.collect(con, self.processing, run_id="test-run")

    def stage(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = run_stages.main(["outputs", "--project", str(self.project), "--run-id", "test-run"])
        return code, json.loads(stdout.getvalue())

    def artifact(self):
        return self.project / "run-outputs" / "test-run" / "outcomes" / rl.ARTIFACT

    def by_platform(self, report):
        return {link["platform"]: link for link in report["links"]}

    def mailbody(self, text, agency="caltrans", when="2026-11-03T09:00:00-08:00"):
        sha, _ = self.document([text], agency, locator={"mime": "text/html", "part": "1"})
        self.mail(sha, when)
        return sha

    def test_lists_links_with_expiry_agency_and_no_token(self):
        sha = self.mailbody(BODY)
        report = self.collect()
        links = self.by_platform(report)
        nr, sp = links["nextrequest"], links["sharepoint"]
        self.assertEqual((nr["expiry"]["date"], nr["expiry_state"], nr["agency_ids"]), ("2026-11-17", "stated", ["caltrans"]))
        self.assertEqual(nr["kind"], "document_download")
        self.assertEqual(nr["url_redacted"], "https://slo.nextrequest.com/documents/9001")
        self.assertTrue(nr["query_present"])
        self.assertEqual(sp["expiry"]["date"], "2026-11-17")  # one statement governs both nearby links
        self.assertIn("[token]", sp["url_redacted"])
        self.assertIsNone(nr["sources"][0]["page"])
        self.assertEqual(nr["sources"][0]["original_sha256"], sha)
        self.assertTrue(report["never_fetched"] and report["review_required"])
        dump = json.dumps(report)
        for secret in ("SECRETVALUE123", "ABC999", "EaBcDeFgHiJkLmNo12345"):
            self.assertNotIn(secret, dump)

    def test_all_four_platforms_and_non_portal_links_ignored(self):
        self.mailbody("See https://a.opengov.com/records/55 and https://b.govqa.us/WEBAPP/_rs/x and "
                      "https://c.nextrequest.com/requests/26-1 and https://www.example.com/page and "
                      "https://evilnextrequest.com/x and https://d.sharepoint.com/x.")
        report = self.collect()
        self.assertEqual(sorted(l["platform"] for l in report["links"]), ["govqa", "nextrequest", "opengov", "sharepoint"])
        self.assertEqual(self.by_platform(report)["nextrequest"]["kind"], "request_page")
        self.assertTrue(all(l["expiry"] is None and l["expiry_state"] == "none_stated" for l in report["links"]))

    def test_outlook_safelink_wrapper_resolves_to_real_platform(self):
        self.mailbody("https://nam10.safelinks.protection.outlook.com/?url=https%3A%2F%2Fx.govqa.us%2Fdoc%2F7&data=zz "
                      "expires on 2026-12-01")
        link = self.collect()["links"][0]
        self.assertEqual((link["platform"], link["host"], link["expiry"]["date"]), ("govqa", "x.govqa.us", "2026-12-01"))

    def test_duplicate_link_across_messages_is_one_entry_with_two_sources(self):
        self.mailbody("Records: https://s.nextrequest.com/documents/1?a=1 expires on November 20, 2026", when="2026-11-02T09:00:00-08:00")
        self.mailbody("Reminder https://s.nextrequest.com/documents/1?a=1 expires on November 18, 2026", when="2026-11-04T09:00:00-08:00")
        links = self.collect()["links"]
        self.assertEqual(len(links), 1)
        self.assertEqual(len(links[0]["sources"]), 2)
        self.assertEqual(links[0]["expiry"]["date"], "2026-11-18")  # earliest, flagged
        self.assertIn("expiry_conflict_earliest_used", links[0]["diagnostics"])

    def test_ambiguous_and_unresolvable_expiry_never_guessed(self):
        self.mailbody("https://a.govqa.us/x expires on 2026-12-01 and the copy expires on 2026-12-05")
        far = self.mailbody("https://c.opengov.com/z " + "filler " * 80 + "expires on 2026-12-01")
        relative_no_time = "https://d.sharepoint.com/q expires in 3 days"
        sha, _ = self.document([relative_no_time], "other", locator={"mime": "text/html", "part": "1"})
        states = {l["host"]: l["expiry_state"] for l in self.collect()["links"]}
        self.assertEqual(states["a.govqa.us"], "ambiguous")
        self.assertEqual(states["c.opengov.com"], "none_stated")
        self.assertEqual(states["d.sharepoint.com"], "unresolved")
        self.assertIsNotNone(far)

    def test_relative_expiry_is_counted_from_the_mail_time(self):
        self.mailbody("https://a.nextrequest.com/documents/4 will expire in 14 days", when="2026-11-03T09:00:00-08:00")
        expiry = self.collect()["links"][0]["expiry"]
        self.assertEqual((expiry["date"], expiry["basis"]), ("2026-11-17", "relative_to_received"))

    def test_remain_active_until_wording_from_a_real_agency_message(self):
        self.mailbody("Please note that this link will remain active until October 30, 2026. 07-30-2026 DeFlock PRA"
                      "<https://cpslo-my.sharepoint.com/:f:/g/personal/pra_calpoly_edu/EaBcDeFgHiJkLmNo12345>")
        expiry = self.collect()["links"][0]["expiry"]
        self.assertEqual(expiry["date"], "2026-10-30")

    def test_never_expires_wording_is_not_an_expiry(self):
        self.mailbody("https://a.nextrequest.com/documents/3 This link does not expire on a schedule, see November 30, 2026.")
        self.assertIsNone(self.collect()["links"][0]["expiry"])

    def test_status_expired_soon_active_and_unknown(self):
        link = {"expiry": {"date": "2026-11-17"}}
        at = lambda day: datetime(2026, 11, day, 12, tzinfo=timezone.utc)
        self.assertEqual(rl.status(link, at(18)), ("expired", -1))
        self.assertEqual(rl.status(link, at(17)), ("expiring_soon", 0))
        self.assertEqual(rl.status(link, at(10)), ("expiring_soon", 7))
        self.assertEqual(rl.status(link, at(9)), ("active", 8))
        self.assertEqual(rl.status({"expiry": None}, at(9)), ("expiry_unknown", None))

    def test_out_of_scope_and_untyped_originals_omitted_unread(self):
        sha, _ = self.document(["https://a.govqa.us/x"], "caltrans", scope="out_of_scope", locator={"mime": "text/html", "part": "1"})
        with patch.object(rl.ri, "_current_units", side_effect=AssertionError("text was read")):
            self.assertEqual(self.collect()["links"], [])
        self.assertIsNotNone(sha)

    def test_stage_writes_private_artifact_replay_is_noop_and_changed_is_refused(self):
        self.mailbody(BODY)
        code, _ = self.stage()
        self.assertEqual(code, 0)
        path = self.artifact()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        first = path.read_bytes()
        self.assertEqual(self.stage()[0], 0)
        self.assertEqual(path.read_bytes(), first)
        path.write_bytes(first + b" ")
        self.assertEqual(self.stage()[0], 1)  # a different existing artifact is refused

    def test_listing_never_opens_a_socket(self):
        self.mailbody(BODY)
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network")), \
                patch("socket.create_connection", side_effect=AssertionError("network")), \
                patch("socket.getaddrinfo", side_effect=AssertionError("network")):
            self.collect()
            self.stage()
            self.assertEqual(self.cli("2026-11-10T00:00:00Z")[0], 0)

    def cli(self, now, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rl.main(["--project", str(self.project), "--run-id", "test-run", "--now", now, *extra])
        return code, out.getvalue(), err.getvalue()

    def test_cli_warns_expired_and_expiring_and_refuses_missing_or_stale(self):
        self.mailbody(BODY)
        self.stage()
        code, out, _ = self.cli("2026-11-20T00:00:00Z")
        self.assertEqual(code, 0)
        self.assertIn("EXPIRED 3d ago", out)
        self.assertNotIn("SECRETVALUE123", out)
        self.assertIn("EXPIRES in 2d", self.cli("2026-11-15T00:00:00Z")[1])
        listed = json.loads(self.cli("2026-11-01T00:00:00Z", "--json")[1])
        self.assertEqual({l["status"] for l in listed["links"]}, {"active"})
        # stale run id and missing artifact are refused with exit 2
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(rl.main(["--project", str(self.project), "--run-id", "other-run"]), 2)
        self.assertIn("refused register_missing", err.getvalue())
        data = json.loads(self.artifact().read_text())
        data["run_id"] = "someone-else"
        self.artifact().write_text(json.dumps(data))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(rl.main(["--project", str(self.project), "--run-id", "test-run"]), 2)

    def test_cli_is_registered_in_records_cli(self):
        from campaign_tool.records import cli
        self.assertEqual(cli.COMMANDS["release-links"], ("outcomes.release_links", "main"))


del ReceivedItemsTests  # helpers only; keep unittest from collecting its tests a second time


if __name__ == "__main__":
    unittest.main()
