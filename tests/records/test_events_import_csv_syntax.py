"""Broken CSV quoting is refused for the whole staged document before any append."""
import contextlib
import csv
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import cli
from campaign_tool.records import events_import as ei
from campaign_tool.records.outcomes import timeline as tl

MAP = "request_ref,agency_key,request_id\nREF-1,alpha-pd,req-1\n"


def tsv(rows, last=None, newline="\n"):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=ei.COLUMNS, delimiter="\t", lineterminator=newline)
    writer.writeheader()
    for i, kind in enumerate(rows):
        writer.writerow(dict.fromkeys(ei.COLUMNS, "") | dict(
            event_id="E-" + str(i), event_type=kind, request_ref="REF-1",
            agency="Synthetic PD", date="2026-11-05", what_happened="Synthetic event"))
    text = stream.getvalue()
    return (text if last is None else text.rstrip("\r\n") + last).encode()


class CsvSyntax(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name)
        self.stage = self.project / "agency-events"
        self.stage.mkdir()
        (self.stage / "request-map.csv").write_text(MAP)
        self.journal = self.project / "manual-events.jsonl"

    def put(self, data):
        (self.stage / "alpha-pd.tsv").write_bytes(data)

    def command(self, *args):
        output = io.StringIO()
        argv = ["records", "events-import", "--project", str(self.project),
                "--agency-key", "alpha-pd", *args]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
            code = cli.main()
        return code, output.getvalue()

    def assert_refused(self, *flags):
        for extra in ((), ("--apply",), ("--allow-unmapped",), ("--apply", "--allow-unmapped")):
            with patch.object(ei, "append_events") as spy:
                code, out = self.command(*extra)
            self.assertEqual((code, out), (1, "events_import_invalid_input\n"))
            spy.assert_not_called()
        self.assertFalse(self.journal.exists())
        self.assertFalse((self.project / "approvals").exists())
        with self.assertRaisesRegex(ValueError, "^events_import_invalid_input$"):
            ei.run(self.project, "alpha-pd", apply=True)

    def test_unterminated_and_junk_quoted_tsv_never_append(self):
        for tail in ('"ok', '"ok"junk'):
            for rows in (["call"], ["call", "call"]):
                with self.subTest(tail=tail, rows=len(rows)):
                    self.put(tsv(rows, tail))
                    self.assert_refused()

    def test_malformed_request_map_never_reaches_event_append(self):
        self.put(tsv(["call"]))
        for text in ('request_ref,agency_key,"request_id\n', MAP + 'REF-2,alpha-pd,"req-2',
                     MAP + 'REF-2,alpha-pd,"req-2\n', MAP + 'REF-2,alpha-pd,"req"x\n'):
            with self.subTest(text=text):
                (self.stage / "request-map.csv").write_text(text)
                self.assert_refused()

    def test_syntax_failure_preserves_existing_journal(self):
        self.put(tsv(["call"]))
        self.assertEqual(self.command("--apply")[0], 0)
        before = (self.journal.read_bytes(), self.journal.stat().st_mode, self.journal.stat().st_mtime_ns,
                  sorted(p.name for p in self.project.iterdir()))
        self.put(tsv(["call", "sent"], '"ok'))
        for extra in ((), ("--apply",), ("--apply", "--allow-unmapped")):
            self.assertEqual(self.command(*extra), (1, "events_import_invalid_input\n"))
        after = (self.journal.read_bytes(), self.journal.stat().st_mode, self.journal.stat().st_mtime_ns,
                 sorted(p.name for p in self.project.iterdir()))
        self.assertEqual(before, after)

    def test_csv_field_limit_refusal_is_fixed(self):
        limit = csv.field_size_limit()
        self.put(tsv(["call"], "a" * (limit + 1)))
        self.assert_refused()
        self.assertEqual(csv.field_size_limit(), limit)

    def test_valid_dialects_and_idempotency_unchanged(self):
        variants = {
            "lf": tsv(["call"]), "crlf": tsv(["call"], newline="\r\n"),
            "bom": b"\xef\xbb\xbf" + tsv(["call"]),
            "quoted": tsv(["call"], '"a\ttab ""q"" and\nnewline"'),
        }
        for name, data in variants.items():
            with self.subTest(name=name):
                self.put(data)
                code, out = self.command()
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out)["mapped"], {"call": 1})
                self.assertFalse(self.journal.exists())
                self.assertEqual(json.loads(self.command("--apply")[1])["added"], 1)
                self.assertEqual(len(tl.read_manual_events(self.journal)), 1)
                self.assertEqual(json.loads(self.command("--apply")[1])["added"], 0)
                self.journal.unlink()

    def test_existing_header_width_and_semantic_refusals_unchanged(self):
        good = tsv(["call"]).decode().splitlines()
        cases = {"short": "\n".join(good[:1] + ["E-0\t2026-11-05"]) + "\n",
                 "extra": good[0] + "\n" + good[1] + "\tx\n",
                 "dup": "\n".join(good + good[1:]) + "\n"}
        for name, text in cases.items():
            with self.subTest(name=name):
                self.put(text.encode())
                code, out = self.command("--apply")
                self.assertEqual(code, 1)
                self.assertFalse(self.journal.exists())
        self.put(tsv(["call"]).replace(b"2026-11-05", b"2026-02-30"))
        self.assertEqual(json.loads(self.command("--apply")[1])["refused"], ["E-0"])
        self.put(tsv(["not_a_type"]))
        self.assertEqual(json.loads(self.command("--apply")[1])["unmapped"], ["E-0"])


if __name__ == "__main__":
    unittest.main()
