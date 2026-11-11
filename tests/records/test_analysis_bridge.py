"""DFGL-06: records analysis-sync receives 06A exports and replays every pending run x agency pair (synthetic fixtures)."""
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

from campaign_tool.records import cli
from campaign_tool.records.extraction_ledger import ExtractionLedgerAdapter
from campaign_tool.records.ledger import store
from tests.flock import test_review
from tests.records import test_outcome_factsheets as _fs

R1, R2, R3 = ("run_2026110%dT000000Z_%s" % (n, c * 8) for n, c in ((1, "a"), (2, "b"), (3, "c")))
AGX, AGY = "agency-x", "agency-y"
SLUG = {AGX: "agency-x-sheriff", AGY: "agency-y-police"}
PAGES = ["First page text", "Second page text"]
REPO = Path(__file__).parents[2]
PROFILE = REPO / "tests" / "fixtures" / "records" / "analysis-profile.json"


def digest_tree(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(path).rglob("*")) if p.is_file()}


class AnalysisBridgeTests(unittest.TestCase):
    private, document, enroll = (_fs.FactSheetsTests.private, _fs.FactSheetsTests.document, _fs.FactSheetsTests.enroll)

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="bridge-")
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.project, self.root, self.method = self.tmp / "project", self.tmp / "root", self.tmp / "method"
        for name in " ".join(test_review.SCRIPTS.values()).split():
            test_review.write_stub(self.method, name)
        (self.project / "processing-v1").mkdir(parents=True, mode=0o700)
        self.database = self.project / "processing-v1" / "ledger.sqlite"
        store.initialize(self.database)
        (self.project / "processing-v1" / "artifacts").mkdir(mode=0o700)
        self.adapter = ExtractionLedgerAdapter(database=self.database, evidence_root=self.project / "processing-v1" / "artifacts" / "evidence")
        self.serial = 0
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO runs VALUES('fixture','fixture','2041-01-01',NULL,'fixture',NULL,NULL,NULL,'fixture','{}')")
            for agency in (AGX, AGY):
                con.execute("INSERT INTO agencies VALUES(?,?,NULL,NULL)", (agency, agency))
            con.commit()

    def make_run(self, run, records=(), manifest=True, items=()):
        """records: (agency, sha, text_state, request_id); items: received-items rows."""
        outcomes = self.project / "run-outputs" / run / "outcomes"
        outcomes.mkdir(mode=0o700, parents=True, exist_ok=True)
        if manifest:
            groups = {}
            for agency, digest, state, request in records:
                groups.setdefault((agency, request), []).append({"sha256": digest, "text_state": state})
            ready = {"schema": "records-analysis-ready-v1", "run_id": run, "review_required": True, "inputs": {}, "agencies": [
                {"agency_id": a, "request_id": q, "records": recs, "comparison_rows": [], "gap_proposal_ids": [],
                 "law_package": {"id": "us-ca", "status": "draft", "revision": None}} for (a, q), recs in groups.items()]}
            (outcomes / "analysis-ready.json").write_text(json.dumps(ready))
        (outcomes / "received-items.json").write_text(json.dumps({"run_id": run, "items": [
            {"original_sha256": d, "agency_id": a, "request_id": q, "received_at": at, "received_at_source": "mail_received_at"}
            for a, d, q, at in items]}))

    def record(self, run, agency, request="rq1", at="2026-11-02T18:00:00Z", digest=None):
        digest = digest or self.document(PAGES, agency)[0]
        self.make_run(run, [(agency, digest, "text_reviewed", request)], items=[(agency, digest, request, at)])
        return digest

    def profile(self, agency, **review):
        profile = json.loads(PROFILE.read_text().replace("@ROOT@", str(self.root)).replace("@METHOD@", str(self.method))
                             .replace("@PY@", sys.executable))
        profile.update(records_agency_id=agency, slug=SLUG[agency], fact_agency=SLUG[agency], agency_name=SLUG[agency])
        profile["review"].update(review)
        path = self.root / "profiles" / (SLUG[agency] + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(profile))

    def cli(self, *args, command="analysis-sync", stdin=None):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", command, *args], cwd=REPO, input=stdin,
                              capture_output=True, check=False)

    def export_tar(self):
        done = self.cli("--project", str(self.project), "--after", "-", "--max-runs", "10", "--stdout", command="analysis-export")
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def sync(self, *args, stdin=None):
        done = self.cli("--root", str(self.root), *args, stdin=stdin)
        return done.returncode, done.stdout.decode(), done.stderr.decode()

    def receive(self, data=None):
        return self.sync("--receive", "--tar", "-", stdin=self.export_tar() if data is None else data)

    def ledger(self, kind="sync"):
        path = self.root / ".analysis" / "analysis-ledger.jsonl"
        lines = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [e for e in lines if e["kind"] == kind]

    def outcomes(self, run, agency):
        return [e["outcome"] for e in self.ledger() if (e["run_id"], e["agency_id"]) == (run, agency)]

    def inputs(self, agency):
        return self.root / "agencies" / SLUG[agency] / "review" / "inputs"

    def csv_rows(self, path):
        lines = path.read_text().splitlines()
        return [dict(zip(lines[0].split(","), line.split(","))) for line in lines[1:]]

    def flock_check(self, agency):
        code, out = test_review.tool("--check", self.root / "agencies" / SLUG[agency] / "review-config.json")
        self.assertEqual(code, 0, out)
        self.assertIn("errors=0", out)

    def test_receive_and_sync_one_run_places_text_dates_and_config(self):
        self.assertIn("analysis-sync", cli.COMMANDS)
        digest = self.record(R1, AGY)
        self.profile(AGY)
        code, out, err = self.receive()
        self.assertEqual((code, out.strip()), (0, "RECEIVE %s RECEIVED" % R1), err)
        self.assertEqual(self.sync("--last-received")[1].strip(), R1)
        code, out, err = self.sync()
        self.assertEqual(code, 0, err)
        self.assertIn("SYNC %s %s SYNCED new_texts=1" % (R1, AGY), out)
        self.assertNotIn(PAGES[0], out)
        text = self.inputs(AGY) / "records" / (digest + ".txt")
        self.assertEqual(text.read_text(), "[[PAGE 1]]\nFirst page text\n[[PAGE 2]]\nSecond page text\n")
        self.assertEqual(text.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.csv_rows(self.inputs(AGY) / "records" / "MANIFEST.csv"), [{"file": digest + ".txt", "orig_sha256": digest}])
        (source,) = self.csv_rows(self.inputs(AGY) / "SOURCES.csv")
        self.assertEqual((source["source_id"], source["original_sha256"], source["run_id"], source["received_at"], source["received_at_source"]),
                         ("REC-" + digest[:12], digest, R1, "2026-11-02T18:00:00Z", "mail_received_at"))
        self.assertEqual(source["text_sha256"], hashlib.sha256(text.read_bytes()).hexdigest())
        self.flock_check(AGY)
        self.assertEqual([e["outcome"] for e in self.ledger("receive")], ["RECEIVED"])
        self.assertEqual(list(self.root.rglob("FACTS*")), [])

    def replay_scenario(self, code, break_x, repair_x):
        dx, dy = self.record(R1, AGX, "rqx"), self.record(R2, AGY, "rqy")
        self.profile(AGY)
        break_x(dx)
        self.assertEqual(self.receive()[0], 0)
        self.assertEqual(self.sync("--last-received")[1].strip(), R2)
        rc, out, _ = self.sync()
        self.assertEqual(rc, 1)
        self.assertIn("SYNC %s %s %s" % (R1, AGX, code), out)
        self.assertIn("SYNC %s %s SYNCED" % (R2, AGY), out)
        tree_y, lines = digest_tree(self.root / "agencies" / SLUG[AGY]), self.ledger()
        rc, out, _ = self.sync()  # a second call retries A/X but logs nothing new and never touches B/Y
        self.assertEqual((rc, self.ledger()), (1, lines))
        self.assertNotIn(R2, out)
        self.assertEqual(self.sync("--pending")[1].split(), [R1, AGX, code])
        repair_x(dx)
        rc, out, err = self.sync()
        self.assertEqual(rc, 0, err)
        self.assertIn("SYNC %s %s SYNCED" % (R1, AGX), out)
        self.assertNotIn(R2, out)
        self.assertEqual((self.outcomes(R1, AGX)[-1], self.outcomes(R2, AGY)), ("SYNCED", ["SYNCED"]))
        self.assertEqual(len(self.csv_rows(self.inputs(AGY) / "SOURCES.csv")), 1)
        self.assertEqual(digest_tree(self.root / "agencies" / SLUG[AGY]), tree_y)
        self.assertEqual(self.sync("--pending")[1], "")
        self.flock_check(AGX)

    def test_failed_run_is_replayed_when_the_profile_is_added(self):
        self.replay_scenario("PROFILE_MISSING", lambda d: None, lambda d: self.profile(AGX))

    def test_failed_run_is_replayed_when_a_text_conflict_is_removed(self):
        def conflict(digest):
            self.profile(AGX)
            (self.inputs(AGX) / "records").mkdir(parents=True)
            (self.inputs(AGX) / "records" / (digest + ".txt")).write_text("different bytes\n")
        self.replay_scenario("TEXT_CONFLICT", conflict, lambda d: (self.inputs(AGX) / "records" / (d + ".txt")).unlink())
        self.assertEqual(self.outcomes(R1, AGX), ["TEXT_CONFLICT", "SYNCED"])

    def test_failed_run_is_replayed_when_the_config_becomes_valid(self):
        self.replay_scenario("CONFIG_INVALID", lambda d: self.profile(AGX, method_dir=str(self.tmp / "no-method")), lambda d: self.profile(AGX))

    def test_zero_agency_and_missing_manifest_runs_are_final(self):
        self.make_run(R1)
        self.make_run(R2, manifest=False)
        self.record(R3, AGY)
        self.profile(AGY)
        self.receive()
        rc, out, err = self.sync()
        self.assertEqual(rc, 0, err)
        self.assertEqual([self.outcomes(R1, ""), self.outcomes(R2, ""), self.outcomes(R3, AGY)],
                         [["NO_AGENCIES"], ["ANALYSIS_READY_MISSING"], ["SYNCED"]])
        lines = self.ledger()
        self.assertEqual((self.sync()[0], self.sync("--pending")[1], self.ledger()), (0, "", lines))

    def test_tampered_middle_folder_stops_receipt_and_conflicts_are_refused(self):
        for run in (R1, R2, R3):
            self.record(run, AGY)
        good, tampered = self.export_tar(), io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(good)) as source, tarfile.open(fileobj=tampered, mode="w", format=tarfile.USTAR_FORMAT) as out:
            for member in source:
                data = (source.extractfile(member) or io.BytesIO()).read()
                if member.name.startswith(R2 + "/texts/"):
                    data, member.size = b"tampered\n", 9
                out.addfile(member, io.BytesIO(data))
        rc, out, _ = self.receive(tampered.getvalue())
        self.assertEqual(rc, 1)
        self.assertEqual(out.split(), ["RECEIVE", R1, "RECEIVED", "RECEIVE", R2, "EXPORT_INVALID"])
        self.assertEqual(self.sync("--last-received")[1].strip(), R1)
        self.assertFalse((self.root / ".analysis" / "runs" / R3).exists())
        folders = self.tmp / "folders"
        with tarfile.open(fileobj=io.BytesIO(good)) as source:
            source.extractall(folders, filter="data")
        shutil.rmtree(folders / R2)
        shutil.rmtree(folders / R3)
        self.assertEqual(self.sync("--receive", str(folders))[1].split(), ["RECEIVE", R1, "RECEIVED_UNCHANGED"])
        retained = digest_tree(self.root / ".analysis" / "runs" / R1)
        texts = folders / R1 / "TEXTS.csv"
        texts.write_text(texts.read_text().replace("2026-11-02", "2026-11-03"))
        export = json.loads((folders / R1 / "EXPORT.json").read_text())
        export["files"]["TEXTS.csv"] = hashlib.sha256(texts.read_bytes()).hexdigest()
        (folders / R1 / "EXPORT.json").write_text(json.dumps(export))
        rc, out, _ = self.sync("--receive", str(folders))
        self.assertEqual((rc, out.split()), (1, ["RECEIVE", R1, "EXPORT_CONFLICT"]))
        self.assertEqual(digest_tree(self.root / ".analysis" / "runs" / R1), retained)

    def test_an_exported_row_without_its_text_file_is_refused_even_when_it_claims_the_empty_hash(self):
        self.record(R1, AGY)
        self.profile(AGY)
        folders = self.tmp / "folders"
        with tarfile.open(fileobj=io.BytesIO(self.export_tar())) as source:
            source.extractall(folders, filter="data")
        (text,) = (folders / R1 / "texts").glob("*.txt")
        texts, export = folders / R1 / "TEXTS.csv", folders / R1 / "EXPORT.json"
        texts.write_text(texts.read_text().replace(hashlib.sha256(text.read_bytes()).hexdigest(), hashlib.sha256(b"").hexdigest()))
        text.unlink()
        listing = json.loads(export.read_text())
        listing["files"] = {n: d for n, d in listing["files"].items() if not n.startswith("texts/")}
        listing["files"]["TEXTS.csv"] = hashlib.sha256(texts.read_bytes()).hexdigest()
        export.write_text(json.dumps(listing))
        rc, out, err = self.sync("--receive", str(folders))
        self.assertEqual((rc, out.split()), (1, ["RECEIVE", R1, "EXPORT_INVALID"]), err)
        self.assertFalse((self.root / ".analysis" / "runs" / R1).exists())
        self.assertEqual(self.sync()[0], 0)
        self.assertEqual(self.receive()[1].split(), ["RECEIVE", R1, "RECEIVED"])
        self.sync()
        self.assertEqual(self.outcomes(R1, AGY), ["SYNCED"])

    def test_the_same_original_in_two_runs_is_placed_once_with_a_source_row_per_run(self):
        digest = self.record(R1, AGY)
        self.record(R2, AGY, "rq2", "2026-11-05T09:00:00Z", digest=digest)
        self.profile(AGY)
        self.receive()
        self.assertEqual(self.sync()[0], 0)
        records = self.inputs(AGY) / "records"
        self.assertEqual(sorted(p.name for p in records.iterdir()), [digest + ".txt", "MANIFEST.csv"])
        self.assertEqual(len(self.csv_rows(records / "MANIFEST.csv")), 1)
        self.assertEqual([(r["run_id"], r["received_at"]) for r in self.csv_rows(self.inputs(AGY) / "SOURCES.csv")],
                         [(R1, "2026-11-02T18:00:00Z"), (R2, "2026-11-05T09:00:00Z")])

    def test_dry_run_writes_nothing_and_the_run_store_is_never_modified(self):
        self.record(R1, AGY)
        self.profile(AGY)
        self.receive()
        before, runs = digest_tree(self.root), digest_tree(self.root / ".analysis" / "runs")
        rc, out, _ = self.sync("--dry-run")
        self.assertEqual(rc, 1)
        self.assertIn("WOULD_SYNC new_texts=1", out)
        self.assertNotIn(PAGES[0], out)
        self.assertEqual(digest_tree(self.root), before)
        self.assertEqual(self.sync()[0], 0)
        self.assertEqual(digest_tree(self.root / ".analysis" / "runs"), runs)
        self.assertEqual(list(self.root.rglob("FACTS*")), [])
        self.assertEqual(self.sync("--root", str(REPO / "tests"))[1].strip(), "ROOT_REFUSED")


if __name__ == "__main__":
    unittest.main()
