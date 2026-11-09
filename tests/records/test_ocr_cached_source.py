"""RF1415-2: a published OCR result is not reused for a changed original.

Synthetic PDFs and fake backends only; no real tool or model is called.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import shutil
import unittest
from unittest import mock

from campaign_tool.records import ocr_stage as stage
from tests.records import test_ocr_stage as base
from tests.records.ocr_stage_fixtures import FakeTools, FakeVision, noise_page, scanned_pdf


def mutate_same_length(data: bytes) -> bytes:
    changed = data.replace(b"%PDF-1.4", b"%PDF-1.5", 1)
    assert changed != data and len(changed) == len(data)
    return changed


def snapshot(out: Path) -> dict:
    """Bytes, modes and mtimes of everything published (the lock file is stage state)."""
    rows = {}
    for path in sorted(Path(out).rglob("*")):
        if path.name == ".lock":
            continue
        info = path.lstat()
        rows[str(path.relative_to(out))] = (info.st_mode, info.st_mtime_ns,
                                            None if path.is_dir() else path.read_bytes())
    return rows


class CachedSourceTest(unittest.TestCase):
    def setUp(self):
        self.ws = base.Workspace(self)
        self.config = self.ws.config(ocrmypdf=base.fake_ocrmypdf(self.ws.tmp))
        self.sidecars = {}
        self.originals = {}

    def add(self, name, text=base.CLEAN, seed=0):
        data = scanned_pdf([noise_page(seed)])
        digest = self.ws.add(name, data)
        self.sidecars[digest] = [text]
        self.originals[name] = data
        return digest

    def run_stage(self, max_documents=50):
        tools, vision = FakeTools(self.sidecars), FakeVision()
        line, code, outcomes = stage.run(self.ws.args(self.config, max_documents=max_documents),
                                         runner=tools, transport=vision)
        return line, code, outcomes, tools, vision

    def assert_no_work_dirs(self):
        self.assertEqual(list((self.ws.out / ".work").iterdir()), [])

    def test_same_length_changed_source_is_not_cached(self):
        self.add("doc.pdf", seed=1)
        self.ws.write_queue()
        first = self.run_stage()
        self.assertEqual(first[2][0].status, "ocr_complete")
        before = snapshot(self.ws.out)
        path = self.ws.sources / "doc.pdf"
        path.write_bytes(mutate_same_length(self.originals["doc.pdf"]))
        line, code, outcomes, tools, vision = self.run_stage()
        self.assertEqual([(o.status, o.code, o.cached) for o in outcomes],
                         [("source_invalid", "source_hash_mismatch", False)])
        self.assertEqual((tools.calls, vision.requests, code), ([], [], 0))
        self.assertEqual(line["counts"], {"input": 1, "output": 0, "failed": 1, "skipped": 0})
        self.assertEqual((line["status"], line["error_code"]), ("warn", "OCR_DOCUMENT_FAILED"))
        self.assertEqual(line["needs_g"], [{"code": "OCR_SOURCE_INVALID", "severity": "action", "count": 1}])
        self.assertNotIn("new_data", line)
        self.assertEqual(snapshot(self.ws.out), before)
        self.assert_no_work_dirs()
        self.assertEqual(path.read_bytes(), mutate_same_length(self.originals["doc.pdf"]))

    def test_same_length_atomic_replacement_is_not_cached(self):
        self.add("doc.pdf", seed=2)
        self.ws.write_queue()
        self.run_stage()
        before = snapshot(self.ws.out)
        path = self.ws.sources / "doc.pdf"
        staged = self.ws.sources / "doc.pdf.new"
        staged.write_bytes(mutate_same_length(self.originals["doc.pdf"]))
        os.replace(staged, path)
        _, _, outcomes, tools, _ = self.run_stage()
        self.assertEqual([(o.status, o.code) for o in outcomes], [("source_invalid", "source_hash_mismatch")])
        self.assertEqual(tools.calls, [])
        self.assertEqual(snapshot(self.ws.out), before)
        # Restoring the original bytes makes the retained result reusable again.
        staged.write_bytes(self.originals["doc.pdf"])
        os.replace(staged, path)
        _, _, outcomes, tools, _ = self.run_stage()
        self.assertEqual([(o.status, o.cached) for o in outcomes], [("cached", True)])
        self.assertEqual(tools.calls, [])
        self.assertEqual(snapshot(self.ws.out), before)

    def test_cache_validation_runs_after_work_cap(self):
        self.add("bad.pdf", seed=3)
        self.add("partial.pdf", text="", seed=4)
        self.ws.write_queue()
        self.run_stage()
        (self.ws.sources / "bad.pdf").write_bytes(mutate_same_length(self.originals["bad.pdf"]))
        new = self.add("new.pdf", seed=5)
        later = self.add("later.pdf", seed=6)
        by_name = {json.loads(line)["document_id"]: line for line in self.ws.lines}
        self.ws.lines = [by_name["doc:new.pdf"], by_name["doc:bad.pdf"], by_name["doc:partial.pdf"],
                         by_name["doc:later.pdf"]]
        self.ws.write_queue()
        line, code, outcomes, tools, _ = self.run_stage(max_documents=1)
        self.assertEqual([(o.document_id, o.status, o.code) for o in outcomes], [
            ("doc:new.pdf", "ocr_complete", None),
            ("doc:bad.pdf", "source_invalid", "source_hash_mismatch"),
            ("doc:partial.pdf", "cached", "ocr_pages_unresolved")])
        self.assertEqual(tools.count("ocrmypdf"), 1)  # only the one allowed new item
        self.assertEqual(line["counts"], {"input": 4, "output": 1, "failed": 1, "skipped": 2})
        self.assertIn({"code": "OCR_SOURCE_INVALID", "severity": "action", "count": 1}, line["needs_g"])
        self.assertIn({"code": "OCR_PAGES_UNRESOLVED", "severity": "action", "count": 1}, line["needs_g"])
        self.assertIn({"code": "OCR_BACKLOG", "severity": "info", "count": 1}, line["needs_g"])
        self.assertEqual((line["status"], line["error_code"], code), ("warn", "OCR_DOCUMENT_FAILED", 0))
        self.assertTrue((self.ws.out / new).is_dir())
        self.assertFalse((self.ws.out / later).exists())

    def test_unchanged_source_reuses_cache_without_copy_or_work(self):
        self.add("doc.pdf", seed=7)
        self.ws.write_queue()
        self.run_stage()
        before = snapshot(self.ws.out)
        _, _, outcomes, tools, vision = self.run_stage()
        self.assertEqual([(o.status, o.cached, o.attempted) for o in outcomes], [("cached", True, False)])
        # A different regular inode holding identical bytes is still the same original.
        path = self.ws.sources / "doc.pdf"
        inode = path.stat().st_ino
        staged = self.ws.sources / "doc.pdf.copy"
        staged.write_bytes(self.originals["doc.pdf"])
        os.replace(staged, path)
        self.assertNotEqual(path.stat().st_ino, inode)
        line, _, outcomes, tools, vision = self.run_stage()
        self.assertEqual([(o.status, o.cached, o.attempted) for o in outcomes], [("cached", True, False)])
        self.assertEqual((tools.calls, vision.requests), ([], []))
        self.assertEqual(line["counts"], {"input": 1, "output": 0, "failed": 0, "skipped": 1})
        self.assertEqual(snapshot(self.ws.out), before)
        self.assert_no_work_dirs()

    def test_missing_symlink_and_damaged_cache_guards_still_win(self):
        for index, name in enumerate(("missing.pdf", "linked.pdf", "damaged.pdf", "good.pdf")):
            self.add(name, seed=10 + index)
        self.ws.write_queue()
        self.run_stage()
        os.unlink(self.ws.sources / "missing.pdf")
        os.unlink(self.ws.sources / "linked.pdf")
        os.symlink(self.ws.tmp / "elsewhere.pdf", self.ws.sources / "linked.pdf")
        damaged = next((self.ws.out / hashlib.sha256(self.originals["damaged.pdf"]).hexdigest()).iterdir())
        with open(damaged / "page-0001.txt", "ab") as stream:
            stream.write(b"x")
        before = snapshot(self.ws.out)
        line, _, outcomes, tools, _ = self.run_stage()
        self.assertEqual([(o.document_id, o.status, o.code) for o in outcomes], [
            ("doc:missing.pdf", "source_invalid", "source_missing"),
            ("doc:linked.pdf", "source_invalid", "source_symlink"),
            ("doc:damaged.pdf", "cache_invalid", "cache_damaged"),
            ("doc:good.pdf", "cached", None)])
        self.assertEqual(tools.calls, [])
        self.assertEqual(snapshot(self.ws.out), before)  # damaged evidence is neither repaired nor deleted
        self.assertEqual(line["counts"], {"input": 4, "output": 0, "failed": 3, "skipped": 1})

    @unittest.skipUnless(os.path.isdir("/proc/self/fd"), "needs /proc")
    def test_refused_hit_closes_the_source_descriptor_and_continues(self):
        self.add("a.pdf", seed=20)
        self.add("b.pdf", seed=21)
        self.ws.write_queue()
        self.run_stage()
        (self.ws.sources / "a.pdf").write_bytes(mutate_same_length(self.originals["a.pdf"]))
        opened = len(os.listdir("/proc/self/fd"))
        _, _, outcomes, _, _ = self.run_stage()
        self.assertEqual([(o.status, o.code) for o in outcomes],
                         [("source_invalid", "source_hash_mismatch"), ("cached", None)])
        self.assertEqual(len(os.listdir("/proc/self/fd")), opened)


class ChunkedHashTest(unittest.TestCase):
    """_verify_source_hash with deterministic read hooks and a tiny chunk and size bound."""

    CHUNK = 64

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="ocr-cached-source-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.path = tmp / "source.pdf"
        self.data = bytes(range(256)) * 2  # 512 bytes, eight chunks
        self.path.write_bytes(self.data)
        self.set_old_mtime()
        self.digest = hashlib.sha256(self.data).hexdigest()
        self.fd = os.open(self.path, os.O_RDONLY)
        self.addCleanup(self.close)
        patcher = mock.patch.object(stage, "SOURCE_CHUNK", self.CHUNK)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sizes = []

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    def set_old_mtime(self):
        info = self.path.stat()
        os.utime(self.path, ns=(info.st_atime_ns, info.st_mtime_ns - 10 ** 9))

    def hook(self, after_first=None, error=None):
        real, calls = os.read, []

        def read(fd, size):
            self.sizes.append(size)
            calls.append(size)
            if error is not None:
                raise error
            chunk = real(fd, size)
            if len(calls) == 1 and after_first is not None:
                after_first()
            return chunk
        return mock.patch.object(stage, "_read_source_chunk", read)

    def code(self, expected_bytes=None, digest=None):
        with self.assertRaises(stage.BackendError) as caught:
            stage._verify_source_hash(self.fd, digest or self.digest,
                                      len(self.data) if expected_bytes is None else expected_bytes)
        return caught.exception.code

    def test_valid_source_is_read_in_bounded_chunks(self):
        with self.hook():
            stage._verify_source_hash(self.fd, self.digest, len(self.data))
        self.assertGreater(len(self.sizes), 2)
        self.assertEqual(set(self.sizes), {self.CHUNK})

    def test_wrong_digest_or_recorded_length_is_a_mismatch(self):
        self.assertEqual(self.code(digest="0" * 64), "source_hash_mismatch")
        self.assertEqual(self.code(expected_bytes=len(self.data) - 1), "source_hash_mismatch")
        self.assertEqual(self.sizes, [])  # a size mismatch is refused before any read

    def test_in_place_edit_of_an_already_read_region_is_observed(self):
        def edit():
            with open(self.path, "r+b") as stream:
                stream.write(b"\xff")  # byte 0 was already hashed: the digest alone cannot see it
        with self.hook(after_first=edit):
            self.assertEqual(self.code(), "source_hash_mismatch")

    def test_shrink_during_read_is_a_mismatch(self):
        with self.hook(after_first=lambda: os.truncate(self.path, 100)):
            self.assertEqual(self.code(), "source_hash_mismatch")

    def test_growth_is_a_mismatch_and_growth_past_the_bound_is_oversize(self):
        def grow():
            with open(self.path, "ab") as stream:
                stream.write(b"z" * 10)
        with self.hook(after_first=grow):
            self.assertEqual(self.code(), "source_hash_mismatch")
        small = hashlib.sha256(self.path.read_bytes()[:100]).hexdigest()
        os.truncate(self.path, 100)
        os.lseek(self.fd, 0, os.SEEK_SET)

        def grow_past_bound():
            with open(self.path, "ab") as stream:
                stream.write(b"z" * 200)
        with mock.patch.object(stage, "MAX_SOURCE_BYTES", 100), self.hook(after_first=grow_past_bound):
            self.assertEqual(self.code(expected_bytes=100, digest=small), "source_oversize")

    def test_source_larger_than_the_bound_is_oversize_without_reading(self):
        with mock.patch.object(stage, "MAX_SOURCE_BYTES", 100), self.hook():
            self.assertEqual(self.code(), "source_oversize")
        self.assertEqual(self.sizes, [])

    def test_read_error_is_unreadable(self):
        with self.hook(error=OSError(5, "io")):
            self.assertEqual(self.code(), "source_unreadable")

    def test_process_reports_read_error_and_oversize_as_per_item_outcomes(self):
        ws = base.Workspace(self)
        config = ws.config(ocrmypdf=base.fake_ocrmypdf(ws.tmp))
        sidecars = {}
        for index in range(2):
            data = scanned_pdf([noise_page(30 + index)])
            sidecars[ws.add(f"d{index}.pdf", data)] = [base.CLEAN]
        ws.write_queue()
        stage.run(ws.args(config), runner=FakeTools(sidecars), transport=FakeVision())
        before = snapshot(ws.out)
        tools = FakeTools(sidecars)
        with mock.patch.object(stage, "_read_source_chunk", side_effect=[OSError(5, "io"), b""]):
            _, _, outcomes = stage.run(ws.args(config), runner=tools, transport=FakeVision())
        self.assertEqual([(o.status, o.code) for o in outcomes],
                         [("source_invalid", "source_unreadable"), ("source_invalid", "source_hash_mismatch")])
        with mock.patch.object(stage, "MAX_SOURCE_BYTES", 10):
            _, _, outcomes = stage.run(ws.args(config), runner=tools, transport=FakeVision())
        self.assertEqual([(o.status, o.code) for o in outcomes], [("source_invalid", "source_oversize")] * 2)
        self.assertEqual(tools.calls, [])
        self.assertEqual(snapshot(ws.out), before)


if __name__ == "__main__":
    unittest.main()
