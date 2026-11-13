"""Synthetic bridge boundaries: no live records, trust files, containers or network."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest import mock

from campaign_tool.records import packet_changes as bridge
from campaign_tool.records import public_packets as pp
from tests.records.test_public_packets import Base, T0, doc

ROOT = Path(__file__).resolve().parents[2]


class ExportTests(Base):
    def written(self, **fields):
        ident = self.approved(doc(**fields))
        self.turn_on()
        pp.write_change(self.project, ident, now=T0)
        return ident

    def out(self):
        return Path(self.tmp.name) / "export"

    def test_cli_export_once_read_only_source_and_no_record_bytes(self):
        ident = self.written()
        source = Path(self.project)
        (source / "original.pdf").write_bytes(b"SYNTHETIC RECORD BYTES MUST STAY PRIVATE")
        before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()}
        for path in source.rglob("*"):
            path.chmod(0o500 if path.is_dir() else 0o400)
        source.chmod(0o500)
        self.addCleanup(source.chmod, 0o700)
        self.addCleanup((source / pp.OUT).chmod, 0o700)
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            for _ in range(2):
                self.assertEqual(pp.main(["export-changes", "--project", self.project, "--out", str(self.out())]), 0)
        rows = [json.loads(line) for line in capture.getvalue().splitlines()]
        self.assertEqual(rows, [{"exported": 1, "existing": 0, "active": 1}, {"exported": 0, "existing": 1, "active": 1}])
        self.assertEqual(set(p.name for p in self.out().iterdir()), {f"{ident}-r0.json", bridge.MANIFEST})
        self.assertEqual(before, {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()})
        public = bridge.read_export(self.out())
        self.assertEqual(public[0]["text"], doc()["text"])
        self.assertNotIn("signed_by", public[0])
        self.assertNotIn(b"SYNTHETIC RECORD BYTES", b"".join(p.read_bytes() for p in self.out().iterdir()))
        self.assertEqual(self.out().stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in self.out().iterdir()))

    def test_superseded_written_revision_not_active_even_if_old_export_remains(self):
        ident = self.written()
        bridge.export_changes(self.project, self.out())
        pp.propose(self.project, doc(text="Synthetic revised public text."), now=T0)
        self.assertEqual(bridge.export_changes(self.project, self.out())["active"], 0)
        self.assertTrue((self.out() / f"{ident}-r0.json").exists())
        self.assertEqual(bridge.read_export(self.out()), [])
        pp.approve(self.project, ident, self.sign(pp.payload(self.project, ident)), now=T0)
        pp.write_change(self.project, ident, now=T0)
        self.assertEqual(bridge.export_changes(self.project, self.out())["exported"], 1)
        self.assertEqual(bridge.read_export(self.out())[0]["revision"], 1)

    def test_source_hash_mismatch_and_destination_conflict_fail_closed(self):
        ident = self.written()
        file = Path(self.project) / pp.OUT / f"{ident}-r0.json"
        raw = file.read_bytes()
        file.write_bytes(raw + b" ")
        self.assertEqual(self.code(bridge.export_changes, self.project, self.out()), "change_hash_mismatch")
        self.assertFalse(self.out().exists())
        file.write_bytes(raw)
        with mock.patch.object(bridge.os, "link", side_effect=OSError("synthetic interrupted copy")):
            with self.assertRaises(OSError):
                bridge.export_changes(self.project, self.out())
        self.assertEqual(list(self.out().iterdir()), [])  # no partial final or manifest
        bridge.export_changes(self.project, self.out())
        (self.out() / file.name).write_bytes(b"conflict")
        self.assertEqual(self.code(bridge.export_changes, self.project, self.out()), "export_conflict")
        self.assertEqual((self.out() / file.name).read_bytes(), b"conflict")

    def test_no_link_following_kill_and_private_output_boundary(self):
        ident = self.written()
        file = Path(self.project) / pp.OUT / f"{ident}-r0.json"
        other = Path(self.tmp.name) / "other.json"
        file.rename(other)
        file.symlink_to(other)
        self.assertEqual(self.code(bridge.export_changes, self.project, self.out()), "file_invalid")
        file.unlink()
        file.write_bytes(other.read_bytes())
        self.assertEqual(self.code(bridge.export_changes, self.project, Path(self.project) / "out"), "export_boundary")
        self.out().symlink_to(Path(self.project), target_is_directory=True)
        self.assertEqual(self.code(bridge.export_changes, self.project, self.out()), "directory_invalid")
        self.out().unlink()
        self.out().mkdir(mode=0o755)
        self.out().chmod(0o755)  # do not let the worker's private umask mask the fixture
        self.assertEqual(self.code(bridge.export_changes, self.project, self.out()), "export_not_private")
        (Path(self.project) / pp.KILL).touch()
        self.assertEqual(self.code(bridge.export_changes, self.project, self.out()), "killed")

    def test_unwritten_and_orphan_files_are_never_exported(self):
        pp.propose(self.project, doc(), now=T0)
        directory = Path(self.project) / pp.OUT
        directory.mkdir()
        (directory / ("f" * 16 + "-r0.json")).write_text("not approved")
        self.assertEqual(bridge.export_changes(self.project, self.out()), {"exported": 0, "existing": 0, "active": 0})

    def test_manifest_tampering_and_duplicate_packets_refused(self):
        self.written()
        bridge.export_changes(self.project, self.out())
        file = self.out() / bridge.MANIFEST
        manifest = json.loads(file.read_text())
        manifest["changes"].append(manifest["changes"][0])
        file.write_text(json.dumps(manifest))
        self.assertEqual(self.code(bridge.read_export, self.out()), "manifest_invalid")
        manifest["changes"] = [{"file": "../original.pdf", "sha256": "a" * 64}]
        file.write_text(json.dumps(manifest))
        self.assertEqual(self.code(bridge.read_export, self.out()), "manifest_invalid")

    def test_contract_unknown_fields_privacy_and_redaction_refused(self):
        ident = self.written()
        file = Path(self.project) / pp.OUT / f"{ident}-r0.json"
        change = json.loads(file.read_text())
        for altered in ({**change, "record_bytes": "secret"}, {**change, "target": "../../outside"},
                        {**change, "revision": True}, {**change, "approved_at": "2026-02-31T00:00:00Z"},
                        {**change, "text": "person@example.invalid"},
                        {**change, "text": "https://example.invalid/download?token=synthetic"},
                        {**change, "documents": [{**change["documents"][0], "source_request": "805-555-1234"}]},
                        {**change, "documents": [{**change["documents"][0], "redaction_ids": []}]}):
            with self.subTest(fields=set(altered)):
                with self.assertRaises(pp.PacketError):
                    bridge.public_change(altered)

    @unittest.skipUnless(shutil.which("node"), "Node is needed for the site renderer")
    def test_renderer_escapes_text_no_signer_no_download_and_syntax_check(self):
        self.written(title="Synthetic <script> update", text="Public <b>plain text</b> & no link.")
        bridge.export_changes(self.project, self.out())
        public = bridge.read_export(self.out())
        fixture = Path(self.tmp.name) / "public.json"
        fixture.write_text(json.dumps(public))
        renderer = (ROOT / "tools/site-packet-updates.mjs").as_uri()
        script = Path(self.tmp.name) / "render.mjs"
        script.write_text(f"import fs from 'node:fs';\nimport {{renderPacketUpdates}} from {json.dumps(renderer)};\n"
                          f"console.log(renderPacketUpdates(JSON.parse(fs.readFileSync({json.dumps(str(fixture))}, 'utf8'))));\n")
        result = subprocess.run(["node", str(script)], check=True, capture_output=True, text=True)
        self.assertIn("&lt;script&gt;", result.stdout)
        self.assertIn("&lt;b&gt;plain text&lt;/b&gt;", result.stdout)
        self.assertNotIn("g@x", result.stdout)
        self.assertNotIn("href=", result.stdout)
        self.assertIn("No download offered", result.stdout)


if __name__ == "__main__":
    unittest.main()
