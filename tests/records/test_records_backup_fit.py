"""BACKUP-FIT-1: the records backup profile fits the live layout (every live family, legacy out of scope,
referenced paths outside the set, the raised bounds). Synthetic files only; no real records."""
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from campaign_tool.records import approval_queue, e2e_proof, public_packets, records_backup
from campaign_tool.records.intake import imap_intake
from campaign_tool.records.recovery import backup as b
from campaign_tool.records.recovery import backup_v2 as v2
from campaign_tool.records.recovery import records_profile as rp
from tests.records.test_records_backup_profile import PACKET, tree_state
from tests.records.test_run_slice import synthetic_email

SHA = "ab" * 32
RUN = "run_20261101T060000Z_0a1b2c3d"
# One tiny file per family the live project holds (names collapsed in the card's survey of 2026-11-13).
LIVE_FAMILIES = (
    "processing-v1/.activation-intent-v1.json", "processing-v1/.adoption-disabled-v1.json",
    "processing-v1/.bound-trial-v1.json",
    "processing-v1/artifacts/evidence/" + "cd" * 32,
    *("processing-v1/backups/%s/%s" % (RUN, n) for n in ("intake.sqlite", "ledger.sqlite", "ledger.sqlite-shm",
                                                          "ledger.sqlite-wal", "manifest.json")),
    "processing-v1/captured-inputs/%s.sqlite" % SHA,
    "processing-v1/context-v1/%s.json" % SHA, "processing-v1/context-v1/second-reader-v1/%s.json" % SHA,
    *("processing-v1/extract/extract-k2x_9qzt/" + n for n in (
        "blobs/" + SHA, "derived/digest.json", "derived/text.txt", "derived/units.jsonl", "extraction.json", "input",
        "ocr-derived/digest.json", "ocr-derived/units.jsonl", "ocr-source/blobs/" + SHA)),
    "processing-v1/intake/document-receipts/" + "ef" * 32, "processing-v1/intake/writer.lock",
    "processing-v1/intake/intake.sqlite-wal", "processing-v1/intake/intake.sqlite-shm",
    *("processing-v1/ocr/%s/page-000001/%s" % (SHA, n) for n in (
        ".page.lock", "current.json", *(SHA + "/" + m for m in ("confidence.tsv", "manifest.json", "page.pdf",
                                                                 "page.png", "receipt.json", "sidecar.txt")))),
    "processing-v1/ocr/indexes/blocked/%s.json" % SHA, "processing-v1/ocr/indexes/visual-check/%s.json" % SHA,
    "processing-v1/run.lock", "processing-v1/runs/extract-%s.json" % RUN, "processing-v1/runs/run-%s.json" % SHA[:32],
    "portal-queue-v1/.canonical-binding-v1.json", "portal-queue-v1/.lock",
    "run-outputs/%s.json" % RUN, "run.lock", ".adoption-config-live-v1.json")
LEGACY = ("analysis-pipeline-2026-09-22/batch/notes.txt", "received/2026-10-01/scan.pdf", "research-2026-09-14/a.md",
          "RESEARCH-MEMORY.md")


def put(root, rel, raw=b"x"):
    path = Path(root) / rel
    for parent in reversed(path.relative_to(root).parents[:-1]):
        (Path(root) / parent).mkdir(mode=0o700, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o600)
    return path


def run_check(project, *extra):
    out = io.StringIO()
    with redirect_stdout(out):
        code = records_backup.backup_main(["--check-profile", "--project", str(project), *extra])
    first, *rest = out.getvalue().splitlines()
    return code, json.loads(first), rest


class BackupFit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="records-backup-fit-")
        cls.addClassCleanup(cls.tmp.cleanup)
        base = Path(cls.tmp.name)
        content = base / "campaign" / "content"
        content.mkdir(parents=True)
        (content / "agencies.json").write_text(json.dumps(
            [{"agency_id": "ca-agency", "name": "Agency", "profile": {"status": "Records requested", "summary": "s"}}]))
        message = base / "m.eml"
        message.write_bytes(synthetic_email())
        assert e2e_proof.replay(message, base / "out", base / "campaign")["result"] == "PASS"
        project = base / "out" / "replay" / "project"
        with sqlite3.connect(project / "processing-v1" / "ledger.sqlite") as con:  # what a polled mailbox adds
            con.executescript(imap_intake.CHECKPOINTS_SQL)
            con.execute("INSERT INTO mail_checkpoints VALUES('local','inbox',1,7,NULL,'2026-11-01T00:00:00+00:00')")
        con.close()
        approval_queue.add_draft(str(project), item_id="1" * 64, kind="reply-extension", agency_id="ca-agency",
                                 request_id="req-1", body="Please extend.", evidence={"findings": [], "deadlines": []},
                                 source_run_id=RUN)
        public_packets.init(str(project))
        public_packets.propose(str(project), PACKET)
        for rel in LIVE_FAMILIES:
            if not (project / rel).exists():  # sidecars stay empty: SQLite ignores them for a rollback-mode store
                put(project, rel, b"" if "sqlite-" in rel else b"{}" if rel.endswith(".json") else b"x")
        for rel in LEGACY:
            put(project, rel, b"legacy")
        cls.project = project
        cls.counter = 0

    def setUp(self):
        type(self).counter += 1
        self.work = Path(self.tmp.name) / ("w%d" % self.counter)
        self.work.mkdir(mode=0o700)

    def copy(self):
        target = self.work / "project"
        shutil.copytree(self.project, target, symlinks=True)
        target.chmod(0o700)
        return target

    def evidence(self, project):
        path = self.work / "quiescence.json"
        path.write_text(json.dumps({
            "schema": "records-quiescence-v1", "basis": "independent_all_writer_audit", "roots": [str(project)],
            "observed_utc": datetime.now(timezone.utc).isoformat(), "all_writers_stopped": True,
            "checks": {"units": {}, "open_files": {}, "run_lock": "free", "arrival_pending": False},
            "auditor": "records quiescence-audit"}))
        return str(path)

    # A1 -------------------------------------------------------------------------------------------------------
    def test_every_live_family_is_classified_and_legacy_is_counted_out_of_scope(self):
        before = tree_state(self.project)
        code, line, rest = run_check(self.project, "--explain")
        self.assertEqual(tree_state(self.project), before, "check-profile wrote something")
        self.assertEqual((code, line["code"], line["unclassified"], line["referenced_outside_set"]), (0, "OK", 0, 0))
        self.assertEqual(line["out_of_scope_legacy"], {"entries": 4, "files": 4, "bytes": 4 * len(b"legacy")})
        self.assertEqual(rest, ["out_of_scope_legacy: files 4, bytes 24", "referenced_outside_set: 0"])
        self.assertTrue({"prior_backup", "rebuildable", "process_state", "sqlite_sidecar"} <= set(line["excluded"]))
        rules = {rel: rp._rule(rel) for rel in (*LIVE_FAMILIES, "processing-v1/ledger.sqlite-wal",
                                                 "processing-v1/ledger.sqlite-shm")}
        self.assertEqual([rel for rel, rule in rules.items() if rule is None], [])
        self.assertEqual({rel for rel, rule in rules.items() if not rule[0] and rule[2] == "prior_backup"},
                         {rel for rel in LIVE_FAMILIES if "/backups/" in rel})
        self.assertTrue(all(not rules[rel][0] and rules[rel][2] == "rebuildable" for rel in LIVE_FAMILIES
                            if "/extract/extract-" in rel))
        self.assertEqual(rules["processing-v1/intake/document-receipts/" + "ef" * 32], (True, ("receipt",), ""))
        self.assertEqual(rules[".adoption-config-live-v1.json"], (True, ("configuration",), ""))

    def test_explain_prints_exactly_the_unknown_family(self):
        project = self.copy()
        for name in ("11" * 32, "22" * 32):
            put(project, "processing-v1/newstore/%s.bin" % name, b"abc")
        code, line, rest = run_check(project, "--explain")
        self.assertEqual((code, line["code"], line["unclassified"]), (1, "PROFILE_INCOMPLETE", 2))
        self.assertEqual([r for r in rest if r.startswith("unclassified: ")],
                         ["unclassified: processing-v1/newstore/<sha>.bin files 2, bytes 6"])
        self.assertEqual(run_check(project)[2], ["out_of_scope_legacy: files 4, bytes 24",
                                                 "referenced_outside_set: not_checked"])  # no --explain: counts only

    # A3 -------------------------------------------------------------------------------------------------------
    def test_referenced_path_outside_the_set_is_counted_and_refused(self):
        project = self.copy()  # the copy's ledger still names the fixture's absolute paths: those count too
        baseline = run_check(project)[1]
        self.assertEqual((baseline["code"], baseline["unclassified"]), ("REFERENCED_OUTSIDE_SET", 0))
        off = self.work / "retained" / "cas" / "49" / ("49" * 32)
        put(self.work, off.relative_to(self.work), b"o")
        legacy = put(project, "received/" + "5a" * 32, b"l")
        with closing(sqlite3.connect(project / rp.LEDGER)) as con:
            for digest, path in (("49" * 32, str(off)), ("5a" * 32, str(legacy)), ("6b" * 32, None)):
                con.execute("INSERT INTO originals(sha256,bytes,role,scope,storage_path,preservation_status,"
                            "provenance_json) VALUES(?,1,'document','records',?,'preserved','{}')", (digest, path))
            con.commit()
        code, line, rest = run_check(project, "--explain")
        # the NULL storage_path row is no reference; the off-project and the legacy-tree originals are
        self.assertEqual((code, line["code"], line["referenced_outside_set"]),
                         (1, "REFERENCED_OUTSIDE_SET", baseline["referenced_outside_set"] + 2))
        self.assertIn("referenced_outside_set: %d" % line["referenced_outside_set"], rest)
        self.assertIn("referenced_outside: original_blob <off-project>/cas/<hh>/<sha> refs 1", rest)
        self.assertIn("referenced_outside: original_blob received/<sha> refs 1", rest)
        self.assertFalse(any(str(self.work) in r for r in rest))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(records_backup.backup_main(["--project", str(project), "--into", str(self.work / "sets"),
                                                         "--quiescence", self.evidence(project)]), 1)
        self.assertEqual(json.loads(out.getvalue())["code"], "REFERENCED_OUTSIDE_SET")
        self.assertFalse((self.work / "sets").exists())

    # A4 -------------------------------------------------------------------------------------------------------
    def test_round_trip_restores_included_roles_byte_identical(self):
        project = self.project
        before = tree_state(project)
        out = io.StringIO()
        with redirect_stdout(out):
            code = records_backup.backup_main(["--project", str(project), "--into", str(self.work / "sets"),
                                               "--quiescence", self.evidence(project)])
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(tree_state(project), before, "backup changed the source tree")
        (target,) = (self.work / "sets").glob("set-*")
        digests = self.work / "sets" / records_backup.DIGESTS
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(records_backup.verify_main(["--set", str(target), "--digests", str(digests)]), 0)
        manifest_sha = json.loads(digests.read_text().splitlines()[-1])["manifest_sha256"]
        manifest = json.loads((target / "manifest.json").read_bytes())
        reasons = {row["reason"] for row in manifest["excluded"]}
        self.assertTrue({"prior_backup", "rebuildable", "out_of_scope_legacy"} <= reasons)
        self.assertEqual(sorted(Path(r["source"]).name for r in manifest["excluded"] if r["reason"] == "out_of_scope_legacy"),
                         ["RESEARCH-MEMORY.md", "analysis-pipeline-2026-09-22", "received", "research-2026-09-14"])
        restored = self.work / "restored"
        self.assertTrue(v2.restore_v2(str(target), str(restored), manifest_sha256=manifest_sha)["declared_scope_closed"])
        for row in manifest["files"]:
            raw = (restored / row["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), row["sha256"])
            if row["kind"] == "file":  # SQLite rows are transaction-consistent snapshots, files exact copies
                self.assertEqual(raw, Path(row["source"]).read_bytes(), row["source"])
        included = {row["source"] for row in manifest["files"]}
        for rel in LIVE_FAMILIES:
            rule = rp._rule(rel)
            self.assertEqual(str(project / rel) in included, rule[0], rel)


class RaisedBounds(unittest.TestCase):
    """A2: the documented bounds, and one over each refuses with today's codes."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="records-backup-bounds-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "project"
        self.root.mkdir(mode=0o700)
        for rel in (rp.LEDGER, rp.INTAKE):
            put(self.root, rel, b"")

    def test_documented_values(self):
        self.assertEqual((b.MAX_ENTRIES, rp.SCAN_LIMIT, b.MAX_TOTAL, b.MAX_FILE, b.MAX_MANIFEST, v2.OPERATION_SECONDS),
                         (20000, 80000, 8 * 1024 ** 3, 256 * 1024 ** 2, 16 * 1024 ** 2, 600))

    def check(self):
        return run_check(self.root)[1]

    def test_one_stored_entry_over_is_limit_entries(self):
        directory = self.root / "processing-v1" / "ocr"
        directory.mkdir(mode=0o700)
        for index in range(b.MAX_ENTRIES + 1 - 2):
            (directory / ("%05d.json" % index)).write_bytes(b"")
        line = self.check()
        self.assertEqual((line["code"], line["files"]), ("LIMIT_ENTRIES", b.MAX_ENTRIES + 1))

    def test_one_scanned_entry_over_is_limit_entries(self):
        directory = self.root / "processing-v1" / "ocr"
        directory.mkdir(mode=0o700)
        dirs = rp.SCAN_LIMIT + 1 - 2 - 3  # two stores, three parent directories already present
        for index in range(dirs):
            (directory / ("d%05d" % index)).mkdir(mode=0o700)
        line = self.check()
        self.assertEqual((line["code"], line["files"] + line["directories"]), ("LIMIT_ENTRIES", rp.SCAN_LIMIT + 1))
        with self.assertRaisesRegex(b.RecoveryError, "scope_entry_bound"):
            v2._inventory(self.root)

    def sparse(self, rel, size):
        with open(put(self.root, rel, b""), "r+b") as stream:
            stream.truncate(size)

    def test_one_member_byte_over_is_limit_bytes(self):
        self.sparse("processing-v1/ocr/big", b.MAX_FILE + 1)
        self.assertEqual(self.check()["code"], "LIMIT_BYTES")

    def test_one_total_byte_over_is_limit_bytes(self):
        for index in range(b.MAX_TOTAL // b.MAX_FILE - 1):
            self.sparse("processing-v1/ocr/big-%02d" % index, b.MAX_FILE)
        self.sparse("processing-v1/ocr/last", b.MAX_FILE)
        line = self.check()
        self.assertEqual((line["code"], line["included_bytes"]), ("PROFILE_INCOMPLETE", b.MAX_TOTAL))  # at the bound
        self.sparse("processing-v1/ocr/one-more", 1)
        line = self.check()
        self.assertEqual((line["code"], line["included_bytes"]), ("LIMIT_BYTES", b.MAX_TOTAL + 1))

    def test_one_manifest_byte_over_is_refused_by_verify(self):
        target = Path(self.root.parent) / "set"
        target.mkdir(mode=0o700)
        (target / "payload").mkdir(mode=0o700)
        raw = b" " * (b.MAX_MANIFEST + 1)
        put(target, "manifest.json", raw)
        with self.assertRaisesRegex(b.RecoveryError, "manifest_size_bound"):
            v2.verify_v2(str(target), manifest_sha256=hashlib.sha256(raw).hexdigest())


if __name__ == "__main__":
    unittest.main()
