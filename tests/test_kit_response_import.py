"""kit-response-import records a transcribed timeline row by row through record_step's dry run, in one checked write.
Offline, synthetic: invented agencies and dates; no socket may be opened."""
import contextlib
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
from campaign_tool import cli, kit, kit_import_steps, kit_track  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402

SENT_ON = "2026-03-02"
HEADER = "date,kind,note,amount,expected_on,partial"


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


class KitResponseImportTests(unittest.TestCase):
    def setUp(self):
        for target, effect in (("socket.socket", AssertionError("socket opened")),
                               ("urllib.request.urlopen", AssertionError("network used"))):
            patcher = mock.patch(target, side_effect=effect)
            patcher.start()
            self.addCleanup(patcher.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-import-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        self.assertEqual(run("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                             "--state", "CA")[0], 0)
        self.assertEqual(run("kit", "--directory", self.root)[0], 0)
        self.sent = self.root / "kit" / "sent.json"
        self.agency = sorted((self.root / "kit" / "requests").glob("*.md"))[0].stem
        ready_drafts(self.root)
        self.assertEqual(run("kit-sent", "--directory", self.root, "--agency", self.agency, "--date", SENT_ON,
                             "--channel", "email")[0], 0)

    def write(self, *lines, header=HEADER, name="timeline.csv"):
        path = self.tmp / name
        path.write_text("\n".join([header, *lines]) + "\n", encoding="utf-8")
        return path

    def do(self, path, *extra, agency=None):
        return run("kit-response-import", "--directory", self.root, "--agency", agency or self.agency,
                   "--csv", path, *extra)

    def steps(self):
        return kit_track.read_sent(self.root)[self.agency]["steps"]

    def refused(self, result, *words):
        code, out, err = result
        self.assertEqual(code, 2, out + err)
        self.assertTrue(err.startswith("Stopped: "), err)
        for word in words:
            self.assertIn(word, err)

    def refused_untouched(self, path, *words, **kwargs):
        before = self.sent.read_bytes()
        self.refused(self.do(path, "--apply", **kwargs), *words)
        self.assertEqual(self.sent.read_bytes(), before)

    GOOD = ("2026-03-05,acknowledged,,,,", "2026-03-10,extension,Letter received,,,",
            "2026-03-20,produced,First batch,,,yes")

    def test_good_timeline_check_writes_nothing_and_apply_records_in_order_with_one_write(self):
        path = self.write(*self.GOOD)
        before = self.sent.read_bytes()
        code, out, err = self.do(path)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out.split("Checked only")[0])["added"], 3)
        self.assertIn("nothing was written", out)
        self.assertEqual(self.sent.read_bytes(), before)
        with mock.patch.object(kit, "_write_json", wraps=kit._write_json) as write:
            code, out, err = self.do(path, "--apply")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len([c for c in write.call_args_list if c.args[0] == self.sent]), 1)
        report = json.loads(out.split("Recorded only")[0])
        self.assertEqual((report["added"], report["skipped"], report["applied"]), (3, 0, True))
        steps = self.steps()
        self.assertEqual([(s["on"], s["kind"]) for s in steps], [("2026-03-05", "acknowledged"),
                         ("2026-03-10", "extension"), ("2026-03-20", "produced")])
        self.assertEqual((steps[1]["note"], steps[2].get("partial")), ("Letter received", True))

    def test_rows_are_checked_cumulatively_against_the_earlier_rows(self):
        path = self.write("2026-03-05,acknowledged,,,,", "2026-03-06,closed,,,,", "2026-03-07,extension,,,,")
        self.refused_untouched(path, "line 4:", "closed")
        alone = self.write("2026-03-07,extension,,,,", name="alone.csv")  # the same row alone passes
        self.assertEqual(self.do(alone)[0], 0)

    def test_each_bad_row_is_refused_by_its_line_and_nothing_is_written(self):
        for row, words in (("2026-02-01,determination,,,,", ("before the request was sent",)),
                           ("2999-01-01,determination,,,,", ("in the future",)),
                           ("2026-03-05,bogus,,,,", ("--kind must be one of",)),
                           ("2026-03-05,determination,,,,yes", ("--partial is only for produced",)),
                           ("2026-03-05,determination,,10,,", ("--amount is only for fee_estimate",)),
                           ("2026-03-05,determination,,,,maybe", ("partial must be empty, yes or no",)),
                           ("2026-03-05,determination,,,", ("expected 6 cells, found 5",)),
                           ("not-a-date,determination,,,,", ("--date must be a real",))):
            with self.subTest(row=row):
                self.refused_untouched(self.write("2026-03-04,acknowledged,,,,", row, name="bad.csv"), "line 3:", *words)
        self.assertEqual(self.steps(), [])

    def test_a_refusal_never_echoes_note_text(self):
        path = self.write('2026-03-05,acknowledged,"secret <b> text",,,')
        code, out, err = self.do(path)
        self.assertEqual(code, 2)
        self.assertIn("line 2:", err)
        self.assertNotIn("secret", out + err)

    def test_header_must_be_exact_but_a_bom_is_accepted(self):
        for header, words in ((HEADER + ",extra", ("unexpected column 'extra'",)),
                              ("date,kind,note,amount,expected_on", ("missing column partial",)),
                              ("kind,date,note,amount,expected_on,partial", ("out of order",)),
                              ("date,kind,note,note,expected_on,partial", ("missing column amount", "repeated")),
                              ("date,kind,note,amount,expected_on,partial,partial", ("repeated",))):
            with self.subTest(header=header):
                self.refused_untouched(self.write("2026-03-05,acknowledged,,,,", header=header), *words)
        path = self.tmp / "bom.csv"
        path.write_bytes(b"\xef\xbb\xbf" + (HEADER + "\n2026-03-05,acknowledged,,,,\n").encode())
        self.assertEqual(self.do(path, "--apply")[0], 0)
        self.assertEqual(len(self.steps()), 1)

    def test_file_problems_are_refused_before_any_row_is_checked(self):
        self.refused_untouched(self.write(), "no rows")
        self.refused_untouched(self.write(name="empty.csv", header=""), "no rows")
        rows = ["2026-03-05,acknowledged,,,,"] * 201
        self.refused_untouched(self.write(*rows), "at most 200 rows")
        self.refused_untouched(self.write(*rows[:200]), "same as line 2")  # 200 rows pass the row-count gate
        big = self.tmp / "big.csv"
        big.write_bytes(b"x" * (257 * 1024))
        self.refused_untouched(big, "larger than 256 KiB")
        link = self.tmp / "link.csv"
        link.symlink_to(self.write("2026-03-05,acknowledged,,,,", name="real.csv"))
        self.refused_untouched(link, "symlinks are refused")
        self.refused_untouched(self.tmp, "regular file")
        self.refused_untouched(self.tmp / "missing.csv", "cannot be opened")
        bad = self.tmp / "latin.csv"
        bad.write_bytes((HEADER + "\n2026-03-05,acknowledged,caf\xe9,,,\n").encode("latin-1"))
        self.refused_untouched(bad, "not UTF-8")
        self.assertEqual(self.steps(), [])

    def test_a_recorded_step_is_skipped_and_a_repeat_inside_the_file_is_refused(self):
        self.assertEqual(run("kit-response", "--directory", self.root, "--agency", self.agency, "--date", "2026-03-05",
                             "--kind", "acknowledged", "--note", "Portal   receipt")[0], 0)
        path = self.write("2026-03-05,acknowledged,Portal receipt,,,", "2026-03-10,extension,,,,")
        code, out, err = self.do(path, "--apply")
        self.assertEqual((code, err), (0, ""))
        report = json.loads(out.split("Recorded only")[0])
        self.assertEqual((report["added"], report["skipped"]), (1, 1))
        self.assertEqual(report["lines"], ["line 2: already recorded, skipped"])
        self.assertEqual(len(self.steps()), 2)
        before = self.sent.read_bytes()
        again = self.do(path, "--apply")  # everything is now recorded: nothing to write
        self.assertEqual(again[0], 0)
        self.assertEqual(json.loads(again[1].split("Checked only")[0])["skipped"], 2)
        self.assertEqual(self.sent.read_bytes(), before)
        twice = self.write("2026-03-12,determination,,,,", "2026-03-12,determination,,,,", name="twice.csv")
        self.refused_untouched(twice, "line 3: same as line 2")

    def test_an_amount_row_equal_to_a_recorded_fee_step_is_skipped(self):
        self.assertEqual(run("kit-response", "--directory", self.root, "--agency", self.agency, "--date", "2026-03-05",
                             "--kind", "fee_estimate", "--amount", "245")[0], 0)
        code, out, err = self.do(self.write("2026-03-05,fee_estimate,,245.00,,"), "--apply")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual((json.loads(out.split("Checked only")[0])["skipped"], len(self.steps())), (1, 1))

    def test_twenty_bad_rows_are_listed_then_the_rest_are_counted(self):
        path = self.write("2026-03-05,acknowledged,,,,", *[f"2026-03-{day},bogus,,,," for day in range(6, 31)])
        before = self.sent.read_bytes()
        code, out, err = self.do(path, "--apply")
        self.assertEqual(code, 2)
        self.assertEqual(err.count("\nline "), 20)
        self.assertIn("25 bad row(s)", err)
        self.assertIn("line 22:", err)
        self.assertNotIn("line 23:", err)
        self.assertIn("and 5 more", err)
        self.assertEqual(self.sent.read_bytes(), before)

    def test_later_rows_are_checked_without_the_refused_rows(self):
        path = self.write("2026-03-05,acknowledged,,,,", "2026-03-06,clarified,,,,", "2026-03-07,determination,,,,")
        code, out, err = self.do(path)
        self.assertEqual(code, 2)
        self.assertIn("1 bad row(s)", err)
        self.assertIn("line 3:", err)

    def test_unknown_agency_or_request_uses_the_record_step_message_and_exit_2(self):
        path = self.write(*self.GOOD)
        self.refused(self.do(path, agency="ca-nowhere-police"), "no hand-sent request recorded for ca-nowhere-police")
        self.refused(self.do(path, "--request", "second"), f"no sent request {self.agency}/second")
        self.refused(self.do(path, "--request", "Bad Name"), "--request must be")
        self.refused(self.do(path, agency=self.agency + "#second"), "no hand-sent request recorded")
        self.assertEqual(self.steps(), [])

    def test_a_named_request_is_imported_separately(self):
        drafts = self.root / "kit" / "requests"
        (drafts / f"{self.agency}--second.md").write_text((drafts / f"{self.agency}.md").read_text(encoding="utf-8"),
                                                          encoding="utf-8")
        self.assertEqual(run("kit-sent", "--directory", self.root, "--agency", self.agency, "--date", SENT_ON,
                             "--channel", "email", "--request", "second")[0], 0)
        self.assertEqual(self.do(self.write(*self.GOOD), "--apply", "--request", "second")[0], 0)
        self.assertEqual(self.steps(), [])
        self.assertEqual(len(kit_track.read_sent(self.root)[f"{self.agency}#second"]["steps"]), 3)

    def test_a_crash_before_the_write_leaves_the_file_unchanged(self):
        path = self.write(*self.GOOD)
        before = self.sent.read_bytes()
        with mock.patch.object(kit, "_write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                kit_import_steps.import_steps(self.root, self.agency, path, apply=True)
        self.assertEqual(self.sent.read_bytes(), before)

    def test_the_api_takes_a_fixed_today(self):
        path = self.write("2026-03-20,acknowledged,,,,")
        with self.assertRaises(kit_import_steps.ImportRefused) as raised:
            kit_import_steps.import_steps(self.root, self.agency, path, today=date(2026, 3, 10))
        self.assertIn("line 2: --date 2026-03-20 is in the future", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
