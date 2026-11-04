"""Per-scope answers on a sent kit request (DF1207-02). Offline, synthetic kit dirs and the us-ca law package."""
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
from campaign_tool import cli, kit_track, law  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402

SCOPES = [scope["scope_id"] for scope in law.load_package("us-ca")["request_scopes"]]
A, B, C = SCOPES[0], SCOPES[1], SCOPES[2]


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


class ScopeAnswerTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket", "socket.create_connection", "urllib.request.urlopen"):
            offline = mock.patch(target, side_effect=AssertionError("network used"))
            offline.start()
            self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-scope-answers-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        self.assertGreaterEqual(len(SCOPES), 3)
        code, _, err = run("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                           "--state", "CA")
        self.assertEqual(code, 0, err)
        self.kit = self.root / "kit"
        self.sent = self.kit / "sent.json"

    def build(self, *scopes):
        args = [x for scope in scopes for x in ("--scope", scope)]
        code, _, err = run("kit", "--directory", self.root, *args)
        self.assertEqual(code, 0, err)
        self.agency = sorted(p.stem for p in (self.kit / "requests").glob("*.md"))[0]
        ready_drafts(self.root)

    def send(self, agency=None, *extra):
        return run("kit-sent", "--directory", self.root, "--agency", agency or self.agency, "--date", "2026-03-02",
                   "--channel", "email", *extra)

    def sent_ok(self, *scopes):
        self.build(*scopes)
        code, _, err = self.send()
        self.assertEqual(code, 0, err)

    def respond(self, kind, *extra, on="2026-03-10"):
        return run("kit-response", "--directory", self.root, "--agency", self.agency, "--date", on, "--kind", kind,
                   *extra)

    def ok(self, kind, *extra, on="2026-03-10"):
        code, out, err = self.respond(kind, *extra, on=on)
        self.assertEqual(code, 0, err)
        return out

    def stored(self):
        return json.loads(self.sent.read_text(encoding="utf-8"))["requests"][self.agency]

    def refused(self, kind, *words, extra=(), code=1):
        before = self.sent.read_bytes()
        result = self.respond(kind, *extra)
        self.assertEqual(result[0], code, result[1])
        for word in words:
            self.assertIn(word, result[2])
        self.assertEqual(self.sent.read_bytes(), before)

    def row(self):
        code, out, err = run("kit-status", "--directory", self.root)
        self.assertEqual(code, 0, err)
        return next(r for r in json.loads(out)["requests"] if r["agency_id"] == self.agency)

    def coverage_row(self):
        code, out, err = run("kit-coverage", "--directory", self.root, "--csv")
        self.assertEqual(code, 0, err)
        rows = list(csv.reader(io.StringIO(out)))
        self.assertEqual(rows[0][-1], "scopes")
        self.assertEqual(rows[0][:-1], list(kit_track.CSV_HEADER[:-1]))
        return next(dict(zip(rows[0], r)) for r in rows[1:] if r[0] == self.agency)

    def edit(self, change):
        document = json.loads(self.sent.read_text(encoding="utf-8"))
        change(document["requests"][self.agency])
        self.sent.write_text(json.dumps(document), encoding="utf-8")

    # 1
    def test_kit_without_scope_stores_every_package_scope_in_order(self):
        self.sent_ok()
        self.assertEqual(self.stored()["scopes"], SCOPES)
        self.assertEqual([s["scope_id"] for s in self.row()["scopes"]], SCOPES)

    # 2
    def test_kit_with_two_scopes_stores_exactly_those_and_refuses_a_third(self):
        self.sent_ok(B, A)
        self.assertEqual(self.stored()["scopes"], [A, B])  # package order, as kit writes it
        self.refused("determination", f"--scope {C} was not asked in this request",
                     extra=("--scope", C, "--disposition", "produced"))
        self.refused("determination", f"--scope {C} was not asked", extra=("--scope", A, "--scope", C,
                                                                           "--disposition", "produced"))
        self.assertEqual(self.stored()["steps"], [])

    # 3
    def test_latest_answer_and_date_per_scope_others_have_no_answer_yet(self):
        self.sent_ok()
        self.ok("produced", "--partial", "--scope", A, "--scope", B, "--disposition", "partly_produced")
        self.ok("produced", "--scope", A, "--disposition", "produced", on="2026-03-20")
        [first, second] = self.stored()["steps"]
        self.assertEqual(first["scope_answers"], {A: "partly_produced", B: "partly_produced"})
        self.assertEqual(second["scope_answers"], {A: "produced"})
        scopes = self.row()["scopes"]
        self.assertEqual(scopes[0], {"scope_id": A, "answer": "produced", "on": "2026-03-20"})
        self.assertEqual(scopes[1], {"scope_id": B, "answer": "partly_produced", "on": "2026-03-10"})
        for later in scopes[2:]:
            self.assertEqual((later["answer"], later["on"]), (None, None))
        cells = self.coverage_row()
        self.assertEqual(cells["scopes"], f"2/{len(SCOPES)} answered")

    def test_stored_scope_answer_keys_are_sorted(self):
        self.sent_ok()
        self.ok("denied", "--scope", B, "--scope", A, "--disposition", "withheld")
        self.assertEqual(list(self.stored()["steps"][0]["scope_answers"]), sorted([A, B]))

    # 4
    def test_scope_and_disposition_go_together_and_values_are_checked(self):
        self.sent_ok()
        self.refused("denied", "go together", extra=("--scope", A))
        self.refused("denied", "go together", extra=("--disposition", "withheld"))
        self.refused("denied", "given twice", extra=("--scope", A, "--scope", A, "--disposition", "withheld"))
        self.refused("denied", "invalid choice", code=2, extra=("--scope", A, "--disposition", "refused"))
        with self.assertRaisesRegex(ValueError, "--disposition must be one of"):
            kit_track.record_step(self.root, self.agency, "2026-03-10", "denied", scope_ids=[A], disposition="maybe")
        self.assertEqual(self.stored()["steps"], [])

    # 5
    def test_older_request_without_scopes_reads_unchanged(self):
        self.sent_ok()
        with_scopes = self.row()
        self.edit(lambda entry: entry.pop("scopes"))
        old = self.row()
        self.assertIsNone(old["scopes"])
        self.assertEqual({k: v for k, v in old.items() if k != "scopes"},
                         {k: v for k, v in with_scopes.items() if k != "scopes"})
        self.assertEqual(self.coverage_row()["scopes"], "not recorded")
        self.refused("denied", "scopes not recorded for this request",
                     extra=("--scope", A, "--disposition", "withheld"))
        self.ok("denied")  # a step without --scope is accepted as before
        self.assertNotIn("scope_answers", self.stored()["steps"][0])
        self.assertNotIn("scopes", self.stored())

    # 6
    def test_scope_refused_on_kinds_that_are_not_an_answer(self):
        self.sent_ok()
        for kind in ("acknowledged", "extension", "clarification_requested", "clarified", "fee_estimate"):
            extra = ("--amount", "10") if kind == "fee_estimate" else ()
            self.refused(kind, "--scope is not allowed", extra=(*extra, "--scope", A, "--disposition", "produced"))
        self.assertEqual(self.stored()["steps"], [])

    # 7
    def test_missing_symlinked_or_unreadable_summary_stores_no_scopes(self):
        for how in ("missing", "symlink", "garbage", "not-a-dict", "bad-scopes"):
            with self.subTest(how=how):
                self.build(A)
                summary = self.kit / "summary.json"
                real = summary.read_text(encoding="utf-8")
                if how == "missing":
                    summary.unlink()
                elif how == "symlink":
                    target = self.root / "elsewhere.json"
                    target.write_text(real, encoding="utf-8")
                    summary.unlink()
                    summary.symlink_to(target)
                else:
                    summary.write_text({"garbage": "{not json", "not-a-dict": "[1]",
                                        "bad-scopes": json.dumps({"scopes": [7]})}[how], encoding="utf-8")
                self.sent.unlink(missing_ok=True)
                code, _, err = self.send()
                self.assertEqual(code, 0, err)
                self.assertNotIn("scopes", self.stored())
                self.assertIsNone(self.row()["scopes"])
                self.refused("denied", "scopes not recorded", extra=("--scope", A, "--disposition", "withheld"))
                summary.unlink(missing_ok=True)

    def test_summary_naming_a_scope_outside_the_package_refuses_kit_sent(self):
        self.build(A)
        summary = self.kit / "summary.json"
        document = json.loads(summary.read_text(encoding="utf-8"))
        document["scopes"] = [A, "not-a-scope"]
        summary.write_text(json.dumps(document), encoding="utf-8")
        code, out, err = self.send()
        self.assertEqual(code, 1, out)
        self.assertIn("'not-a-scope'", err)
        self.assertFalse(self.sent.exists())

    def test_replace_rereads_the_current_kit_summary(self):
        self.sent_ok()
        self.assertEqual(self.stored()["scopes"], SCOPES)
        self.build(A)
        code, _, err = self.send(self.agency, "--replace")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.stored()["scopes"], [A])

    # 8
    def test_hand_edited_answers_are_a_malformed_steps_refusal(self):
        self.sent_ok(A, B)
        self.ok("denied", "--scope", A, "--disposition", "withheld")
        for change in (lambda e: e["steps"][0].__setitem__("scope_answers", {C: "withheld"}),
                       lambda e: e["steps"][0].__setitem__("scope_answers", {A: "maybe"}),
                       lambda e: e["steps"][0].__setitem__("scope_answers", [A]),
                       lambda e: e["steps"][0].__setitem__("kind", "acknowledged"),
                       lambda e: e.__setitem__("scopes", [A, A]),
                       lambda e: e.__setitem__("scopes", A),
                       lambda e: e.pop("scopes")):
            with self.subTest():
                document = self.sent.read_bytes()
                self.edit(change)
                edited = self.sent.read_bytes()
                for result in (run("kit-status", "--directory", self.root),
                               self.respond("closed", on="2026-03-11")):
                    self.assertEqual(result[0], 1, result[1])
                    self.assertIn("malformed steps", result[2])
                self.assertEqual(self.sent.read_bytes(), edited)
                self.sent.write_bytes(document)

    # coverage text and the two renderings
    def test_coverage_table_and_csv_show_not_recorded_and_k_of_n(self):
        self.sent_ok(A, B)
        code, table, err = run("kit-coverage", "--directory", self.root)
        self.assertEqual(code, 0, err)
        self.assertIn("scopes", table.splitlines()[0].split()[-1])
        self.assertIn("0/2 answered", table)
        self.ok("determination", "--scope", A, "--disposition", "no_records_stated")
        self.assertEqual(self.coverage_row()["scopes"], "1/2 answered")
        self.ok("produced", "--scope", A, "--scope", B, "--disposition", "produced", on="2026-03-12")
        self.assertEqual(self.coverage_row()["scopes"], "2/2 answered")
        self.assertIn("2/2 answered", run("kit-coverage", "--directory", self.root)[1])

    def test_dispositions_are_independent_of_withheld_basis(self):
        self.sent_ok()
        self.ok("denied", "--withheld-basis", "Gov. Code 7923.600")
        step = self.stored()["steps"][0]
        self.assertNotIn("scope_answers", step)
        self.assertEqual({s["answer"] for s in self.row()["scopes"]}, {None})


if __name__ == "__main__":
    unittest.main()
