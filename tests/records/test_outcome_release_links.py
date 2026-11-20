"""Release-link register (synthetic only): lists portal links with expiry; never fetches."""
import contextlib
import hashlib
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import socket
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from campaign_tool.records import run_stages
from campaign_tool.records.ledger import store
from campaign_tool.records.outcomes import release_links as rl
from campaign_tool.records.portal.store import Queue
from tests.records import test_outcome_received_items as _ri

ReceivedItemsTests = _ri.ReceivedItemsTests
del _ri

NRC = "nextrequest" + ".com"
NEXT = "https://slo." + NRC + "/documents/9001?token=SECRETVALUE123"
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

    # --- retrieval (DF2307-09): exact-URL evidence from a synthetic portal store, read read-only ---
    def portal_dir(self):
        return self.project / "portal-queue-v1"

    def queue(self):
        """Replace the skeletal fixture store with a real v1 store built by portal.store.Queue."""
        (self.portal_dir() / "portal.sqlite").unlink(missing_ok=True)
        return Queue(self.portal_dir())

    def enrol(self, queue, url, sha, item="i1", fetched=False):
        key = queue.inventory(urlsplit(url).hostname.lower(), "r1", item, url, sha)
        if fetched:  # seed the attempt -> version -> current rows a portal run would leave; no run happens
            with queue.db:
                attempt = queue.db.execute("INSERT INTO portal_attempts(item,generation,started,finished,result,"
                                           "evidence_json) VALUES(?,1,0,1,'fetched','{}')", (key,)).lastrowid
                queue.db.execute("INSERT INTO portal_versions(id,item,sha256,byte_count,attempt,provenance_json) "
                                 "VALUES(?,?,?,1,?,'{}')", ("v" + item, key, "c" * 64, attempt))
                queue.db.execute("UPDATE portal_items SET current_sha256=? WHERE id=?", ("c" * 64, key))
        return key

    def retrieval(self, report, url):
        want = hashlib.sha256(rl.classify_url(url)[4].encode()).hexdigest()
        return next(l for l in report["links"] if l["url_sha256"] == want)["retrieval"]

    def test_portal_retrieval_by_exact_url(self):
        urls = {k: "https://%s.%s/documents/%d?t=%s" % (k, NRC, n, k) for n, k in enumerate("abcd", 1)}
        shas = {k: self.mailbody("Records %s expires on 2026-12-01" % u) for k, u in urls.items()}
        queue = self.queue()
        self.enrol(queue, urls["a"], shas["a"], "ia", fetched=True)
        self.enrol(queue, urls["b"], shas["b"], "ib")
        for code in ("host_not_allowlisted", "bad_scheme"):
            queue.refuse("c." + NRC, code, urls["c"], shas["c"])
        queue.close()
        report = self.collect()
        got = {k: self.retrieval(report, u) for k, u in urls.items()}
        self.assertEqual(got, {"a": {"state": "retrieved", "reason": "retrieved"},
                               "b": {"state": "pending", "reason": "pending"},
                               "c": {"state": "refused", "reason": ["bad_scheme", "host_not_allowlisted"]},
                               "d": {"state": "not_in_portal", "reason": "not_in_portal"}})

    def test_distinct_urls_never_share_retrieval(self):
        # same path, different query tokens, one message: only the enrolled+fetched one is retrieved
        base = "https://s.%s/documents/5" % NRC
        one, two = base + "?t=one1", base + "?t=two2"
        sha = self.mailbody("Records %s and %s expire on 2026-12-01" % (one, two))
        queue = self.queue()
        self.enrol(queue, one, sha, "i1", fetched=True)
        # both enrolled to one item, fetched through only one of them
        p, q = "https://s.%s/documents/6?t=" % NRC + "p1", "https://s.%s/documents/6?t=" % NRC + "q2"
        sha2 = self.mailbody("Files %s and %s expire on 2026-12-01" % (p, q))
        self.enrol(queue, p, sha2, "i2", fetched=True)
        self.enrol(queue, q, sha2, "i2")
        # a refusal of one spelling does not apply to its same-path sibling
        r1, r2 = "https://s.%s/documents/8?t=r1" % NRC, "https://s.%s/documents/8?t=r2" % NRC
        sha3 = self.mailbody("Refused %s and %s expire on 2026-12-01" % (r1, r2))
        queue.refuse("s." + NRC, "host_not_allowlisted", r1, sha3)
        queue.close()
        report = self.collect()
        unproven = {"state": "unknown", "reason": "link_identity_unproven"}
        self.assertEqual(self.retrieval(report, one)["state"], "retrieved")
        self.assertEqual(self.retrieval(report, two), unproven)
        self.assertEqual(self.retrieval(report, p)["state"], "retrieved")
        self.assertEqual(self.retrieval(report, q), {"state": "unknown", "reason": "item_retrieved_via_other_link"})
        self.assertEqual(self.retrieval(report, r1)["state"], "refused")
        self.assertEqual(self.retrieval(report, r2), unproven)

    def test_host_case_and_default_port_variants_are_not_retrieved(self):
        canonical = "https://s.%s/documents/7?q=1" % NRC
        upper, port = "https://S.%s/documents/7?q=1" % NRC, "https://s.%s:443/documents/7?q=1" % NRC
        sha = self.mailbody("Records %s expire on 2026-12-01" % canonical)
        self.mailbody("Again %s and %s" % (upper, port))
        queue = self.queue()
        self.enrol(queue, canonical, sha, fetched=True)
        queue.close()
        # one normalised bucket holds three spellings; a proven one never certifies the unproven ones
        self.assertEqual(self.retrieval(self.collect(), canonical), {"state": "unknown", "reason": "link_identity_unproven"})

    def test_conflicting_exact_evidence_is_unknown(self):
        twice, refused = "https://s.%s/documents/1?t=twice" % NRC, "https://s.%s/documents/2?t=both" % NRC
        sha = self.mailbody("Records %s and %s expire on 2026-12-01" % (twice, refused))
        queue = self.queue()
        self.enrol(queue, twice, sha, "i1")
        self.enrol(queue, twice, sha, "i2")  # one exact URL leading to two items
        self.enrol(queue, refused, sha, "i3")
        queue.refuse("s." + NRC, "host_not_allowlisted", refused, sha)  # a refusal and an item
        queue.close()
        report = self.collect()
        for url in (twice, refused):
            self.assertEqual(self.retrieval(report, url), {"state": "unknown", "reason": "link_identity_conflict"})

    def test_unusable_store_is_unknown_and_never_created(self):
        url = "https://s.%s/documents/1?t=x" % NRC
        sha = self.mailbody("Records %s expire on 2026-12-01" % url)
        path = self.portal_dir() / "portal.sqlite"

        def good():
            queue = self.queue()
            self.enrol(queue, url, sha, fetched=True)
            queue.close()

        def version2():
            good()
            with contextlib.closing(sqlite3.connect(path)) as con, con:
                con.execute("UPDATE portal_meta SET version=2")

        def symlink():
            good()
            path.rename(self.project / "moved.sqlite")
            path.symlink_to(self.project / "moved.sqlite")

        cases = {"absent": lambda: None, "loose": lambda: (good(), path.chmod(0o644)),
                 "version": version2, "symlink": symlink}
        for name, make in cases.items():
            with self.subTest(name):
                path.unlink(missing_ok=True)
                (self.project / "moved.sqlite").unlink(missing_ok=True)
                make()
                self.assertEqual(self.retrieval(self.collect(), url), {"state": "unknown", "reason": "store_unavailable"})
                if name == "absent":
                    self.assertFalse(path.exists())  # reading never creates the store

    def test_artifact_holds_no_exact_hash_or_query_and_store_is_untouched(self):
        mixed = "https://S.%s/documents/9?tok=QQQ111" % NRC
        sha = self.mailbody("Records %s expire on 2026-12-01" % mixed)
        queue = self.queue()
        self.enrol(queue, mixed, sha, fetched=True)
        queue.close()
        path = self.portal_dir() / "portal.sqlite"
        before = (path.read_bytes(), path.stat().st_mtime_ns, sorted(os.listdir(self.portal_dir())))
        self.assertEqual(self.stage()[0], 0)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns, sorted(os.listdir(self.portal_dir()))), before)
        text = self.artifact().read_text()
        self.assertEqual(json.loads(text)["links"][0]["retrieval"]["state"], "retrieved")
        self.assertNotIn(hashlib.sha256(mixed.encode()).hexdigest(), text)  # exact hash stays in memory
        for token in ("QQQ111", "tok=", "?"):
            self.assertNotIn(token, text)

    def test_lists_links_with_expiry_agency_and_no_token(self):
        sha = self.mailbody(BODY)
        report = self.collect()
        links = self.by_platform(report)
        nr, sp = links["nextrequest"], links["sharepoint"]
        self.assertEqual((nr["expiry"]["date"], nr["expiry_state"], nr["agency_ids"]), ("2026-11-17", "stated", ["caltrans"]))
        self.assertEqual(nr["kind"], "document_download")
        self.assertEqual(nr["url_redacted"], "https://slo." + NRC + "/documents/9001")
        self.assertTrue(nr["query_present"])
        self.assertEqual(sp["expiry"]["date"], "2026-11-17")  # one statement governs both nearby links
        self.assertIn("[token]", sp["url_redacted"])
        self.assertIsNone(nr["sources"][0]["page"])
        self.assertEqual(nr["sources"][0]["original_sha256"], sha)
        self.assertTrue(report["never_fetched"] and report["review_required"])
        for link in (nr, sp):  # the skeletal fixture store is unavailable, not an empty valid v1 store
            self.assertEqual(link["retrieval"], {"state": "unknown", "reason": "store_unavailable"})
        dump = json.dumps(report)
        for secret in ("SECRETVALUE123", "ABC999", "EaBcDeFgHiJkLmNo12345"):
            self.assertNotIn(secret, dump)

    def test_all_letter_and_percent_encoded_tokens_are_masked(self):
        letters = "Eabcdefghijklm" + "nopqrstuvwxyzABCDE"
        encoded = "Ab%2FcdEfGhIjKl%2BmNoPq"
        self.mailbody("Files https://z.sharepoint.com/:f:/s/clerk/%s and https://y.sharepoint.com/share/%s expires on 2026-12-01"
                      % (letters, encoded))
        report = self.collect()
        self.assertEqual(report["links"][0]["url_redacted"].count("[token]"), 1)
        dump = json.dumps(report)
        for secret in (letters, encoded):
            self.assertNotIn(secret, dump)
        self.assertEqual(rl.redact_path("/documents/public-records-requests"), "/documents/public-records-requests")
        self.assertNotIn(letters, rl.redact_text("see https://z.sharepoint.com/:f:/s/clerk/%s now" % letters))

    def test_all_four_platforms_and_non_portal_links_ignored(self):
        self.mailbody("See https://a.opengov.com/records/55 and https://b.govqa.us/WEBAPP/_rs/x and "
                      "https://c." + NRC + "/requests/26-1 and https://www.example.com/page and "
                      "https://evil" + NRC + "/x and https://d.sharepoint.com/x.")
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
        self.mailbody("Records: https://s." + NRC + "/documents/1?a=1 expires on November 20, 2026", when="2026-11-02T09:00:00-08:00")
        self.mailbody("Reminder https://s." + NRC + "/documents/1?a=1 expires on November 18, 2026", when="2026-11-04T09:00:00-08:00")
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
        self.mailbody("https://a." + NRC + "/documents/4 will expire in 14 days", when="2026-11-03T09:00:00-08:00")
        expiry = self.collect()["links"][0]["expiry"]
        self.assertEqual((expiry["date"], expiry["basis"]), ("2026-11-17", "relative_to_received"))

    def test_remain_active_until_wording_from_a_real_agency_message(self):
        self.mailbody("Please note that this link will remain active until October 30, 2026. 07-30-2026 DeFlock PRA"
                      "<https://cpslo-my.sharepoint.com/:f:/g/personal/pra_calpoly_edu/EaBcDeFgHiJkLmNo12345>")
        expiry = self.collect()["links"][0]["expiry"]
        self.assertEqual(expiry["date"], "2026-10-30")

    def test_never_expires_wording_is_not_an_expiry(self):
        self.mailbody("https://a." + NRC + "/documents/3 This link does not expire on a schedule, see November 30, 2026.")
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
        self.assertIn("2026-11-17 retrieval=unknown", out)
        legacy = {"run_id": "r", "links": [dict(self.collect()["links"][0], expiry=None)]}  # older v1 row: no key
        legacy["links"][0].pop("retrieval")
        self.assertIn("retrieval=unknown", rl.render(legacy, datetime(2026, 11, 20, tzinfo=timezone.utc), 7))
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
