"""FA49: truncated tesseract TSV rows must not abort OCR page processing.

Synthetic only. Every OCR child (pdftoppm/tesseract) is an injected double, so no
installed OCR executable, model service or network endpoint is used. Fixtures are
generated under a fresh temporary root per test. Assertions call
``ocr._confidence`` through the module attribute at call time so a preserved
baseline can be substituted by the acceptance negative control.

Coverage map:
  A1  PositiveConfidenceTests   ordinary, quoted and tab-bearing fields, rounding,
                                mixed valid/truncated rows, empty-text rows ignored
  A2  MalformedConfidenceTests  header-only, absent text column, truncated last row,
                                missing conf field, null-like/non-string cells,
                                nonnumeric/out-of-domain/NaN/infinity confidences
  A3  ConsumerTests             real extract_image_only_pages with an injected
                                truncated confidence TSV, peer page, replay/cache
"""
import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from pypdf import PdfWriter

from campaign_tool.records.extract import ocr

HEADER = ("level", "page_num", "block_num", "par_num", "line_num", "word_num",
          "left", "top", "width", "height", "conf", "text")
SYNTHETIC_BIN = "/synthetic/bin/"
TOOLS = {name: {"available": True, "path": SYNTHETIC_BIN + name, "version": name + " 0.0-fa49"}
         for name in ocr.OCR_TOOLS}
TOOLS["pypdf"] = {"available": True, "version": "0.0-fa49"}


def tsv(*rows):
    """Tesseract-shaped TSV: header then rows, each given as already-joined text."""
    return "\t".join(HEADER) + "\n" + "".join(row + "\n" for row in rows)


def structural(level, word=0):
    """Non-word layout rows tesseract emits with conf -1 and an empty text cell."""
    return "\t".join([str(level), "1", "1", "1", "1", str(word), "0", "0", "100", "100", "-1", ""])


def word(conf, text, number=1):
    return "\t".join(["5", "1", "1", "1", "1", str(number), "10", "10", "40", "12", str(conf), text])


def truncated(conf, number=9):
    """A word row cut off after the conf cell: the text cell is absent entirely."""
    return "\t".join(["5", "1", "1", "1", "1", str(number), "60", "10", "40", "12", str(conf)])


def no_conf(number=9):
    """A word row cut off before the conf cell: conf and text are both absent."""
    return "\t".join(["5", "1", "1", "1", "1", str(number), "60", "10", "40", "12"])


def confidence(text):
    return ocr._confidence(text)


class PositiveConfidenceTests(unittest.TestCase):
    """A1: usable rows keep the existing mean/rounding behaviour."""

    def test_a1_two_valid_word_rows_mean(self):
        self.assertEqual(confidence(tsv(word(90, "alpha", 1), word(80, "beta", 2))), 85.0)

    def test_a1_tesseract_layout_rows_ignored_and_rounding_preserved(self):
        text = tsv(structural(1), structural(2), structural(3), structural(4),
                   word(90, "alpha", 1), word(80, "beta", 2), word(81, "gamma", 3))
        self.assertEqual(confidence(text), 83.67)
        self.assertEqual(confidence(tsv(word(1, "a", 1), word(2, "b", 2), word(2, "c", 3))), 1.67)
        self.assertEqual(confidence(tsv(word("96.123456", "precise"))), 96.12)

    def test_a1_quoted_and_tab_bearing_text_fields(self):
        quoted = tsv(word(70, '"tab\tinside"', 1), word(90, '"say ""hi"""', 2))
        rows = list(csv.DictReader(io.StringIO(quoted), delimiter="\t"))
        self.assertEqual([row["text"] for row in rows], ["tab\tinside", 'say "hi"'])
        self.assertEqual(confidence(quoted), 80.0)

    def test_a1_mixed_valid_and_truncated_rows_average_valid_rows_only(self):
        text = tsv(structural(4), word(90, "alpha", 1), truncated(5, 2), word(100, "beta", 3),
                   no_conf(4), truncated(0, 5))
        self.assertEqual(confidence(text), 95.0)

    def test_a1_empty_and_blank_text_rows_remain_ignored(self):
        text = tsv(word(10, "", 1), word(20, "   ", 2), word(30, "\u00a0", 3), word(60, "kept", 4))
        self.assertEqual(confidence(text), 60.0)

    def test_a1_domain_boundaries_zero_and_hundred_kept(self):
        self.assertEqual(confidence(tsv(word(0, "low", 1), word(100, "high", 2))), 50.0)

    def test_a1_row_longer_than_header_keeps_its_text_cell(self):
        self.assertEqual(confidence(tsv(word(40, "extra", 1) + "\tspill\tover")), 40.0)


class MalformedConfidenceTests(unittest.TestCase):
    """A2: malformed rows are unusable evidence; no AttributeError leaks."""

    def assertUnavailable(self, text):
        result = confidence(text)
        self.assertIsNone(result)
        self.assertIsNot(result, 0)

    def test_a2_empty_and_header_only_input(self):
        self.assertUnavailable("")
        self.assertUnavailable(tsv())

    def test_a2_absent_text_column(self):
        header = "\t".join(HEADER[:-1])
        self.assertUnavailable(header + "\n" + "\t".join(["5", "1", "1", "1", "1", "1", "1", "1", "1", "1", "88"]) + "\n")

    def test_a2_truncated_last_row_has_no_text_cell(self):
        self.assertUnavailable(tsv(structural(4), truncated(88)))

    def test_a2_truncated_last_row_without_newline(self):
        self.assertUnavailable(tsv(structural(4)) + truncated(88))
        self.assertEqual(confidence(tsv(word(70, "kept")) + truncated(10)), 70.0)

    def test_a2_missing_conf_field(self):
        self.assertUnavailable(tsv(no_conf()))
        self.assertUnavailable(tsv("5\t1"))

    def test_a2_null_like_and_non_string_text_cells(self):
        def reader(rows):
            # Mimic csv.DictReader: _confidence first validates the header (fieldnames),
            # then iterates rows whose cells are the non-string values under test.
            class Reader:
                fieldnames = list(HEADER)

                def __init__(self, *args, **kwargs):
                    self._rows = iter(rows)

                def __iter__(self):
                    return self._rows
            return Reader
        rows = [{"text": None, "conf": "90"}, {"text": 7, "conf": "90"}, {"text": b"bytes", "conf": "90"},
                {"text": ["list"], "conf": "90"}, {"conf": "90"}]
        with mock.patch.object(ocr.csv, "DictReader", reader(rows)):
            self.assertUnavailable("ignored")
        with mock.patch.object(ocr.csv, "DictReader", reader(rows + [{"text": "ok", "conf": "60"}])):
            self.assertEqual(confidence("ignored"), 60.0)

    def test_a2_nonnumeric_confidence(self):
        self.assertUnavailable(tsv(word("high", "alpha", 1), word("", "beta", 2), word("9O", "gamma", 3)))

    def test_a2_blank_text_only(self):
        self.assertUnavailable(tsv(word(90, "", 1), word(90, " ", 2), word(90, "  ", 3)))

    def test_a2_out_of_domain_numbers(self):
        self.assertUnavailable(tsv(word(-1, "a", 1), word("100.001", "b", 2), word(101, "c", 3),
                                   word("-0.5", "d", 4), word("1e3", "e", 5)))

    def test_a2_nan_and_infinity(self):
        self.assertUnavailable(tsv(word("nan", "a", 1), word("NaN", "b", 2), word("inf", "c", 3),
                                   word("-inf", "d", 4), word("Infinity", "e", 5)))

    def test_a2_all_unusable_mix_returns_none_not_zero(self):
        self.assertUnavailable(tsv(structural(1), truncated(95, 1), no_conf(2), word("nan", "x", 3),
                                   word(90, "", 4), word(150, "y", 5)))


def two_page_pdf():
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_blank_page(width=72, height=72)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


class RoutingRunner:
    """Injected OCR double: writes a per-page confidence TSV chosen by page directory."""

    def __init__(self, tsv_by_page):
        self.tsv_by_page = tsv_by_page
        self.calls = []

    def __call__(self, command, **kwargs):
        if not str(command[0]).startswith(SYNTHETIC_BIN):
            raise AssertionError("non-synthetic OCR executable requested")
        name = Path(command[0]).name
        self.calls.append(name)
        if name == "pdftoppm":
            Path(command[-1] + ".png").write_bytes(b"synthetic raster")
        elif name == "tesseract":
            page_dir = Path(command[1]).parent.parent.name
            Path(command[2] + ".txt").write_text("synthetic text\n")
            Path(command[2] + ".tsv").write_text(self.tsv_by_page[page_dir])
        else:
            raise AssertionError("unexpected OCR child " + name)
        return subprocess.CompletedProcess(command, 0, "", "")


TRUNCATED_PAGE = tsv(structural(1), structural(4), truncated(93))
PEER_PAGE = tsv(structural(4), word(90, "alpha", 1), word(100, "beta", 2), truncated(5, 3))


class ConsumerTests(unittest.TestCase):
    """A3: the real extraction API isolates the truncated page and keeps its peer."""

    def fresh_root(self):
        temp = tempfile.TemporaryDirectory(prefix="fa49-ocr-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "blobs").mkdir(mode=0o700)
        raw = two_page_pdf()
        sha = hashlib.sha256(raw).hexdigest()
        (root / "blobs" / sha).write_bytes(raw)
        return root, sha, root / "private-ocr"

    def extract(self, root, sha, output, runner):
        return ocr.extract_image_only_pages(root, sha, output, tool_signature="fa49-stub",
                                            tools=TOOLS, runner=runner)

    def runner(self):
        return RoutingRunner({"page-000001": TRUNCATED_PAGE, "page-000002": PEER_PAGE})

    def test_a3_truncated_page_blocked_while_peer_page_unreviewed(self):
        root, sha, output = self.fresh_root()
        fake = self.runner()
        receipts = self.extract(root, sha, output, fake)
        self.assertEqual([r["locator"]["page"] for r in receipts], [1, 2])
        blocked, peer = receipts
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["blocked_reason"], "confidence_unavailable")
        self.assertIsNone(blocked["confidence"])
        self.assertFalse(blocked["visual_check_required"])
        self.assertEqual(set(blocked["artifact_sha256"]), set(ocr.ARTIFACTS))
        final = output / sha / "page-000001" / blocked["receipt_id"]
        self.assertEqual((final / "confidence.tsv").read_text(), TRUNCATED_PAGE)
        index = json.loads((output / "indexes" / "blocked" / (blocked["receipt_id"] + ".json")).read_text())
        self.assertEqual((index["blocked_reason"], index["confidence"]), ("confidence_unavailable", None))
        current = json.loads((output / sha / "page-000001" / "current.json").read_text())
        self.assertEqual((current["status"], current["blocked_reason"], current["receipt_id"]),
                         ("blocked", "confidence_unavailable", blocked["receipt_id"]))
        self.assertEqual(peer["status"], "ocr_text_unreviewed")
        self.assertIsNone(peer["blocked_reason"])
        self.assertEqual(peer["confidence"], 95.0)
        self.assertFalse(peer["visual_check_required"])
        loaded = ocr.load_page_receipt(output, sha, 2, peer["receipt_id"])
        self.assertEqual(loaded["receipt"], peer)
        self.assertEqual(loaded["sidecar"], b"synthetic text\n")
        for receipt in receipts:
            self.assertEqual(receipt["review_status"], "not_reviewed")
            self.assertEqual(receipt["visual_check_status"], "not_done")
            self.assertEqual(receipt["fidelity_status"], "not_checked")
            self.assertIn(receipt["status"], ocr.STATES)
        self.assertFalse((output / "indexes" / "visual-check" / (peer["receipt_id"] + ".json")).exists())
        leftovers = [p.name for page in ("page-000001", "page-000002")
                     for p in (output / sha / page).iterdir() if p.name.startswith(".attempt-")]
        self.assertEqual(leftovers, [])
        self.assertEqual(fake.calls, ["pdftoppm", "tesseract", "pdftoppm", "tesseract"])

    def test_a3_replay_preserves_cache_and_fresh_root_repeats(self):
        root, sha, output = self.fresh_root()
        fake = self.runner()
        first = self.extract(root, sha, output, fake)
        receipt_bytes = (output / sha / "page-000001" / first[0]["receipt_id"] / "receipt.json").read_bytes()
        self.assertEqual(self.extract(root, sha, output, fake), first)
        self.assertEqual(len(fake.calls), 4)
        self.assertEqual((output / sha / "page-000001" / first[0]["receipt_id"] / "receipt.json").read_bytes(),
                         receipt_bytes)
        other_root, other_sha, other_output = self.fresh_root()
        self.assertNotEqual(other_root, root)
        other = self.runner()
        second = self.extract(other_root, other_sha, other_output, other)
        self.assertEqual(len(other.calls), 4)
        project = lambda rs: [(r["receipt_id"], r["status"], r["blocked_reason"], r["confidence"]) for r in rs]
        self.assertEqual(project(second), project(first))
        self.assertEqual(project(first)[0][1:], ("blocked", "confidence_unavailable", None))


if __name__ == "__main__":
    unittest.main()
