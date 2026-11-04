"""kit-response --kind produced --download-expires-on D: the production stays 'download pending' until records are
linked to it; kit-status, kit-due and today name it with days left and, once past, 'link expired'. The link itself is
never stored or fetched. Offline, synthetic; every 'as of' date is passed in, never read from the wall clock."""
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit_track, law  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402

SENT_ON, PRODUCED_ON, EXPIRES = "2026-03-02", "2026-03-02", "2026-03-16"
SECOND = "second-ask"
EXPIRED_TEXT = "link expired 2026-03-16: ask the agency for a new one"


def run(*arguments):
    out, err, code = io.StringIO(), io.StringIO(), 0
    with mock.patch.object(sys, "argv", [sys.argv[0], *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class DownloadPendingTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-download-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        for arguments in (("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                           "--state", "CA"), ("kit", "--directory", self.root)):
            self.assertEqual(run(*arguments)[0], 0)
        self.sent = self.root / "kit" / "sent.json"
        self.agency = sorted((self.root / "kit" / "requests").glob("*.md"))[0].stem
        ready_drafts(self.root)
        (self.root / "kit" / "requests" / f"{self.agency}--{SECOND}.md").write_text("Narrowed request.\n",
                                                                                   encoding="utf-8")
        self.assertEqual(self.cli("kit-sent", "--agency", self.agency, "--date", SENT_ON, "--channel", "email")[0], 0)

    def cli(self, command, *extra):
        return run(command, "--directory", self.root, *extra)

    def step(self, on, kind, *extra, request="main"):
        return self.cli("kit-response", "--agency", self.agency, "--date", on, "--kind", kind, "--request", request,
                        *extra)

    def ok(self, on, kind, *extra, request="main"):
        code, out, err = self.step(on, kind, *extra, request=request)
        self.assertEqual(code, 0, err)
        return json.loads(out.split("Recorded only.")[0])

    def refused_untouched(self, args, message, **kwargs):
        before = self.sent.read_bytes()
        code, out, err = self.step(*args, **kwargs)
        self.assertEqual((code, err), (1, f"Stopped: {message}\n"), out)
        self.assertEqual(self.sent.read_bytes(), before)

    def produce(self, expires=EXPIRES, on=PRODUCED_ON, *extra, request="main"):
        return self.ok(on, "produced", "--download-expires-on", expires, *extra, request=request)

    def rows(self, as_of):  # kit-status as of a date (the reader takes it; the CLI has no --as-of)
        report = kit_track.status(self.root, today=date.fromisoformat(as_of))
        law.validate(report, json.loads((REPO_ROOT / "schemas/kit-status.schema.json").read_text()))
        return report["requests"]

    def row(self, as_of, request="main"):
        return next(r for r in self.rows(as_of) if r["request"] == request)

    def due(self, as_of, expect):
        code, out, err = self.cli("kit-due", "--as-of", as_of)
        self.assertEqual(code, expect, err)
        report = json.loads(out)
        law.validate(report, json.loads((REPO_ROOT / "schemas/kit-due.schema.json").read_text()))
        return report

    def today(self, as_of):
        code, out, err = self.cli("today", "--as-of", as_of)
        self.assertEqual(code, 0, err)
        return out

    def ingest(self, name):
        source = self.tmp / (name + ".txt")
        source.write_bytes(f"synthetic production page {name}\n".encode())
        self.assertEqual(run("ingest", "--directory", self.root, "--file", source, "--source-id", "syn-" + name)[0], 0)
        return hashlib.sha256(source.read_bytes()).hexdigest()[:16]

    def download_rows(self, report):
        return [r for r in report["open"] if r.get("kind") == "download"]

    def test_six_days_left_in_status_due_and_today(self):
        printed = self.produce()
        self.assertEqual(printed["steps"][-1]["download_expires_on"], EXPIRES)
        line = "Download pending: produced 2026-03-02, link expires 2026-03-16 (6 days left)"
        self.assertEqual([d["line"] for d in self.row("2026-03-10")["downloads_pending"]], [line])
        report = self.due("2026-03-10", 0)
        [item] = self.download_rows(report)
        self.assertEqual((item["state"], item["due"], item["days"], item["kind"], item["note"]),
                         ("download_pending", EXPIRES, 6, "download", line))
        self.assertEqual(report["download_expired"], 0)
        shown = self.today("2026-03-10")
        self.assertIn("[request_download_pending] Download records from ", shown)
        self.assertIn(f"before {EXPIRES} (6 days left)", shown)
        code, _, err = self.cli("kit-status")  # the CLI reminder on stderr (wall-clock date; only the shape here)
        self.assertEqual(code, 0)
        self.assertIn("Download pending: produced 2026-03-02, link ", err)

    def test_after_expiry_asks_for_a_new_link_and_kit_due_exits_3(self):
        self.produce()
        line = "Download pending: produced 2026-03-02, " + EXPIRED_TEXT
        self.assertEqual([d["line"] for d in self.row("2026-03-17")["downloads_pending"]], [line])
        report = self.due("2026-03-17", 3)
        [item] = self.download_rows(report)
        self.assertEqual((item["state"], item["days"], report["download_expired"]), ("download_expired", -1, 1))
        shown = self.today("2026-03-17")
        self.assertIn(f"[request_download_expired] ", shown)
        self.assertIn(f"({self.agency}): {EXPIRED_TEXT}", shown)
        self.assertNotIn("days left", shown)
        # the day itself is not past: 0 days left, no exit 3
        self.assertEqual(self.download_rows(self.due("2026-03-16", 0))[0]["days"], 0)
        self.assertIn("(0 days left)", self.today("2026-03-16"))
        self.assertIn("(1 day left)", self.today("2026-03-15"))

    def test_expiry_equal_to_step_date_accepted_day_before_refused_naming_both(self):
        self.produce(expires=PRODUCED_ON)
        self.assertEqual(self.row(PRODUCED_ON)["downloads_pending"][0]["days_left"], 0)
        self.refused_untouched(("2026-03-05", "produced", "--download-expires-on", "2026-03-04"),
                               "--download-expires-on 2026-03-04 is before the produced step on 2026-03-05")

    def test_malformed_or_url_values_refused_and_no_link_is_stored(self):
        for value in ("2026-02-30", "03/16/2026", "https://synthetic.example/download?token=abc", ""):
            self.refused_untouched((PRODUCED_ON, "produced", "--download-expires-on", value),
                                   "--download-expires-on must be a real YYYY-MM-DD date")
        self.produce()
        self.assertNotIn("http", self.sent.read_text())
        help_text = run("kit-response", "--help")[1]
        self.assertIn("never stored or fetched", " ".join(help_text.split()))

    def test_only_for_produced_steps(self):
        for kind, extra in (("acknowledged", ()), ("denied", ()), ("fee_estimate", ("--amount", "10"))):
            self.refused_untouched((PRODUCED_ON, kind, "--download-expires-on", EXPIRES, *extra),
                                   "--download-expires-on is only for produced steps")

    def test_records_on_the_same_step_mean_not_pending(self):
        prefix = self.ingest("page-one")
        printed = self.produce(EXPIRES, PRODUCED_ON, "--record", prefix)
        self.assertEqual(printed["steps"][-1]["download_expires_on"], EXPIRES)
        self.assertNotIn("downloads_pending", self.row("2026-03-10"))
        report = self.due("2026-03-17", 0)
        self.assertEqual((self.download_rows(report), "download_expired" in report), ([], False))

    def test_later_produced_step_with_records_clears_the_latest_pending_on_that_request_only(self):
        self.assertEqual(self.cli("kit-sent", "--agency", self.agency, "--date", SENT_ON, "--channel", "email",
                                  "--request", SECOND)[0], 0)
        self.produce()
        self.produce(request=SECOND)
        self.assertEqual(len(self.row("2026-03-10")["downloads_pending"]), 1)
        self.ok("2026-03-08", "produced", "--record", self.ingest("page-two"))
        self.assertNotIn("downloads_pending", self.row("2026-03-10"))  # main: cleared
        self.assertEqual(len(self.row("2026-03-10", SECOND)["downloads_pending"]), 1)  # other request: untouched
        [item] = self.download_rows(self.due("2026-03-10", 0))
        self.assertEqual(item["request"], SECOND)

    def test_records_linked_before_the_pending_step_do_not_clear_it(self):
        self.ok("2026-03-02", "produced", "--record", self.ingest("page-early"))
        self.produce(EXPIRES, "2026-03-05")
        self.assertEqual(len(self.row("2026-03-10")["downloads_pending"]), 1)

    def test_one_record_step_clears_only_the_latest_of_two_pending_steps(self):
        self.produce(EXPIRES, "2026-03-03")
        self.produce("2026-03-20", "2026-03-04")
        self.ok("2026-03-08", "produced", "--record", self.ingest("page-three"))
        [left] = self.row("2026-03-10")["downloads_pending"]
        self.assertEqual((left["produced_on"], left["expires_on"]), ("2026-03-03", EXPIRES))

    def test_partial_with_expiry_is_pending_and_the_request_stays_open(self):
        self.produce(EXPIRES, "2026-03-05", "--partial", "--expected-on", "2026-03-20")
        row = self.row("2026-03-10")
        self.assertEqual((row["producing"], row["next_installment"], len(row["downloads_pending"])),
                         (True, "2026-03-20", 1))
        report = self.due("2026-03-10", 0)
        self.assertEqual(sorted(r["state"] for r in report["open"]), ["download_pending", "producing"])
        self.assertEqual(report["answered"], [])
        # kit-followup still sees the producing row, not the earlier-dated download row
        code, _, err = self.cli("kit-followup", "--agency", self.agency, "--as-of", "2026-03-12")
        self.assertEqual((code, err), (1, "Stopped: records still coming: next installment 2026-03-20; not "
                                          "passed\n"))
        self.assertEqual(sorted(r["state"] for r in self.due("2026-03-17", 3)["open"]),
                         ["download_expired", "producing"])

    def test_older_file_without_the_key_reads_unchanged(self):
        self.ok("2026-03-05", "determination", "--expected-on", "2026-03-20")
        self.ok("2026-03-06", "produced", "--note", "final set")
        before = self.sent.read_bytes()
        row = self.row("2026-03-10")
        self.assertNotIn("downloads_pending", row)
        self.assertEqual(self.sent.read_bytes(), before)
        report = self.due("2026-03-10", 0)
        self.assertEqual(self.download_rows(report), [])
        self.assertNotIn("download_expired", report)
        self.assertEqual(self.today("2026-03-10"), "Nothing due on 2026-03-10.\n")
        code, _, err = self.cli("kit-status")
        self.assertEqual((code, err), (0, ""))

    def test_a_closed_request_lists_no_download(self):
        self.produce()
        self.ok("2026-03-08", "closed")
        self.assertNotIn("downloads_pending", self.row("2026-03-17"))
        self.assertEqual(self.due("2026-03-17", 0)["open"], [])

    def test_hand_edited_bad_expiry_is_malformed(self):
        self.produce()
        for bad in ("2026-03-01", "soon", 5):
            document = json.loads(self.sent.read_text())
            document["requests"][self.agency]["steps"][0]["download_expires_on"] = bad
            self.sent.write_text(json.dumps(document), encoding="utf-8")
            code, out, err = self.cli("kit-status")
            self.assertEqual(code, 1, out)
            self.assertIn("kit/sent.json unreadable: malformed steps", err)
        document["requests"][self.agency]["steps"][0].update(kind="acknowledged", download_expires_on=EXPIRES)
        self.sent.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(self.cli("kit-status")[0], 1)


if __name__ == "__main__":
    unittest.main()
