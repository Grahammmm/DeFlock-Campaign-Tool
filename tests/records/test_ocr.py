"""Synthetic M2 tests: external OCR and version calls are injected."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import tempfile
import unittest
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter
from campaign_tool.records.extract import ocr

TOOLS = {name: {"available": True, "path": "/synthetic/bin/" + name, "version": name + " 0.0-test"}
         for name in ocr.OCR_TOOLS}
TOOLS["pypdf"] = {"available": True, "version": "0.0-test"}


class FakeRunner:
    def __init__(self, confidence="42", *, sidecar="synthetic text\n", symlink=False):
        self.calls = []
        self.confidence = confidence
        self.sidecar = sidecar
        self.symlink = symlink

    def __call__(self, command, **kwargs):
        name = Path(command[0]).name
        self.calls.append(name)
        self.paths = getattr(self, "paths", []) + [command[0]]
        self.kwargs = getattr(self, "kwargs", []) + [kwargs]
        if name == "pdftoppm":
            image = Path(command[-1] + ".png")
            if self.symlink:
                image.symlink_to("/etc/passwd")
            else:
                image.write_bytes(b"synthetic raster")
        elif name == "ocrmypdf":
            Path(command[command.index("--sidecar") + 1]).write_text(self.sidecar)
            Path(command[-1]).write_bytes(b"synthetic derived pdf")
        elif name == "tesseract":
            return subprocess.CompletedProcess(command, 0, "text\tconf\nword\t" + self.confidence + "\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")


class OCRTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "blobs").mkdir()
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        writer.add_blank_page(width=72, height=72)
        stream = io.BytesIO()
        writer.write(stream)
        self.raw = stream.getvalue()
        self.sha = hashlib.sha256(self.raw).hexdigest()
        (self.root / "blobs" / self.sha).write_bytes(self.raw)
        self.output = self.root / "private-ocr"

    def call(self, runner, **kwargs):
        return ocr.extract_image_only_pages(self.root, self.sha, self.output,
                                            tool_signature="stub-v1", runner=runner,
                                            tools=kwargs.pop("tools", TOOLS), **kwargs)

    def final(self, receipt):
        return self.output / self.sha / f"page-{receipt['locator']['page']:06d}" / receipt["receipt_id"]

    def test_low_confidence_receipt_index_and_idempotent_rerun(self):
        fake = FakeRunner()
        receipt = self.call(fake, pages=(2,))[0]
        self.assertEqual(receipt["status"], "visual_check_queued")
        self.assertEqual(receipt["source"]["sha256"], self.sha)
        self.assertEqual(receipt["locator"], {"page": 2})
        self.assertEqual(receipt["confidence"], 42.0)
        self.assertEqual(receipt["review_status"], "not_reviewed")
        self.assertEqual(len(receipt["artifact_sha256"]), 5)
        index = self.output / "indexes" / "visual-check" / (receipt["receipt_id"] + ".json")
        entry = json.loads(index.read_text())
        self.assertEqual(entry["receipt_sha256"], hashlib.sha256((self.final(receipt) / "receipt.json").read_bytes()).hexdigest())
        self.assertEqual(entry["manifest_sha256"], hashlib.sha256((self.final(receipt) / "manifest.json").read_bytes()).hexdigest())
        self.assertEqual(self.call(fake, pages=(2,)), [receipt])
        self.assertEqual(fake.calls, ["pdftoppm", "ocrmypdf", "tesseract"])
        self.assertEqual((self.root / "blobs" / self.sha).read_bytes(), self.raw)

    def test_new_attempt_retries_transient_failure_without_rewriting_history(self):
        def missing(command, **kwargs):
            raise FileNotFoundError(command[0])
        blocked = self.call(missing, pages=(1,))[0]
        self.assertEqual(blocked["blocked_reason"], "local_ocr_dependency_or_output_missing")
        original_receipt = (self.final(blocked) / "receipt.json").read_bytes()
        original_manifest = (self.final(blocked) / "manifest.json").read_bytes()
        blocked_index = self.output / "indexes" / "blocked" / (blocked["receipt_id"] + ".json")
        original_index = blocked_index.read_bytes()
        fake = FakeRunner()
        self.assertEqual(self.call(fake, pages=(1,)), [blocked])
        self.assertEqual(fake.calls, [])
        retried = self.call(fake, pages=(1,), attempt_id="retry-1")[0]
        self.assertEqual(retried["status"], "visual_check_queued")
        self.assertEqual(retried["identity"]["tool_signature"], blocked["identity"]["tool_signature"])
        self.assertEqual(retried["identity"]["attempt_id"], "retry-1")
        self.assertNotEqual(retried["receipt_id"], blocked["receipt_id"])
        self.assertEqual((self.final(blocked) / "receipt.json").read_bytes(), original_receipt)
        self.assertEqual((self.final(blocked) / "manifest.json").read_bytes(), original_manifest)
        self.assertEqual(blocked_index.read_bytes(), original_index)
        self.assertTrue((self.output / "indexes" / "visual-check" /
                         (retried["receipt_id"] + ".json")).is_file())
        self.assertEqual(self.call(fake, pages=(1,), attempt_id="retry-1"), [retried])
        self.assertEqual(fake.calls, ["pdftoppm", "ocrmypdf", "tesseract"])
        current = json.loads((self.output / self.sha / "page-000001" / "current.json").read_text())
        self.assertEqual(current["receipt_id"], retried["receipt_id"])
        self.assertIn(blocked["receipt_id"], current["supersedes_receipt_ids"])

    def test_attempt_id_rejects_unsafe_or_unbounded_labels(self):
        for label in ("", "../retry", "retry/2", "x" * 65):
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, "attempt_id"):
                self.call(FakeRunner(), pages=(1,), attempt_id=label)

    def test_high_confidence_remains_unreviewed(self):
        receipt = self.call(FakeRunner("97"), pages=(1,))[0]
        self.assertEqual(receipt["status"], "ocr_text_unreviewed")
        self.assertFalse(receipt["visual_check_required"])
        self.assertEqual(receipt["fidelity_status"], "not_checked")

    def test_fidelity_hold_atomic_and_sticky(self):
        fake = FakeRunner()
        held = self.call(fake, pages=(1,), fidelity_holds=(1,))[0]
        self.assertEqual(held["blocked_reason"], "fidelity_hold_requires_visual_comparison")
        self.assertEqual(self.call(fake, pages=(1,)), [held])
        page_dir = self.output / self.sha / "page-000001"
        self.assertEqual([p.name for p in page_dir.iterdir() if p.name.startswith(".hold-")], [])
        self.assertEqual(fake.calls, [])
        self.assertTrue((self.output / "indexes" / "blocked" / (held["receipt_id"] + ".json")).is_file())

    def test_no_confidence_no_text_and_missing_dependency(self):
        self.assertEqual(self.call(FakeRunner("invalid"), pages=(1,))[0]["blocked_reason"], "confidence_unavailable")
        self.assertEqual(self.call(FakeRunner(sidecar=""), pages=(2,))[0]["blocked_reason"], "no_text_after_ocr")
        def missing(command, **kwargs):
            raise FileNotFoundError(command[0])
        with tempfile.TemporaryDirectory() as alternate:
            self.output = Path(alternate)
            receipt = self.call(missing, pages=(1,))[0]
            self.assertEqual(receipt["blocked_reason"], "local_ocr_dependency_or_output_missing")

    def test_source_and_nested_derivative_symlinks_rejected(self):
        blob = self.root / "blobs" / self.sha
        blob.unlink()
        blob.symlink_to("/etc/passwd")
        with self.assertRaisesRegex(ValueError, "symlink_file_rejected"):
            self.call(FakeRunner(), pages=(1,))
        blob.unlink()
        blob.write_bytes(self.raw)
        self.output.mkdir()
        (self.output / self.sha).symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink_or_nondirectory_rejected"):
            self.call(FakeRunner(), pages=(1,))

    def test_page_and_final_directory_symlinks_rejected(self):
        (self.output / self.sha).mkdir(parents=True)
        page = self.output / self.sha / "page-000001"
        page.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink_or_nondirectory_rejected"):
            self.call(FakeRunner(), pages=(1,))
        page.unlink()
        page.mkdir()
        identity = {"source_sha256": self.sha, "page": 1, "version": ocr.VERSION,
                    "tool_signature": "stub-v1", "attempt_id": "initial", "language": "eng",
                    "low_confidence": 80.0,
                    "fidelity_hold": False}
        (page / ocr._hash(ocr._json(identity))).symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink_or_nondirectory_rejected"):
            self.call(FakeRunner(), pages=(1,))

    def test_generated_symlink_becomes_blocked_receipt_without_symlink(self):
        receipt = self.call(FakeRunner(symlink=True), pages=(1,))[0]
        self.assertEqual(receipt["blocked_reason"], "page_derivative_symlink_rejected")
        self.assertEqual(set(p.name for p in self.final(receipt).iterdir()),
                         {"page.pdf", "receipt.json", "manifest.json"})

    def test_receipt_manifest_artifact_and_index_tampering_rejected(self):
        receipt = self.call(FakeRunner(), pages=(1,))[0]
        self.assertEqual(self.call(FakeRunner(), pages=(1,)), [receipt])
        final = self.final(receipt)
        (final / "receipt.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "published_ocr_identity_mismatch"):
            self.call(FakeRunner(), pages=(1,))
        (final / "receipt.json").write_bytes(ocr._json(receipt))
        (final / "manifest.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "existing_ocr_manifest_mismatch"):
            self.call(FakeRunner(), pages=(1,))
        manifest = {"schema_version": ocr.VERSION,
                    "receipt_sha256": hashlib.sha256(ocr._json(receipt)).hexdigest(),
                    "artifact_sha256": receipt["artifact_sha256"]}
        (final / "manifest.json").write_bytes(ocr._json(manifest))
        (final / "sidecar.txt").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "existing_ocr_artifact_mismatch"):
            self.call(FakeRunner(), pages=(1,))
        (final / "sidecar.txt").write_text("synthetic text\n")
        index = self.output / "indexes" / "visual-check" / (receipt["receipt_id"] + ".json")
        index.write_text("{}")
        with self.assertRaisesRegex(ValueError, "ocr_index_conflict"):
            self.call(FakeRunner(), pages=(1,))

    def test_text_probe_failure_is_page_level_and_batch_continues(self):
        reader = PdfReader(io.BytesIO(self.raw), strict=False)
        class Broken:
            def extract_text(self):
                raise RuntimeError("synthetic parser failure")
        class FakeReader:
            pages = [Broken(), reader.pages[1]]
            is_encrypted = False
        with patch("pypdf.PdfReader", return_value=FakeReader()):
            receipts = self.call(FakeRunner(), pages=(1, 2))
        self.assertEqual(receipts[0]["blocked_reason"], "page_text_probe_failed")
        self.assertEqual(receipts[1]["status"], "visual_check_queued")

    def test_no_replace_publication_preserves_existing_directory(self):
        parent = self.root / "publish"
        parent.mkdir()
        source, destination = parent / "source", parent / "destination"
        source.mkdir()
        destination.mkdir()
        (destination / "sentinel").write_text("existing")
        self.assertFalse(ocr._publish_directory(source, destination))
        self.assertEqual((destination / "sentinel").read_text(), "existing")
        self.assertTrue(source.is_dir())

    def test_world_readable_managed_directories_rejected_before_ocr(self):
        for level in ("root", "source", "page", "index"):
            with self.subTest(level=level), tempfile.TemporaryDirectory() as alternate:
                self.output = Path(alternate) / "ocr"
                self.output.mkdir(mode=0o700)
                if level == "root":
                    unsafe = self.output
                elif level == "source":
                    unsafe = self.output / self.sha
                    unsafe.mkdir(mode=0o700)
                elif level == "page":
                    unsafe = self.output / self.sha / "page-000001"
                    unsafe.mkdir(parents=True, mode=0o700)
                else:
                    unsafe = self.output / "indexes" / "visual-check"
                    unsafe.mkdir(parents=True, mode=0o700)
                unsafe.chmod(0o755)
                fake = FakeRunner()
                with self.assertRaisesRegex(ValueError, "managed_output_directory_not_owner_only"):
                    self.call(fake, pages=(1,))
                self.assertEqual(fake.calls, [])

    def test_hold_arriving_during_ocr_blocks_publication(self):
        started = threading.Event()
        release = threading.Event()
        results = []
        errors = []
        class Paused(FakeRunner):
            def __call__(self, command, **kwargs):
                if Path(command[0]).name == "pdftoppm":
                    started.set()
                    if not release.wait(3):
                        raise RuntimeError("synthetic pause timed out")
                return super().__call__(command, **kwargs)
        def worker():
            try:
                results.extend(self.call(Paused(), pages=(1,)))
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(started.wait(3))
        held = self.call(FakeRunner(), pages=(1,), attempt_id="hold-1",
                         fidelity_holds=(1,))[0]
        release.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(held["blocked_reason"], "fidelity_hold_requires_visual_comparison")
        self.assertEqual(results[0]["blocked_reason"], "fidelity_hold_requires_visual_comparison")
        self.assertEqual(results[0]["artifact_sha256"], {})
        current = json.loads((self.output / self.sha / "page-000001" / "current.json").read_text())
        self.assertEqual(current["status"], "blocked")
        self.assertEqual(current["blocked_reason"], "fidelity_hold_requires_visual_comparison")

    def test_reconcile_repairs_index_after_publication_crash(self):
        original = ocr._index
        calls = []
        def crash_once(*args):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("synthetic crash after receipt publication")
            return original(*args)
        with patch.object(ocr, "_index", side_effect=crash_once):
            with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
                self.call(FakeRunner(), pages=(1,))
        summary = ocr.reconcile_pages(self.output, self.sha)
        self.assertEqual(summary["published_receipts"], 1)
        current = json.loads((self.output / self.sha / "page-000001" / "current.json").read_text())
        index = self.output / "indexes" / "visual-check" / (current["receipt_id"] + ".json")
        self.assertTrue(index.is_file())
        self.assertEqual(current["status"], "visual_check_queued")

    def test_writer_failure_is_page_block_and_next_page_continues(self):
        real_writer = PdfWriter
        calls = []
        def writer():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("synthetic writer failure")
            return real_writer()
        with patch("pypdf.PdfWriter", side_effect=writer):
            receipts = self.call(FakeRunner(), pages=(1, 2))
        self.assertEqual(receipts[0]["blocked_reason"], "page_writer_failed")
        self.assertEqual(receipts[1]["status"], "visual_check_queued")
        self.assertEqual(len(calls), 2)

    def test_cleanup_failure_is_persisted_and_next_page_continues(self):
        real_publish = ocr._publish_directory
        real_cleanup = ocr.shutil.rmtree
        published = []
        cleaned = []
        def publish(source, destination):
            published.append(1)
            if len(published) == 1:
                raise OSError("synthetic publication interruption")
            return real_publish(source, destination)
        def cleanup(path, *args, **kwargs):
            cleaned.append(1)
            if len(cleaned) == 1:
                raise OSError("synthetic cleanup failure")
            return real_cleanup(path, *args, **kwargs)
        with patch.object(ocr, "_publish_directory", side_effect=publish), patch.object(
                ocr.shutil, "rmtree", side_effect=cleanup):
            receipts = self.call(FakeRunner(), pages=(1, 2))
        self.assertEqual(receipts[0]["blocked_reason"], "attempt_cleanup_failed")
        self.assertEqual(receipts[1]["status"], "visual_check_queued")
        current = json.loads((self.output / self.sha / "page-000001" / "current.json").read_text())
        self.assertEqual(current["blocked_reason"], "attempt_cleanup_failed")

    def test_doctor_injected_versions_and_missing_tools(self):
        commands = []
        def probe(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "version 1\n")
        status = ocr.dependency_status(which=lambda name: "/fake/" + name,
                                       probe=probe, module_probe=lambda: "6.10.0")
        self.assertTrue(status["ready"])
        self.assertEqual(status["tools"]["pypdf"]["version"], "6.10.0")
        self.assertEqual(status["tools"]["pdftoppm"]["version"], "version 1")
        self.assertEqual(status["tools"]["gs"], {"available": True, "path": "/fake/gs",
                                                 "version": "version 1"})
        self.assertEqual(commands, [["/fake/ocrmypdf", "--version"],
                                    ["/fake/tesseract", "--version"],
                                    ["/fake/pdftoppm", "-v"],
                                    ["/fake/gs", "--version"]])
        absent = ocr.dependency_status(which=lambda name: None, probe=probe,
                                        module_probe=lambda: "6.10.0")
        self.assertFalse(absent["ready"])
        self.assertEqual(absent["missing"], ["ocrmypdf", "tesseract", "pdftoppm", "gs"])
        no_gs = ocr.dependency_status(which=lambda name: None if name == "gs" else "/fake/" + name,
                                      probe=probe, module_probe=lambda: "6.10.0")
        self.assertFalse(no_gs["ready"])
        self.assertEqual(no_gs["missing"], ["gs"])
        relative = ocr.dependency_status(which=lambda name: "bin/" + name, probe=probe,
                                         module_probe=lambda: "6.10.0")
        self.assertTrue(all(Path(relative["tools"][n]["path"]).is_absolute() for n in ocr.OCR_TOOLS))

    def test_extraction_uses_doctor_paths_and_records_versions(self):
        fake = FakeRunner()
        receipt = self.call(fake, pages=(1,))[0]
        self.assertEqual(fake.paths, ["/synthetic/bin/pdftoppm", "/synthetic/bin/ocrmypdf",
                                      "/synthetic/bin/tesseract"])
        self.assertEqual(receipt["tool_versions"],
                         {name: TOOLS[name]["version"] for name in (*ocr.OCR_TOOLS, "pypdf")})
        self.assertEqual(json.loads((self.final(receipt) / "receipt.json").read_text())["tool_versions"],
                         receipt["tool_versions"])
        for kwargs in fake.kwargs:
            self.assertIs(kwargs["preexec_fn"], ocr._limit_child)
            self.assertTrue(kwargs["env"]["PATH"].startswith("/synthetic/bin" + ocr.os.pathsep))
        for bad in (None, {**TOOLS, "gs": {"path": None, "version": "x"}},
                    {**TOOLS, "tesseract": {"path": "tesseract", "version": "x"}},
                    {**TOOLS, "pdftoppm": {"path": "/synthetic/bin/pdftoppm", "version": None}}):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "tools"):
                self.call(FakeRunner(), pages=(2,), tools=bad)

    def test_child_limits_apply_address_space_and_file_size(self):
        code = ("import resource;print(resource.getrlimit(resource.RLIMIT_AS)[0],"
                "resource.getrlimit(resource.RLIMIT_FSIZE)[0])")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                check=True, preexec_fn=ocr._limit_child)
        self.assertEqual(result.stdout.split(), [str(ocr.CHILD_MEMORY_BYTES), str(ocr.CHILD_FILE_BYTES)])

    def test_raster_bound_blocks_oversized_page_and_next_page_continues(self):
        writer = PdfWriter()
        writer.add_blank_page(width=72 * 50, height=72 * 50)  # 15000 x 15000 px at 300 dpi
        writer.add_blank_page(width=72, height=72)
        stream = io.BytesIO()
        writer.write(stream)
        self.raw = stream.getvalue()
        self.sha = hashlib.sha256(self.raw).hexdigest()
        (self.root / "blobs" / self.sha).write_bytes(self.raw)
        fake = FakeRunner()
        receipts = self.call(fake, pages=(1, 2))
        self.assertEqual(receipts[0]["status"], "blocked")
        self.assertEqual(receipts[0]["blocked_reason"], "page_raster_bound")
        self.assertEqual(receipts[0]["artifact_sha256"], {})
        self.assertTrue((self.output / "indexes" / "blocked" / (receipts[0]["receipt_id"] + ".json")).is_file())
        self.assertEqual(receipts[1]["status"], "visual_check_queued")
        self.assertEqual(fake.calls, ["pdftoppm", "ocrmypdf", "tesseract"])

    def test_raster_bound_threshold(self):
        reader = PdfReader(io.BytesIO(self.raw), strict=False)
        self.assertAlmostEqual(ocr._raster_pixels(reader.pages[0]), 90000.0)
        class Box:
            width, height = float("nan"), 72
        class Page:
            mediabox = Box()
        with self.assertRaisesRegex(ocr.PageFailure, "page_geometry_unavailable"):
            ocr._raster_pixels(Page())

    def test_page_local_value_error_in_publication_yields_receipts_for_next_page(self):
        real_validate = ocr._validate_receipt
        calls = []
        def validate(*args):
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("synthetic page-local publication rejection")
            return real_validate(*args)
        with patch.object(ocr, "_validate_receipt", side_effect=validate):
            receipts = self.call(FakeRunner(), pages=(1, 2))
        self.assertEqual(receipts[0]["status"], "blocked")
        self.assertEqual(receipts[0]["blocked_reason"], "page_publication_rejected")
        self.assertEqual(receipts[1]["status"], "visual_check_queued")
        current = json.loads((self.output / self.sha / "page-000001" / "current.json").read_text())
        self.assertEqual(current["blocked_reason"], "page_publication_rejected")
        self.assertTrue((self.final(receipts[1]) / "receipt.json").is_file())


    def test_repeated_recovery_reuse_leaves_no_temporary_directories(self):
        original = self.call(FakeRunner("97"), pages=(1,))[0]
        page_dir = self.final(original).parent
        for _ in range(3):
            with ocr._page_lock(page_dir):
                recovered = ocr._publish_blocked_disposition(
                    page_dir, self.output, original, "synthetic_recovery_failure",
                    self.sha, 1)
            self.assertEqual(recovered["blocked_reason"], "synthetic_recovery_failure")
            self.assertFalse(any(p.name.startswith(".recovery-") for p in page_dir.iterdir()))
        self.assertEqual(len([p for p in page_dir.iterdir() if ocr.SHA256.fullmatch(p.name)]), 2)

    def test_reconcile_rejects_permissive_index_parent(self):
        original = self.call(FakeRunner(), pages=(1,))[0]
        indexes = self.output / "indexes"
        indexes.chmod(0o777)
        try:
            with self.assertRaisesRegex(ValueError, "managed_output_directory_not_owner_only"):
                ocr.reconcile_pages(self.output, self.sha)
        finally:
            indexes.chmod(0o700)
        self.assertEqual(json.loads((self.final(original) / "receipt.json").read_text()), original)

    def test_unexpected_generated_directory_blocks_page_and_continues(self):
        fake = FakeRunner("97")
        added = False
        def runner(command, **kwargs):
            nonlocal added
            result = fake(command, **kwargs)
            if Path(command[0]).name == "pdftoppm" and not added:
                extra = Path(command[-1]).parent / "unexpected"
                extra.mkdir()
                (extra / "generated.txt").write_text("synthetic unexpected output")
                added = True
            return result
        receipts = self.call(runner, pages=(1, 2))
        self.assertEqual(receipts[0]["blocked_reason"], "unexpected_page_derivative_directory")
        self.assertEqual(receipts[0]["status"], "blocked")
        self.assertEqual(receipts[1]["status"], "ocr_text_unreviewed")
        self.assertFalse((self.final(receipts[0]) / "unexpected").exists())
        self.assertEqual((self.root / "blobs" / self.sha).read_bytes(), self.raw)


if __name__ == "__main__":
    unittest.main()
