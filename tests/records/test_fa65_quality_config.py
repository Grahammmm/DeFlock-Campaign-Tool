"""FA65: real OCR quality/configuration boundary, synthetic offline data only.

Replaces the incomplete gpt-6-luna harness retained with the card. GateControl's
coercing builder intentionally reproduces the four-line pinned-base builder;
it is a mutation, not the implementation tested by Regression.
"""
import argparse
from contextlib import ExitStack, contextmanager
import io
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest import mock
import urllib.request


def denied(*args, **kwargs):
    raise AssertionError("FA65 offline sentinel: unexpected external execution")


@contextmanager
def offline():
    """Scoped sentinels, installed BEFORE import; never poison peer suites.

    Inspected import-time closure: both package initializers are inert;
    ocr_stage's top-level imports are standard library only. Lazy imports in
    process() are not reached by this slice. No backend is probed at import.
    These are not a hostile-code sandbox.
    """
    with ExitStack() as stack:
        for owner, name in ((subprocess, "Popen"), (socket, "create_connection"),
                            (socket.socket, "connect"), (socket, "getaddrinfo"),
                            (urllib.request, "urlopen"),
                            (urllib.request.OpenerDirector, "open")):
            stack.enter_context(mock.patch.object(owner, name, side_effect=denied))
        yield


with offline():
    from campaign_tool.records import ocr_stage as stage


INVALID_CHARS = (
    ("bool_true", True), ("bool_false", False), ("null", None),
    ("numeric_string", "1"), ("list", []), ("object", {}),
    ("negative", -1), ("fractional", 1.5), ("integral_float", 1.0),
)
INVALID_RATIOS = (
    ("bool_true", True), ("bool_false", False), ("null", None),
    ("numeric_string", "0.6"), ("list", []), ("object", {}),
    ("negative", -0.1), ("greater_than_one", 1.1),
    ("nan", float("nan")), ("positive_infinity", float("inf")),
    ("negative_infinity", float("-inf")), ("huge_integer", 10 ** 400),
)
VALID = (
    (0, 0), (0, 1), (0, 0.0), (0, 1.0), (1, 0.5), (20, 0.6),
    (10 ** 400, 0.6),
)


def require_refusal(test, function):
    # Convert incidental baseline exceptions into explicit assertion failures,
    # never count an unrelated exception as proof of the promised typed error.
    try:
        function()
    except Exception as error:
        test.assertIs(type(error), ValueError, repr(error))
        test.assertEqual(str(error), "quality_config_invalid")
    else:
        test.fail("missing ValueError quality_config_invalid")


class Regression(unittest.TestCase):
    def setUp(self):
        self.guards = offline()
        self.guards.__enter__()
        self.addCleanup(self.guards.__exit__, None, None, None)
        # Retain synthetic fixtures. The evidence driver sets tempfile.tempdir
        # beneath its unique output root before loading this module.
        self.root = Path(tempfile.mkdtemp(prefix="fa65-quality-"))

    def config(self, values):
        path = self.root / "config.json"
        path.write_text(json.dumps({"config_schema": "ocr-stage-config-v1", **values}),
                        encoding="utf-8")
        path.chmod(0o600)
        return stage.load_config(str(path))

    def build(self, values, name="stage"):
        # All bound operational defaults are avoided: synthetic executable paths
        # and explicit runner/transport, even for valid configurations.
        config = {"ocrmypdf": str(self.root / "synthetic-ocrmypdf"),
                  "pdftoppm": str(self.root / "synthetic-pdftoppm"),
                  "vision": {"endpoint": "http://127.0.0.1:1/v1/chat/completions",
                             "model": "synthetic-fixture"}, **values}
        return stage.build_stage(str(self.root / name), self.config(config),
                                 runner=denied, transport=denied)

    def test_invalid_constructor(self):
        for field, cases in (("min_chars", INVALID_CHARS),
                             ("min_alnum_ratio", INVALID_RATIOS)):
            for branch, value in cases:
                with self.subTest(path="Quality", field=field, branch=branch):
                    require_refusal(self, lambda: stage.Quality(**{field: value}))

    def test_invalid_config_to_build_stage(self):
        for field, cases in (("min_chars", INVALID_CHARS),
                             ("min_alnum_ratio", INVALID_RATIOS)):
            for branch, value in cases:
                with self.subTest(path="load_config/build_stage", field=field, branch=branch):
                    with mock.patch.object(stage, "OcrmypdfBackend", wraps=stage.OcrmypdfBackend) as primary, \
                         mock.patch.object(stage, "vision_backend", wraps=stage.vision_backend) as fallback, \
                         mock.patch.object(stage, "OcrStage", wraps=stage.OcrStage) as constructor:
                        require_refusal(self, lambda: self.build({field: value}))
                        primary.assert_not_called()
                        fallback.assert_not_called()
                        constructor.assert_not_called()
                    self.assertFalse((self.root / "stage").exists())

    def test_invalid_public_run_preserves_no_output(self):
        # Consumer proof in addition to the exact ValueError public builder.
        for field, value in (("min_chars", True), ("min_chars", 1.5),
                             ("min_alnum_ratio", float("nan"))):
            with self.subTest(field=field, value=repr(value)):
                path = self.root / "config.json"
                path.write_text(json.dumps({field: value}), encoding="utf-8")
                path.chmod(0o600)
                args = argparse.Namespace(config=str(path), out=str(self.root / "output"),
                                          queue=str(self.root / "absent-queue"),
                                          source_root=str(self.root / "absent-source"), max_documents=1)
                with mock.patch.object(stage.OcrmypdfBackend, "available", return_value=False) as probe, \
                     mock.patch.object(stage, "read_queue", side_effect=denied):
                    line, code, outcomes = stage.run(args, runner=denied, transport=denied)
                self.assertNotEqual(code, 0)
                self.assertEqual(line["error_code"], "OCR_CONFIG_INVALID")
                probe.assert_not_called()
                self.assertEqual(outcomes, [])
                self.assertFalse((self.root / "output").exists())

    def test_valid_constructor_and_classification(self):
        self.assertEqual((stage.Quality().min_chars, stage.Quality().min_alnum_ratio), (20, 0.6))
        for chars, ratio in VALID:
            with self.subTest(chars=chars, ratio=ratio):
                quality = stage.Quality(chars, ratio)
                self.assertIs(type(quality.min_chars), int)
                self.assertEqual(quality.min_chars, chars)
                self.assertEqual(quality.min_alnum_ratio, ratio)
                self.assertEqual(quality.check(" \t\n"), "ocr_empty")
        for chars, ratio, text, expected in (
            (20, 0.6, "!!!", "ocr_low_quality"),
            (20, 0.6, "This is readable OCR text.", None),
            (0, 0, "!!!", None), (0, 1, "!!!", "ocr_low_quality"),
            (0, 1.0, "é９字", None), (1, 0.5, "é!", None),
            (1, 0.6, "é!", "ocr_low_quality"), (2, 0, "A", "ocr_low_quality"),
        ):
            with self.subTest(chars=chars, ratio=ratio, text=text):
                self.assertEqual(stage.Quality(chars, ratio).check(text), expected)

    def test_valid_config_to_build_stage_and_defaults(self):
        for index, (chars, ratio) in enumerate(VALID):
            with self.subTest(chars=chars, ratio=ratio):
                built = self.build({"min_chars": chars, "min_alnum_ratio": ratio}, str(index))
                self.assertEqual(built.quality, stage.Quality(chars, ratio))
                self.assertIs(built.primary.runner, denied)
                self.assertIs(built.fallback.runner, denied)
                self.assertIs(built.fallback.transport, denied)
                self.assertFalse(built.out_root.exists())
        built = self.build({})
        self.assertEqual(built.quality, stage.Quality(20, 0.6))
        self.assertFalse(built.out_root.exists())


class _DecidingAssertions(unittest.TestCase):
    """Same deciding assertions on otherwise valid inputs, reused in controls."""
    def test_constructor_bool(self):
        require_refusal(self, lambda: stage.Quality(True, 0.6))

    def test_constructor_fractional(self):
        require_refusal(self, lambda: stage.Quality(1.5, 0.6))

    def test_constructor_nan(self):
        require_refusal(self, lambda: stage.Quality(0, float("nan")))

    def test_builder_bool(self):
        require_refusal(self, lambda: stage.build_stage("synthetic-unused", {"min_chars": True},
                                                      runner=denied, transport=denied))

    def test_builder_fractional(self):
        require_refusal(self, lambda: stage.build_stage("synthetic-unused", {"min_chars": 1.5},
                                                      runner=denied, transport=denied))

    def test_builder_nan(self):
        require_refusal(self, lambda: stage.build_stage("synthetic-unused", {"min_chars": 0,
                          "min_alnum_ratio": float("nan")}, runner=denied, transport=denied))

    def test_legitimate_zero_boundaries(self):
        for ratio, text in ((0, "!"), (1, "A")):
            self.assertIsNone(stage.Quality(0, ratio).check(text))
            built = stage.build_stage("synthetic-unused", {"min_chars": 0, "min_alnum_ratio": ratio},
                                      runner=denied, transport=denied)
            self.assertEqual(built.quality, stage.Quality(0, ratio))
            self.assertFalse(built.out_root.exists())


def coercing_builder(out_root, config, runner=denied, transport=denied):
    """Isolated in-memory mutation: pinned-base builder, no source-file writes."""
    primary = stage.OcrmypdfBackend(config.get("ocrmypdf", stage.DEFAULT_OCRMYPDF),
                                   config.get("language", "eng"), config.get("ocrmypdf_timeout", 900), runner=runner)
    fallback = stage.vision_backend(config, runner=runner, transport=transport)
    quality = stage.Quality(int(config.get("min_chars", 20)), float(config.get("min_alnum_ratio", 0.6)))
    return stage.OcrStage(out_root, primary, fallback, quality)


def control_run(label):
    stream = io.StringIO()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(_DecidingAssertions)
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    print("FA65 CONTROL " + label + "\n" + stream.getvalue())
    return result


class GateControl(unittest.TestCase):
    def test_unchecked_constructor_and_coercion_detected_and_restored(self):
        with offline(), mock.patch.object(stage.Quality, "__post_init__", lambda self: None), \
             mock.patch.object(stage, "build_stage", coercing_builder):
            result = control_run("unchecked-constructor-and-coercing-builder")
        self.assertEqual(result.testsRun, 7)
        self.assertEqual(len(result.errors), 0)
        self.assertEqual({case._testMethodName for case, _ in result.failures},
                         {"test_constructor_bool", "test_constructor_fractional", "test_constructor_nan",
                          "test_builder_bool", "test_builder_fractional", "test_builder_nan"})
        self.assertEqual(result.skipped, [])
        with offline():
            restored = control_run("restored-constructor")
        self.assertTrue(restored.wasSuccessful())
        self.assertEqual(restored.testsRun, 7)

    def test_coercing_builder_detected_and_restored(self):
        with offline(), mock.patch.object(stage, "build_stage", coercing_builder):
            result = control_run("coercing-builder")
        self.assertEqual(result.testsRun, 7)
        self.assertEqual(len(result.errors), 0)
        self.assertEqual({case._testMethodName for case, _ in result.failures},
                         {"test_builder_bool", "test_builder_fractional"})
        self.assertEqual(result.skipped, [])
        with offline():
            restored = control_run("restored-builder")
        self.assertTrue(restored.wasSuccessful())
        self.assertEqual(restored.testsRun, 7)


if __name__ == "__main__":
    unittest.main()
