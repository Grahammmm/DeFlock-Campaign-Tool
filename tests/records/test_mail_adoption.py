"""Synthetic tests for mail-history receipt adoption (records adopt-mail).

No live mailbox, private records, credentials or network. Fixtures reuse the
independent legacy export builder from the export bridge suite.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from campaign_tool.records import mail_adoption
from campaign_tool.records.intake import eml_export
from campaign_tool.records.run import Pipeline

from tests.records.test_export_bridge import legacy_export


def write_private(path, data):
    path = Path(path)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
    os.chmod(path, 0o600)


class MailAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.pipeline = Pipeline(self.base / "processing")
        self.root = self.pipeline.root.path
        self.export = self.base / "mail-history"
        self.export.mkdir(mode=0o700)
        self.receipts = [legacy_export(self.export, uid=uid)[1] for uid in (41, 42, 43)]

    def config(self, adoption="enabled", marker=None):
        path = self.base / "adoption.json"
        write_private(path, json.dumps({"schema": mail_adoption.CONFIG_SCHEMA, "source": "mail-history",
                                        "adoption": adoption, "disable_marker_sha256": marker}).encode())
        return path

    def marker(self):
        data = b'{"action":"disable_new_adoption","schema":"synthetic"}\n'
        write_private(self.root / mail_adoption.DISABLE_MARKER, data)
        return hashlib.sha256(data).hexdigest()

    def count(self, sql):
        con = sqlite3.connect((self.root / "ledger.sqlite").as_uri() + "?mode=ro", uri=True)
        try:
            return con.execute(sql).fetchone()[0]
        finally:
            con.close()

    def adopt(self, **kw):
        return mail_adoption.adopt(self.root, self.export, **kw)

    def test_dry_run_counts_and_writes_nothing(self):
        before = (self.root / "ledger.sqlite").read_bytes()
        report = self.adopt()
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual((report["receipts"], report["attachments"]), (3, 3))
        self.assertEqual((report["pending_messages"], report["pending_attachments"]), (3, 3))
        self.assertEqual(report["already_adopted"], 0)
        self.assertEqual(report["gate"]["state"], "disabled_no_config")
        self.assertEqual(self.count("SELECT count(*) FROM mail_messages"), 0)
        self.assertEqual((self.root / "ledger.sqlite").read_bytes(), before)
        # Report is count-only: no account names, folders or paths.
        text = json.dumps(report)
        for private in ("fixture-account", "Records", str(self.export), "objects/"):
            self.assertNotIn(private, text)

    def test_dry_run_leaves_records_root_tree_identical(self):
        # store.ledger(readonly=True) would create -wal/-shm sidecars; a dry run
        # must leave every path, mode, size, mtime and byte unchanged, both
        # before and after a completed apply.
        def tree():
            result = {}
            for base, dirs, files in os.walk(self.base):
                result[base] = os.lstat(base).st_mtime_ns
                for name in files:
                    path = Path(base) / name
                    info = path.lstat()
                    result[str(path)] = (info.st_mode, info.st_size, info.st_mtime_ns,
                                         hashlib.sha256(path.read_bytes()).hexdigest())
            return result
        config = self.config()
        before = tree()
        self.adopt(config_path=config)
        self.assertEqual(tree(), before)
        self.adopt(config_path=config, apply=True)
        before = tree()
        report = self.adopt(config_path=config)
        self.assertEqual((report["pending_messages"], report["already_adopted"]), (0, 3))
        self.assertEqual(tree(), before)

    def test_apply_refused_without_open_gate(self):
        with self.assertRaisesRegex(mail_adoption.AdoptionError, "disabled_no_config"):
            self.adopt(apply=True)
        with self.assertRaisesRegex(mail_adoption.AdoptionError, "disabled_by_config"):
            self.adopt(apply=True, config_path=self.config("disabled"))
        self.assertEqual(self.count("SELECT count(*) FROM mail_messages"), 0)

    def test_disable_marker_honoured_until_config_pins_it(self):
        sha = self.marker()
        with self.assertRaisesRegex(mail_adoption.AdoptionError, "disabled_by_marker"):
            self.adopt(apply=True, config_path=self.config())
        with self.assertRaisesRegex(mail_adoption.AdoptionError, "disabled_by_marker"):
            self.adopt(apply=True, config_path=self.config(marker="0" * 64))
        report = self.adopt(apply=True, config_path=self.config(marker=sha))
        self.assertEqual(report["gate"]["state"], "enabled_marker_superseded_by_config")
        self.assertEqual(report["adopted_messages"], 3)
        # Gate is configuration only: the marker is never deleted or changed.
        self.assertEqual(hashlib.sha256((self.root / mail_adoption.DISABLE_MARKER).read_bytes()).hexdigest(), sha)

    def test_pinned_marker_that_disappeared_fails_closed(self):
        sha = self.marker()
        (self.root / mail_adoption.DISABLE_MARKER).unlink()
        with self.assertRaisesRegex(mail_adoption.AdoptionError, "marker_binding_mismatch"):
            self.adopt(apply=True, config_path=self.config(marker=sha))

    def test_apply_then_rerun_is_idempotent(self):
        config = self.config()
        report = self.adopt(apply=True, config_path=config)
        self.assertEqual((report["adopted_messages"], report["adopted_attachments"]), (3, 3))
        self.assertEqual(report["failures"], {})
        self.assertEqual(report["after"], {"already_adopted": 3, "new_messages": 0, "incomplete_messages": 0,
                                           "pending_messages": 0, "identity_conflicts": {}, "unfinalized_runs": 0})
        self.assertEqual(self.count("SELECT count(*) FROM mail_messages"), 3)
        self.assertEqual(self.count("SELECT count(*) FROM occurrences WHERE kind='attachment'"), 3)
        summary = json.loads(self.count("SELECT summary FROM runs WHERE run_id LIKE 'mail-history-%'"))
        self.assertEqual(summary["runner_summary"]["source"], "mail-history")
        self.assertEqual(summary["runner_outcome"], "completed")
        runs = self.count("SELECT count(*) FROM runs")
        before = (self.root / "ledger.sqlite").read_bytes()
        again = self.adopt(apply=True, config_path=config)
        self.assertEqual((again["adopted_messages"], again["pending_messages"], again["already_adopted"]), (0, 0, 3))
        self.assertEqual(self.count("SELECT count(*) FROM mail_messages"), 3)
        self.assertEqual(self.count("SELECT count(*) FROM runs"), runs)
        self.assertEqual((self.root / "ledger.sqlite").read_bytes(), before)

    def test_partial_prior_adoption_only_adopts_remainder(self):
        config = self.config()
        extra = legacy_export(self.export, uid=44)[0]
        os.rename(extra, self.base / "held.json")
        self.adopt(apply=True, config_path=config)
        os.rename(self.base / "held.json", extra)
        report = self.adopt(apply=True, config_path=config)
        self.assertEqual((report["receipts"], report["already_adopted"], report["adopted_messages"]), (4, 3, 1))
        self.assertEqual(self.count("SELECT count(*) FROM mail_messages"), 4)

    def test_invalid_receipt_is_a_coded_gap(self):
        eml_export._ensure_private(self.export / "receipts" / "broken.json", b'{"complete": false}\n')
        report = self.adopt()
        self.assertEqual(report["receipts"], 3)
        self.assertEqual(report["gaps"], {"incomplete_receipt": 1})

    def test_legacy_unresolved_original_is_predicted_and_refused(self):
        _, receipt = legacy_export(self.export, uid=50, body="A different synthetic body.\n")
        sha = receipt["original_eml"]["sha256"]
        from campaign_tool.records.ledger import store
        with store.ledger(self.root / "ledger.sqlite") as con:
            con.execute("INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (sha, receipt["original_eml"]["bytes"], None, "2026-01-01T00:00:00+00:00",
                         "unresolved", "unresolved", None, "captured", "eml", "{}"))
            con.commit()
        dry = self.adopt()
        self.assertEqual(dry["predicted_binding_conflicts"], {"original_scope_unresolved": 1})
        report = self.adopt(apply=True, config_path=self.config())
        self.assertEqual(report["adopted_messages"], 3)
        self.assertEqual(report["failures"], {"canonical_original_binding_conflict": 1})
        self.assertEqual(report["after"], {"already_adopted": 3, "new_messages": 1, "incomplete_messages": 0,
                                           "pending_messages": 1, "identity_conflicts": {}, "unfinalized_runs": 0})
        # The legacy declaration is left untouched for its own resolution contract.
        self.assertEqual(self.count("SELECT scope FROM originals WHERE sha256='" + sha + "'"), "unresolved")

    def test_rejects_invalid_config_and_export_inside_root(self):
        path = self.base / "bad.json"
        write_private(path, json.dumps({"schema": "other"}).encode())
        with self.assertRaisesRegex(mail_adoption.AdoptionError, "adoption_config_invalid"):
            self.adopt(config_path=path)
        os.chmod(self.config(), 0o644)
        with self.assertRaisesRegex(mail_adoption.AdoptionError, "adoption_config_invalid"):
            self.adopt(config_path=self.base / "adoption.json")
        with self.assertRaisesRegex(mail_adoption.AdoptionError, "export_inside_records_root"):
            mail_adoption.adopt(self.root, self.root / "mail")


if __name__ == "__main__":
    unittest.main()
