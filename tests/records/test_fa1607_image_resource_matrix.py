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
import io
import unittest

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


if __name__ == "__main__":
    unittest.main()
