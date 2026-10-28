"""Offline cache-axis qualification; fictional PDF and injected file writers only."""
from contextlib import ExitStack, contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import traceback
import unittest
from unittest.mock import patch
import urllib.request


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def safe_directory(path):
    path = Path(os.path.abspath(path))
    for component in reversed((path, *path.parents)):
        if component.is_symlink():
            raise ValueError("fixture ancestor is a symlink")
        if component.exists() and not component.is_dir():
            raise ValueError("fixture ancestor is not a directory")
    return path


def forbidden(*args, **kwargs):
    raise AssertionError("real child/network/model execution is forbidden")


@contextmanager
def offline_runtime():
    # Closure inspected: ocr -> pdf_eligibility has no data-path constants or
    # bound real-data defaults. Keep subprocess.run unchanged when importing:
    # its bound default must not capture a mock that poisons later suites.
    with ExitStack() as stack:
        for target in ("subprocess.Popen", "os.system", "os.posix_spawn",
                       "os.posix_spawnp", "socket.socket.connect",
                       "socket.socket.connect_ex", "socket.create_connection",
                       "socket.getaddrinfo", "urllib.request.urlopen"):
            stack.enter_context(patch(target, side_effect=forbidden))
        from campaign_tool.records.extract import ocr
        yield ocr


class FileWriter:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.tools = {name: {"path": str(root / "fictional-bin" / name),
                             "version": name + " fictional-v1"}
                      for name in ("pdftoppm", "tesseract")}
        self.tools["pypdf"] = {"version": "fictional-fixture-version"}

    def __call__(self, command, **kwargs):
        name = Path(command[0]).name
        if name not in self.tools or command[0] != self.tools[name]["path"]:
            raise AssertionError("unexpected fake command")
        destination = Path(command[-1] if name == "pdftoppm" else command[2])
        if not destination.is_relative_to(self.root):
            raise AssertionError("fake output escaped fixture")
        self.calls.append((list(command), kwargs))
        if name == "pdftoppm":
            Path(str(destination) + ".png").write_bytes(b"fictional raster bytes")
        elif name == "tesseract":
            Path(str(destination) + ".txt").write_text("fictional words\n")
            Path(str(destination) + ".tsv").write_text("text\tconf\nfictional\t75\n")
        return subprocess.CompletedProcess(command, 0, "", "")


def assert_distinct(case, identities):
    case.assertEqual(len(set(identities)), len(identities), "cache identity collision")


class CacheAxesTests(unittest.TestCase):
    def setUp(self):
        parent = safe_directory(os.environ.get("TMPDIR") or tempfile.gettempdir())
        self.root = Path(tempfile.mkdtemp(prefix="fa1705-fixture-", dir=parent))
        # Retained by design: acceptance logs and stopped-run residue are not
        # fixture teardown targets. No global tempfile defaults are modified.
        self.output = self.root / "ocr-output"

    def matrix(self):
        with offline_runtime() as ocr:
            from pypdf import PdfWriter
            writer = PdfWriter()
            writer.add_blank_page(width=72, height=72)
            writer.add_blank_page(width=72, height=72)
            buffer = io.BytesIO()
            writer.write(buffer)
            source = buffer.getvalue()
            source_hash = digest(source)
            (self.root / "blobs").mkdir(mode=0o700)
            blob = self.root / "blobs" / source_hash
            blob.write_bytes(source)
            fake = FileWriter(self.root)
            base = dict(language="eng", low_confidence=80.0,
                        tool_signature="fictional-pinned-v1", page=1)
            variants = [base, {**base, "language": "spa"},
                        {**base, "low_confidence": 60.0},
                        {**base, "low_confidence": 75.0},
                        {**base, "tool_signature": "fictional-pinned-v2"},
                        {**base, "page": 2}]
            receipts, snapshots = [], {}

            def unchanged():
                self.assertEqual(blob.read_bytes(), source)
                for path, content in snapshots.items():
                    self.assertEqual(path.read_bytes(), content, str(path))

            def call(variant):
                config = {k: v for k, v in variant.items() if k != "page"}
                result = ocr.extract_image_only_pages(
                    self.root, source_hash, self.output, tools=fake.tools,
                    attempt_id="fixed-attempt", fidelity_holds=(),
                    pages=(variant["page"],), runner=fake, **config)
                self.assertEqual(len(result), 1)
                return result[0]

            for position, variant in enumerate(variants):
                with self.subTest(variant=variant):
                    count = len(fake.calls)
                    receipt = call(variant)
                    self.assertEqual(len(fake.calls), count + 2)
                    raster, text = fake.calls[-2:]
                    self.assertEqual(Path(raster[0][0]).name, "pdftoppm")
                    self.assertEqual(Path(text[0][0]).name, "tesseract")
                    self.assertEqual(text[0][3:], ["-l", variant["language"], "txt", "tsv"])
                    self.assertEqual(raster[0][1:9],
                                     ["-f", "1", "-l", "1", "-singlefile", "-r", "300", "-png"])
                    for command, kwargs in (raster, text):
                        self.assertIs(kwargs["preexec_fn"], ocr._limit_child)
                        self.assertEqual(kwargs["timeout"], 180)
                    expected = dict(source_sha256=source_hash, page=variant["page"],
                                    version=ocr.VERSION, attempt_id="fixed-attempt",
                                    fidelity_hold=False,
                                    **{k: v for k, v in variant.items() if k != "page"})
                    self.assertEqual(receipt["identity"], expected)
                    self.assertEqual(receipt["receipt_id"], digest(encoded(expected)))
                    if position:
                        changed = {k for k in expected if expected[k] != receipts[0]["identity"][k]}
                        self.assertEqual(len(changed), 1)
                    self.assertEqual(receipt["confidence"], 75.0)
                    queued = variant["low_confidence"] > 75.0
                    self.assertEqual(receipt["status"],
                                     "visual_check_queued" if queued else "ocr_text_unreviewed")
                    self.assertEqual(receipt["visual_check_required"], queued)
                    self.assertEqual(receipt["review_status"], "not_reviewed")
                    self.assertEqual(receipt["visual_check_status"], "not_done")
                    self.assertEqual(receipt["fidelity_status"], "not_checked")
                    self.assertIsNone(receipt["blocked_reason"])
                    self.assertEqual(receipt["page_count"], 2)
                    self.assertEqual(receipt["tool_versions"],
                                     {k: v["version"] for k, v in fake.tools.items()})
                    final = (self.output / source_hash / f"page-{variant['page']:06d}"
                             / receipt["receipt_id"])
                    self.assertEqual({p.name for p in final.iterdir()},
                                     set(ocr.ARTIFACTS) | {"receipt.json", "manifest.json"})
                    self.assertEqual(set(receipt["artifact_sha256"]), set(ocr.ARTIFACTS))
                    for name, value in receipt["artifact_sha256"].items():
                        self.assertEqual(digest((final / name).read_bytes()), value)
                    loaded = ocr.load_page_receipt(self.output, source_hash,
                                                   variant["page"], receipt["receipt_id"])
                    self.assertEqual(loaded["receipt"], receipt)
                    self.assertEqual(loaded["sidecar"], b"fictional words\n")
                    index = self.output / "indexes" / "visual-check" / (receipt["receipt_id"] + ".json")
                    if queued:
                        entry = json.loads(index.read_bytes())
                        self.assertEqual(entry, dict(
                            source_sha256=source_hash, locator={"page": variant["page"]},
                            receipt_id=receipt["receipt_id"],
                            receipt_sha256=digest((final / "receipt.json").read_bytes()),
                            manifest_sha256=digest((final / "manifest.json").read_bytes()),
                            status="visual_check_queued", blocked_reason=None, confidence=75.0))
                        snapshots[index] = index.read_bytes()
                    else:
                        self.assertFalse(index.exists())
                    unchanged()
                    snapshots.update({p: p.read_bytes() for p in final.iterdir()})
                    self.assertEqual(call(variant), receipt)
                    self.assertEqual(len(fake.calls), count + 2)
                    unchanged()
                    receipts.append(receipt)
            assert_distinct(self, [r["receipt_id"] for r in receipts])
            # Old variants must still be reusable after every other axis changed.
            for variant, receipt in zip(variants, receipts):
                self.assertEqual(call(variant), receipt)
                self.assertEqual(ocr.load_page_receipt(
                    self.output, source_hash, variant["page"], receipt["receipt_id"])["receipt"], receipt)
            self.assertEqual(len(fake.calls), 2 * len(variants))
            self.assertEqual({p.stem for p in (self.output / "indexes" / "visual-check").iterdir()},
                             {r["receipt_id"] for r in receipts if r["visual_check_required"]})
            self.assertEqual(list((self.output / "indexes" / "blocked").iterdir()), [])
            unchanged()
            (self.root / "matrix.json").write_text(json.dumps(dict(
                fixture_root=str(self.root), variants=variants, receipts=receipts,
                fake_commands=[c for c, _ in fake.calls],
                preserved={str(p.relative_to(self.root)): digest(b) for p, b in snapshots.items()}),
                indent=2) + "\n")
            return receipts

    def test_axes_replay_and_history(self):
        self.matrix()

    def test_language_projection_oracle(self):
        receipts = self.matrix()
        pair = receipts[:2]
        projected = [{k: v for k, v in r["identity"].items() if k != "language"} for r in pair]
        self.assertEqual(projected[0], projected[1])
        self.assertNotEqual(pair[0]["identity"]["language"], pair[1]["identity"]["language"])
        faulty_ids = [digest(encoded(identity)) for identity in projected]
        try:
            assert_distinct(self, faulty_ids)
        except AssertionError as error:
            self.assertIn("cache identity collision", str(error))
            failure = traceback.format_exc()
            (self.root / "expected-projection-failure.txt").write_text(failure)
            print("EXPECTED test-local omitted-language assertion failure:\n" + failure)
        else:
            self.fail("faulty projection was not detected")
        # Same deciding assertion, actual returned IDs, no product mutation.
        assert_distinct(self, [r["receipt_id"] for r in pair])


if __name__ == "__main__":
    unittest.main()
