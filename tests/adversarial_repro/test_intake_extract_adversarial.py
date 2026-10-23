"""Adversarial reproductions for the intake and extract stages (RFC822 inventory, native_extract queue/outcome files).

Synthetic mail and fake pipelines only.
"""
import json
from pathlib import Path
import random
import shutil
import tempfile
import unittest

from campaign_tool.records import native_extract, ocr_stage
from campaign_tool.records.intake import rfc822_adapter, rfc822_inventory
from tests.adversarial_repro._support import requires_repro

MAIL = (b"From: a@example.invalid\r\nTo: b@example.invalid\r\nSubject: synthetic\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=\"B\"\r\n\r\n--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nhello\r\n"
        b"--B\r\nContent-Type: application/pdf; name=\"x.pdf\"\r\nContent-Disposition: attachment; filename=\"x.pdf\"\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\nJVBERi0=\r\n--B--\r\n")


@requires_repro
class OutcomeFileNames(unittest.TestCase):
    """C1: two different run ids can map to the same outcome file, so the later run replaces the earlier one."""

    def test_distinct_run_ids_get_distinct_outcome_files(self):
        root = Path(tempfile.mkdtemp(prefix="adv-extract-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "runs").mkdir()
        native_extract.write_private_list(root, "2026-10-22/run-1", [{"original_sha256": "a" * 64, "outcome": "EXTRACTED"}])
        native_extract.write_private_list(root, "2026-10-22run-1", [{"original_sha256": "b" * 64, "outcome": "CORRUPT_DOCUMENT"}])
        self.assertEqual(len(list((root / "runs").glob("extract-*.json"))), 2,
                         "the second run's outcome list silently replaced the first run's")


class FakePipeline:
    class root:
        path = "/srv/records-root"

    def __init__(self, originals):
        self.originals = originals

    def _original(self, subject):
        return {"storage_path": self.originals[subject]}

    def _form(self, subject, original):
        return "pdf"


@requires_repro
class QueueContract(unittest.TestCase):
    """Behaviour that currently HOLDS: every queue line native_extract writes is accepted by ocr_stage."""

    def test_odd_but_legal_storage_paths_round_trip_through_the_ocr_stage_parser(self):
        names = ["a b.pdf", "café.pdf", "café.pdf", "q\"uote.pdf", "back\\slash.pdf", "per%cent.pdf",
                 "nl\nname.pdf", "‮txt.pdf", "sub/dir/x.pdf", "..dots.pdf", "x" * 200 + ".pdf"]
        subjects = {format(index, "064x"): "/srv/records-root/blobs/" + name for index, name in enumerate(names)}
        documents = [{"outcome": native_extract.AWAITING_OCR, "original_sha256": sha, "form": "pdf"} for sha in subjects]
        lines, images = native_extract.awaiting_ocr_queue(FakePipeline(subjects), documents)
        self.assertEqual(len(lines), len(names))
        for line in lines:
            item = ocr_stage.parse_queue_line(json.dumps(line, sort_keys=True), Path("/srv/records-root"))
            self.assertEqual(item.path, line["path"])

    def test_queue_is_sorted_and_non_awaiting_documents_are_left_out(self):
        subjects = {"b" * 64: "/srv/records-root/b.pdf", "a" * 64: "/srv/records-root/a.pdf", "c" * 64: "/srv/records-root/c.pdf"}
        documents = [{"outcome": native_extract.AWAITING_OCR, "original_sha256": "b" * 64, "form": "pdf"},
                     {"outcome": native_extract.EXTRACTED, "original_sha256": "c" * 64, "form": "pdf"},
                     {"outcome": native_extract.AWAITING_OCR, "original_sha256": "a" * 64, "form": "pdf"}]
        lines, _ = native_extract.awaiting_ocr_queue(FakePipeline(subjects), documents)
        self.assertEqual([line["sha256"] for line in lines], ["a" * 64, "b" * 64])


@requires_repro
class MimeFuzz(unittest.TestCase):
    """Behaviour that currently HOLDS: mutated mail is accepted or refused with a fixed code, never a stray exception."""

    def test_mutated_messages_only_raise_fixed_codes(self):
        rng = random.Random(20261022)
        for _ in range(1500):
            data = bytearray(MAIL)
            for _ in range(rng.randint(1, 6)):
                position = rng.randrange(len(data))
                kind = rng.choice("fid")
                if kind == "f":
                    data[position] = rng.randrange(256)
                elif kind == "i":
                    data[position:position] = bytes(rng.randrange(256) for _ in range(rng.randint(1, 5)))
                else:
                    del data[position:position + rng.randint(1, 8)]
            try:
                rfc822_adapter.prepare_rfc822_intake(bytes(data))
            except (rfc822_inventory.RFC822InventoryError, rfc822_adapter.RFC822AdapterError) as error:
                self.assertIn(str(error), rfc822_adapter.RFC822_FAILURE_CODES)

    def test_odd_encodings_are_refused_not_guessed(self):
        cases = {
            "bogus charset in filename": MAIL.replace(b'filename="x.pdf"', b"filename*=bogus-cs''%FF%FE.pdf"),
            "invalid encoded word": MAIL.replace(b'name="x.pdf"', b'name="=?x-bogus?b?!!!!?="'),
            "NUL in header": MAIL.replace(b"Subject: synthetic", b"Subject: syn\x00thetic"),
            "8-bit header": MAIL.replace(b"Subject: synthetic", "Subject: café".encode()),
            "truncated": MAIL[: len(MAIL) // 2],
            "bare CR": MAIL.replace(b"\r\n", b"\r"),
        }
        for label, data in cases.items():
            with self.subTest(label), self.assertRaises(rfc822_inventory.RFC822InventoryError):
                rfc822_adapter.prepare_rfc822_intake(data)


if __name__ == "__main__":
    unittest.main()
