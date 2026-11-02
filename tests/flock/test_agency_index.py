"""RF2330-7: flock_agency.py index STORE prints one status line per agency folder, built from timeline() itself."""
import contextlib
import io
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.flock.approval_field import APPROVAL_LINE, trusted, write_field
from tests.flock.test_agency_timeline import BANNED, HEAD, HEADER, MOD, SHA, STEPS, UNSENT, row

NOTE = "recorded fields only; not a legal status; the canonical ledger wins"
LINE = re.compile(r"index \| (\S+) \| requests=(\d+) open=(\d+) \| oldest open sent (\S+) \| (last \S+ \S+|last -) \| "
                  r"next reminder (\S+) \(provisional\) \| failing_runs=(\d+) \| errors=(\d+)")


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = self.tmp / "store"
        (self.store / "agencies").mkdir(parents=True)

    def agency(self, slug, name, rows, steps=None):
        folder = self.store / "agencies" / slug
        folder.mkdir()
        (folder / "REQUESTS.csv").write_text(HEADER + "".join(row(*r[:2], agency=name, **r[2]) for r in rows), encoding="utf-8")
        if steps is not None:
            (folder / "REQUEST-STEPS.csv").write_text(STEPS + steps, encoding="utf-8")
        return folder

    def failing_email(self, folder, day):  # a signed draft edited after signing: approval-unverified, a failing run state
        run = folder / "runs" / day
        run.mkdir(parents=True)
        draft = run / "EMAIL-DRAFT.md"
        draft.write_text("\n".join([HEAD, "To: planted-local@city.example", "Subject: s", "---", "planted body", "---",
                                    APPROVAL_LINE, f"Sent by: {UNSENT}"]) + "\n", encoding="utf-8")
        write_field(run)
        draft.write_text(draft.read_text(encoding="utf-8").replace("planted body", "edited body"), encoding="utf-8")

    def three(self):
        self.agency("alpha-city", "Alpha City", [("R-1", "closed", {"sent": "2026-09-01"}), ("R-2", "withdrawn", {"sent": "2026-09-03"})],
                    f"R-1,closed,2026-09-20,x,{SHA},2026-09-21\n")
        self.agency("beta-town", "Beta Town", [
            ("R-3", "open", {"sent": "2026-10-03", "ev": "ack", "rdate": "2026-10-05", "sha": SHA}),
            ("R-4", "open", {"sent": "2026-10-01"}), ("R-5", "produced", {"sent": "2026-08-01"}),
            ("R-7", "open", {"sent": "2026-10-02", "ev": "ack", "rdate": "2026-10-02", "sha": SHA})])
        self.failing_email(self.agency("gamma-county", "Gamma County", [("R-6", "open", {"sent": "2026-10-02"})]), "2026-10-04")

    def run_index(self, *extra, store=None, as_of="2026-10-20"):
        argv = ["index", str(store or self.store), *(["--as-of", as_of] if as_of else []), *extra]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), trusted():
            code = MOD.main(argv)
        return code, out.getvalue(), err.getvalue()

    def timeline_json(self, slug, as_of="2026-10-20"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), trusted():
            MOD.main(["timeline", str(self.store), "--agency", slug, "--as-of", as_of, "--json"])
        return json.loads(out.getvalue())

    def test_three_agencies_in_slug_order_with_counts_and_failing_run_exit(self):
        self.three()
        code, out, err = self.run_index()
        self.assertEqual((code, err), (1, ""), out)
        beta_due = sorted(o["reminder"].split()[1] for o in self.timeline_json("beta-town")["open"] if o["request"] == "R-7")
        self.assertRegex(beta_due[0], r"^2026-1[01]-[0-9]{2}$")
        self.assertEqual(out.splitlines(), [
            "index | alpha-city | requests=2 open=0 | oldest open sent - | last 2026-09-20 closed | next reminder - (provisional)"
            " | failing_runs=0 | errors=0",
            "index | beta-town | requests=4 open=3 | oldest open sent 2026-10-01 | last 2026-10-05 receipt"
            f" | next reminder {beta_due[0]} (provisional) | failing_runs=0 | errors=0",
            "index | gamma-county | requests=1 open=1 | oldest open sent 2026-10-02 | last 2026-10-04 email-approval-unverified"
            " | next reminder - (provisional) | failing_runs=1 | errors=0",
            f"index | agencies=3 open=4 failing_runs=1 errors=0 skipped=0 | {NOTE}"])
        r3 = [o["reminder"].split()[1] for o in self.timeline_json("beta-town")["open"] if o["request"] == "R-3"]
        self.assertLess(beta_due[0], r3[0])  # the earliest reminder wins, not the first row
        (self.store / "agencies" / "gamma-county" / "runs").rename(self.tmp / "runs-moved")
        self.assertEqual(self.run_index()[0], 0)

    def test_index_counts_equal_timeline_json_for_every_agency(self):
        self.three()
        (self.store / "agencies" / "alpha-city" / "REQUEST-STEPS.csv").write_text(STEPS + "R-1,receipt,someday,x,y,z\n",
                                                                                  encoding="utf-8")
        code, raw, _ = self.run_index("--json")
        data = json.loads(raw)
        self.assertEqual((code, data["schema"], data["review_required"], data["as_of"], data["note"], data["skipped"]),
                         (1, "flock-agency-index-v1", True, "2026-10-20", NOTE, []))
        self.assertEqual([a["agency"] for a in data["agencies"]], ["alpha-city", "beta-town", "gamma-county"])
        for a in data["agencies"]:
            with self.subTest(a["agency"]):
                t = self.timeline_json(a["agency"])
                self.assertEqual((a["requests"], a["open"], a["failing_runs"], sum(a["errors"].values())),
                                 (sum(e["kind"] == "sent" for e in t["events"]), len(t["open"]), t["failing_runs"], len(t["errors"])))
        _, text, _ = self.run_index()
        self.assertEqual([m.groups() for m in map(LINE.fullmatch, text.splitlines()[:-1])],
                         [(a["agency"], str(a["requests"]), str(a["open"]), a["oldest_open_sent"],
                           "last %s %s" % (a["last_event"]["date"], a["last_event"]["kind"]) if a["last_event"] else "last -",
                           a["next_reminder"], str(a["failing_runs"]), str(sum(a["errors"].values()))) for a in data["agencies"]])

    def test_broken_source_counts_on_its_line_only_and_links_and_bad_names_are_skipped_unread(self):
        self.three()
        clean = self.run_index()[1].splitlines()
        (self.store / "agencies" / "alpha-city" / "REQUEST-STEPS.csv").write_text(STEPS + "R-1,receipt,someday,x,y,z\n",
                                                                                  encoding="utf-8")
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "REQUESTS.csv").write_text(HEADER + row("R-OUT", agency="Linked"), encoding="utf-8")
        (self.store / "agencies" / "linked").symlink_to(outside, target_is_directory=True)
        (self.store / "agencies" / "Bad Name").mkdir()
        (self.store / "agencies" / "Overdue").mkdir()
        (self.store / "agencies" / "notes.txt").write_text("not a folder", encoding="utf-8")
        with mock.patch.object(MOD, "timeline", wraps=MOD.timeline) as called:
            code, out, err = self.run_index()
        self.assertEqual(sorted(c.args[1] for c in called.call_args_list), ["alpha-city", "beta-town", "gamma-county"])
        lines = out.splitlines()
        self.assertEqual((code, err), (1, ""))
        self.assertRegex(lines[0], r"^index \| alpha-city \| requests=2 .* \| errors=1$")
        self.assertEqual(lines[1:3], clean[1:3])
        self.assertEqual(lines[3:], ["skipped | ? | not a slug", "skipped | (unsafe-label) | not a slug", "skipped | linked | link",
                                     f"index | agencies=3 open=4 failing_runs=1 errors=1 skipped=3 | {NOTE}"])
        self.assertNotIn("R-OUT", out)
        real = MOD.requests_source

        def boom(folder, errors):  # one agency whose timeline() raises
            if folder.name == "beta-town":
                raise OSError("planted")
            return real(folder, errors)
        with mock.patch.object(MOD, "requests_source", side_effect=boom):
            code, raw, _ = self.run_index("--json")
        self.assertEqual(code, 1)
        self.assertEqual([(a["agency"], a["requests"], a["open"], a["failing_runs"], a["errors"]) for a in json.loads(raw)["agencies"]],
                         [("alpha-city", 2, 0, 0, {"REQUEST-STEPS.csv": 1}), ("beta-town", 0, 0, 0, {"agency": 1}),
                          ("gamma-county", 1, 1, 1, {})])
        self.assertNotIn("planted", raw)

    def test_as_of_before_every_event_and_undated_status_never_last(self):
        self.three()
        lines = self.run_index(as_of="2026-01-01")[1].splitlines()
        self.assertEqual([LINE.fullmatch(ln)[5] for ln in lines[:3]], ["last -"] * 3)
        self.agency("delta-port", "Delta Port", [("R-8", "withdrawn", {"sent": "2026-10-01"})])
        lines = self.run_index()[1].splitlines()
        self.assertEqual(LINE.fullmatch(lines[2]).groups()[:5], ("delta-port", "1", "0", "-", "last 2026-10-01 sent"))
        self.assertEqual(json.loads(self.run_index("--json")[1])["agencies"][2]["last_event"], {"date": "2026-10-01", "kind": "sent"})

    def test_empty_agencies_folder_prints_only_totals(self):
        for extra in ((), ("--json",)):
            code, out, err = self.run_index(*extra)
            self.assertEqual((code, err), (0, ""))
            if extra:
                self.assertEqual((json.loads(out)["agencies"], json.loads(out)["skipped"]), ([], []))
            else:
                self.assertEqual(out, f"index | agencies=0 open=0 failing_runs=0 errors=0 skipped=0 | {NOTE}\n")

    def test_planted_address_and_banned_slug_are_never_printed(self):
        self.agency("late-city", "Late City", [("planted-local@city.example", "open", {"sent": "2026-10-01"}),
                                               ("R-1", "open", {"sent": "2026-10-02", "ev": "ack", "rdate": "2026-10-02", "sha": SHA})])
        self.failing_email(self.store / "agencies" / "late-city", "2026-10-03")
        for extra in ((), ("--json",)):
            code, out, err = self.run_index(*extra)
            self.assertEqual((code, err), (1, ""))
            self.assertIn("(unsafe-label)", out)
            self.assertFalse(BANNED.search(out), out)
            self.assertNotRegex(out, r"@|planted|R-1|[0-9a-f]{64}")

    def test_oldest_open_sent_uses_request_identity_not_masked_labels(self):
        # two distinct ids that both display as (unsafe-label): the closed one must not supply the open one's sent date
        self.agency("mask-city", "Mask City", [("overdue-old", "closed", {"sent": "2026-09-01"}),
                                               ("late-new", "open", {"sent": "2026-10-01"})])
        for extra in ((), ("--json",)):
            code, out, err = self.run_index(*extra)
            self.assertEqual(err, "")
            if extra:
                a = json.loads(out)["agencies"][0]
                self.assertEqual((a["open"], a["oldest_open_sent"]), (1, "2026-10-01"))
            else:
                self.assertEqual(LINE.fullmatch(out.splitlines()[0]).groups()[2:4], ("1", "2026-10-01"))
            self.assertFalse(BANNED.search(out), out)
            self.assertNotRegex(out, r"-old|-new")

    def test_store_refusals_print_only_the_refusal(self):
        self.three()
        with mock.patch.object(MOD, "REPO", self.tmp):
            self.assertEqual(self.run_index(), (1, "", "index: store is inside the repository\n"))
        refused = (1, "", "index: store is not a folder with an agencies folder\n")
        self.assertEqual(self.run_index(store=self.tmp / "none"), refused)
        (self.tmp / "s2").mkdir()
        (self.tmp / "s2" / "agencies").symlink_to(self.store / "agencies", target_is_directory=True)
        self.assertEqual(self.run_index(store=self.tmp / "s2"), refused)
        (self.tmp / "s3").symlink_to(self.store, target_is_directory=True)
        self.assertEqual(self.run_index("--json", store=self.tmp / "s3"), refused)

    def test_usage_errors_exit_2_with_empty_stdout(self):
        self.three()
        s = str(self.store)
        for argv in (["index"], ["index", s, "--agency", "x"], ["index", s, "--inventory", "f"], ["index", s, "--as-of", "2026-02-30"],
                     ["index", s, "--json", "--json"], ["index", s, "--as-of"], ["index", s, "--as-of", "2026-10-20", "--as-of", "2026-10-21"],
                     ["index", s, "extra"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()) as err, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual((MOD.main(argv), out.getvalue()), (2, ""))
                self.assertTrue(err.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(MOD.main(["--help"]), 0)
        self.assertIn("index STORE [--as-of YYYY-MM-DD] [--json]", out.getvalue())


if __name__ == "__main__":
    unittest.main()
