"""Synthetic-only DFGL-05 analysis-ready manifest and its outputs-stage wiring."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing
from unittest import mock

from campaign_tool.records import run_stages
from campaign_tool.records.ledger.migrations import v001
from campaign_tool.records.outcomes import analysis_ready as ar

FIXTURE = Path(__file__).parent / "fixtures" / "outcome_register" / "tracker.csv"
CANARY = "Secret Synthetic Document Text"


def h(n):
    return f"{n:064x}"


class AnalysisReadyTests(unittest.TestCase):
    def setUp(self):
        work = Path(tempfile.gettempdir()) / "outcome-analysis-ready" / "test-workspaces"
        work.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="ar-", dir=work)
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        processing = self.project / "processing-v1"
        (processing / "runs").mkdir(mode=0o700, parents=True)
        with closing(sqlite3.connect(processing / "ledger.sqlite")) as con, con:
            con.executescript(v001.SQL)
        (self.project / "MASTER-REQUEST-TRACKER.csv").write_bytes(FIXTURE.read_bytes())
        (self.project / "portal-queue-v1").mkdir()
        with closing(sqlite3.connect(self.project / "portal-queue-v1" / "portal.sqlite")) as con, con:
            con.execute("CREATE TABLE portal_items(id TEXT)")

    def outcomes(self, run):
        directory = self.project / "run-outputs" / run / "outcomes"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return directory

    def put(self, path, data):
        path.write_text(json.dumps(data), encoding="utf-8")

    def item(self, n, kind="records", agency="ag1", request="rq1"):
        return {"original_sha256": h(n), "kind": kind, "agency_id": agency, "request_id": request,
                "quote": CANARY, "findings": [{"finding_id": h(100 + n), "rating": "gap", "quote": CANARY}]}

    def fixture_run(self, run, items):
        directory = self.outcomes(run)
        self.put(directory / "received-items.json", {"schema": "records-received-items-v1", "run_id": run, "items": items})
        self.put(directory / "gap-proposals.json", {"run_id": run, "agencies": [
            {"agency_id": "ag1", "requests": [{"request_id": "rq1", "proposals": [{"proposal_id": h(200), "quote": CANARY}]}]}]})
        self.put(directory.parent / "text-state.json", {"run_id": run, "records": [
            {"sha256": h(1), "state": "text_reviewed", "words": CANARY}]})
        return directory

    def stage(self, run):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run_stages.main(["outputs", "--project", str(self.project), "--run-id", run])
        return code, json.loads(out.getvalue())

    def test_records_item_gives_one_entry_with_hashes_ids_and_draft_package(self):
        directory = self.fixture_run("r1", [self.item(1), self.item(2, kind="denial")])
        report = ar.write_run_analysis_ready(self.project, "r1")
        raw = (directory / "analysis-ready.json").read_bytes()
        self.assertEqual(json.loads(raw), report)
        self.assertEqual(report["schema"], "records-analysis-ready-v1")
        self.assertEqual(len(report["agencies"]), 1)
        entry = report["agencies"][0]
        self.assertEqual((entry["agency_id"], entry["request_id"]), ("ag1", "rq1"))
        self.assertEqual(entry["records"], [{"sha256": h(1), "text_state": "text_reviewed"}])
        self.assertEqual(entry["comparison_rows"], [{"finding_id": h(101), "rating": "gap"}])
        self.assertEqual(entry["gap_proposal_ids"], [h(200)])
        self.assertEqual(entry["law_package"]["status"], "draft")
        self.assertEqual(set(report["inputs"]), {"received-items.json", "gap-proposals.json", "text-state.json"})
        self.assertEqual(stat.S_IMODE((directory / "analysis-ready.json").stat().st_mode), 0o600)
        self.assertNotIn(CANARY.encode(), raw)

    def test_no_records_item_writes_empty_agencies(self):
        directory = self.fixture_run("r1", [self.item(2, kind="denial")])
        self.assertEqual(ar.write_run_analysis_ready(self.project, "r1")["agencies"], [])
        self.assertEqual(json.loads((directory / "analysis-ready.json").read_text())["agencies"], [])

    def test_rerun_is_byte_identical_and_stale_input_refused(self):
        directory = self.fixture_run("r1", [self.item(1)])
        ar.write_run_analysis_ready(self.project, "r1")
        first = (directory / "analysis-ready.json").read_bytes()
        ar.write_run_analysis_ready(self.project, "r1")
        self.assertEqual((directory / "analysis-ready.json").read_bytes(), first)
        self.put(directory / "received-items.json", {"run_id": "other", "items": []})
        with self.assertRaises(OSError):
            ar.write_run_analysis_ready(self.project, "r1")

    def test_writer_failure_warns_and_keeps_earlier_outputs(self):
        processing = self.project / "processing-v1" / "runs"
        (processing / "extract-r1.json").write_text(json.dumps({
            "schema": "records-extract-outcomes-v1", "run_id": "r1", "documents": [
                {"original_sha256": h(1), "form": "pdf", "outcome": "EXTRACTED"}]}))
        with mock.patch.object(ar, "write_run_analysis_ready", side_effect=RuntimeError(CANARY)):
            code, line = self.stage("r1")
        self.assertEqual((code, line["status"], line["error_code"]), (0, "warn", "OUTPUTS_ANALYSIS_READY_UNAVAILABLE"))
        self.assertNotIn(CANARY, json.dumps(line))
        directory = self.project / "run-outputs" / "r1"
        for name in ("comparison.json", "text-state.json", "outcomes/received-items.json", "outcomes/gap-proposals.json"):
            self.assertTrue((directory / name).is_file(), name)
        self.assertTrue((self.project / "run-outputs" / "r1.json").is_file())
        self.assertFalse((directory / "outcomes" / "analysis-ready.json").exists())

    def test_stage_writes_manifest_after_earlier_outputs(self):
        (self.project / "processing-v1" / "runs" / "extract-r2.json").write_text(json.dumps({
            "schema": "records-extract-outcomes-v1", "run_id": "r2", "documents": []}))
        code, line = self.stage("r2")
        self.assertEqual((code, line["status"], line["error_code"]), (0, "ok", None))
        report = json.loads((self.project / "run-outputs" / "r2" / "outcomes" / "analysis-ready.json").read_text())
        self.assertEqual(report["agencies"], [])

    def test_symlinked_run_outputs_or_run_directory_or_outcomes_is_refused(self):
        outside = Path(self.temp.name + "-outside")
        outside.mkdir(mode=0o700)
        self.addCleanup(lambda: shutil.rmtree(outside, ignore_errors=True))
        for kind in ("run-outputs", "run", "outcomes"):
            with self.subTest(kind=kind):
                project = Path(tempfile.mkdtemp(prefix=f"ar-{kind}-", dir=self.temp.name))
                target = outside / kind
                target.mkdir(mode=0o700, parents=True)
                (target / "text-state.json").write_text(json.dumps({"run_id": "r1", "records": []}))
                if kind == "run-outputs":
                    (project / "run-outputs").symlink_to(target)
                else:
                    (project / "run-outputs").mkdir()
                    if kind == "run":
                        (project / "run-outputs" / "r1").symlink_to(target)
                    else:
                        (project / "run-outputs" / "r1").mkdir()
                        (project / "run-outputs" / "r1" / "outcomes").symlink_to(target)
                before = sorted(p.name for p in target.iterdir())
                with self.assertRaises(OSError):
                    ar.write_run_analysis_ready(project, "r1")
                self.assertEqual(sorted(p.name for p in target.iterdir()), before)
                self.assertEqual(sorted(p.name for p in outside.rglob("analysis-ready.json")), [])

    def test_preexisting_loose_temp_is_replaced_and_result_is_0600(self):
        directory = self.fixture_run("r1", [self.item(1)])
        temporary = directory / ".analysis-ready.json.tmp"
        temporary.write_text("stale" * 100)
        os.chmod(temporary, 0o644)
        ar.write_run_analysis_ready(self.project, "r1")
        target = directory / "analysis-ready.json"
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertNotIn(b"stale", target.read_bytes())
        self.assertFalse(temporary.exists())

    def test_symlinked_temp_is_not_followed(self):
        directory = self.fixture_run("r1", [self.item(1)])
        victim = Path(self.temp.name) / "victim.txt"
        victim.write_text("keep")
        (directory / ".analysis-ready.json.tmp").symlink_to(victim)
        ar.write_run_analysis_ready(self.project, "r1")
        self.assertEqual(victim.read_text(), "keep")
        self.assertFalse((directory / "analysis-ready.json").is_symlink())

    def test_report_matches_the_published_schema_file(self):
        schema = json.loads((Path(__file__).parents[2] / "schemas" / "records-analysis-ready-v1.json").read_text())
        self.assertEqual(schema["title"], ar.SCHEMA)
        self.fixture_run("r1", [self.item(1)])
        report = ar.write_run_analysis_ready(self.project, "r1")
        self.assertEqual(set(schema["required"]), set(report))
        self.assertEqual(set(schema["properties"]), set(report))
        entry_schema = schema["properties"]["agencies"]["items"]
        self.assertEqual(set(entry_schema["required"]), set(report["agencies"][0]))
        self.assertEqual(set(entry_schema["properties"]["records"]["items"]["required"]), set(report["agencies"][0]["records"][0]))
        self.assertEqual(set(entry_schema["properties"]["law_package"]["required"]), set(report["agencies"][0]["law_package"]))
        self.assertEqual(set(schema["properties"]["inputs"]["properties"]), set(report["inputs"]))
        self.assertEqual(schema["properties"]["schema"]["const"], report["schema"])


if __name__ == "__main__":
    unittest.main()
