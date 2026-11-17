import hashlib
import io
import json
import os
import socket
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from campaign_tool.records.agency_run import pages
from tests.records.ocr_fixtures import mixed_pdf

H = "path\tlocation\tkind\tdate_pacific\trequest_ref\tsha256\tpdf_pages\ttext_layer\trole\n"


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root, self.out, self.ocr = self.tmp / "root", self.tmp / "out", self.tmp / "ocr"
        self.root.mkdir()
        self.ocr.mkdir()
        self.a, self.b = mixed_pdf("Synthetic Alpha text 26-133"), mixed_pdf("Synthetic Beta text 26-134")
        (self.root / "a.pdf").write_bytes(self.a)
        (self.root / "b.pdf").write_bytes(self.b)
        (self.root / "n.md").write_bytes(b"notes\n")
        self.rows = [["a.pdf", "x", "pdf", "2026-11-01", "26-133", sha(self.a), "2", "native", "original"],
                     ["b.pdf", "x", "pdf", "2026-11-02", "26-134", sha(self.b), "2", "native", "original"]]

    def run_tool(self, rows=None, header=H, out=None, extra=()):
        index = self.tmp / "evidence-index.tsv"
        index.write_text(header + "".join("\t".join(r) + "\n" for r in (self.rows if rows is None else rows)))
        argv = ["--index", str(index), "--root", str(self.root), "--out", str(out or self.out), "--ocr-dir", str(self.ocr), *extra]
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            code = pages.main(argv)
        return code, o.getvalue(), e.getvalue()

    def read(self, name):
        return (self.out / name).read_text()


class AgencyPagesTests(Base):
    def test_golden_pages_and_inventory(self):
        code, out, _ = self.run_tool()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"ok": True, "documents": 2, "pages_written": 2, "no_text_pages": 2, "skipped_not_pdf": 0})
        self.assertEqual(self.read("pages/D1_p001.txt"), "=== D1 p.1 ===\nSynthetic Alpha text 26-133\n")
        self.assertEqual(self.read("pages/D2_p001.txt"), "=== D2 p.1 ===\nSynthetic Beta text 26-134\n")
        self.assertEqual(sorted(os.listdir(self.out / "pages")), ["D1_p001.txt", "D2_p001.txt"])
        self.assertEqual(self.read("inventory.tsv"), (
            "doc_id\ttitle\treceived_date\tsource\tpages\tsha256\ttext_layer\ttier\trequest_ref\tnotes\n"
            "D1\ta.pdf\t2026-11-01\ta.pdf\t2\t%s\tnative\tunclassified\t26-133\tno text layer: p.2\n"
            "D2\tb.pdf\t2026-11-02\tb.pdf\t2\t%s\tnative\tunclassified\t26-134\tno text layer: p.2\n") % (sha(self.a), sha(self.b)))
        self.assertEqual(os.stat(self.out / "inventory.tsv").st_mode & 0o777, 0o600)

    def test_ocr_text_fills_image_only_page(self):
        (self.ocr / (sha(self.a) + ".txt")).write_text("=== Page 1 ===\nocr one should not win\n--- Page 2 ---\nScanned page two words\n")
        self.assertEqual(self.run_tool()[0], 0)
        self.assertEqual(self.read("pages/D1_p002.txt"), "=== D1 p.2 ===\nScanned page two words\n")
        self.assertIn("Synthetic Alpha", self.read("pages/D1_p001.txt"))
        self.assertIn("\tnative+ocr\t", self.read("inventory.tsv"))

    def test_padded_counts_and_markers_remain_valid(self):
        self.rows[0][6] = "0" * 5000 + "2"
        (self.ocr / (sha(self.a) + ".txt")).write_text("Page " + "٠" * 5000 + "2\nScanned page two words\n")
        self.assertEqual(self.run_tool()[0], 0)
        self.assertEqual(self.read("pages/D1_p002.txt"), "=== D1 p.2 ===\nScanned page two words\n")

    def test_hash_mismatch_refuses_and_writes_nothing(self):
        (self.root / "a.pdf").write_bytes(self.a + b"\n")
        code, _, err = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("agency-pages: hash_mismatch:D1", err)
        self.assertFalse(self.out.exists())

    def test_page_count_and_hash_unknown(self):
        for cell, value, expect in ((6, "3", "page_count_mismatch:D1"), (6, "9" * 5000, "page_count_mismatch:D1"),
                                    (6, "unknown - not held", "page_count_unknown:D1"),
                                    (5, "unknown - too large", "hash_unknown:D1")):
            rows = [list(r) for r in self.rows]
            rows[0][cell] = value
            code, _, err = self.run_tool(rows)
            self.assertEqual(code, 1)
            self.assertIn(expect, err)
        self.assertFalse(self.out.exists())

    def test_image_only_document_lists_no_text_layer(self):
        from pypdf import PdfWriter
        w, buf = PdfWriter(), io.BytesIO()
        w.add_blank_page(72, 72)
        w.add_blank_page(72, 72)
        w.write(buf)
        (self.root / "a.pdf").write_bytes(buf.getvalue())
        self.rows[0][5] = sha(buf.getvalue())
        self.assertEqual(self.run_tool([self.rows[0]])[0], 0)
        self.assertEqual(os.listdir(self.out / "pages"), [])
        line = self.read("inventory.tsv").splitlines()[1].split("\t")
        self.assertEqual((line[6], line[9]), ("none", "no text layer: p.1, p.2"))

    def test_ocr_marker_errors(self):
        for text, expect in (("no markers here\n", "ocr_markers_unrecognised:D1"), ("Page 5\nx\n", "ocr_page_out_of_range:D1"),
                             ("Page " + "9" * 5000 + "\nx\n", "ocr_page_out_of_range:D1"),
                             ("Page 2\nx\nPage 2\ny\n", "ocr_page_duplicate:D1")):
            (self.ocr / (sha(self.a) + ".txt")).write_text(text)
            code, _, err = self.run_tool()
            self.assertEqual(code, 1)
            self.assertIn(expect, err)
        self.assertFalse(self.out.exists())

    def test_invalid_present_ocr_file_refuses(self):
        target = self.ocr / (sha(self.a) + ".txt")
        target.write_text("Page 2\nx\n")
        with mock.patch.object(pages, "MAX_OCR", 3):
            code, _, err = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("ocr_invalid:D1", err)
        target.unlink()
        (self.tmp / "real.txt").write_text("Page 2\nx\n")
        target.symlink_to(self.tmp / "real.txt")
        code, _, err = self.run_tool()
        self.assertEqual((code, "ocr_invalid:D1" in err), (1, True))
        self.assertFalse(self.out.exists())

    def test_page_extraction_error_refuses(self):
        from pypdf import PageObject
        with mock.patch.object(PageObject, "extract_text", side_effect=ValueError("boom")):
            code, _, err = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("page_extract_failed:D1:p.1", err)
        self.assertFalse(self.out.exists())

    def test_never_runs_ocr_or_subprocess(self):
        boom = mock.Mock(side_effect=AssertionError("forbidden"))
        with mock.patch.object(subprocess, "run", boom), mock.patch.object(subprocess, "Popen", boom), \
                mock.patch.object(socket, "socket", boom):
            self.assertEqual(self.run_tool()[0], 0)

    def test_path_and_out_guards(self):
        rows = [list(r) for r in self.rows]
        rows[0][0] = "../x.pdf"
        self.assertIn("path_invalid:D1", self.run_tool(rows)[2])
        (self.tmp / "real.pdf").write_bytes(self.a)
        os.symlink(self.tmp / "real.pdf", self.root / "link.pdf")
        rows[0][0] = "link.pdf"
        self.assertIn("path_invalid:D1", self.run_tool(rows)[2])
        self.assertIn("out_inside_root", self.run_tool(out=self.root / "o")[2])
        self.out.mkdir()
        (self.out / "x").write_text("x")
        self.assertIn("out_not_empty", self.run_tool()[2])
        self.assertIn("index_invalid", self.run_tool(out=self.tmp / "o2", header=H.replace("role", "kind2"))[2])

    def test_non_pdf_and_non_original_rows_skipped(self):
        rows = self.rows[:1] + [["n.md", "x", "md", "", "", sha(b"notes\n"), "-", "-", "original"],
                                ["b.pdf", "x", "pdf", "", "", sha(self.b), "2", "native", "lead"]]
        code, out, _ = self.run_tool(rows)
        self.assertEqual((code, json.loads(out)["skipped_not_pdf"], json.loads(out)["documents"]), (0, 2, 1))
        self.assertEqual(sorted(os.listdir(self.out / "pages")), ["D1_p001.txt"])

    def test_source_files_unchanged(self):
        ocr = self.ocr / (sha(self.a) + ".txt")
        ocr.write_text("Page 2\nScanned words\n")
        paths = [self.root / "a.pdf", self.root / "b.pdf", ocr]
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
        self.assertEqual(self.run_tool()[0], 0)
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths])


if __name__ == "__main__":
    unittest.main()
