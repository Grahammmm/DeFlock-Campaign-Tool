import csv
import hashlib
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from campaign_tool.records.agency_run import evidence_index as ei
from tests.records.ocr_fixtures import mixed_pdf

H = "path\tlocation\tkind\tdate_pacific\trequest_ref\tsha256\tpdf_pages\ttext_layer\trole\n"
MSG = ["folder", "uidvalidity", "uid", "internaldate", "Message-ID", "Date", "From", "To", "Cc", "Subject", "eml_path", "sha256", "attachments", "receipt"]
ATT = ["folder", "uid", "original_filename", "sha256", "bytes", "path"]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_csv(path, header, rows):
    with open(path, "w", newline="") as handle:
        w = csv.writer(handle)
        w.writerow(header)
        w.writerows(rows)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "root"
        self.out = self.tmp / "out"
        self.aliases = self.tmp / "aliases.txt"
        self.aliases.write_text("# agency\nPismo Test PD\npismo-test\n26-901\nzzz-nothing\n")
        self.files = {
            "received/website/pismo-test-flock-contract.pdf": mixed_pdf("Synthetic Pismo Test text"),
            "prior/NOTES-pismo-test.md": b"notes\n",
            "third-party/other-county-pismo-test.pdf": b"%PDF-broken",
            "mail-history/eml/a.eml": b"mail a\n",
            "mail-history/att/26-901-reply.pdf": mixed_pdf("Attachment text for 26-901"),
            "unrelated/other.md": b"x\n",
        }
        for rel, data in self.files.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        mh = self.root / "mail-history"
        write_csv(mh / "MESSAGE-INDEX.csv", MSG, [
            ["INBOX", "1", "1", "02-Nov-2026 00:20:00 +0000", "<a>", "", "clerk@pismo-test.example", "me", "", "Re 26-901", "mail-history/eml/a.eml", "x", "1", ""],
            ["INBOX", "1", "2", "02-Nov-2026 18:59:00 +0000", "<b>", "", "me", "records@pismo_test_pd.example", "", "Request", "mail-history/eml/gone.eml", "ab" * 32, "0", ""],
            ["INBOX", "1", "3", "02-Nov-2026 18:59:00 +0000", "<c>", "", "x@other.example", "me", "", "Other agency", "mail-history/eml/c.eml", "cc", "0", ""],
        ])
        write_csv(mh / "ATTACHMENT-INDEX.csv", ATT, [
            ["INBOX", "1", "reply.pdf", "x", "9", "mail-history/att/26-901-reply.pdf"]])

    def run_tool(self, aliases=None, out=None):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = ei.main([str(aliases or self.aliases), "--root", str(self.root), "--out", str(out or self.out)])
        return code, err.getvalue().strip()

    def tsv(self):
        return (self.out / "evidence-index.tsv").read_text()


class EvidenceIndexTests(Base):
    def test_golden_tsv_and_roles(self):
        self.assertEqual(self.run_tool(), (0, ""))
        f = self.files
        expected = H + "".join("\t".join(r) + "\n" for r in [
            ["mail-history/att/26-901-reply.pdf", "records-ro", "mail", "2026-11-01", "26-901", sha(f["mail-history/att/26-901-reply.pdf"]), "2", "native", "original"],
            ["mail-history/eml/a.eml", "records-ro", "mail", "2026-11-01", "26-901", sha(f["mail-history/eml/a.eml"]), "none", "none", "original"],
            ["mail-history/eml/gone.eml", "index-only", "mail", "2026-11-02", "", "ab" * 32, "none", "none", "original"],
            ["prior/NOTES-pismo-test.md", "records-ro", "prior-analysis", "", "", sha(f["prior/NOTES-pismo-test.md"]), "none", "none", "lead"],
            ["received/website/pismo-test-flock-contract.pdf", "records-ro", "portal-file", "", "", "%s" % sha(f["received/website/pismo-test-flock-contract.pdf"]), "2", "native", "original"],
            ["third-party/other-county-pismo-test.pdf", "records-ro", "third-party", "", "", sha(f["third-party/other-county-pismo-test.pdf"]), "unknown", "unknown", "third-party"],
        ])
        self.assertEqual(self.tsv(), expected)
        md = (self.out / "EVIDENCE-INDEX.md").read_text()
        self.assertIn("alias: zzz-nothing - no files", md)
        self.assertIn("alias: 26-901 - 2 files", md)
        self.assertIn("Root: root", md)
        self.assertNotIn(str(self.root), md)
        for name in ("evidence-index.tsv", "EVIDENCE-INDEX.md"):
            self.assertEqual((self.out / name).stat().st_mode & 0o777, 0o600)

    def test_pdf_facts(self):
        d = self.tmp
        (d / "a.pdf").write_bytes(mixed_pdf("   "))
        (d / "b.md").write_bytes(b"hi")
        self.assertEqual(ei.pdf_facts(str(d / "a.pdf"), 1), ("2", "none"))
        self.assertEqual(ei.pdf_facts(str(d / "b.md"), 1), ("none", "none"))

    def test_alias_key_and_normalisation_match_once(self):
        (self.root / "grover_beach-notes.md").write_bytes(b"g")
        self.aliases.write_text("grover-beach\nGrover Beach\ngrover_beach\n")
        self.assertEqual(self.run_tool()[0], 0)
        self.assertEqual(self.tsv().count("grover_beach-notes.md"), 1)
        self.assertIn("alias: Grover Beach - 1 files", (self.out / "EVIDENCE-INDEX.md").read_text())

    def test_angle_brackets_replaced(self):
        (self.root / "pismo-test <x>.md").write_bytes(b"g")
        self.assertEqual(self.run_tool()[0], 0)
        self.assertIn("pismo-test _x_.md", self.tsv())
        self.assertNotIn("<", self.tsv())

    def test_no_file_outside_root_is_read(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "canary.md").write_bytes(b"CANARY-BYTES")
        os.symlink(outside / "canary.md", self.root / "pismo-test-link.md")
        write_csv(self.root / "mail-history/MESSAGE-INDEX.csv", MSG, [
            ["INBOX", "1", "9", "", "<z>", "", "pismo test pd", "", "", "s", "../outside/canary.md", "x", "0", ""]])
        opened = []
        real_open = open

        def spy(path, *a, **k):
            opened.append(os.path.realpath(path))
            return real_open(path, *a, **k)

        with mock.patch("builtins.open", spy):
            self.assertEqual(self.run_tool()[0], 0)
        root = os.path.realpath(self.root)
        self.assertTrue([p for p in opened if p.startswith(root)])
        self.assertFalse([p for p in opened if str(outside) in p])
        self.assertNotIn(sha(b"CANARY-BYTES"), self.tsv())
        md = (self.out / "EVIDENCE-INDEX.md").read_text()
        self.assertIn("symlinks skipped: 1", md)
        self.assertIn("refused paths: 1", md)

    def test_mail_paths_with_symlinked_parent_are_refused(self):
        os.symlink(self.root / "mail-history/eml", self.root / "link")
        write_csv(self.root / "mail-history/MESSAGE-INDEX.csv", MSG, [
            ["INBOX", "1", "9", "", "<z>", "", "pismo test pd", "", "", "s", "link/a.eml", "x", "0", ""]])
        write_csv(self.root / "mail-history/ATTACHMENT-INDEX.csv", ATT, [
            ["INBOX", "9", "reply", "x", "9", "link/a.eml"]])
        self.assertEqual(self.run_tool()[0], 0)
        self.assertNotIn("link/a.eml", self.tsv())
        self.assertIn("refused paths: 2", (self.out / "EVIDENCE-INDEX.md").read_text())

    def test_symlinked_mail_history_index_is_not_read(self):
        (self.root / "mail-history").rename(self.root / "stored-mail")
        os.symlink(self.root / "stored-mail", self.root / "mail-history")
        self.assertEqual(self.run_tool()[0], 0)
        self.assertNotIn("\tmail\t", self.tsv())

    def test_read_only_and_guards(self):
        def snap():
            return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        before = snap()
        self.assertEqual(self.run_tool()[0], 0)
        self.assertEqual(snap(), before)
        self.assertEqual(self.run_tool(), (2, "out_not_empty"))
        self.assertEqual(self.run_tool(out=self.root / "o"), (2, "out_inside_root"))
        self.aliases.write_text("ab\n")
        self.assertEqual(self.run_tool(out=self.tmp / "o2"), (2, "alias_invalid"))
        self.aliases.write_text("# only\n\n")
        self.assertEqual(self.run_tool(out=self.tmp / "o3"), (2, "alias_file_empty"))

    def test_date_conversion(self):
        self.assertEqual(ei.pacific_date("02-Nov-2026 00:20:00 +0000"), "2026-11-01")
        self.assertEqual(ei.pacific_date("02-Nov-2026 18:59:00 +0000"), "2026-11-02")
        self.assertEqual(ei.pacific_date("garbage"), "")


if __name__ == "__main__":
    unittest.main()
