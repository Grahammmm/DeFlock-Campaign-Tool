"""BACKUP-FIT-1: the records backup profile fits the live layout (every live family, legacy out of scope,
referenced paths outside the set, the raised bounds). Synthetic files only; no real records."""
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from campaign_tool.records import approval_queue, e2e_proof, public_packets, records_backup
from campaign_tool.records import quiescence_audit as qa
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
        self.assertEqual(rest, ["out_of_scope_legacy: files 4, bytes 24", "referenced_outside_set: 0",
                                "absent_roles: none", "root_owner: caller"])
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
                                                 "referenced_outside_set: not_checked",  # no --explain: counts only
                                                 "root_owner: caller"])

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
        # Q2a(4): a set in the shape written before BACKUP-FIT-1 (full role list, no absent_roles, no root_owner)
        # still verifies.
        self.assertEqual(manifest["absent_roles"], {})
        old = b.canonical({k: v for k, v in manifest.items() if k not in ("absent_roles", "root_owner")})
        aged = self.work / "aged"
        shutil.copytree(target, aged)
        (aged / "manifest.json").chmod(0o600)
        (aged / "manifest.json").write_bytes(old)
        (aged / "manifest.json").chmod(0o400)
        self.assertTrue(v2.verify_v2(str(aged), manifest_sha256=hashlib.sha256(old).hexdigest())["declared_scope_closed"])
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



def replay_project(base):
    """A project produced by the real pipeline (no mail checkpoint table)."""
    content = base / "campaign" / "content"
    content.mkdir(parents=True)
    (content / "agencies.json").write_text(json.dumps(
        [{"agency_id": "ca-agency", "name": "Agency", "profile": {"status": "Records requested", "summary": "s"}}]))
    message = base / "m.eml"
    message.write_bytes(synthetic_email())
    assert e2e_proof.replay(message, base / "out", base / "campaign")["result"] == "PASS"
    return base / "out" / "replay" / "project"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class DeclaredRootsAndAbsentRoles(unittest.TestCase):
    """A7: declared off-project roots (Q1a), conditional roles absent at source (Q2a), owner/mode only on what the
    backup reads (Q3a). The caller's home is a synthetic directory holding the reviewed EXTRA_ROOTS names."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="records-backup-roots-")
        cls.addClassCleanup(cls.tmp.cleanup)
        base = Path(cls.tmp.name)
        cls.project = replay_project(base)
        shutil.rmtree(cls.project / "approvals")  # as on live: no approvals or public-packets journal at all
        cls.home = base / "home"
        cls.home.mkdir(mode=0o700)
        cls.roots = [cls.home / rel for rel in rp.EXTRA_ROOTS]
        for root in cls.roots:
            for parent in reversed(root.relative_to(cls.home).parents[:-1]):
                (cls.home / parent).mkdir(mode=0o700, exist_ok=True)
            root.mkdir(mode=0o700)
        cas, evidence, live, _remaining = cls.roots
        original = b"synthetic retained original"
        cls.original = put(cas, "%s/%s" % (digest(original)[:2], digest(original)), original)
        receipt = b'{"synthetic": "off-project receipt"}'
        cls.receipt = put(evidence, "%s/%s" % (digest(receipt)[:2], digest(receipt)), receipt)
        units = b'{"synthetic": "units"}'
        cls.units = put(live, digest(units), units)
        put(evidence, "ff/" + "ff" * 32, b"unreferenced")  # in a declared root, named by no row: legacy
        manifest = json.dumps({"artifacts": {
            "source_receipt": {"path": str(cls.receipt), "sha256": digest(receipt), "bytes": len(receipt)},
            "derived_units": {"path": str(cls.units), "sha256": digest(units), "bytes": len(units)}}})
        with closing(sqlite3.connect(cls.project / rp.LEDGER)) as con:
            run_id = con.execute("SELECT run_id FROM runs LIMIT 1").fetchone()[0]
            con.execute("INSERT INTO originals(sha256,bytes,role,scope,storage_path,preservation_status,provenance_json)"
                        " VALUES(?,?,'document','records',?,'preserved','{}')",
                        (digest(original), len(original), str(cls.original)))
            con.execute("INSERT INTO extraction_adapter_imports VALUES(?,?,?,?,?,?,?)",
                        ("9" * 64, digest(original), digest(receipt), digest(manifest.encode()), manifest, run_id,
                         "2026-11-13T00:00:00+00:00"))
            con.commit()
        cls.ledger = (cls.project / rp.LEDGER).read_bytes()
        cls.counter = 0

    def setUp(self):
        type(self).counter += 1
        self.work = Path(self.tmp.name) / ("w%d" % self.counter)
        self.work.mkdir(mode=0o700)
        patcher = mock.patch.object(rp, "_home", return_value=str(self.home))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.restore_ledger)

    def restore_ledger(self):
        path = self.project / rp.LEDGER
        if path.read_bytes() != self.ledger:
            path.write_bytes(self.ledger)

    def mutate(self, sql, *args):
        with closing(sqlite3.connect(self.project / rp.LEDGER)) as con:
            con.executescript(sql) if not args else con.execute(sql, args)
            con.commit()

    def evidence(self, roots):
        path = self.work / "quiescence.json"
        path.write_text(json.dumps({
            "schema": "records-quiescence-v1", "basis": "independent_all_writer_audit", "roots": roots,
            "observed_utc": datetime.now(timezone.utc).isoformat(), "all_writers_stopped": True,
            "checks": {"units": {}, "open_files": {}, "run_lock": "free", "arrival_pending": False},
            "auditor": "records quiescence-audit"}))
        return str(path)

    def backup(self, evidence):
        out = io.StringIO()
        with redirect_stdout(out):
            code = records_backup.backup_main(["--project", str(self.project), "--into", str(self.work / "sets"),
                                               "--quiescence", evidence])
        return code, json.loads(out.getvalue())

    # Q1a + Q2a passing shape --------------------------------------------------------------------------------
    def test_declared_roots_and_absent_roles_check_and_round_trip(self):
        code, line, rest = run_check(self.project, "--explain")
        self.assertEqual((code, line["code"], line["unclassified"], line["referenced_outside_set"]), (0, "OK", 0, 0))
        self.assertEqual(line["absent_roles"], {"cursor": "source_absent", "review_bundle": "source_absent"})
        self.assertEqual((line["root_owner"], line["extra_roots"]["roots"], line["extra_roots"]["files"]),
                         ("caller", 4, 3))
        self.assertEqual(line["out_of_scope_legacy"]["files"], 1)  # the unreferenced file in a declared root
        self.assertIn("absent_roles: cursor source_absent, review_bundle source_absent", rest)
        self.assertIn("extra_roots: 4, owner other 0, included files 3, bytes %d" % line["extra_roots"]["bytes"], rest)
        self.assertFalse(any(str(self.home) in r for r in rest))
        # the quiescence audit names every root the backup reads, and the backup accepts that evidence
        with mock.patch.object(qa, "PROC_ROOT", str(self.work / "proc")):
            (self.work / "proc").mkdir()
            doc = qa.audit(str(self.project), str(self.work), self.units_file())
        self.assertEqual(doc["roots"], [str(self.project), *map(str, self.roots)])
        self.assertIs(doc["all_writers_stopped"], True, doc["checks"]["codes"])
        code, out = self.backup(self.evidence(doc["roots"]))
        self.assertEqual((code, out["code"]), (0, "OK"), out)
        (target,) = (self.work / "sets").glob("set-*")
        manifest = json.loads((target / "manifest.json").read_bytes())
        self.assertEqual(manifest["absent_roles"], {"cursor": "source_absent", "review_bundle": "source_absent"})
        self.assertEqual(manifest["required_roles"], sorted(v2.BASE_ROLES))
        self.assertEqual(manifest["root_owner"], {r: "caller" for r in doc["roots"]})
        by_source = {row["source"]: row for row in manifest["files"]}
        self.assertEqual(by_source[str(self.original)]["roles"], ["original_blob"])
        self.assertEqual(by_source[str(self.receipt)]["roles"], ["receipt", "derived_resume"])
        self.assertEqual(by_source[str(self.units)]["roles"], ["derived_resume"])
        legacy = [row["source"] for row in manifest["excluded"] if row["reason"] == "out_of_scope_legacy"]
        self.assertIn(str(self.roots[1] / "ff"), legacy)
        digests = self.work / "sets" / records_backup.DIGESTS
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(records_backup.verify_main(["--set", str(target), "--digests", str(digests)]), 0)
        pin = json.loads(digests.read_text().splitlines()[-1])["manifest_sha256"]
        restored = self.work / "restored"
        v2.restore_v2(str(target), str(restored), manifest_sha256=pin)
        for source in (self.original, self.receipt, self.units):
            self.assertEqual((restored / by_source[str(source)]["path"]).read_bytes(), source.read_bytes())

    def units_file(self):
        path = self.work / "units.txt"
        path.write_text("Captured=%d\n\n" % time.time() + "\n\n".join(
            "Id=%s\nActiveState=inactive\nSubState=dead" % unit for unit in qa.WRITER_UNITS) + "\n")
        return str(path)

    # Q1a refusals -------------------------------------------------------------------------------------------
    def test_undeclared_referenced_path_still_refused(self):
        undeclared = put(self.home, "deflock-private-artifacts/records-project-retained-v1/other/aa/" + "aa" * 32,
                         b"aa")
        self.addCleanup(undeclared.unlink)
        self.mutate("INSERT INTO originals(sha256,bytes,role,scope,storage_path,preservation_status,provenance_json) "
                    "VALUES(?,2,'document','records',?,'preserved','{}')", "aa" * 32, str(undeclared))
        code, line, rest = run_check(self.project, "--explain")
        self.assertEqual((code, line["code"], line["referenced_outside_set"]), (1, "REFERENCED_OUTSIDE_SET", 1))
        self.assertIn("referenced_outside: original_blob <off-project>/other/<hh>/<sha> refs 1", rest)

    def test_missing_file_in_a_declared_root_is_refused(self):
        self.mutate("INSERT INTO originals(sha256,bytes,role,scope,storage_path,preservation_status,provenance_json) "
                    "VALUES(?,2,'document','records',?,'preserved','{}')", "bb" * 32,
                    str(self.roots[0] / "bb" / ("bb" * 32)))
        code, line, rest = run_check(self.project, "--explain")
        self.assertEqual((line["code"], line["referenced_outside_set"]), ("REFERENCED_OUTSIDE_SET", 1))
        self.assertIn("referenced_outside: original_blob <extra-root-1>/<hh>/<sha> (missing) refs 1", rest)

    def test_symlinked_declared_root_or_component_is_refused(self):
        real = self.roots[3]
        moved = self.work / "remaining"
        real.rename(moved)
        self.addCleanup(moved.rename, real)
        real.symlink_to(moved, target_is_directory=True)
        self.addCleanup(real.unlink)
        code, line, _rest = run_check(self.project)
        self.assertEqual((code, line["code"], line["reason"]), (1, "PROFILE_INCOMPLETE", "extra_root_unsafe"))
        (self.work / "proc").mkdir()
        with mock.patch.object(qa, "PROC_ROOT", str(self.work / "proc")):
            doc = qa.audit(str(self.project), str(self.work), self.units_file())
        self.assertIn("ROOTS_UNSAFE", doc["checks"]["codes"])
        self.assertIs(doc["all_writers_stopped"], False)

    def test_symlink_inside_a_declared_root_on_a_referenced_path_is_refused(self):
        cas = self.roots[0]
        hh = self.original.parent
        moved = self.work / "hh"
        hh.rename(moved)
        self.addCleanup(moved.rename, hh)
        hh.symlink_to(moved, target_is_directory=True)
        self.addCleanup(hh.unlink)
        code, line, _rest = run_check(self.project)
        self.assertEqual((code, line["code"]), (1, "PROFILE_INCOMPLETE"))
        self.assertTrue(cas.is_dir())

    def test_evidence_naming_only_the_project_is_refused(self):
        code, out = self.backup(self.evidence([str(self.project)]))
        self.assertEqual((code, out["code"]), (1, "QUIESCENCE_REFUSED"))
        self.assertFalse((self.work / "sets").exists())

    # Q2a refusals -------------------------------------------------------------------------------------------
    def test_present_but_unreadable_or_drifted_source_is_not_absent(self):
        journal = self.project / "public-packets.jsonl"
        journal.symlink_to(self.work / "elsewhere.jsonl")
        try:
            code, line, _rest = run_check(self.project)
        finally:
            journal.unlink()
        self.assertEqual((code, line["code"], line["unreadable"]), (1, "PROFILE_INCOMPLETE", 1))
        self.assertNotIn("absent_roles", line)
        self.mutate("CREATE TABLE mail_checkpoints(account TEXT)")  # exists, fails its schema pin
        code, line, _rest = run_check(self.project)
        self.assertEqual((code, line["code"], line["reason"]), (1, "PROFILE_INCOMPLETE", "schema_drift"))
        self.assertNotIn("absent_roles", line)

    def test_library_refuses_absent_role_present_or_base_role_absent(self):
        profile = rp.build_profile(str(self.project))
        for absent, reason in ((("cursor", "receipt"), "absent_roles_invalid"), (("cursor", "cursor"), "absent_roles_invalid"),
                               ((), "required_roles_and_two_databases")):
            with self.subTest(absent=absent), self.assertRaisesRegex(b.RecoveryError, reason):
                v2._profile(replace(profile, absent_roles=absent))
        self.mutate(imap_intake.CHECKPOINTS_SQL)  # the ledger now has the checkpoint table the profile called absent
        q = v2.CallerQuiescence(profile.roots, datetime.now(timezone.utc).isoformat(), "e" * 64, all_writers_stopped=True)
        hashes = {s.source: s.schema_sha256 for s in rp.build_profile(str(self.project)).stores}
        stores = tuple(replace(s, schema_sha256=hashes[s.source]) if s.kind == "sqlite" else s for s in profile.stores)
        with self.assertRaisesRegex(b.RecoveryError, "absent_role_source_present"):
            v2.backup_v2(str(self.work / "set"), replace(profile, stores=stores), quiescence=q)

    def test_verify_refuses_overlap_incomplete_or_base_absent_and_old_shape_means_every_role(self):
        code, out = self.backup(self.evidence(list(rp.backup_roots(str(self.project)))))
        self.assertEqual(code, 0, out)
        (target,) = (self.work / "sets").glob("set-*")
        original = json.loads((target / "manifest.json").read_bytes())

        def reseal(manifest):
            raw = b.canonical(manifest)
            path = target / "manifest.json"
            path.chmod(0o600)
            path.write_bytes(raw)
            path.chmod(0o400)
            return digest(raw)

        bad = ({"required_roles": sorted(v2.REQUIRED_ROLES)},  # overlap: cursor required and absent
               {"absent_roles": {"cursor": "source_absent"}},  # together they are not the full set
               {"required_roles": sorted(v2.BASE_ROLES - {"receipt"}),
                "absent_roles": {"cursor": "source_absent", "review_bundle": "source_absent", "receipt": "source_absent"}},
               {"absent_roles": {"cursor": "unknown", "review_bundle": "source_absent"}})
        for change in bad:
            with self.subTest(change=change), self.assertRaisesRegex(b.RecoveryError, "manifest_schema_invalid"):
                v2.verify_v2(str(target), manifest_sha256=reseal({**original, **change}))
        old = {k: v for k, v in original.items() if k not in ("absent_roles", "root_owner")}
        with self.assertRaisesRegex(b.RecoveryError, "manifest_schema_invalid"):  # old shape means the full role set
            v2.verify_v2(str(target), manifest_sha256=reseal(old))
        reseal(original)

    # Q3a ----------------------------------------------------------------------------------------------------
    def test_owner_only_rule_on_included_files_only(self):
        created = []
        for rel in ("processing-v1/backups", "processing-v1/backups/" + RUN):  # prior_backup: excluded
            if not (self.project / rel).exists():
                (self.project / rel).mkdir(mode=0o750)
                created.append(self.project / rel)
        stray = self.project / "processing-v1/backups" / RUN / "manifest.json"
        stray.write_bytes(b"{}")
        stray.chmod(0o640)
        self.addCleanup(lambda: [p.rmdir() for p in reversed(created)])
        self.addCleanup(stray.unlink)
        self.assertEqual(run_check(self.project)[1]["code"], "OK")  # excluded: metadata only
        included = self.receipt
        included.chmod(0o640)
        self.addCleanup(included.chmod, 0o600)
        line = run_check(self.project)[1]
        self.assertEqual((line["code"], line["reason"]), ("PROFILE_INCOMPLETE", "owner_only_permissions_required"))
        included.chmod(0o600)
        self.receipt.parent.chmod(0o750)  # a directory on an included file's path
        self.addCleanup(self.receipt.parent.chmod, 0o700)
        line = run_check(self.project)[1]
        self.assertEqual((line["code"], line["reason"]), ("PROFILE_INCOMPLETE", "owner_only_permissions_required"))

    def test_root_may_be_group_readable_never_group_or_other_writable(self):
        self.addCleanup(self.project.chmod, 0o700)
        for mode, code in ((0o750, "OK"), (0o770, "PROFILE_INCOMPLETE"), (0o702, "PROFILE_INCOMPLETE")):
            with self.subTest(mode=oct(mode)):
                self.project.chmod(mode)
                line = run_check(self.project)[1]
                self.assertEqual(line["code"], code, line)
                if code != "OK":
                    self.assertEqual(line["reason"], "root_group_or_other_writable")
        self.project.chmod(0o700)
        self.roots[2].chmod(0o770)  # a declared root follows the same rule
        self.addCleanup(self.roots[2].chmod, 0o700)
        self.assertEqual(run_check(self.project)[1]["reason"], "root_group_or_other_writable")


if __name__ == "__main__":
    unittest.main()
