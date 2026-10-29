"""FA1607: image extraction resource limits are checked per frame before decoding.

Fake-Pillow resource qualification only, not real decoder qualification. The real
``PIL.Image.open`` is replaced by a test-owned fake whose ``size`` depends on the
current ``seek`` position and which traces every seek/size/load call; no raster
data is allocated. ``campaign_tool.records.extraction_routes._image`` is called
directly (no OCR, image tools, containers or the extraction subprocess route).

The product assigns ``PIL.Image.MAX_IMAGE_PIXELS``; every test restores the real
global and the ``open`` seam in test-owned setup/teardown (test hygiene only, not
a product restoration fix).

Limits under test (campaign_tool/records/extraction_routes.py ``_image``):
at most 1000 frames, at most 40000000 pixels per frame, at most 160000000
pixels summed over frames, each checked before that frame's ``load()``.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from PIL import Image as PILImage

from campaign_tool.records import extraction_routes as er

FRAME_LIMIT = 1000
LOCAL_PIXEL_LIMIT = 40000000
TOTAL_PIXEL_LIMIT = 160000000
FIXTURE_BYTES = b"fa1607 fictional image bytes"


class TracingImage:
    """Seek-dependent dimensions; records calls instead of decoding anything."""

    def __init__(self, frames, form="TIFF", declare_frames=True):
        self._frames = list(frames)
        self._current = 0
        self.format = form
        if declare_frames:
            self.n_frames = len(self._frames)
        self.trace = []
        self.loads = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True
        return False

    def seek(self, frame):
        if not 0 <= frame < len(self._frames):
            raise EOFError("fa1607_seek_outside_fixture")
        self._current = frame
        self.trace.append(("seek", frame))

    @property
    def size(self):
        self.trace.append(("size", self._current))
        return self._frames[self._current]

    def load(self):
        self.trace.append(("load", self._current))
        self.loads.append(self._current)


def image_without_cumulative_accounting(source, raw):
    """LABELLED FAULTY TEST-LOCAL VARIANT for the A3 negative control only.

    Mirrors ``er._image`` but omits only the cumulative pixel accounting. It is
    never used by the ordinary tests and never written into product code.
    """
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = LOCAL_PIXEL_LIMIT
    units = []
    with Image.open(io.BytesIO(raw)) as image:
        frames = getattr(image, "n_frames", 1)
        form = image.format
        if frames > FRAME_LIMIT:
            raise ValueError("image_resource_limit")
        for frame in range(frames):
            image.seek(frame)
            width, height = image.size
            if width * height > LOCAL_PIXEL_LIMIT:
                raise ValueError("image_resource_limit")
            image.load()
            units.append({"kind": "image", "locator": {"image": 1, "frame": frame + 1}, "text": "",
                          "data": {"width": width, "height": height, "frames": frames, "format": form,
                                   "visual_review": "not_done"}})
    return units, "pillow", "faulty-variant"


class FakePillowCase(unittest.TestCase):
    """Owns the PIL seam: patches only ``Image.open`` and restores the real global."""

    READER = None  # None -> the unchanged product reader, looked up at call time

    def setUp(self):
        self._saved_max = PILImage.MAX_IMAGE_PIXELS
        self._saved_open = PILImage.open
        self.addCleanup(self._restore_pil)
        self.opened = []
        self.next_image = None

        def fake_open(stream):
            if not isinstance(stream, io.BytesIO) or stream.getvalue() != FIXTURE_BYTES:
                raise AssertionError("fa1607 fake open received unexpected input")
            self.opened.append(self.next_image)
            return self.next_image

        PILImage.open = fake_open

    def _restore_pil(self):
        PILImage.open = self._saved_open
        PILImage.MAX_IMAGE_PIXELS = self._saved_max

    def read(self, frames, **kwargs):
        self.next_image = TracingImage(frames, **kwargs)
        reader = type(self).READER or er._image
        return reader(None, FIXTURE_BYTES)

    # Shared decisive checks (also driven against the faulty variant by A3).
    def check_frame_count_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.read([(1, 1)] * (FRAME_LIMIT + 1))
        self.assertEqual(str(caught.exception), "image_resource_limit")
        image = self.next_image
        self.assertEqual(image.trace, [])
        self.assertEqual(image.loads, [])
        self.assertTrue(image.closed)

    def check_local_pixels_refused(self):
        frames = [(3, 2), (5, 7), (LOCAL_PIXEL_LIMIT + 1, 1), (2, 2)]
        self.assertLess(sum(w * h for w, h in frames), TOTAL_PIXEL_LIMIT)
        with self.assertRaises(ValueError) as caught:
            self.read(frames)
        self.assertEqual(str(caught.exception), "image_resource_limit")
        image = self.next_image
        self.assertEqual(image.loads, [0, 1])
        self.assertNotIn(("load", 2), image.trace)
        self.assertEqual([e for e in image.trace if e[0] == "seek"], [("seek", 0), ("seek", 1), ("seek", 2)])
        self.assertTrue(image.closed)

    def check_cumulative_refused(self):
        frames = [(8000, 4000)] * 5 + [(1, 1), (2, 2)]
        self.assertTrue(all(w * h <= LOCAL_PIXEL_LIMIT for w, h in frames))
        self.assertEqual(sum(w * h for w, h in frames[:6]), TOTAL_PIXEL_LIMIT + 1)
        with self.assertRaises(ValueError) as caught:
            self.read(frames)
        self.assertEqual(str(caught.exception), "image_resource_limit")
        image = self.next_image
        self.assertEqual(image.loads, [0, 1, 2, 3, 4])
        self.assertNotIn(("load", 5), image.trace)
        self.assertNotIn(("seek", 6), image.trace)
        self.assertTrue(image.closed)

    def assert_units(self, units, frames, form="TIFF"):
        self.assertEqual(len(units), len(frames))
        self.assertEqual([u["locator"] for u in units],
                         [{"image": 1, "frame": n} for n in range(1, len(frames) + 1)])
        self.assertEqual([(u["data"]["width"], u["data"]["height"]) for u in units], list(frames))
        for unit in units:
            self.assertEqual(unit["kind"], "image")
            self.assertEqual(unit["text"], "")
            self.assertEqual(unit["data"]["visual_review"], "not_done")
            self.assertEqual(unit["data"]["frames"], len(frames))
            self.assertEqual(unit["data"]["format"], form)


class FA1607ImageResourceMatrixTests(FakePillowCase):
    def test_exactly_frame_limit_of_tiny_frames_is_accepted(self):
        frames = [(1, 1)] * FRAME_LIMIT
        units, parser, _ = self.read(frames)
        self.assertEqual(parser, "pillow")
        self.assert_units(units, frames)
        self.assertEqual(self.next_image.loads, list(range(FRAME_LIMIT)))
        self.assertEqual(units[-1]["locator"], {"image": 1, "frame": 1000})

    def test_exactly_local_pixel_limit_in_one_frame_is_accepted(self):
        frames = [(8000, 5000)]
        self.assertEqual(8000 * 5000, LOCAL_PIXEL_LIMIT)
        units, _, _ = self.read(frames, form="PNG", declare_frames=False)
        self.assert_units(units, frames, form="PNG")
        self.assertEqual(self.next_image.loads, [0])

    def test_exactly_total_pixel_limit_across_safe_frames_is_accepted(self):
        frames = [(8000, 4000)] * 5
        self.assertTrue(all(w * h < LOCAL_PIXEL_LIMIT for w, h in frames))
        self.assertEqual(sum(w * h for w, h in frames), TOTAL_PIXEL_LIMIT)
        units, _, _ = self.read(frames)
        self.assert_units(units, frames)
        self.assertEqual(self.next_image.loads, [0, 1, 2, 3, 4])

    def test_mixed_frame_dimensions_follow_seek_and_load_after_check(self):
        frames = [(8000, 5000), (5000, 8000), (10000, 4000), (4000, 10000)]
        self.assertEqual(sum(w * h for w, h in frames), TOTAL_PIXEL_LIMIT)
        units, _, _ = self.read(frames, form="GIF")
        self.assert_units(units, frames, form="GIF")
        expected = []
        for n in range(len(frames)):
            expected += [("seek", n), ("size", n), ("load", n)]
        self.assertEqual(self.next_image.trace, expected)
        self.assertTrue(self.next_image.closed)

    def test_product_sets_max_image_pixels_inside_scoped_seam(self):
        self.read([(2, 3)])
        self.assertEqual(PILImage.MAX_IMAGE_PIXELS, LOCAL_PIXEL_LIMIT)

    def test_frame_count_over_limit_refused_before_any_seek_or_load(self):
        self.check_frame_count_refused()

    def test_local_pixels_over_limit_refused_before_offending_load(self):
        self.check_local_pixels_refused()

    def test_cumulative_pixels_over_limit_refused_before_offending_load(self):
        self.check_cumulative_refused()


class FA1607SeamHygieneTests(unittest.TestCase):
    def test_setup_and_teardown_restore_real_pil_global_and_open(self):
        sentinel = 12345
        real_open = PILImage.open
        saved = PILImage.MAX_IMAGE_PIXELS
        PILImage.MAX_IMAGE_PIXELS = sentinel
        try:
            inner = FA1607ImageResourceMatrixTests("test_product_sets_max_image_pixels_inside_scoped_seam")
            result = unittest.TestResult()
            inner.run(result)
            self.assertEqual((result.testsRun, result.failures, result.errors), (1, [], []))
            self.assertEqual(PILImage.MAX_IMAGE_PIXELS, sentinel)
            self.assertIs(PILImage.open, real_open)
        finally:
            PILImage.MAX_IMAGE_PIXELS = saved


class FA1607FaultyReaderControlTests(unittest.TestCase):
    """A3: only the cumulative assertion detects a reader lacking cumulative accounting."""

    def run_check(self, reader, check):
        case_type = type("FaultyReaderCase", (FakePillowCase,), {
            "READER": staticmethod(reader) if reader else None, "runTest": lambda self: getattr(self, check)()})
        result = unittest.TestResult()
        case_type().run(result)
        return result

    def test_faulty_reader_fails_only_cumulative_check(self):
        faulty = self.run_check(image_without_cumulative_accounting, "check_cumulative_refused")
        self.assertEqual((faulty.testsRun, len(faulty.errors), len(faulty.failures)), (1, 0, 1))
        self.assertIn("ValueError not raised", faulty.failures[0][1])
        for check in ("check_frame_count_refused", "check_local_pixels_refused"):
            control = self.run_check(image_without_cumulative_accounting, check)
            self.assertEqual((control.testsRun, control.errors, control.failures), (1, [], []), check)
        for check in ("check_frame_count_refused", "check_local_pixels_refused", "check_cumulative_refused"):
            restored = self.run_check(None, check)
            self.assertEqual((restored.testsRun, restored.errors, restored.failures), (1, [], []), check)
        self.assertEqual(PILImage.open.__module__, "PIL.Image")
        positives = unittest.TestSuite(FA1607ImageResourceMatrixTests(name) for name in POSITIVE_TESTS)
        rerun = unittest.TestResult()
        positives.run(rerun)
        self.assertEqual((rerun.testsRun, rerun.errors, rerun.failures, rerun.skipped),
                         (len(POSITIVE_TESTS), [], [], []))
        self.assertEqual(PILImage.open.__module__, "PIL.Image")


POSITIVE_TESTS = (
    "test_exactly_frame_limit_of_tiny_frames_is_accepted",
    "test_exactly_local_pixel_limit_in_one_frame_is_accepted",
    "test_exactly_total_pixel_limit_across_safe_frames_is_accepted",
    "test_mixed_frame_dimensions_follow_seek_and_load_after_check",
)


def load_acceptance_helper():
    """Load tools/accept_fa1607.py by path (not registered in sys.modules)."""
    path = Path(__file__).resolve().parents[2] / "tools" / "accept_fa1607.py"
    spec = importlib.util.spec_from_file_location("fa1607_acceptance_helper_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FA1607LauncherEvidenceTests(unittest.TestCase):
    """Launcher evidence contract with mocked children only; no child process is started."""

    def setUp(self):
        self.helper = load_acceptance_helper()
        holder = tempfile.TemporaryDirectory(prefix="fa1607-launcher-")
        self.addCleanup(holder.cleanup)
        self.run_dir = Path(holder.name)
        self.record = {"run_directory": str(self.run_dir), "candidate_head": "fa1607-fictional-head"}
        self.calls = []
        self.answers = []
        patcher = mock.patch.object(self.helper.subprocess, "run", side_effect=self.fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_run(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs.get("cwd")))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            return answer(argv)
        return subprocess.CompletedProcess(argv, *answer)

    def assert_retained(self, entry, stdout, stderr):
        for label, raw in (("stdout", stdout), ("stderr", stderr)):
            path = Path(entry[label + "_path"])
            self.assertEqual(path.parent, self.run_dir / "children")
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(entry[label + "_bytes"], len(raw))
            self.assertEqual(entry[label + "_sha256"], hashlib.sha256(raw).hexdigest())
        receipt = self.run_dir / "children" / "{:02d}.json".format(entry["index"])
        self.assertEqual(json.loads(receipt.read_text()), entry)

    def test_completed_child_streams_are_retained_byte_for_byte(self):
        good = (b"fa1607 fictional stdout\n\x00\xff", b"fa1607 fictional warning\n")
        bad = (b"fa1607 partial output", b"fa1607 fictional failure detail\n")
        self.answers = [(0, *good), (7, *bad)]
        first = self.helper.run_child(["fictional-tool", "ok"], self.run_dir, self.record)
        second = self.helper.run_child(["fictional-tool", "fail"], self.run_dir, self.record)
        self.assertEqual((first.returncode, second.returncode), (0, 7))
        entries = self.record["subprocesses"]
        self.assertEqual([(e["index"], e["exit"]) for e in entries], [(0, 0), (1, 7)])
        self.assert_retained(entries[0], *good)
        self.assert_retained(entries[1], *bad)

    def test_launch_failure_is_recorded_not_raised(self):
        self.answers = [FileNotFoundError("fa1607 fictional missing cwd")]
        done = self.helper.run_child(["fictional-tool"], self.run_dir / "absent", self.record)
        self.assertEqual(done.returncode, self.helper.LAUNCH_FAILED)
        entry = self.record["subprocesses"][0]
        self.assertIsNone(entry["exit"])
        self.assertIn("FileNotFoundError: fa1607 fictional missing cwd", entry["launch_error"])
        receipt = json.loads((self.run_dir / "children" / "00.json").read_text())
        self.assertEqual(receipt, entry)

    def test_failed_clone_stops_before_checkout(self):
        self.answers = [(128, b"", b"fatal: fa1607 fictional clone failure\n")]
        problems = []
        dest = self.helper.scan_checkout(self.run_dir, "scan-checkout", self.record, problems)
        self.assertIsNone(dest)
        self.assertEqual(problems, ["scan_checkout_clone_failed:scan-checkout"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0][:2], ["git", "clone"])
        self.assert_retained(self.record["subprocesses"][0], b"", b"fatal: fa1607 fictional clone failure\n")

    def test_failed_checkout_stops_before_dependent_checks(self):
        def clone(argv):
            Path(argv[-1]).mkdir()
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        self.answers = [clone, (1, b"", b"error: fa1607 fictional checkout failure\n")]
        problems = []
        with mock.patch.object(self.helper, "git", side_effect=AssertionError("no dependent git check")):
            dest = self.helper.scan_checkout(self.run_dir, "public-tree-checkout", self.record, problems)
        self.assertIsNone(dest)
        self.assertEqual(problems, ["scan_checkout_checkout_failed:public-tree-checkout"])
        self.assertEqual([c[0][:2] for c in self.calls], [["git", "clone"], ["git", "checkout"]])
        self.assertEqual(self.calls[1][1], self.run_dir / "public-tree-checkout")
        self.assert_retained(self.record["subprocesses"][1], b"", b"error: fa1607 fictional checkout failure\n")

    def test_case_exception_becomes_fatal_problem(self):
        problems, stream = [], io.StringIO()

        def broken_case():
            raise RuntimeError("fa1607 fictional case failure")

        def exiting_case():
            raise SystemExit("fa1607 fictional exit")

        self.helper.run_case(broken_case, problems, stream)
        self.helper.run_case(exiting_case, problems, stream)
        self.assertEqual(problems, ["aborted:broken_case:RuntimeError:fa1607 fictional case failure",
                                    "aborted:exiting_case:SystemExit:fa1607 fictional exit"])
        self.assertIn("Traceback", stream.getvalue())
        self.assertIn("fa1607 fictional case failure", stream.getvalue())

    def test_main_seals_nonzero_result_when_setup_and_seal_fail(self):
        failing_git = mock.patch.object(self.helper, "git", side_effect=SystemExit("git_failed:fa1607 fictional"))
        fixed_run = mock.patch.object(self.helper, "make_run_directory", return_value=(self.run_dir, self.run_dir))
        printed = io.StringIO()
        with failing_git, fixed_run, contextlib.redirect_stdout(printed):
            code = self.helper.main(["--case", "behavior", "--output-root", str(self.run_dir)])
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, [])
        result = json.loads((self.run_dir / "result.json").read_text())
        self.assertEqual(result["exit"], 1)
        self.assertEqual(result["problems"], ["aborted:setup:SystemExit:git_failed:fa1607 fictional",
                                              "aborted:seal:SystemExit:git_failed:fa1607 fictional"])
        output = (self.run_dir / "output.txt").read_bytes()
        self.assertEqual(result["output_sha256"], hashlib.sha256(output).hexdigest())
        self.assertIn(b"aborted during setup", output)

    def test_seal_flags_launch_failure_receipt(self):
        problems = []
        record = {"subprocesses": [{"argv": ["fictional-tool"], "exit": None}],
                  "status_before": [], "source_sha256": {}}
        with mock.patch.object(self.helper, "git", return_value=""), \
                mock.patch.object(self.helper, "hashes", return_value={}):
            self.helper.seal(self.run_dir, record, problems, io.StringIO(), None, [])
        self.assertEqual(problems, ["child_process_failed:fictional-tool"])
        self.assertEqual(record["problems"], problems)


if __name__ == "__main__":
    unittest.main()
