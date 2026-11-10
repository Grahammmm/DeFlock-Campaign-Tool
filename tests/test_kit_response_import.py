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
EXT = "2026-03-05,extension,Notice received,,2026-03-20,"


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

    # An extension's CSV expected_on cell is its stored extension_expected_on: replay identity compares that.
    def respond(self, *arguments):
        code, out, err = run("kit-response", "--directory", self.root, "--agency", self.agency, *arguments)
        self.assertEqual((code, err), (0, ""))

    def stamp(self):
        info = self.sent.stat()
        return self.sent.read_bytes(), info.st_mode, info.st_mtime_ns

    def replays(self, path, skipped=1):
        before = self.stamp()
        for extra in ((), ("--apply",)):
            with mock.patch.object(kit, "_write_json", wraps=kit._write_json) as write:
                code, out, err = self.do(path, *extra)
            self.assertEqual((code, err), (0, ""))
            report = json.JSONDecoder().raw_decode(out)[0]
            self.assertEqual((report["added"], report["skipped"], report["applied"]), (0, skipped, False))
            self.assertEqual([c for c in write.call_args_list if c.args[0] == self.sent], [])
            self.assertEqual(self.stamp(), before)

    def test_dated_extension_import_replays_without_writes(self):
        path = self.write(EXT)
        code, out, err = self.do(path)
        self.assertEqual((code, err, json.loads(out.split("Checked only")[0])["added"]), (0, "", 1))
        self.assertEqual(self.steps(), [])
        self.assertEqual(self.do(path, "--apply")[0], 0)
        (step,) = self.steps()
        self.assertEqual((step["extension_expected_on"], "expected_on" in step), ("2026-03-20", False))
        self.replays(path)
        self.assertEqual(self.steps(), [step])
        self.assertIn("line 2: already recorded, skipped", self.do(path)[1])

    def test_manual_extension_replays_from_csv_after_later_steps(self):
        self.respond("--date", "2026-03-05", "--kind", "extension", "--note", "Notice received",
                     "--expected-on", "2026-03-20", "--reason", "Voluminous records")
        self.respond("--date", "2026-03-10", "--kind", "determination")
        before = self.steps()
        self.replays(self.write(EXT))
        self.assertEqual(self.steps(), before)
        self.assertEqual(before[0]["extension_reason"], "Voluminous records")
        self.assertEqual([s["kind"] for s in before], ["extension", "determination"])

    def test_changed_or_omitted_extension_date_is_not_a_duplicate(self):
        self.respond("--date", "2026-03-05", "--kind", "extension", "--note", "Notice received",
                     "--expected-on", "2026-03-20")
        for name, row in (("blank", "2026-03-05,extension,Notice received,,,"),
                          ("other", "2026-03-05,extension,Notice received,,2026-03-25,")):
            with self.subTest(name):  # a valid row ahead of the conflict does not authorize a partial commit
                path = self.write("2026-03-05,fee_estimate,,50.00,,", row, name=name + ".csv")
                before = self.sent.read_bytes()
                for extra in ((), ("--apply",)):
                    self.refused(self.do(path, *extra), "1 bad row(s)", "line 3: extension is allowed only once")
                self.assertEqual(self.sent.read_bytes(), before)

    def test_stored_undated_extension_versus_supplied_date(self):
        self.respond("--date", "2026-03-05", "--kind", "extension", "--note", "Notice received")
        self.refused_untouched(self.write(EXT), "line 2: extension is allowed only once")
        self.assertNotIn("extension_expected_on", self.steps()[0])

    def test_undated_legacy_extension_still_replays(self):
        self.respond("--date", "2026-03-05", "--kind", "extension", "--note", "Notice received")
        self.replays(self.write("2026-03-05,extension,Notice received,,,"))
        self.refused_untouched(self.write("2026-03-05,extension,Notice received,,2026-03-20,"),
                               "line 2: extension is allowed only once")
        self.assertEqual(self.steps()[0].keys() & {"expected_on", "extension_expected_on"}, set())

    def test_extension_and_production_date_projections_stay_separate(self):
        extension = {"on": "2026-03-05", "kind": "extension", "extension_expected_on": "2026-03-20"}
        production = {"on": "2026-03-05", "kind": "determination", "expected_on": "2026-03-20"}
        self.assertEqual(kit_import_steps._step_signature(extension),
                         ("2026-03-05", "extension", None, None, "2026-03-20", False))
        self.assertEqual(kit_import_steps._step_signature(production),
                         ("2026-03-05", "determination", None, None, "2026-03-20", False))
        self.assertIsNone(kit_import_steps._step_signature({"on": "2026-03-05", "kind": "extension",
                                                            "expected_on": "2026-03-20"})[4])  # no fallback
        rows = (EXT, "2026-03-10,determination,,,2026-03-30,", "2026-03-12,produced,First batch,,2026-04-15,yes")
        path = self.write(*rows)
        self.assertEqual(self.do(path, "--apply")[0], 0)
        steps = self.steps()
        self.assertEqual([("expected_on" in s, "extension_expected_on" in s) for s in steps],
                         [(False, True), (True, False), (True, False)])
        self.replays(path, skipped=3)
        duplicate = self.write(EXT, EXT, name="twice.csv")
        self.refused_untouched(duplicate, "line 3: same as line 2")

    def test_extension_replay_is_request_local_and_preserves_validation(self):
        drafts = self.root / "kit" / "requests"
        (drafts / f"{self.agency}--second.md").write_text((drafts / f"{self.agency}.md").read_text(encoding="utf-8"),
                                                          encoding="utf-8")
        self.assertEqual(run("kit-sent", "--directory", self.root, "--agency", self.agency, "--date", SENT_ON,
                             "--channel", "email", "--request", "second")[0], 0)
        self.respond("--date", "2026-03-05", "--kind", "extension", "--note", "Notice received",
                     "--expected-on", "2026-03-20")
        main_before = self.steps()
        sent_before = self.sent.read_bytes()
        for row, words in (("2026-03-05,extension,,,2026-02-30,", "--expected-on"),
                           ("2026-03-06,extension,,,2026-03-01,", "before the extension step"),
                           ("2999-01-01,extension,,,2999-02-01,", "in the future"),
                           ("2026-03-01,extension,,,2026-03-20,", "before the request was sent")):
            with self.subTest(row=row):
                self.refused(self.do(self.write(row, name="bad.csv"), "--apply", "--request", "second"),
                             "line 2:", words)
                self.assertEqual(self.sent.read_bytes(), sent_before)
        self.assertEqual(self.do(self.write(EXT), "--apply", "--request", "second")[0], 0)
        self.assertEqual(self.steps(), main_before)
        second = kit_track.read_sent(self.root)[f"{self.agency}#second"]["steps"]
        self.assertEqual([(s["kind"], s["extension_expected_on"]) for s in second], [("extension", "2026-03-20")])

    # CSV syntax: broken quoting refuses the whole file before any row is checked or anything is written.
    SYNTAX = "malformed CSV (quotes or field size)"

    def raw(self, data, name="raw.csv"):
        path = self.tmp / name
        path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
        return path

    def tree(self):
        return sorted((str(p.relative_to(self.root)), p.is_dir() or p.read_bytes(), p.lstat().st_mode,
                       p.lstat().st_mtime_ns) for p in self.root.rglob("*"))

    def syntax_refused(self, path, line):
        """Preview and --apply both stop on the record's first line; no writer runs and the campaign is unchanged."""
        before = self.tree()
        for extra in ((), ("--apply",)):
            with mock.patch.object(kit_import_steps, "_write", wraps=kit_import_steps._write) as write:
                code, out, err = self.do(path, *extra)
            self.assertEqual((code, out, err), (2, "", f"Stopped: csv line {line}: {self.SYNTAX}\n"))
            write.assert_not_called()
        with self.assertRaises(kit_import_steps.ImportRefused) as raised:
            kit_import_steps._read_rows(path)
        self.assertEqual(str(raised.exception), f"csv line {line}: {self.SYNTAX}")
        self.assertEqual(self.tree(), before)

    def test_unclosed_final_partial_field_refuses_preview_and_apply(self):
        for end in ("", "\n"):  # the reproduced bytes, without and with a final newline
            with self.subTest(end=repr(end)):
                self.syntax_refused(self.raw(HEADER + '\n2026-03-05,produced,,,,"no' + end), 2)
        self.assertEqual(self.steps(), [])

    def test_junk_after_closing_quote_is_not_repaired(self):
        path = self.raw(HEADER + '\n2026-03-05,acknowledged,"Portal"receipt,,,\n')
        self.syntax_refused(path, 2)
        self.assertEqual(self.steps(), [])

    def test_bad_later_record_prevents_entire_timeline_import(self):
        self.respond("--date", "2026-03-05", "--kind", "acknowledged")  # the first row below is already recorded
        recorded = self.steps()
        for first in ("2026-03-05,acknowledged,,,,", "2026-03-06,clarified,,,,"):
            with self.subTest(first=first):
                self.syntax_refused(self.raw(f'{HEADER}\n{first}\n2026-03-10,determination,"Denied"x,,,\n'), 3)
        self.assertEqual(self.steps(), recorded)

    def test_multiline_csv_error_names_record_start(self):
        legal = '2026-03-05,acknowledged,"two\r\nlines",,,'  # one record on physical lines 2 and 3
        for bad, line in (('2026-03-06,clarified,"a"b,,,', 4), ('2026-03-06,clarified,"open\r\nstill open', 4),
                          ('\r\n2026-03-06,clarified,"x"y,,,', 5)):
            with self.subTest(bad=bad):
                self.syntax_refused(self.raw(b"\xef\xbb\xbf" + "\r\n".join((HEADER, legal, bad, "")).encode()), line)
        rows = kit_import_steps._read_rows(self.raw("\r\n".join((HEADER, legal, "2026-03-06,clarified,,,,", ""))))
        self.assertEqual([(line, cells[2]) for line, cells in rows], [(2, "two\r\nlines"), (4, "")])

    def test_syntax_diagnostic_never_echoes_private_cells(self):
        marker = "PRIVATE-MARKER-7731 <script>\x1b[2J"
        for name, row in (("junk", f'2026-03-05,acknowledged,"{marker}"{marker},,,'),
                          ("open", f'2026-03-05,acknowledged,"{marker}'),
                          ("size", f'2026-03-05,acknowledged,"{marker}{"A" * 140000}",,,')):
            with self.subTest(name):
                path = self.raw(f"{HEADER}\n{row}\n", name=f"{marker[:19]}-{name}.csv")
                self.syntax_refused(path, 2)
                code, out, err = self.do(path, "--apply")
                for leaked in ("PRIVATE-MARKER", "<script>", "\x1b", "expected after", "field limit", str(self.tmp)):
                    self.assertNotIn(leaked, out + err)

    def test_valid_quoted_csv_and_no_final_newline_keep_semantics(self):
        rows = ('2026-03-05,acknowledged,"Portal, receipt",,,', '2026-03-10,produced,"Batch ""A""   one",,,yes')
        for name, data in (("lf", "\n".join((HEADER, *rows))), ("crlf", "\r\n".join((HEADER, *rows, ""))),
                           ("bom", "\ufeff" + "\n".join((HEADER, *rows, "")))):
            with self.subTest(name):
                path = self.raw(data, name=name + ".csv")
                self.assertEqual([cells[2] for _, cells in kit_import_steps._read_rows(path)],
                                 ["Portal, receipt", 'Batch "A"   one'])
                code, out, err = self.do(path)
                self.assertEqual((code, err, json.loads(out.split("Checked only")[0])["added"]), (0, "", 2))
        self.assertEqual(self.do(path, "--apply")[0], 0)
        self.assertEqual([(s["kind"], s["note"], s.get("partial")) for s in self.steps()],
                         [("acknowledged", "Portal, receipt", None), ("produced", 'Batch "A" one', True)])

    def test_strict_admission_preserves_replay_and_named_request_contracts(self):
        drafts = self.root / "kit" / "requests"
        (drafts / f"{self.agency}--second.md").write_text((drafts / f"{self.agency}.md").read_text(encoding="utf-8"),
                                                          encoding="utf-8")
        self.assertEqual(run("kit-sent", "--directory", self.root, "--agency", self.agency, "--date", SENT_ON,
                             "--channel", "email", "--request", "second")[0], 0)
        quoted = '2026-03-05,extension,"Notice received",,2026-03-20,'
        path = self.write(quoted, '2026-03-12,produced,"First, batch",,2026-04-15,yes')
        code, out, err = self.do(path)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(sorted(json.loads(out.split("Checked only")[0])),
                         ["added", "agency_id", "applied", "lines", "request", "rows", "skipped", "steps"])
        with mock.patch.object(kit, "_write_json", wraps=kit._write_json) as write:
            self.assertEqual(self.do(path, "--apply")[0], 0)
        self.assertEqual(len([c for c in write.call_args_list if c.args[0] == self.sent]), 1)
        self.replays(path, skipped=2)
        self.refused_untouched(self.write('2026-03-05,extension,"Notice received",,2026-03-25,', name="moved.csv"),
                               "1 bad row(s)", "line 2:")  # another dispatch date is not a replay
        self.assertEqual(self.do(path, "--apply", "--request", "second")[0], 0)
        second = kit_track.read_sent(self.root)[f"{self.agency}#second"]["steps"]
        self.assertEqual([(s["kind"], s.get("extension_expected_on")) for s in second],
                         [("extension", "2026-03-20"), ("produced", None)])

    # Capacity: the whole prospective sent store must stay within the ceiling read_sent enforces.
    TODAY = date(2026, 3, 25)
    OVER = "kit/sent.json: import would exceed 1 MiB; nothing was written"
    CONTACT = {"on": "2026-03-03", "channel": "phone", "role": "Records clerk", "recorded_at": "2026-03-03T00:00:00+00:00"}

    @staticmethod
    def encoded(document):
        return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    def make_second(self):
        drafts = self.root / "kit" / "requests"
        (drafts / f"{self.agency}--second.md").write_text((drafts / f"{self.agency}.md").read_text(encoding="utf-8"),
                                                          encoding="utf-8")
        self.assertEqual(run("kit-sent", "--directory", self.root, "--agency", self.agency, "--date", SENT_ON,
                             "--channel", "email", "--request", "second")[0], 0)

    def calibrate(self, path, request="main"):  # bytes a real apply adds to this store; the store is then restored
        before = self.sent.read_bytes()
        kit_import_steps.import_steps(self.root, self.agency, path, request=request, apply=True, today=self.TODAY)
        delta = self.sent.stat().st_size - len(before)
        self.sent.write_bytes(before)
        return delta

    def fill_to(self, target, key=None, wide=0, keep_bytes=False):  # synthetic contacts make the store exactly target bytes
        document = json.loads(self.sent.read_text(encoding="utf-8"))
        entry = document["requests"][key or self.agency]

        def build(lengths):
            entry["contacts"] = [dict(self.CONTACT, note="X" * n) for n in lengths]
            return len(self.encoded(document))
        first, step = build([1]), build([1, 1]) - build([1])
        count = 1
        while target - first - (count - 1) * step > 499 * count:
            count += 1
        extra, lengths = target - first - (count - 1) * step, [1] * count
        for i in range(count):
            lengths[i] += min(499, extra)
            extra -= min(499, extra)
        self.assertEqual(build(lengths), target)
        if wide:  # the same number of characters in more UTF-8 bytes, or the same bytes in fewer characters
            note = entry["contacts"][0]["note"]
            entry["contacts"][0]["note"] = "é" * wide + "X" * (len(note) - (2 if keep_bytes else 1) * wide)
            self.assertEqual(len(self.encoded(document)), target if keep_bytes else target + wide)
        self.sent.write_bytes(self.encoded(document))
        kit_track.read_sent(self.root)  # the starting store is readable
        return document

    def snapshot(self, *paths):
        out = []
        for path in paths:
            info = path.stat()
            out.append((path.read_bytes(), info.st_mode, info.st_ino, info.st_mtime_ns))
        return out

    def import_refused(self, path, apply, request="main", message=None):
        with mock.patch.object(kit, "_write_json", wraps=kit._write_json) as write:
            with self.assertRaises(kit_import_steps.ImportRefused) as raised:
                kit_import_steps.import_steps(self.root, self.agency, path, request=request, apply=apply,
                                              today=self.TODAY)
        self.assertEqual(str(raised.exception), message or self.OVER)
        self.assertEqual([c for c in write.call_args_list if c.args[0] == self.sent], [])
        return write

    def test_final_new_row_over_reader_cap_refuses_preview_and_apply(self):
        path = self.write("2026-03-05,acknowledged,,,,")
        delta = self.calibrate(path)
        self.fill_to(kit_track.MAX_BYTES - delta + 1)  # the real ceiling: the one new row ends one byte over
        before = self.snapshot(self.sent, path)
        for apply in (False, True):
            with self.subTest(apply=apply):
                self.import_refused(path, apply)
                self.assertEqual(self.snapshot(self.sent, path), before)
                self.assertEqual(kit_track.read_sent(self.root)[self.agency]["steps"], [])

    def test_actual_cli_capacity_refusal_is_exit_two_without_success(self):
        path = self.write("2026-03-05,acknowledged,PRIVATE-MARKER,,,")
        delta = self.calibrate(path)
        self.fill_to(kit_track.MAX_BYTES - delta + 1)
        before = self.snapshot(self.sent, path)
        for extra in ((), ("--apply",)):
            with self.subTest(extra=extra):
                code, out, err = self.do(path, *extra)
                self.assertEqual((code, out), (2, ""))
                self.assertEqual(err, "Stopped: " + self.OVER + "\n")
                for leaked in ("Traceback", "PRIVATE-MARKER", "XXXX", str(self.tmp), "Recorded only", "Checked only"):
                    self.assertNotIn(leaked, out + err)
                self.assertEqual(self.snapshot(self.sent, path), before)
                self.assertEqual(kit_track.read_sent(self.root)[self.agency]["steps"], [])

    def last_row_case(self, request):
        extra = () if request == "main" else ("--request", request)
        key = self.agency if request == "main" else f"{self.agency}#second"
        if request == "second":
            self.make_second()
        recorded = self.write("2026-03-04,acknowledged,,,,", name="recorded.csv")
        kit_import_steps.import_steps(self.root, self.agency, recorded, request=request, apply=True, today=self.TODAY)
        path = self.write("2026-03-04,acknowledged,,,,", "2026-03-10,extension,Letter received,,,",
                          "2026-03-20,produced,First batch,,,yes", name="all.csv")  # one skipped row, two new rows
        one = self.write("2026-03-04,acknowledged,,,,", "2026-03-10,extension,Letter received,,,", name="one.csv")
        total, first = self.calibrate(path, request), self.calibrate(one, request)
        self.assertGreater(total, first)
        self.fill_to(kit_track.MAX_BYTES - total + 1)  # only the last new row crosses the ceiling
        before, stored = self.snapshot(self.sent, path), kit_track.read_sent(self.root)
        for apply in (False, True):
            write = self.import_refused(path, apply, request)
            # the working copy took the initial store and the first new row, never the overflowing last row
            self.assertEqual(len(write.call_args_list), 2)
            self.assertEqual(self.snapshot(self.sent, path), before)
            self.assertEqual(kit_track.read_sent(self.root), stored)
        self.assertEqual(self.do(path, "--apply", *extra)[0], 2)
        self.assertEqual(self.snapshot(self.sent, path), before)
        self.assertEqual(self.do(one, "--apply", *extra)[0], 0)  # the first new row alone fits and is recorded
        self.assertEqual([s["kind"] for s in kit_track.read_sent(self.root)[key]["steps"]],
                         ["acknowledged", "extension"])
        self.assertLessEqual(self.sent.stat().st_size, kit_track.MAX_BYTES)

    def test_last_row_capacity_refusal_discards_all_earlier_new_rows(self):
        self.last_row_case("main")

    def test_last_row_capacity_refusal_in_a_named_request(self):
        self.last_row_case("second")

    def test_prospective_exact_byte_limit_includes_final_newline(self):
        path = self.write("2026-03-05,acknowledged,,,,")
        self.make_second()
        delta, empty = self.calibrate(path), self.sent.read_bytes()
        for final, ok in ((kit_track.MAX_BYTES - 1, True), (kit_track.MAX_BYTES, True),
                          (kit_track.MAX_BYTES + 1, False)):
            with self.subTest(final=final):
                self.sent.write_bytes(empty)  # no step recorded yet
                self.fill_to(final - delta, key=f"{self.agency}#second")  # padding lives in the other request
                before = self.snapshot(self.sent, path)
                for apply in (False, True):
                    if ok:
                        report = kit_import_steps.import_steps(self.root, self.agency, path, apply=apply,
                                                               today=self.TODAY)
                        self.assertEqual((report["added"], report["applied"]), (1, apply))
                    else:
                        self.import_refused(path, apply)
                if ok:
                    self.assertEqual(self.sent.stat().st_size, final)
                    self.assertTrue(self.sent.read_bytes().endswith(b"}\n"))
                    self.assertEqual(len(kit_track.read_sent(self.root)[self.agency]["steps"]), 1)
                    self.assertEqual(kit_track.read_sent(self.root)[f"{self.agency}#second"]["contacts"][0]["note"][0], "X")
                else:
                    self.assertEqual(self.snapshot(self.sent, path), before)
                    self.assertEqual(len(self.sent.read_bytes()) + delta, final)

    def test_utf8_bytes_and_unrelated_requests_count_toward_capacity(self):
        path = self.write("2026-03-05,acknowledged,,,,")
        self.make_second()
        delta = self.calibrate(path)
        second = f"{self.agency}#second"
        cap = kit_track.MAX_BYTES
        self.fill_to(cap - delta, key=second, wide=10, keep_bytes=True)  # fewer characters, same bytes: fits exactly
        unrelated = kit_track.read_sent(self.root)[second]
        self.assertEqual(kit_import_steps.import_steps(self.root, self.agency, path, apply=True,
                                                       today=self.TODAY)["applied"], True)
        self.assertEqual(self.sent.stat().st_size, cap)
        after = kit_track.read_sent(self.root)
        self.assertEqual(after[second], unrelated)
        self.assertTrue(after[second]["contacts"][0]["note"].startswith("é" * 10))
        self.sent.write_bytes(self.encoded({"schema_version": 1, "requests": {**after, self.agency: dict(
            after[self.agency], steps=[])}}))
        self.fill_to(cap - delta, key=second, wide=10)  # as many characters as the fitting store, 10 more bytes
        document = json.loads(self.sent.read_text(encoding="utf-8"))
        self.assertLessEqual(len(self.sent.read_bytes()), cap - delta + 10)
        self.assertLess(sum(len(c["note"]) for c in document["requests"][second]["contacts"]), cap)
        before = self.snapshot(self.sent, path)
        for apply in (False, True):
            self.import_refused(path, apply)
            self.assertEqual(self.snapshot(self.sent, path), before)

    def test_skipped_only_import_does_not_add_capacity_or_rewrite_refusal(self):
        path = self.write("2026-03-05,acknowledged,,,,")
        self.assertEqual(self.do(path, "--apply")[0], 0)
        document = json.loads(self.sent.read_text(encoding="utf-8"))
        document["requests"][self.agency]["contacts"] = []

        def compact():
            return (json.dumps(document, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        contacts = document["requests"][self.agency]["contacts"]
        full = dict(self.CONTACT, note="X" * 500)
        while True:
            contacts.append(dict(full))
            if len(compact()) > kit_track.MAX_BYTES:
                contacts.pop()
                break
        self.assertGreater(len(self.encoded(document)), kit_track.MAX_BYTES)  # its canonical rewrite is too big
        self.sent.write_bytes(compact())
        kit_track.read_sent(self.root)  # yet the compact file itself is readable
        before = self.snapshot(self.sent, path)
        for extra in ((), ("--apply",)):
            code, out, err = self.do(path, *extra)
            self.assertEqual((code, err), (0, ""))
            report = json.loads(out.split("Checked only")[0].split("Recorded only")[0])
            self.assertEqual((report["added"], report["skipped"], report["applied"]), (0, 1, False))
            self.assertEqual(self.snapshot(self.sent, path), before)
        twice = self.write("2026-03-05,acknowledged,,,,", "2026-03-05,acknowledged,,,,", name="twice.csv")
        self.refused_untouched(twice, "line 3:", "same as line 2")

    def test_encoding_boundary_refusal_is_fixed_and_value_free(self):
        for tree in ({"a": {"note": "\ud800PRIVATE"}}, {"\udfff": {"steps": []}}):
            with self.assertRaises(kit_import_steps.ImportRefused) as raised:
                kit_import_steps._check_capacity(tree)
            self.assertEqual(str(raised.exception),
                             "kit/sent.json: import cannot be encoded as UTF-8; nothing was written")
            self.assertIsNone(raised.exception.__cause__)
            self.assertTrue(raised.exception.__suppress_context__)
        self.assertIsNone(kit_import_steps._check_capacity({"a": {"steps": [], "note": "é"}}))


if __name__ == "__main__":
    unittest.main()
