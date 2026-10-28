"""Backup export / verify / restore round trip, tamper detection, modes, hosted export, CLI.

Synthetic campaign directories only.
"""
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import backup  # noqa: E402


def cli(*args, timeout=20):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, args)], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=timeout)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synthetic-backup-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "campaign"
        r = cli("init", "--directory", self.root, "--name", "Synthetic", "--county", "Synthetic", "--state", "CA")
        self.assertEqual(r.returncode, 0, r.stderr)
        fixture = self.base / "policy.txt"
        fixture.write_bytes(b"Synthetic policy text.\n")
        r = cli("ingest", "--directory", self.root, "--file", fixture, "--source-id", "demo-1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.sha = hashlib.sha256(b"Synthetic policy text.\n").hexdigest()
        (self.root / "content").mkdir()
        (self.root / "content" / "site.json").write_text('{"schema_version":1,"tagline":"t","about_md":"a"}')
        (self.root / "kit").mkdir()
        (self.root / "kit" / "summary.json").write_text("{}")
        # things that must NOT be archived
        (self.root / "public").mkdir()
        (self.root / "public" / "index.html").write_text("<p>built</p>")
        (self.root / ".env").write_text("SECRET=nope")
        os.symlink(fixture, self.root / "content" / "link.txt")

    def test_export_verify_restore_round_trip(self):
        out = self.base / "backup.tar"
        manifest = backup.export(self.root, out)
        names = sorted(manifest["files"])
        self.assertEqual(names, ["campaign.json", "content/site.json", "kit/summary.json", "private/ledger.sqlite",
                                 "private/objects/" + self.sha])
        self.assertEqual(manifest["counts"]["objects"], 1)
        self.assertEqual(manifest["counts"]["receipts"], 1)
        self.assertEqual(manifest["counts"]["files"], 5)
        self.assertEqual(manifest["engine_version"], backup.__version__)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        with tarfile.open(out) as tar:
            members = tar.getnames()
            self.assertEqual(members[0], "manifest.json")
            self.assertNotIn("public/index.html", members)
            self.assertNotIn(".env", members)
            self.assertNotIn("content/link.txt", members)
            self.assertTrue(all(m.mode == 0o600 and m.uid == 0 for m in tar.getmembers()))
        report = backup.verify(out)
        self.assertTrue(report["ok"])
        self.assertEqual(report["verified"], 5)
        target = self.base / "restored"
        restored = backup.restore(out, target)
        self.assertEqual(restored["restored_to"], str(target))
        self.assertEqual((target / "private" / "objects" / self.sha).read_bytes(), b"Synthetic policy text.\n")
        self.assertEqual(json.loads((target / "campaign.json").read_text())["name"], "Synthetic")
        for path in target.rglob("*"):
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o700 if path.is_dir() else 0o600, path)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        # the restored ledger is a working database with the receipt
        r = cli("status", "--directory", target)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["receipt_occurrences"], 1)
        with self.assertRaises(backup.BackupError):
            backup.export(self.root, out)  # refuses to overwrite

    def test_restore_refuses_non_empty_target(self):
        out = self.base / "backup.tar"
        backup.export(self.root, out)
        target = self.base / "busy"
        target.mkdir()
        (target / "keep.txt").write_text("x")
        with self.assertRaises(backup.BackupError):
            backup.restore(out, target)
        self.assertTrue((target / "keep.txt").exists())

    def _tamper(self, out, mutate):
        """Rewrite the archive with ``mutate(name, data) -> data`` applied to members."""
        tampered = self.base / "tampered.tar"
        with tarfile.open(out) as src, tarfile.open(tampered, "w") as dst:
            for member in src.getmembers():
                data = src.extractfile(member).read() if member.isfile() else b""
                data = mutate(member.name, data)
                if data is None:
                    continue
                info = tarfile.TarInfo(member.name)
                info.size = len(data)
                info.mode = member.mode
                dst.addfile(info, io.BytesIO(data))
        return tampered

    def test_tamper_detection(self):
        out = self.base / "backup.tar"
        backup.export(self.root, out)
        # same-length replacement: size still matches, only the hash can catch it
        flipped = self._tamper(out, lambda n, d: d.replace(b"Synthetic policy", b"Synthetic p0licy") if n.startswith("private/objects/") else d)
        with self.assertRaisesRegex(backup.BackupError, "hash_mismatch"):
            backup.verify(flipped)
        # different-length replacement: reported once as size_mismatch, never as missing
        resized = self._tamper(out, lambda n, d: d.replace(b"Synthetic policy", b"Altered policy") if n.startswith("private/objects/") else d)
        with self.assertRaisesRegex(backup.BackupError, "size_mismatch") as ctx:
            backup.verify(resized)
        self.assertNotIn("missing_member", str(ctx.exception))
        target = self.base / "never"
        with self.assertRaises(backup.BackupError):
            backup.restore(flipped, target)
        self.assertFalse(target.exists())
        missing = self._tamper(out, lambda n, d: None if n == "kit/summary.json" else d)
        with self.assertRaisesRegex(backup.BackupError, "missing_member"):
            backup.verify(missing)
        extra = self.base / "extra.tar"
        with tarfile.open(out) as src, tarfile.open(extra, "w") as dst:
            for member in src.getmembers():
                dst.addfile(member, src.extractfile(member))
            info = tarfile.TarInfo("../escape.txt")
            info.size = 1
            dst.addfile(info, io.BytesIO(b"x"))
        with self.assertRaisesRegex(backup.BackupError, "unsafe_member_name"):
            backup.verify(extra)
        no_manifest = self._tamper(out, lambda n, d: None if n == "manifest.json" else d)
        with self.assertRaisesRegex(backup.BackupError, "manifest.json missing"):
            backup.verify(no_manifest)
        # A rewritten archive whose manifest was rewritten to match passes the integrity
        # check; only the digest recorded out of band at export time catches it.
        manifest = backup.export(self.root, self.base / "again.tar")
        self.assertRegex(manifest["manifest_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(backup.verify(self.base / "again.tar", expected_manifest_sha256=manifest["manifest_sha256"].upper())["ok"], True)
        rewritten = self._tamper(self.base / "again.tar", lambda n, d: d.replace(b"\n", b"\n\n", 1) if n == "manifest.json" else d)
        backup.verify(rewritten)  # integrity alone cannot tell
        with self.assertRaisesRegex(backup.BackupError, "manifest_digest_mismatch"):
            backup.verify(rewritten, expected_manifest_sha256=manifest["manifest_sha256"])

    def test_export_detects_corrupt_stored_object(self):
        (self.root / "private" / "objects" / self.sha).write_bytes(b"corrupted")
        with self.assertRaisesRegex(backup.BackupError, "does not match its hash"):
            backup.export(self.root, self.base / "x.tar")
        self.assertFalse((self.base / "x.tar").exists())

    def test_cli_backup_verify_restore(self):
        out = self.base / "cli.tar"
        r = cli("backup", "--directory", self.root, "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout.split("\n}\n")[0] + "\n}")["files"], 5)
        self.assertIn("unencrypted", r.stdout)
        r = cli("verify", "--file", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(json.loads(r.stdout)["ok"])
        r = cli("restore", "--file", out, "--directory", self.base / "cli-restored")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.base / "cli-restored" / "private" / "ledger.sqlite").is_file())
        r = cli("restore", "--file", out, "--directory", self.base / "cli-restored")
        self.assertEqual(r.returncode, 1)
        self.assertIn("refuses to overwrite", r.stderr)
        r = cli("verify", "--file", self.base / "nope.tar")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)


class FakeWorkspace:
    """Stand-in for the runner client: serves export.json and originals by hash."""

    def __init__(self, originals, campaign_id="01synthetic000000000000000"):
        self.originals = originals
        self.calls = []
        self.export = {
            "schema_version": 1, "campaign_id": campaign_id, "exported_at": "2026-09-30T00:00:00Z",
            "tables": ["campaign", "original", "receipt_occurrence"],
            "rows": {
                "campaign": [{"campaign_id": campaign_id, "name": "Hosted Synthetic", "jurisdiction": "us-ca",
                              "county_name": "Synthetic", "law_package_status": "draft", "external_sends": "approval_required"}],
                "original": [{"campaign_id": campaign_id, "sha256": sha, "byte_count": len(data)} for sha, data in originals.items()],
                "receipt_occurrence": [{"receipt_id": "r1", "sha256": next(iter(originals), ""), "source_id": "s"}],
            },
        }

    def get(self, path):
        self.calls.append(path)
        if path == "/api/runner/export.json":
            return json.dumps(self.export).encode("utf-8")
        sha = path.rsplit("/", 1)[-1]
        return self.originals[sha]


class HostedExportTests(unittest.TestCase):
    def test_export_hosted_round_trip(self):
        data = b"hosted synthetic original\n"
        sha = hashlib.sha256(data).hexdigest()
        client = FakeWorkspace({sha: data})
        with tempfile.TemporaryDirectory(prefix="synthetic-hosted-") as tmp:
            out = Path(tmp) / "hosted.tar"
            manifest = backup.export_hosted(client, out)
            self.assertEqual(sorted(manifest["files"]), ["campaign.json", "private/d1-export.json", "private/objects/" + sha])
            self.assertEqual(manifest["source"], {"kind": "hosted", "campaign_id": "01synthetic000000000000000", "exported_at": "2026-09-30T00:00:00Z"})
            self.assertEqual(manifest["counts"]["objects"], 1)
            self.assertEqual(manifest["counts"]["receipts"], 1)
            self.assertEqual(client.calls, ["/api/runner/export.json", "/api/runner/originals/" + sha])
            self.assertTrue(backup.verify(out)["ok"])
            target = Path(tmp) / "restored"
            backup.restore(out, target)
            campaign = json.loads((target / "campaign.json").read_text())
            self.assertEqual(campaign["state"], "CA")
            self.assertEqual(campaign["hosted_campaign_id"], "01synthetic000000000000000")
            export = json.loads((target / "private" / "d1-export.json").read_text())
            self.assertEqual(export["rows"]["campaign"][0]["name"], "Hosted Synthetic")
            self.assertEqual((target / "private" / "objects" / sha).read_bytes(), data)

    def test_export_hosted_refuses_hash_mismatch(self):
        sha = hashlib.sha256(b"expected").hexdigest()
        client = FakeWorkspace({sha: b"something else"})
        with tempfile.TemporaryDirectory(prefix="synthetic-hosted-") as tmp:
            out = Path(tmp) / "bad.tar"
            with self.assertRaisesRegex(backup.BackupError, "did not match its hash"):
                backup.export_hosted(client, out)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
