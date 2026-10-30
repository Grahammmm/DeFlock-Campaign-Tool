"""Synthetic selected exports only; no live records or network."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from campaign_tool.records import unattended
from campaign_tool.records.container_host import Rejected
from tests.records.test_export_bridge import legacy_export, synthetic_message


class ExportSnapshotTests(unittest.TestCase):
    def setUp(self):
        parent = os.environ.get("TMPDIR", str(Path.home()))
        self.tmp = tempfile.TemporaryDirectory(dir=str(Path.home()) if parent in ("/tmp", "/var/tmp") else parent)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.export = self.base / "exports"
        self.export.mkdir(mode=0o700)
        self.path, self.receipt = legacy_export(self.export)
        self.names = (str(self.path.relative_to(self.export)),)

    def snapshot(self, **kwargs):
        from campaign_tool.records.intake.export_snapshot import snapshot
        return snapshot(self.export, self.names, **kwargs)

    def pipeline(self):
        from campaign_tool.records.export_selection import ExportSelection
        from campaign_tool.records.portal.collections import Scope
        pipeline = unattended.UnattendedPipeline(self.base / "canonical")
        selected = ExportSelection(self.export, self.names, Scope(frozenset()), pipeline.root.path)
        return pipeline, selected

    def run_selected(self, pipeline, selected, sha):
        with patch.object(pipeline, "advance_all", return_value=[]):
            return pipeline.run(exports=selected, export_manifest_sha256=sha)

    def test_unattended_stale_manifest_precedes_backend_and_preservation(self):
        pipeline, selected = self.pipeline()
        sha = self.snapshot()["sha256"]
        self.path.write_bytes(self.path.read_bytes() + b" ")
        with patch.object(unattended.CanonicalMailBackend, "start_run") as start:
            report = self.run_selected(pipeline, selected, sha)
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(report["safety"]["stop_code"], "retention_integrity_failed")
        start.assert_not_called()
        self.assertEqual(pipeline._query("SELECT * FROM mail_messages"), [])
        self.assertEqual(pipeline._query("SELECT * FROM originals"), [])
        self.assertEqual(list((pipeline.root.intake / "blobs").iterdir()), [])

    def test_explicit_hash_bound_selection_without_directory_discovery(self):
        with patch("os.listdir", side_effect=AssertionError("no discovery")), patch("os.scandir", side_effect=AssertionError("no discovery")):
            first = self.snapshot()
            self.assertEqual(first["sha256"], self.snapshot()["sha256"])
        self.assertEqual((first["receipts"], first["members"]), (1, 2))
        self.assertEqual(first["entries"][0]["receipt"], self.names[0])
        self.assertTrue(all(not Path(m["path"]).is_absolute() for m in first["entries"][0]["members"]))
        self.path.write_bytes(self.path.read_bytes() + b" ")
        self.assertNotEqual(first["sha256"], self.snapshot()["sha256"])
        with self.assertRaises(Rejected):
            self.snapshot(expected=first["sha256"])

    def test_members_order_counts_and_inode_pin(self):
        from campaign_tool.records.intake.export_snapshot import snapshot
        second, _ = legacy_export(self.export, uid=42)
        names = self.names + (str(second.relative_to(self.export)),)
        first = snapshot(self.export, names)
        for changed in (names[::-1], names[:1], names[1:]):
            self.assertNotEqual(first["sha256"], snapshot(self.export, changed)["sha256"])
        member = self.export / self.receipt["attachments"][0]["path"]
        raw = member.read_bytes()
        member.rename(self.base / "old")
        member.write_bytes(raw)
        member.chmod(0o600)
        self.assertEqual(first["sha256"], snapshot(self.export, names)["sha256"])
        with self.assertRaises(Rejected):
            snapshot(self.export, names, prior=first)
        for data in (raw + b"x", bytes([raw[0] ^ 1]) + raw[1:]):
            member.write_bytes(data)
            with self.assertRaises(Rejected):
                self.snapshot()
        member.write_bytes(raw)
        original = self.export / self.receipt["original_eml"]["path"]
        original.write_bytes(b"x" * original.stat().st_size)
        with self.assertRaises(Rejected):
            self.snapshot()

    def test_unsafe_selections_and_private_members(self):
        from campaign_tool.records.intake import export_snapshot as module
        for names in (("../escape",), (str(self.path),), self.names * 2, tuple(str(i) for i in range(61))):
            with self.subTest(names=names), self.assertRaises(Rejected) as error:
                module.snapshot(self.export, names)
            self.assertEqual(str(error.exception), "")
        with patch.object(module, "MAX_SELECTION_BYTES", 1), self.assertRaises(Rejected):
            self.snapshot()
        member = self.export / self.receipt["attachments"][0]["path"]
        member.chmod(0o644)
        with self.assertRaises(Rejected):
            self.snapshot()
        member.chmod(0o600)
        os.link(member, self.base / "hardlink")
        with self.assertRaises(Rejected):
            self.snapshot()
        (self.base / "hardlink").unlink()
        for target in (member, self.path, member.parent, self.export):
            saved = target.with_name(target.name + "-saved")
            target.rename(saved)
            target.symlink_to(saved, target_is_directory=saved.is_dir())
            with self.assertRaises(Rejected) as error:
                self.snapshot()
            self.assertEqual(str(error.exception), "")
            target.unlink()
            saved.rename(target)

    def test_two_identities_single_lifecycle_and_replay(self):
        second, declaration = legacy_export(self.export, uid=42)
        self.names += (str(second.relative_to(self.export)),)
        sha = self.snapshot()["sha256"]
        pipeline, selected = self.pipeline()
        backend = unattended.CanonicalMailBackend(self.export, pipeline.root.intake, pipeline.root.ledger)
        with patch.object(unattended, "CanonicalMailBackend", return_value=backend), patch.object(backend, "start_run", wraps=backend.start_run) as start, patch.object(backend, "finish_run", wraps=backend.finish_run) as finish:
            first = self.run_selected(pipeline, selected, sha)
        self.assertEqual((start.call_count, finish.call_count), (1, 1))
        rows = pipeline._query("SELECT account,folder,uidvalidity,uid,eml_sha256 FROM mail_messages ORDER BY uid")
        self.assertEqual([tuple(r.values()) for r in rows], [("fixture-account", "Records", 900, uid, self.receipt["original_eml"]["sha256"]) for uid in (41, 42)])
        originals = pipeline._query("SELECT sha256 FROM originals ORDER BY sha256")
        self.assertIn(self.receipt["attachments"][0]["sha256"], [r["sha256"] for r in originals])
        pipeline, selected = self.pipeline()
        second = self.run_selected(pipeline, selected, sha)
        self.assertEqual(len(second["intake"]["replayed"]), 2)
        self.assertEqual(pipeline._query("SELECT sha256 FROM originals ORDER BY sha256"), originals)
        self.assertEqual(len(pipeline._query("SELECT * FROM mail_messages")), 2)
        self.assertEqual(len(first["intake"]["preserved"]), 2)

    def test_disk_refusal_and_between_snapshot_mutation(self):
        from campaign_tool.records.intake import export_snapshot as module
        second, _ = legacy_export(self.export, uid=42)
        self.names += (str(second.relative_to(self.export)),)
        pipeline, selected = self.pipeline()
        sha = self.snapshot()["sha256"]
        with patch.object(unattended.folder, "check_space", side_effect=OSError):
            report = self.run_selected(pipeline, selected, sha)
        self.assertEqual(report["intake"]["refusal"], "insufficient_disk_for_preservation")
        self.assertEqual(pipeline._query("SELECT * FROM mail_messages"), [])
        # A fresh safety state/root is needed after a durable stop.
        pipeline = unattended.UnattendedPipeline(self.base / "other-canonical")
        from campaign_tool.records.export_selection import ExportSelection
        selected = ExportSelection(self.export, self.names, selected.scope, pipeline.root.path)
        real = module.snapshot
        checks = []
        def mutate(*args, **kwargs):
            if kwargs.get("prior") is not None:
                checks.append(True)
                if len(checks) == 2:
                    member = self.export / self.receipt["original_eml"]["path"]
                    member.write_bytes(b"x" * member.stat().st_size)
            return real(*args, **kwargs)
        backend = unattended.CanonicalMailBackend(self.export, pipeline.root.intake, pipeline.root.ledger)
        with patch.object(module, "snapshot", side_effect=mutate), patch.object(unattended, "CanonicalMailBackend", return_value=backend), patch.object(backend, "start_run", wraps=backend.start_run) as start:
            report = self.run_selected(pipeline, selected, sha)
        start.assert_called_once()
        self.assertEqual(report["safety"]["stop_code"], "retention_integrity_failed")
        self.assertEqual(report["intake"]["deferred"], 1)
        self.assertEqual(pipeline._query("SELECT * FROM mail_messages"), [])

    def test_visible_gaps_sanitized_report_and_cli_conflicts(self):
        self.path.unlink()
        self.path, self.receipt = legacy_export(self.export, body="https://unapproved.example.invalid/file.pdf\nhttps://archive.example.invalid/WebLink/Browse.aspx?id=F1\n")
        pipeline, selected = self.pipeline()
        with patch("socket.socket", side_effect=AssertionError("offline")):
            report = self.run_selected(pipeline, selected, self.snapshot()["sha256"])
        self.assertEqual((report["status"], report["exit_code"]), ("completed_with_gaps", 3))
        self.assertFalse(report["intake"]["events"][0]["retrieval_complete"])
        for sentinel in (str(self.export), self.path.name, "fixture-account", "https://"):
            self.assertNotIn(sentinel, json.dumps(report))
        for flag in ("--inbox", "--mail-config", "--documents"):
            with patch("sys.stdout", io.StringIO()) as output, patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
                unattended.main(["--root", str(pipeline.root.path), "--export-root", str(self.export), "--export-receipt", self.names[0], "--export-manifest-sha256", "a" * 64, flag, "synthetic"])
            self.assertEqual(output.getvalue(), "")

    def test_genuine_unattended_cli_and_unchanged_inbox_route(self):
        from tests.records import test_host_offline_inbox as fixtures
        pipeline, selected = self.pipeline()
        with patch.dict(os.environ, {"MODEL_BASE_URL": "", "CHALLENGE_MODEL_BASE_URL": ""}), patch.object(unattended.UnattendedPipeline, "advance_all", return_value=[]), patch("sys.stdout", io.StringIO()) as output:
            code = unattended.main(["--root", str(pipeline.root.path), "--export-root", str(self.export), "--export-receipt", self.names[0], "--export-manifest-sha256", self.snapshot()["sha256"], "--json"])
        self.assertEqual(code, 3)  # Existing review-retirement gap is not overridden.
        self.assertEqual(json.loads(output.getvalue())["intake"]["messages"], 1)
        fixture = fixtures.HostOfflineInboxTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        value = fixture.offline(bind=True)
        with patch.object(unattended.UnattendedPipeline, "advance_all", return_value=[]), patch.dict(os.environ, {"MODEL_BASE_URL": "", "CHALLENGE_MODEL_BASE_URL": ""}), patch("sys.stdout", io.StringIO()) as output:
            unattended.main(["--root", str(fixture.root / "canonical"), "--inbox", value["inbox"], "--unattended", "--json"])
        self.assertEqual(json.loads(output.getvalue())["intake"]["messages"], 1)
