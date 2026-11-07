"""Synthetic staged events: mapping, refusals, idempotency and timeline integration."""
import contextlib
import csv
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import events_import as ei
from campaign_tool.records import cli
from campaign_tool.records import console_manual_events as cm
from campaign_tool.records.outcomes import timeline as tl

EXPECTED = ("sent", "acknowledged", "extension notice", "promised date", "determination",
            "production", "production", "denial", "no records", "fee estimate",
            "our follow-up sent", "our follow-up sent", "our follow-up sent",
            "clarification requested", "call", "portal check", "closed")


def row(ident, kind, ref="REF-1", **changes):
    return dict.fromkeys(ei.COLUMNS, "") | dict(event_id=ident, event_type=kind, request_ref=ref,
        agency="Synthetic Alpha PD", date="2026-11-05", what_happened="Synthetic event") | changes


def tsv(rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=ei.COLUMNS, delimiter="\t")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


class EventsImport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.stage = self.project / "agency-events"
        self.stage.mkdir()
        (self.stage / "request-map.csv").write_text(
            "request_ref,agency_key,request_id\nREF-1,alpha-pd,req-1\nREF-1,beta-pd,req-2\n")
        self.journal = self.project / "manual-events.jsonl"
        self.rows = [row("E-" + str(i), kind) for i, kind in enumerate(ei.TYPE_MAP)]
        self.rows += [row("clock1", "clock_start"), row("clock2", "clock_due"),
                      row("context", "unknown", "context"),
                      row("third", "request_sent", "Other (third party)"),
                      row("unknown", "not_a_type"), row("missing", "request_sent", "REF-404")]
        self.stage_rows(self.rows)

    def stage_rows(self, rows):
        (self.stage / "alpha-pd.tsv").write_bytes(tsv(rows))

    def command(self, *args):
        output = io.StringIO()
        with patch.object(sys, "argv", ["records", "events-import", "--project", str(self.project),
                                        "--agency-key", "alpha-pd", *args]), contextlib.redirect_stdout(output):
            code = cli.main()
        return code, output.getvalue()

    def test_dry_run_refusal_allow_apply_timeline_and_repeat(self):
        code, output = self.command()
        result = json.loads(output)
        self.assertEqual(code, 1)
        self.assertEqual(result["skipped"], 4)
        self.assertEqual(result["unmapped"], ["unknown", "missing"])
        from collections import Counter
        self.assertEqual(result["mapped"], dict(Counter(EXPECTED)))
        self.assertEqual(result["already_present"], 0)
        self.assertEqual(self.command("--apply")[0], 1)
        self.assertFalse(self.journal.exists())
        self.assertFalse((self.project / "approvals").exists())
        self.assertEqual(self.command("--allow-unmapped")[0], 0)
        self.assertFalse(self.journal.exists())
        code, output = self.command("--apply", "--allow-unmapped")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["added"], 17)
        events = tl.read_manual_events(self.journal)
        self.assertEqual([e["type"] for e in events], list(EXPECTED))
        self.assertTrue(all(e["agency_id"] == "alpha-pd" and e["request_id"] == "req-1"
                            and e["actor"] == "events.tsv import" for e in events))
        self.assertEqual(events[0]["event_id"], "tsv-alpha-pd-E-0")
        self.assertEqual(events[3]["new_date"], "2026-11-05")
        register = {"as_of": "2026-11-06", "requests": [
            {"agency_key": "alpha-pd", "ledger_request_id": "req-1"},
            {"agency_key": "beta-pd", "ledger_request_id": "req-2"}]}
        report = tl.build_timeline(register, {}, [], [], events, run_id="synthetic")
        self.assertEqual(len(report["requests"][0]["events"]), 17)
        self.assertEqual(report["requests"][1]["events"], [])
        self.assertNotIn("clarification requested", tl.EVENT_STAGE)
        original = self.journal.read_bytes()
        self.rows[0]["what_happened"] = "Changed source does not rewrite an existing id"
        self.stage_rows(self.rows)
        result = ei.run(self.project, "alpha-pd", apply=True, allow_unmapped=True)
        self.assertEqual((result["added"], result["already_present"]), (0, 17))
        self.assertEqual(self.journal.read_bytes(), original)
        self.assertEqual(self.journal.stat().st_mode & 0o777, 0o600)

    def test_skips_all_fixed_types_and_clips_summary(self):
        rows = [row(str(i), kind, "not-mapped") for i, kind in enumerate(ei.SKIP)]
        rows.append(row("mapped", "request_sent", what_happened="a" * 250))
        result = ei.plan(tsv(rows), {("REF-1", "alpha-pd"): "req-1"}, "alpha-pd")
        self.assertEqual(result["skipped"], 7)
        self.assertEqual(result["unmapped"], [])
        self.assertEqual(len(result["events"][0]["summary"]), 200)

    def test_refused_rows_never_print_content_or_apply_even_when_allowed(self):
        for changes in ({"what_happened": "private > content"}, {"source": "private < content"},
                        {"date": "2026-02-30"}, {"date": "2026-11-05extra"},
                        {"what_happened": "line\u2028break"}, {"what_happened": "a\u2029b"},
                        {"what_happened": "a\u0085b"}):
            with self.subTest(changes=changes):
                self.stage_rows([row("bad", "call", **changes), row("good", "call")])
                code, output = self.command("--apply", "--allow-unmapped")
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(output)["refused"], ["bad"])
                self.assertNotIn("private", output)
                self.assertFalse(self.journal.exists())
        self.stage_rows([row("bad>id", "call")])
        self.assertNotIn(">", self.command("--apply")[1])

    def test_duplicate_source_ids_and_malformed_map_refuse(self):
        self.stage_rows([row("same", "call"), row("same", "denial")])
        self.assertEqual(json.loads(self.command("--apply")[1])["refused"], ["same"])
        self.assertFalse(self.journal.exists())
        (self.stage / "request-map.csv").write_text(
            "request_ref,agency_key,request_id\nREF-1,alpha-pd,r1\nREF-1,alpha-pd,r2\n")
        self.assertEqual(self.command("--apply")[0], 1)
        self.assertFalse(self.journal.exists())

    def test_corrupt_journal_and_batch_limit_preserve_existing_bytes(self):
        self.stage_rows([row("good", "call"), row("second", "call")])
        self.journal.write_bytes(b"{}\n")
        os.chmod(self.journal, 0o600)
        self.assertEqual(self.command("--apply")[0], 1)
        self.assertEqual(self.journal.read_bytes(), b"{}\n")
        self.journal.unlink()
        incoming = ei.plan(tsv([row("a", "call"), row("b", "call")]),
                           {("REF-1", "alpha-pd"): "req-1"}, "alpha-pd")["events"]
        with patch.object(tl, "MAX_MANUAL", 1):
            with self.assertRaises(cm.ManualError):
                cm.append_events(self.project, incoming)
        self.assertEqual(self.journal.read_bytes(), b"")

    def test_bad_headers_and_unsafe_staging_refuse_without_writes(self):
        (self.stage / "alpha-pd.tsv").write_bytes(b"event_id\nwrong\n")
        self.assertEqual(self.command("--apply")[0], 1)
        (self.stage / "alpha-pd.tsv").unlink()
        target = self.project / "untouched"
        target.write_bytes(b"unchanged")
        (self.stage / "alpha-pd.tsv").symlink_to(target)
        self.assertEqual(self.command("--apply")[0], 1)
        self.assertEqual(target.read_bytes(), b"unchanged")
        self.assertFalse(self.journal.exists())
        with self.assertRaises(ValueError):
            ei.run(self.project, "../escape", apply=True)
