"""FA101: malformed OCR page selector containers are refused before preserved source bytes.

The producer is ``campaign_tool.records.extract.ocr.extract_image_only_pages``. Its
caller-supplied selectors have one explicit shape:

  pages           None (every page) or an exact list/tuple of exact positive ints, no duplicates
  fidelity_holds  an exact list/tuple of exact positive ints; duplicates are idempotent membership

Any other shape is refused with the existing ``ValueError("invalid or duplicate page selector")``
before tool resolution, the preserved-source read and the PDF reader. The upper bound against
the real page count stays after source hash and PDF count verification.

Everything here is card-owned synthetic data: a generated blank-page PDF under a temporary
directory, synthetic absolute tool paths and a fake OCR runner. No real OCR binary, source
acquisition, mailbox or private record is used, and no child process is started.
"""
import ast
from contextlib import contextmanager
import hashlib
import io
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

import pypdf
from pypdf import PdfWriter

from campaign_tool.records.extract import ocr

SELECTOR_ERROR = "invalid or duplicate page selector"
PAGE_COUNT = 2
TOOLS = {name: {"available": True, "path": "/synthetic/bin/" + name, "version": name + " 0.0-fa101"}
         for name in ocr.OCR_TOOLS}
TOOLS["pypdf"] = {"available": True, "version": "0.0-fa101"}
SEAM_NAMES = ("_resolved_tools", "_read", "PdfReader")

# The single preflight line the negative control disables, and its pass-through replacement.
GATE_TEXT = "    requested, holds = _page_selectors(pages, fidelity_holds)\n"
GATE_DISABLED = "    requested, holds = pages, fidelity_holds\n"
CONTROL_OUTCOMES = []  # read by tools/accept_fa101.py for the negative-control case


class TupleSubclass(tuple):
    pass


class ListSubclass(list):
    pass


def _generator():
    yield 1


# label -> pages value; each must be refused before any seam.
MALFORMED_PAGES = {
    "nested_list": [[1]],
    "nested_tuple": ((1,),),
    "nested_dict": [{"page": 1}],
    "mapping": {1: 1},
    "set": {1},
    "frozenset": frozenset({1}),
    "string": "1",
    "bytes": b"1",
    "bytearray": bytearray(b"\x01"),
    "scalar_int": 1,
    "null_member": [None],
    "bool_scalar": True,
    "bool_member": [True],
    "float_scalar": 1.0,
    "float_member": [1.0],
    "zero": [0],
    "negative": (-1,),
    "duplicate_list": [1, 1],
    "duplicate_tuple": (2, 2),
    "iterator": iter([1]),
    "generator": _generator(),
    "range": range(1, 2),
    "noniterable_object": object(),
    "tuple_subclass": TupleSubclass((1,)),
    "list_subclass": ListSubclass([1]),
}

# label -> fidelity_holds value; each must be refused before any seam.
MALFORMED_HOLDS = {
    "null": None,
    "nested_list": [[1]],
    "nested_dict": [{"page": 1}],
    "mapping": {1: True},
    "set": {1},
    "string": "1",
    "scalar_int": 1,
    "null_member": (None,),
    "bool_member": [True],
    "float_member": [1.0],
    "zero": [0],
    "negative": [-2],
    "iterator": iter([1]),
    "generator": _generator(),
    "noniterable_object": object(),
    "tuple_subclass": TupleSubclass((1,)),
}


def synthetic_pdf(pages=PAGE_COUNT):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


class FakeRunner:
    """Stands in for pdftoppm/tesseract: writes the files each would write, never executes."""

    def __init__(self, confidence="42"):
        self.calls = []
        self.confidence = confidence

    def __call__(self, command, **kwargs):
        name = Path(command[0]).name
        self.calls.append(name)
        if name == "pdftoppm":
            Path(command[-1] + ".png").write_bytes(b"synthetic raster")
        elif name == "tesseract":
            Path(command[2] + ".txt").write_text("synthetic text\n")
            Path(command[2] + ".tsv").write_text("text\tconf\nword\t" + self.confidence + "\n")
        return subprocess.CompletedProcess(command, 0, "", "")


class Seams:
    """Records every tool-resolution, source-read and PDF-reader call of one producer module."""

    def __init__(self, module):
        self.module = module
        self.calls = []

    @contextmanager
    def watch(self, on_read=None):
        real_tools, real_read, real_reader = self.module._resolved_tools, self.module._read, pypdf.PdfReader

        def tools(*args, **kwargs):
            self.calls.append("_resolved_tools")
            return real_tools(*args, **kwargs)

        def read(*args, **kwargs):
            self.calls.append("_read")
            if on_read is not None:
                on_read()
            return real_read(*args, **kwargs)

        def reader(*args, **kwargs):
            self.calls.append("PdfReader")
            return real_reader(*args, **kwargs)

        with patch.object(self.module, "_resolved_tools", tools), patch.object(self.module, "_read", read), \
                patch.object(pypdf, "PdfReader", reader):
            yield self


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="fa101-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "intake"
        (self.root / "blobs").mkdir(parents=True)
        self.raw = synthetic_pdf()
        self.sha = hashlib.sha256(self.raw).hexdigest()
        self.blob = self.root / "blobs" / self.sha
        self.blob.write_bytes(self.raw)
        self.output = Path(temp.name) / "ocr-output"

    def call(self, runner, module=ocr, **kwargs):
        return module.extract_image_only_pages(self.root, self.sha, self.output, tool_signature="fa101-stub 1",
                                               tools=TOOLS, runner=runner, **kwargs)

    def assert_refused_before_seams(self, module=ocr, **kwargs):
        runner = FakeRunner()
        seams = Seams(module)
        with seams.watch(), self.assertRaises(ValueError) as caught:
            self.call(runner, module=module, **kwargs)
        self.assertEqual(str(caught.exception), SELECTOR_ERROR)
        self.assertEqual(seams.calls, [])
        self.assertEqual(runner.calls, [])
        self.assertFalse(self.output.exists())
        self.assertEqual(self.blob.read_bytes(), self.raw)


class Behavior(Fixture):
    """A1: preflight refusal, frozen valid shapes, later range gate and unchanged fail-closed paths."""

    def test_malformed_pages_are_refused_before_tools_source_and_pdf(self):
        for label, value in MALFORMED_PAGES.items():
            with self.subTest(pages=label):
                self.assert_refused_before_seams(pages=value)

    def test_malformed_fidelity_holds_are_refused_before_tools_source_and_pdf(self):
        for label, value in MALFORMED_HOLDS.items():
            with self.subTest(fidelity_holds=label):
                self.assert_refused_before_seams(pages=(1,), fidelity_holds=value)

    def test_list_and_tuple_pages_select_the_same_receipts(self):
        runner = FakeRunner()
        from_list = self.call(runner, pages=[2])
        self.assertEqual([r["locator"]["page"] for r in from_list], [2])
        self.assertEqual(from_list[0]["status"], "visual_check_queued")
        self.assertEqual(self.call(runner, pages=(2,)), from_list)
        self.assertEqual(runner.calls, ["pdftoppm", "tesseract"])

    def test_none_pages_selects_every_page_in_order(self):
        receipts = self.call(FakeRunner("97"), pages=None)
        self.assertEqual([r["locator"]["page"] for r in receipts], [1, 2])
        self.assertEqual([r["status"] for r in receipts], ["ocr_text_unreviewed"] * 2)
        self.assertEqual([r["page_count"] for r in receipts], [PAGE_COUNT] * 2)

    def test_empty_selections_remain_valid_and_still_verify_the_source(self):
        for label, kwargs in (("empty_list_pages", {"pages": []}), ("empty_tuple_pages", {"pages": ()})):
            with self.subTest(label):
                runner, seams = FakeRunner(), Seams(ocr)
                with seams.watch():
                    self.assertEqual(self.call(runner, **kwargs), [])
                self.assertEqual(seams.calls, ["_resolved_tools", "_read", "PdfReader"])
                self.assertEqual(runner.calls, [])
        for label, holds in (("empty_list_holds", []), ("empty_tuple_holds", ())):
            with self.subTest(label):
                receipt, = self.call(FakeRunner("97"), pages=(1,), fidelity_holds=holds)
                self.assertEqual(receipt["status"], "ocr_text_unreviewed")
                self.assertFalse(receipt["identity"]["fidelity_hold"])

    def test_duplicate_holds_are_idempotent_membership(self):
        runner = FakeRunner()
        held, = self.call(runner, pages=(1,), fidelity_holds=[1, 1])
        self.assertEqual(held["status"], "blocked")
        self.assertEqual(held["blocked_reason"], "fidelity_hold_requires_visual_comparison")
        self.assertTrue(held["identity"]["fidelity_hold"])
        self.assertEqual(self.call(runner, pages=(1,), fidelity_holds=(1,)), [held])
        self.assertEqual(runner.calls, [])

    def test_page_count_bound_is_checked_after_source_verification(self):
        for label, kwargs in (("page_count_plus_one", {"pages": (PAGE_COUNT + 1,)}),
                              ("hold_count_plus_one", {"pages": (1,), "fidelity_holds": [PAGE_COUNT + 1]})):
            with self.subTest(label):
                runner, seams = FakeRunner(), Seams(ocr)
                with seams.watch(), self.assertRaises(ValueError) as caught:
                    self.call(runner, **kwargs)
                self.assertEqual(str(caught.exception), SELECTOR_ERROR)
                self.assertEqual(seams.calls, ["_resolved_tools", "_read", "PdfReader"])
                self.assertEqual(runner.calls, [])
                self.assertFalse(self.output.exists())
        receipts = self.call(FakeRunner("97"), pages=[PAGE_COUNT], fidelity_holds=(PAGE_COUNT,))
        self.assertEqual([r["locator"]["page"] for r in receipts], [PAGE_COUNT])
        self.assertEqual(receipts[0]["blocked_reason"], "fidelity_hold_requires_visual_comparison")

    def test_selectors_are_snapshotted_before_the_source_read(self):
        pages, holds = [1], []

        def mutate():
            pages[:] = [0, 0]
            holds.append(1)

        runner, seams = FakeRunner("97"), Seams(ocr)
        with seams.watch(on_read=mutate):
            receipts = self.call(runner, pages=pages, fidelity_holds=holds)
        self.assertEqual([r["locator"]["page"] for r in receipts], [1])
        self.assertEqual(receipts[0]["status"], "ocr_text_unreviewed")
        self.assertFalse(receipts[0]["identity"]["fidelity_hold"])

    def test_source_mismatch_stays_fail_closed(self):
        self.blob.write_bytes(synthetic_pdf(3))
        runner = FakeRunner()
        with self.assertRaises(ValueError) as caught:
            self.call(runner, pages=[1])
        self.assertEqual(str(caught.exception), "preserved_blob_hash_mismatch")
        self.assertEqual(runner.calls, [])
        self.assertFalse(self.output.exists())

    def test_existing_fidelity_hold_stays_sticky(self):
        runner = FakeRunner()
        held, = self.call(runner, pages=[1], fidelity_holds=[1])
        self.assertEqual(self.call(runner, pages=[1], fidelity_holds=[]), [held])
        self.assertEqual(self.call(runner, pages=(1,)), [held])
        self.assertEqual(runner.calls, [])
        self.assertTrue((self.output / self.sha / "page-000001" / "fidelity-hold.json").is_file())

    def test_producer_and_cache_positive_with_fake_ocr(self):
        runner = FakeRunner("97")
        receipts = self.call(runner, pages=[1, 2])
        self.assertEqual([r["status"] for r in receipts], ["ocr_text_unreviewed"] * 2)
        for receipt in receipts:
            self.assertEqual(set(receipt["artifact_sha256"]), set(ocr.ARTIFACTS))
            self.assertEqual(receipt["source"], {"kind": "preserved_blob", "sha256": self.sha})
            self.assertEqual(receipt["review_status"], "not_reviewed")
            loaded = ocr.load_page_receipt(self.output, self.sha, receipt["locator"]["page"], receipt["receipt_id"])
            self.assertEqual(loaded["receipt"], receipt)
        self.assertEqual(self.call(runner, pages=(1, 2)), receipts)
        self.assertEqual(runner.calls, ["pdftoppm", "tesseract"] * 2)
        self.assertEqual(self.blob.read_bytes(), self.raw)

    def test_in_tree_callers_use_the_documented_selector_shape(self):
        package = Path(ocr.__file__).resolve().parents[2]
        found = []
        for path in sorted(package.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", None)) \
                        == "extract_image_only_pages":
                    keywords = {k.arg: ast.unparse(k.value) for k in node.keywords if k.arg in ("pages", "fidelity_holds")}
                    found.append((path.relative_to(package).as_posix(), keywords))
        # Each value is a tuple: page_numbers is tuple(...) in _ocr_pages; the CLI's argparse lists are tupled.
        self.assertEqual(found, [
            ("records/extract/ocr.py", {"pages": "tuple(args.page) if args.page else None",
                                        "fidelity_holds": "tuple(args.hold_page)"}),
            ("records/extraction_routes.py", {"pages": "page_numbers"}),
            ("records/extraction_routes.py", {"pages": "(page,)"}),
        ])
        routes = (package / "records" / "extraction_routes.py").read_text(encoding="utf-8")
        self.assertIn("page_numbers=tuple(page_numbers)", routes)


def control_module():
    """An in-memory copy of the staged producer with ONLY the selector preflight disabled."""
    source = Path(ocr.__file__).read_text(encoding="utf-8")
    if source.count(GATE_TEXT) != 1:
        raise AssertionError("FA101 preflight text present %d times in the staged module" % source.count(GATE_TEXT))
    broken = source.replace(GATE_TEXT, GATE_DISABLED, 1)
    module = types.ModuleType("fa101_control_ocr")
    module.__file__ = ocr.__file__
    exec(compile(broken, "<fa101-control " + ocr.__file__ + ">", "exec"), module.__dict__)
    return module


CONTROL_PROBES = {
    "nested_list_pages": {"pages": [[1]]},
    "nested_dict_holds": {"pages": (1,), "fidelity_holds": [{"page": 1}]},
    "string_pages": {"pages": "1"},
    "set_holds": {"pages": (1,), "fidelity_holds": {1}},
}


class GateControl(Fixture):
    """A3: with the preflight disabled, malformed selectors reach the source/PDF seams or raise a
    raw TypeError; the restored staged producer refuses them first and valid selection passes."""

    def outcome(self, module, kwargs):
        runner, seams = FakeRunner("97"), Seams(module)
        try:
            with seams.watch():
                result = self.call(runner, module=module, **kwargs)
            outcome, detail = "returned", [r["locator"]["page"] for r in result]
        except Exception as error:  # recorded, then asserted below
            outcome, detail = type(error).__name__, str(error)[:80]
        return {"outcome": outcome, "detail": detail, "seams": list(seams.calls), "runner": list(runner.calls)}

    def test_disabled_preflight_is_detected_and_restored_positive_passes(self):
        control = control_module()
        for name, module in (("control", control), ("staged", ocr)):
            for label, kwargs in CONTROL_PROBES.items():
                self.setUp()
                CONTROL_OUTCOMES.append({"module": name, "case": label, **self.outcome(module, kwargs)})
            self.setUp()
            CONTROL_OUTCOMES.append({"module": name, "case": "valid_selection",
                                     **self.outcome(module, {"pages": [1, 2], "fidelity_holds": (2, 2)})})
        observed = {(o["module"], o["case"]): o for o in CONTROL_OUTCOMES[-2 * (len(CONTROL_PROBES) + 1):]}
        for label in CONTROL_PROBES:
            broken = observed[("control", label)]
            self.assertTrue(broken["outcome"] == "TypeError" or "_read" in broken["seams"], broken)
            clean_refusal = (broken["outcome"], broken["detail"], broken["seams"]) == ("ValueError", SELECTOR_ERROR, [])
            self.assertFalse(clean_refusal, broken)
            staged = observed[("staged", label)]
            self.assertEqual((staged["outcome"], staged["detail"], staged["seams"], staged["runner"]),
                             ("ValueError", SELECTOR_ERROR, [], []))
        for name in ("control", "staged"):
            valid = observed[(name, "valid_selection")]
            self.assertEqual((valid["outcome"], valid["detail"]), ("returned", [1, 2]), valid)


if __name__ == "__main__":
    unittest.main()
