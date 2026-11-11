"""DFGL-06A: records analysis-export (synthetic fixtures only; no live project)."""
import contextlib
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

from campaign_tool.records import analysis_export as ae
from campaign_tool.records import cli
from campaign_tool.records.extraction_ledger import ExtractionLedgerAdapter
from campaign_tool.records.ledger import store
from tests.records import test_outcome_factsheets as _fs

R1, R2, R3 = ("run_2026110%dT000000Z_%s" % (n, c * 8) for n, c in ((1, "a"), (2, "b"), (3, "c")))
PAGES = ["First page text", "Second page text"]
REPO = Path(__file__).parents[2]


class AnalysisExportTests(unittest.TestCase):
    private, document, enroll = (_fs.FactSheetsTests.private, _fs.FactSheetsTests.document,
                                 _fs.FactSheetsTests.enroll)

    def setUp(self):
        workspaces = Path(tempfile.gettempdir()) / "analysis-export" / "test-workspaces"
        workspaces.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="export-", dir=workspaces)
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project, self.dest = root / "project", root / "dest"
        self.project.mkdir(mode=0o700)
        self.dest.mkdir(mode=0o700)
        self.processing = self.project / "processing-v1"
        self.processing.mkdir(mode=0o700)
        self.database = self.processing / "ledger.sqlite"
        store.initialize(self.database)
        self.evidence = self.processing / "artifacts" / "evidence"
        self.evidence.parent.mkdir(mode=0o700)
        self.adapter = ExtractionLedgerAdapter(database=self.database, evidence_root=self.evidence)
        self.serial = 0
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO runs VALUES('fixture','fixture','2041-01-01',NULL,'fixture',NULL,NULL,NULL,'fixture','{}')")
            con.execute("INSERT INTO agencies VALUES(?,?,NULL,NULL)", ("ag1", "Ag1"))
            con.commit()
        (self.project / "mail").mkdir()
        (self.project / "mail" / "msg.eml").write_text("MAIL CANARY")
        (self.project / "approvals").mkdir()
        (self.project / "approvals" / "a.json").write_text("APPROVAL CANARY")

    def make_run(self, run, records=(), manifest=True, items=()):  # manifest: True (build one), False (none), str/dict (verbatim)
        """records: (sha, text_state, request_id) under agency ag1; items: received-items rows."""
        outcomes = self.project / "run-outputs" / run / "outcomes"
        outcomes.mkdir(mode=0o700, parents=True, exist_ok=True)
        if manifest is True:
            by_request = {}
            for digest, state, request in records:
                by_request.setdefault(request, []).append({"sha256": digest, "text_state": state})
            manifest = {"schema": "records-analysis-ready-v1", "run_id": run, "review_required": True,
                        "inputs": {}, "agencies": [
                            {"agency_id": "ag1", "request_id": request, "records": recs, "comparison_rows": [],
                             "gap_proposal_ids": [], "law_package": {"id": "us-ca", "status": "draft", "revision": None}}
                            for request, recs in by_request.items()]}
        if manifest:
            (outcomes / "analysis-ready.json").write_text(manifest if isinstance(manifest, str) else json.dumps(manifest))
        (outcomes / "received-items.json").write_text(json.dumps({"run_id": run, "items": list(items)}))

    def item(self, digest, request, at, source):
        return {"original_sha256": digest, "agency_id": "ag1", "request_id": request, "received_at": at,
                "received_at_source": source}

    def export(self, *args):
        """-> (code, stdout bytes, stderr text)."""
        out, err = io.TextIOWrapper(io.BytesIO()), io.StringIO()
        with mock.patch.object(sys, "stdout", out), contextlib.redirect_stderr(err):
            code = ae.main(["--project", str(self.project), *args])
        out.flush()
        return code, out.buffer.getvalue(), err.getvalue()

    def export_dir(self, *args):
        target = Path(tempfile.mkdtemp(dir=self.dest))
        code, _, err = self.export(*args, "--out", str(target))
        self.assertEqual(code, 0, err)
        return target

    def runs(self, *args):
        return sorted(p.name for p in self.export_dir(*args).iterdir())

    def tree(self):
        found = {}
        for path in sorted(self.project.rglob("*")):
            info = path.lstat()
            found[str(path.relative_to(self.project))] = (
                info.st_mode, info.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
        return found

    def rows(self, folder):
        with open(folder / "TEXTS.csv", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_ready_record_exports_text_dates_and_hashes(self):
        digest, _ = self.document(PAGES, "ag1")
        self.make_run(R1, [(digest, "text_reviewed", "rq1")],
                      items=[self.item(digest, "rq1", "2026-11-02T18:00:00Z", "mail_received_at")])
        folder = self.export_dir("--after", "-") / R1
        text = (folder / "texts" / (digest + ".txt")).read_bytes()
        self.assertEqual(text.decode(), "[[PAGE 1]]\nFirst page text\n[[PAGE 2]]\nSecond page text\n")
        self.assertEqual(oct((folder / "texts" / (digest + ".txt")).stat().st_mode & 0o777), "0o600")
        self.assertEqual(self.rows(folder), [{
            "original_sha256": digest, "agency_id": "ag1", "request_id": "rq1", "pages": "2",
            "text_sha256": hashlib.sha256(text).hexdigest(), "state": "exported",
            "received_at": "2026-11-02T18:00:00Z", "received_at_source": "mail_received_at"}])
        export = json.loads((folder / "EXPORT.json").read_text())
        self.assertEqual((export["schema"], export["run_id"], export["analysis_ready"]),
                         ("records-analysis-export-v1", R1, "present"))
        names = sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file() and p.name != "EXPORT.json")
        schema = json.loads((REPO / "schemas" / "records-analysis-export-v1.json").read_text())
        self.assertEqual((set(export), schema["title"]), (set(schema["required"]), export["schema"]))
        self.assertEqual(sorted(export["files"]), names)
        for name, digest_of in export["files"].items():
            self.assertEqual(hashlib.sha256((folder / name).read_bytes()).hexdigest(), digest_of, name)
        self.assertEqual((folder / "analysis-ready.json").read_bytes(),
                         (self.project / "run-outputs" / R1 / "outcomes" / "analysis-ready.json").read_bytes())

    def test_not_ready_unavailable_and_null_dates_give_codes_and_no_text(self):
        scan, _ = self.document(PAGES, "ag1")
        broken = "d" * 64  # in the manifest but with no current extraction in the ledger
        nodate, _ = self.document(PAGES, "ag1")
        self.make_run(R1, [(scan, "ocr_unreviewed", "rq1"), (broken, "text_reviewed", "rq1"),
                           (nodate, "text_reviewed", None)],
                      items=[self.item(nodate, None, None, None), self.item(scan, "rq1", "2026-11-02T18:00:00Z", "mail_received_at")])
        folder = self.export_dir("--run", R1) / R1
        rows = {r["original_sha256"]: r for r in self.rows(folder)}
        self.assertEqual(rows[scan]["state"], "TEXT_NOT_READY")
        self.assertEqual((rows[scan]["pages"], rows[scan]["text_sha256"]), ("", ""))
        self.assertEqual(rows[scan]["received_at"], "2026-11-02T18:00:00Z")
        self.assertEqual(rows[broken]["state"], "EXTRACTION_UNAVAILABLE")
        self.assertEqual((rows[broken]["received_at"], rows[broken]["received_at_source"]), ("", ""))
        self.assertEqual((rows[nodate]["state"], rows[nodate]["received_at"], rows[nodate]["received_at_source"]),
                         ("exported", "", ""))
        self.assertEqual(sorted(p.name for p in (folder / "texts").iterdir()), [nodate + ".txt"])

    def test_after_selection_settled_paging_and_missing_manifest_marker(self):
        digest, _ = self.document(PAGES, "ag1")
        self.make_run(R1, [(digest, "text_reviewed", "rq1")])
        (self.project / "run-outputs" / "console-snap-20261101").mkdir()
        (self.project / "run-outputs" / "run_proof_x").mkdir()
        self.make_run(R2, manifest=False)
        self.assertEqual(self.runs("--after", "-"), [R1])  # R2 is the newest and has no manifest yet
        self.make_run(R3, manifest=False)
        marker = self.export_dir("--after", "-")
        self.assertEqual(sorted(p.name for p in marker.iterdir()), [R1, R2])  # R2 settled by the later folder
        self.assertEqual(sorted(p.name for p in (marker / R2).iterdir()), ["EXPORT.json"])
        self.assertEqual(json.loads((marker / R2 / "EXPORT.json").read_text())["analysis_ready"], "missing")
        self.make_run(R3, [(digest, "text_reviewed", "rq1")])
        self.assertEqual(self.runs("--after", "-"), [R1, R2, R3])
        self.assertEqual(self.runs("--after", R1), [R2, R3])
        self.assertEqual(self.runs("--after", R1, "--max-runs", "1"), [R2])
        self.assertEqual(self.runs("--after", R3), [])

    def test_unparseable_or_foreign_manifest_is_a_marker_not_a_skip(self):
        self.make_run(R1, manifest="{not json")
        self.make_run(R2, manifest={"schema": "records-analysis-ready-v1", "run_id": R1, "agencies": []})
        self.make_run(R3, manifest={"schema": "records-analysis-ready-v1", "run_id": R3, "agencies": []})
        target = self.export_dir("--after", "-")
        for run in (R1, R2):
            self.assertEqual(json.loads((target / run / "EXPORT.json").read_text())["analysis_ready"], "missing", run)
        self.assertEqual(json.loads((target / R3 / "EXPORT.json").read_text())["analysis_ready"], "present")

    def test_stdout_tar_equals_out_and_exports_are_deterministic(self):
        digest, _ = self.document(PAGES, "ag1")
        self.make_run(R1, [(digest, "text_reviewed", "rq1")], items=[self.item(digest, "rq1", "2026-11-02T18:00:00Z", "mail_received_at")])
        self.make_run(R2, manifest=False)
        self.make_run(R3, [])
        code, first, err = self.export("--after", "-", "--stdout")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.export("--after", "-", "--stdout")[1], first)
        target = self.export_dir("--after", "-")
        with tarfile.open(fileobj=io.BytesIO(first)) as tar:
            members = tar.getmembers()
            self.assertTrue(all(m.isreg() and (m.mtime, m.uid, m.gid) == (0, 0, 0) for m in members))
            self.assertEqual([m.name for m in members], sorted(m.name for m in members))
            in_tar = {m.name: tar.extractfile(m).read() for m in members}
        on_disk = {str(p.relative_to(target)): p.read_bytes() for p in target.rglob("*") if p.is_file()}
        self.assertEqual(in_tar, on_disk)
        self.assertIn("%s/texts/%s.txt" % (R1, digest), in_tar)
        self.assertEqual(self.export_dir("--after", "-").joinpath(R1, "EXPORT.json").read_bytes(), on_disk[R1 + "/EXPORT.json"])

    def test_project_unchanged_and_no_originals_mail_or_approvals_copied(self):
        digest, source = self.document(PAGES, "ag1")
        self.make_run(R1, [(digest, "text_reviewed", "rq1")])
        self.make_run(R2, [])
        before = self.tree()
        target = self.export_dir("--after", "-")
        code, tar_bytes, _ = self.export("--after", "-", "--stdout")
        self.assertEqual(code, 0)
        self.assertEqual(self.tree(), before)
        everything = tar_bytes + b"".join(p.read_bytes() for p in target.rglob("*") if p.is_file())
        names = "\n".join(str(p.relative_to(target)) for p in target.rglob("*"))
        for forbidden in (b"MAIL CANARY", b"APPROVAL CANARY", b"SYNTHETIC RECEIPT FIXTURE"):
            self.assertNotIn(forbidden, everything)
        for forbidden in ("mail", "approvals", "original-", "blob"):
            self.assertNotIn(forbidden, names)
        self.assertNotIn(source.read_bytes(), everything)

    def test_no_record_text_on_stderr(self):
        digest, _ = self.document(PAGES, "ag1")
        self.make_run(R1, [(digest, "text_reviewed", "rq1")])
        self.make_run(R2, [])
        code, _, err = self.export("--after", "-", "--stdout")
        self.assertEqual(code, 0)
        self.assertIn("EXPORT_OK runs=2", err)
        for text in PAGES + [digest[:16]]:
            self.assertNotIn(text, err)
        code, _, err = self.export("--run", R3, "--stdout")
        self.assertEqual((code, err.strip()), (2, "EXPORT_RUN_MISSING"))

    def test_refusals_leave_no_output(self):
        digest, _ = self.document(PAGES, "ag1")
        self.make_run(R1, [(digest, "text_reviewed", "rq1")])
        code, out, err = self.export("--run", R1, "--out", str(self.project / "inside"))
        self.assertEqual((code, err.strip()), (2, "EXPORT_OUT_IN_PROJECT"))
        self.assertFalse((self.project / "inside").exists())
        code, _, err = self.export("--run", R1, "--out", str(self.dest / "existing"))
        self.assertEqual(code, 0, err)
        (self.dest / "existing" / R1 / "TEXTS.csv").write_text("tampered")
        (self.dest / "existing" / R1 / "EXPORT.json").write_text("tampered")
        code, _, err = self.export("--run", R1, "--out", str(self.dest / "existing"))
        self.assertEqual((code, err.strip()), (2, "EXPORT_OUT_EXISTS"))
        (self.processing / "ledger.sqlite-wal").write_bytes(b"pending writer")  # a writer is active
        code, out, err = self.export("--run", R1, "--stdout")
        self.assertEqual((code, out, err.strip()), (75, b"", "EXPORT_LEDGER_BUSY"))
        (self.processing / "ledger.sqlite-wal").unlink()
        self.make_run(R2, manifest=False)
        code, out, err = self.export("--run", R2, "--stdout")
        self.assertEqual((code, out, err.strip()), (3, b"", "EXPORT_RUN_UNSETTLED"))
        self.database.unlink()
        code, out, err = self.export("--run", R1, "--stdout")
        self.assertEqual((code, out, err.strip()), (2, b"", "EXPORT_NO_LEDGER"))

    def test_existing_folder_must_match_every_member_not_only_export_json(self):
        digest, _ = self.document(PAGES, "ag1")
        self.make_run(R1, [(digest, "text_reviewed", "rq1")])
        out = self.dest / "again"
        self.assertEqual(self.export("--run", R1, "--out", str(out))[0], 0)
        self.assertEqual(self.export("--run", R1, "--out", str(out))[0], 0)  # identical: left alone
        text, folder = out / R1 / "texts" / ("%s.txt" % digest), out / R1
        original = text.read_bytes()

        def refused():
            code, _, err = self.export("--run", R1, "--out", str(out))
            self.assertEqual((code, err.strip()), (2, "EXPORT_OUT_EXISTS"))

        text.write_bytes(b"replaced text")
        refused()
        text.write_bytes(original)
        os.chmod(text, 0o644)
        refused()
        os.chmod(text, 0o600)
        (folder / "extra.txt").write_text("extra")
        refused()
        (folder / "extra.txt").unlink()
        text.unlink()
        text.symlink_to(self.dest / "elsewhere")
        refused()
        text.unlink()
        text.write_bytes(original)
        self.assertEqual(self.export("--run", R1, "--out", str(out))[0], 0)

    def test_out_symlink_swapped_in_during_build_is_refused_and_writes_nothing_in_project(self):
        digest, _ = self.document(PAGES, "ag1")
        self.make_run(R1, [(digest, "text_reviewed", "rq1")])
        out = self.dest / "later"
        real_build = ae._build

        def swapping(args):
            built = real_build(args)
            out.symlink_to(self.project)  # planted after the up-front check, before any write
            return built

        before = self.tree()
        with mock.patch.object(ae, "_build", swapping):
            code, _, err = self.export("--run", R1, "--out", str(out))
        self.assertEqual((code, err.strip()), (2, "EXPORT_UNSAFE_PATH"))
        self.assertEqual(self.tree(), before)
        out.unlink()
        out.symlink_to(self.dest)  # a swap to another directory outside the project is refused too
        code, _, err = self.export("--run", R1, "--out", str(out))
        self.assertEqual((code, err.strip()), (2, "EXPORT_UNSAFE_PATH"))
        self.assertEqual(list(self.dest.glob("run_*")), [])

    def test_out_that_resolves_into_the_project_after_the_check_is_refused(self):
        digest, _ = self.document(PAGES, "ag1")
        self.make_run(R1, [(digest, "text_reviewed", "rq1")])
        link = self.dest / "parent-link"
        real_build = ae._build

        def swapping(args):
            built = real_build(args)
            link.symlink_to(self.project)  # an ancestor of --out now leads into the project
            return built

        before = self.tree()
        with mock.patch.object(ae, "_build", swapping):
            code, _, err = self.export("--run", R1, "--out", str(link / "sub"))
        self.assertEqual((code, err.strip()), (2, "EXPORT_OUT_IN_PROJECT"))
        self.assertEqual(self.tree(), before)  # not even an empty folder is made in the project

    def test_symlinked_run_folder_is_refused_not_followed(self):
        self.make_run(R1, [])
        outside = Path(self.temp.name) / "outside"
        (outside / "outcomes").mkdir(parents=True)
        (self.project / "run-outputs" / R2).symlink_to(outside)
        code, out, err = self.export("--after", "-", "--stdout")
        self.assertEqual((code, out, err.strip()), (2, b"", "EXPORT_UNSAFE_PATH"))

    def test_registered_command_runs_through_the_module_entry_point(self):
        self.assertEqual(cli.COMMANDS["analysis-export"], ("analysis_export", "main"))
        digest, _ = self.document(PAGES, "ag1")
        self.make_run(R1, [(digest, "text_reviewed", "rq1")])
        done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "analysis-export", "--project",
                               str(self.project), "--run", R1, "--stdout"], cwd=REPO, capture_output=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        with tarfile.open(fileobj=io.BytesIO(done.stdout)) as tar:
            self.assertIn("%s/texts/%s.txt" % (R1, digest), tar.getnames())


del _fs


if __name__ == "__main__":
    unittest.main()
