"""DF2037-13: `finding packet DRAFT --directory D --out DIR`. Synthetic example campaign and synthetic records only.

Run from the repository root: python3 -B -m unittest tests.test_finding_packet -v
"""
import ast
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import finding_packet, findings  # noqa: E402
from campaign_tool.content import load_finding_preview  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
RECORDS = [b"synthetic packet record %d\n" % index for index in range(2)]
BANNER = '<p class="draft-banner" role="note">Draft - not published</p>'
NOINDEX = '<meta name="robots" content="noindex">'
FILES = ["MANIFEST.sha256", "README.txt", "draft.json", "preview.html", "receipts.json", "records",
         "review-status.json", "reviews.json"]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def draft(**changes):
    base = {"id": "fnd_00000000000000p1", "title": "Cedar packet draft", "slug": "cedar-packet-draft",
            "summary": "Synthetic summary.", "body_md": "Synthetic body.", "classification": "documented_fact",
            "confidence": "verified", "event_date": "2026-01-16", "agency_id": "ca-cedar-police",
            "sources": [{"sha256": sha(RECORDS[0]), "locator": "page:1"}, {"sha256": sha(RECORDS[0]), "locator": "page:2"},
                        {"sha256": sha(RECORDS[1]), "locator": "page:1"}],
            "published_at": None, "corrections": [], "state": "draft", "author": "synthetic-author",
            "limitations": ["Synthetic limits."], "counterevidence": ["None found."]}
    base.update(changes)
    return base


def trees(root):
    """public/, content/ and private/ as {relative path: (mode, bytes)} (directories included)."""
    return {p.relative_to(root).as_posix(): (os.lstat(p).st_mode, p.read_bytes() if p.is_file() else None)
            for name in ("public", "content", "private") for p in sorted((root / name).rglob("*"))}


class FindingPacketTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-finding-packet-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        (self.root / "public").mkdir()
        for data, source_ids in ((RECORDS[0], ("synthetic-a", "synthetic-b")), (RECORDS[1], ("synthetic-a",))):
            path = self.base / ("record-%s.txt" % sha(data)[:8])
            path.write_bytes(data)
            for source_id in source_ids:
                self.cli("ingest", "--directory", self.root, "--file", path, "--source-id", source_id)
        library = self.root / "content" / "sources.json"
        entries = json.loads(library.read_text(encoding="utf-8"))
        entries += [dict(entries[0], sha256=sha(data), title=f"Synthetic packet record {i}") for i, data in enumerate(RECORDS)]
        library.write_text(json.dumps(entries), encoding="utf-8")
        self.objects = self.root / "private" / "objects"
        self.ledger = self.root / "private" / "ledger.sqlite"
        self.draft = self.base / "draft.json"
        self.draft.write_text(json.dumps(draft(), indent=1), encoding="utf-8")
        self.out = self.base / "packet-x"

    def cli(self, *arguments, returncode=0):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, returncode, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        if returncode:
            self.assertTrue(result.stderr.startswith("Stopped: "), result.stderr)
        return result

    def packet(self, out=None, returncode=0):
        return self.cli("finding", "packet", self.draft, "--directory", self.root, "--out", out or self.out,
                        returncode=returncode)

    def refused(self, needle, out=None):
        """In-process refusal: the message names ``needle``; nothing at DIR, no temp folder, stores unchanged."""
        out = Path(out or self.out)
        before, siblings = trees(self.root), sorted(os.listdir(out.parent)) if out.parent.is_dir() else None
        with self.assertRaises(ValueError) as caught:
            finding_packet.build_packet(self.root, self.draft, out)
        self.assertIn(needle, str(caught.exception))
        self.assertEqual(trees(self.root), before)
        if siblings is not None:
            self.assertEqual(sorted(os.listdir(out.parent)), siblings)
        return str(caught.exception)

    def read(self, name):
        return json.loads((self.out / name).read_text(encoding="utf-8"))

    def test_packet_has_records_receipts_reviews_manifest_and_private_modes(self):
        findings.add_receipt(self.root, draft(), "factual", "approve", "Rev One", "Checked synthetic pages.")
        before = trees(self.root)
        result = self.packet()
        self.assertEqual(json.loads(result.stdout.splitlines()[0]), {"written": str(self.out), "files": 9, "records": 2})
        self.assertIn("nothing was zipped, uploaded or sent", result.stdout)
        self.assertEqual(trees(self.root), before)
        self.assertEqual(sorted(os.listdir(self.out)), FILES)
        self.assertEqual(sorted(os.listdir(self.out / "records")), sorted(sha(d) for d in RECORDS))
        for data in RECORDS:
            self.assertEqual((self.out / "records" / sha(data)).read_bytes(), (self.objects / sha(data)).read_bytes())
        self.assertEqual((self.out / "draft.json").read_bytes(), self.draft.read_bytes())
        html = (self.out / "preview.html").read_text(encoding="utf-8")
        self.assertEqual((html.count(BANNER), html.count(NOINDEX)), (1, 1))
        self.assertIn("Cedar packet draft", html)
        db = sqlite3.connect(self.ledger.as_uri() + "?mode=ro", uri=True)
        self.addCleanup(db.close)
        expected = {d: [{"receipt_id": r, "source_id": s, "received_at": t, "original_name": n} for r, s, t, n in db.execute(
            "SELECT receipt_id, source_id, received_at, original_name FROM receipts WHERE object_sha256 = ? "
            "ORDER BY received_at, receipt_id", (d,))] for d in map(sha, RECORDS)}
        self.assertEqual(self.read("receipts.json"), expected)
        self.assertEqual({r["source_id"] for r in expected[sha(RECORDS[0])]}, {"synthetic-a", "synthetic-b"})
        self.assertEqual(len(expected[sha(RECORDS[1])]), 1)
        self.assertEqual(self.read("reviews.json"), json.loads((self.root / "private" / "reviews" /
                                                                f"{draft()['id']}.json").read_text()))
        self.assertEqual(self.read("review-status.json"), findings.review_status(self.root, draft()))
        self.assertEqual(self.read("review-status.json")["receipts_for_this_content"], 1)
        self.assertIn("sha256sum -c MANIFEST.sha256", (self.out / "README.txt").read_text(encoding="utf-8"))
        lines = (self.out / "MANIFEST.sha256").read_text(encoding="ascii").splitlines()
        listed = [line.split("  ", 1)[1] for line in lines]
        files = sorted(p.relative_to(self.out).as_posix() for p in self.out.rglob("*") if p.is_file())
        self.assertEqual(listed, sorted(listed))
        self.assertEqual(listed, [f for f in files if f != "MANIFEST.sha256"])
        for line in lines:  # sha256sum -c
            digest, name = line.split("  ", 1)
            self.assertEqual(sha((self.out / name).read_bytes()), digest, name)
        for path in [self.out, *self.out.rglob("*")]:
            self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o700 if path.is_dir() else 0o600, path)
        self.assertEqual(sorted(os.listdir(self.base)), ["campaign", "draft.json", "packet-x"] +
                         sorted(f"record-{sha(d)[:8]}.txt" for d in RECORDS))

    def test_existing_out_even_empty_or_a_second_run_refused(self):
        self.out.mkdir()
        self.assertIn(f"{self.out} exists; choose a new folder", self.packet(returncode=2).stderr)
        self.assertEqual(os.listdir(self.out), [])
        self.out.rmdir()
        self.packet()
        manifest = (self.out / "MANIFEST.sha256").read_bytes()
        self.refused("exists; choose a new folder")
        self.assertEqual((self.out / "MANIFEST.sha256").read_bytes(), manifest)

    def test_out_inside_public_content_private_or_via_symlinked_parent_refused(self):
        for name in ("public", "content", "private"):
            self.refused(f"--out is inside {name}/", self.root / name / "packet-x")
            (self.root / name / "sub").mkdir()
            self.refused(f"--out is inside {name}/", self.root / name / "sub" / ".." / "packet-x")
        (self.base / "to-root").symlink_to(self.root)
        self.refused("--out is inside content/", self.base / "to-root" / "content" / "packet-x")
        for target in (self.root / "private", self.base):
            link = self.base / f"link-{target.name}"
            link.symlink_to(target)
            self.refused("must be a real folder, not a symlink", link / "packet-x")
            self.assertFalse((target / "packet-x").exists())
        self.refused("must be a real folder", self.base / "missing" / "packet-x")
        self.assertFalse(os.path.lexists(self.out))

    def test_missing_or_altered_object_refused_naming_prefix(self):
        stored = self.objects / sha(RECORDS[1])
        data = stored.read_bytes()
        stored.unlink()
        self.assertIn(f"{sha(RECORDS[1])[:12]} object_missing", self.packet(returncode=2).stderr)
        self.assertFalse(os.path.lexists(self.out))
        stored.write_bytes(data + b"altered")
        self.refused(f"{sha(RECORDS[1])[:12]} hash_mismatch")
        stored.unlink()
        stored.symlink_to(self.base / "draft.json")
        self.refused(f"{sha(RECORDS[1])[:12]} object_missing")

    def test_object_changed_while_copying_leaves_no_folder_or_temp(self):
        real = finding_packet._copy_hash
        with mock.patch.object(finding_packet, "_copy_hash", lambda source, sink=None: real(source, sink)
                               if sink is None else (real(source, sink), "0" * 64)[1]):
            self.refused("changed while copying")
        self.assertFalse(os.path.lexists(self.out))

    def test_records_over_500_mib_refused(self):
        with mock.patch.object(finding_packet, "MAX_RECORD_BYTES", len(RECORDS[0]) + len(RECORDS[1]) - 1):
            self.refused("packet too large")
        self.assertFalse(os.path.lexists(self.out))

    def test_draft_without_receipts_is_written_with_its_blockers(self):
        self.packet()
        status = self.read("review-status.json")
        self.assertEqual(status, findings.review_status(self.root, draft()))
        self.assertTrue(status["review_blockers"])
        self.assertEqual(status["receipts_for_this_content"], 0)
        self.assertEqual(self.read("reviews.json"), [])

    def test_preview_refusal_stops_with_that_refusal(self):
        document = draft(classification="not-a-class")
        self.draft.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            load_finding_preview(document, str(self.draft))
        self.assertIn(str(caught.exception), self.packet(returncode=2).stderr)
        self.assertFalse(os.path.lexists(self.out))
        self.refused(str(caught.exception))

    def test_ledger_with_wal_or_journal_sidecar_refused_and_ledger_unchanged(self):
        ledger = self.ledger.read_bytes()
        for suffix in ("wal", "journal"):
            sidecar = Path(f"{self.ledger}-{suffix}")
            sidecar.write_bytes(b"")
            self.assertIn("journal or WAL file beside it", self.packet(returncode=2).stderr)
            self.refused("journal or WAL file beside it")
            sidecar.unlink()
        self.assertEqual(self.ledger.read_bytes(), ledger)
        self.assertFalse(os.path.lexists(self.out))

    def test_checkpointed_wal_ledger_without_sidecars_read_from_snapshot_private_tree_unchanged(self):
        db = sqlite3.connect(self.ledger)
        self.assertEqual(db.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        db.close()  # last connection checkpoints and removes -wal/-shm; the header still says WAL
        self.assertEqual(self.ledger.read_bytes()[18:20], b"\2\2")
        self.assertEqual(sorted(p.name for p in self.ledger.parent.glob("ledger.sqlite*")), ["ledger.sqlite"])
        before = trees(self.root)
        finding_packet.build_packet(self.root, self.draft, self.out)
        self.assertEqual(sorted(p.name for p in self.ledger.parent.glob("ledger.sqlite*")), ["ledger.sqlite"])
        self.assertEqual(trees(self.root), before)  # no -wal/-shm created in private/, no byte changed
        self.assertEqual(self.read("receipts.json")[sha(RECORDS[1])][0]["source_id"], "synthetic-a")
        shutil.rmtree(self.out)
        before = trees(self.root)
        self.packet()  # the CLI path too
        self.assertEqual(trees(self.root), before)

    def test_packet_oversize_refuses_before_staging(self):
        from campaign_tool import ledger_list
        size = self.ledger.stat().st_size
        with mock.patch.object(ledger_list, "MAX_LEDGER_BYTES", size - 1):
            self.refused(ledger_list.LEDGER_SIZE_ERROR)  # no target, no staging folder, stores and inputs unchanged
        self.assertFalse(os.path.lexists(self.out))
        with open(self.ledger, "r+b") as handle:
            handle.truncate(ledger_list.MAX_LEDGER_BYTES + 1)  # sparse: the production limit plus one byte
        stamp, siblings = self.ledger.stat().st_mtime_ns, sorted(os.listdir(self.base))
        self.assertIn(ledger_list.LEDGER_SIZE_ERROR, self.packet(returncode=2).stderr)
        self.assertEqual((self.ledger.stat().st_size, self.ledger.stat().st_mtime_ns, sorted(os.listdir(self.base))),
                         (ledger_list.MAX_LEDGER_BYTES + 1, stamp, siblings))
        self.assertFalse(os.path.lexists(self.out))

    def test_no_network_imports(self):
        tree = ast.parse((REPO_ROOT / "campaign_tool" / "finding_packet.py").read_text(encoding="utf-8"))
        names = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        names |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        self.assertFalse(names & {"socket", "ssl", "http", "urllib", "requests", "smtplib", "ftplib", "zipfile", "tarfile"})


if __name__ == "__main__":
    unittest.main()
