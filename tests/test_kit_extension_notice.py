"""DF1407-02: the stated date and reason from an agency's extension notice. Offline, synthetic, nothing sent."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, law  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402

SENT = "2026-03-02"
LATE = "the agency's date is later than the provisional extended date; provisional, not a legal finding"


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


class ExtensionNoticeTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-extension-notice-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        self.assertEqual(run("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                             "--state", "CA")[0], 0)
        self.assertEqual(run("kit", "--directory", self.root)[0], 0)
        ready_drafts(self.root)
        self.a, self.b = (p.stem for p in sorted((self.root / "kit" / "requests").glob("*.md"))[:2])
        self.sent = self.root / "kit" / "sent.json"
        package = law.load_package("us-ca")
        self.provisional = law.deadline(package, SENT, extension=True).isoformat()
        self.send(self.a)

    def send(self, agency, *extra):
        code, _, err = run("kit-sent", "--directory", self.root, "--agency", agency, "--date", SENT, "--channel",
                           "email", *extra)
        self.assertEqual(code, 0, err)

    def response(self, *args, agency=None):
        return run("kit-response", "--directory", self.root, "--agency", agency or self.a, *args)

    def extend(self, *extra, on="2026-03-04", agency=None):
        code, out, err = self.response("--date", on, "--kind", "extension", *extra, agency=agency)
        self.assertEqual(code, 0, err)
        return json.loads(out.split("\nRecorded only")[0])

    def undo(self):
        return run("kit-response-undo", "--directory", self.root, "--agency", self.a, "--reason", "synthetic test")

    def refused(self, words, *args, agency=None):
        before = self.sent.read_bytes()
        code, out, err = self.response(*args, agency=agency)
        self.assertEqual(code, 1, out)
        self.assertTrue(err.startswith("Stopped: "), err)
        self.assertIn(words, err)
        self.assertEqual(self.sent.read_bytes(), before)  # a refusal writes nothing

    def row(self, agency=None, request="main"):
        code, out, err = run("kit-status", "--directory", self.root)
        self.assertEqual(code, 0, err)
        return next(r for r in json.loads(out)["requests"] if (r["agency_id"], r["request"]) == (agency or self.a, request))

    def due(self, as_of, expect=None):
        code, out, err = run("kit-due", "--directory", self.root, "--as-of", as_of)
        if expect is not None:
            self.assertEqual(code, expect, err)
        return code, json.loads(out)

    def cues(self, as_of):
        return [r for r in self.due(as_of)[1]["open"] if r.get("kind") == "extension"]

    # --- storage and display
    def test_both_values_stored_apart_from_production_dates_and_shown(self):
        step = self.extend("--expected-on", "2026-03-20", "--reason", "Staff  shortage; see §6253(c)")["steps"][0]
        self.assertEqual((step["extension_expected_on"], step["extension_reason"]),
                         ("2026-03-20", "Staff  shortage; see §6253(c)"))  # exact wording, interior spaces kept
        self.assertTrue({"expected_on", "note", "partial"}.isdisjoint(step))
        row = self.row()
        self.assertEqual((row["extension_expected_on"], row["extension_reason"]),
                         ("2026-03-20", "Staff  shortage; see §6253(c)"))
        self.assertEqual((row["stated_production_date"], row["next_installment"], row["with_extension"]),
                         (None, None, self.provisional))

    def test_each_option_alone_and_length_edges(self):
        self.extend("--reason", "x")
        self.assertEqual(self.row()["extension_reason"], "x")
        self.assertNotIn("extension_expected_on", self.row())
        self.undo()
        self.extend("--expected-on", "2026-03-04")  # equal to the step day is allowed
        self.assertNotIn("extension_reason", self.row())
        self.undo()
        self.extend("--reason", "é" * 200)
        self.assertEqual(self.row()["extension_reason"], "é" * 200)

    def test_bad_reasons_refused_by_name_and_write_nothing(self):
        base = ("--date", "2026-03-04", "--kind", "extension")
        for bad in ("", "   ", "é" * 201, "a<b", "a>b", "line\nbreak", "tab\there", "nul\x00"):
            with self.subTest(reason=repr(bad)[:20]):
                self.refused("--reason", *base, "--reason", bad)
        for kind in ("acknowledged", "determination", "closed"):
            self.refused("--reason is only for extension", "--date", "2026-03-04", "--kind", kind, "--reason", "why")

    def test_dry_run_free_state_is_not_touched_by_a_refused_date(self):
        base = ("--date", "2026-03-04", "--kind", "extension")
        self.refused("--expected-on 2026-03-03 is before the extension step on 2026-03-04", *base,
                     "--expected-on", "2026-03-03")
        self.refused("--expected-on must be a real", *base, "--expected-on", "2026-02-30")
        self.refused("--expected-on is for the determination", "--date", "2026-03-04", "--kind", "acknowledged",
                     "--expected-on", "2026-03-20")

    def test_existing_extension_rules_unchanged(self):
        self.extend("--reason", "first")
        self.refused("extension is allowed only once", "--date", "2026-03-05", "--kind", "extension", "--reason", "b")
        self.assertEqual(self.response("--date", "2026-03-05", "--kind", "determination")[0], 0)
        self.send(self.b)
        self.assertEqual(self.response("--date", "2026-03-05", "--kind", "determination", agency=self.b)[0], 0)
        self.refused("extension is not allowed after", "--date", "2026-03-06", "--kind", "extension",
                     "--expected-on", "2026-03-20", "--reason", "late", agency=self.b)
        self.refused("is in the future", "--date", "2999-01-01", "--kind", "extension", "--expected-on", "2999-02-01",
                     agency=self.a)

    def test_undo_round_trips_and_hand_edited_fields_are_rejected(self):
        self.extend("--expected-on", "2026-03-20", "--reason", "why")
        code, _, err = self.undo()
        self.assertEqual(code, 0, err)
        self.assertNotIn("extension_expected_on", self.row())
        self.extend("--expected-on", "2026-03-20", "--reason", "why")
        original = self.sent.read_text(encoding="utf-8")
        for key, value in (("extension_reason", 5), ("extension_reason", "a<b"), ("extension_expected_on", "2026-03-01"),
                           ("extension_expected_on", "nope")):
            document = json.loads(original)
            document["requests"][self.a]["steps"][0][key] = value
            self.sent.write_text(json.dumps(document), encoding="utf-8")
            self.assertEqual(run("kit-status", "--directory", self.root)[0], 1, (key, value))
        document = json.loads(original)  # the fields on another kind
        document["requests"][self.a]["steps"][0]["kind"] = "acknowledged"
        self.sent.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(run("kit-status", "--directory", self.root)[0], 1)
        document = json.loads(original)  # a production expected_on on the extension stays invalid
        document["requests"][self.a]["steps"][0]["expected_on"] = "2026-03-20"
        self.sent.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(run("kit-status", "--directory", self.root)[0], 1)

    def test_advisory_guard_note_stores_anyway_and_never_repeats_the_text(self):
        text = "The agency violated the Public Records Act"
        code, out, err = self.response("--date", "2026-03-04", "--kind", "extension", "--reason", text)
        self.assertEqual(code, 0, err)
        self.assertIn("review-required: --reason", err)
        self.assertNotIn("violated", err)
        self.assertEqual(self.row()["extension_reason"], text)
        with mock.patch("campaign_tool.legal_claims.legal_claims", side_effect=RuntimeError("boom")):
            self.undo()
            code, _, err = self.response("--date", "2026-03-04", "--kind", "extension", "--reason", "plain")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.row()["extension_reason"], "plain")

    # --- the provisional comparison
    def test_later_stated_date_shows_both_dates_and_the_exact_note(self):
        later = "2026-12-31"
        self.extend("--expected-on", later)
        row = self.row()
        self.assertEqual((row["extension_expected_on"], row["with_extension"], row["extension_note"]),
                         (later, self.provisional, LATE))

    def test_equal_or_earlier_stated_date_has_no_warning(self):
        for stated in (self.provisional, "2026-03-05"):
            self.extend("--expected-on", stated)
            self.assertNotIn("extension_note", self.row())
            self.undo()

    # --- reminders
    def test_cue_only_after_the_stated_date_and_never_past_due(self):
        self.extend("--expected-on", "2026-03-10")
        for as_of in ("2026-03-09", "2026-03-10"):
            self.assertEqual(self.cues(as_of), [])
            self.assertEqual(self.due(as_of)[1]["extension_date_passed"], 0)
        code, report = self.due("2026-03-11", expect=3)
        (cue,) = self.cues("2026-03-11")
        self.assertEqual((cue["state"], cue["due"], cue["days"], cue["note"], cue["with_extension"]),
                         ("stated_extension_date_passed", "2026-03-10", -1, "stated extension date passed",
                          self.provisional))
        self.assertEqual((report["extension_date_passed"], report["past_due"], report["stated_date_passed"],
                          report["installment_passed"]), (1, 0, 0, 0))  # exit 3 on the cue alone
        ordinary = [r for r in report["open"] if r.get("kind") is None]
        self.assertEqual(len(ordinary), 1)  # the provisional row is still there, not double-counted
        self.assertEqual(ordinary[0]["state"], "upcoming")

    def test_cue_carries_the_warning_when_the_agency_date_is_later(self):
        self.extend("--expected-on", "2026-03-10")
        self.undo()
        self.extend("--expected-on", "2026-12-31")
        (cue,) = self.cues("2027-01-02")
        self.assertIn(LATE, cue["note"])
        self.assertTrue(cue["note"].startswith("stated extension date passed"))

    def test_answers_closure_and_withdrawal_suppress_the_cue(self):
        for kind, extra in (("determination", ()), ("denied", ()), ("produced", ()), ("produced", ("--partial",)),
                            ("closed", ()), ("withdrawn", ("--note", "sent twice"))):
            with self.subTest(kind=kind, extra=extra):
                self.extend("--expected-on", "2026-03-10")
                self.assertEqual(self.response("--date", "2026-03-06", "--kind", kind, *extra)[0], 0)
                self.assertEqual(self.cues("2026-04-01"), [])
                self.assertEqual(self.due("2026-04-01")[1]["extension_date_passed"], 0)
                self.undo()
                self.undo()

    def test_waiting_on_clarification_keeps_the_row_and_adds_the_cue(self):
        self.extend("--expected-on", "2026-03-10")
        self.assertEqual(self.response("--date", "2026-03-06", "--kind", "clarification_requested")[0], 0)
        report = self.due("2026-03-20")[1]
        self.assertEqual(sorted(r["state"] for r in report["open"]), ["stated_extension_date_passed", "waiting_on_you"])

    def test_receipt_annotation_joins_the_cue(self):
        self.extend("--expected-on", "2026-03-10")
        code, _, err = run("kit-received", "--directory", self.root, "--agency", self.a, "--on", "2026-03-03",
                           "--evidence", "portal")
        self.assertEqual(code, 0, err)
        (cue,) = self.cues("2026-03-20")
        self.assertEqual((cue["received_on"], cue["receipt_evidence"]), ("2026-03-03", "portal"))
        self.assertTrue(cue["note"].startswith("stated extension date passed"))
        self.assertIn("counted from receipt 2026-03-03 (portal)", cue["note"])

    def test_named_requests_do_not_share_a_cue(self):
        self.extend("--expected-on", "2026-03-10")
        drafts = self.root / "kit" / "requests"
        (drafts / f"{self.a}--second.md").write_bytes((drafts / f"{self.a}.md").read_bytes())
        code, _, err = run("kit-sent", "--directory", self.root, "--agency", self.a, "--request", "second", "--date",
                           SENT, "--channel", "email")
        self.assertEqual(code, 0, err)
        self.assertEqual([(r["agency_id"], r["request"]) for r in self.cues("2026-03-20")], [(self.a, "main")])

    def test_followup_ignores_the_cue(self):
        self.extend("--expected-on", "2026-03-10")
        code, _, err = run("kit-followup", "--directory", self.root, "--agency", self.a, "--as-of", "2026-03-20")
        self.assertEqual(code, 1)
        self.assertIn("not past due", err)

    def test_today_lists_the_cue_once_with_both_dates_and_warning(self):
        self.extend("--expected-on", "2026-12-31")
        code, out, err = run("today", "--directory", self.root, "--as-of", "2027-01-02", "--json")
        self.assertEqual(code, 0, err)
        items = [i for i in json.loads(out) if i["kind"] == "request_stated_extension_date_passed"] \
            if isinstance(json.loads(out), list) else \
            [i for i in json.loads(out)["items"] if i["kind"] == "request_stated_extension_date_passed"]
        self.assertEqual(len(items), 1)
        what = items[0]["what"]
        self.assertIn("stated extension date passed 2026-12-31", what)
        self.assertIn(self.provisional, what)
        self.assertIn(LATE, what)
        self.assertEqual(items[0]["command"], f"kit-status --directory {self.root}")

    # --- compatibility
    def test_extension_recorded_without_the_details_reads_and_prints_unchanged(self):
        step = self.extend()["steps"][0]
        self.assertEqual(set(step), {"on", "kind", "recorded_at"})
        row = self.row()
        self.assertTrue(row["extended"])
        self.assertTrue({"extension_expected_on", "extension_reason", "extension_note"}.isdisjoint(row))
        report = self.due("2027-01-01")[1]
        self.assertNotIn("extension_date_passed", report)
        self.assertEqual([r for r in report["open"] if r.get("kind") == "extension"], [])
        self.assertEqual(report["open"][0]["state"], "past_due")


if __name__ == "__main__":
    unittest.main()
