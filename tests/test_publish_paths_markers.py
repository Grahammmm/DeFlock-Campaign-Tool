"""RF0500-8: the publishing-path registry refuses a marker that names no source text to check. Synthetic source only."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from campaign_tool import publish_paths
from tests.test_publish_path_coverage import lint_problems, source_problem

MESSAGE = "publish path marker must be nonblank text or a nonempty tuple of nonblank texts"


def _consumed():
    raise AssertionError("an unsupported marker must not be iterated")
    yield  # pragma: no cover


BAD = ["", "  \t", (), ("",), ("   ",), ("sender.send(", ""), ("", "sender.send("), None, False, 7, b"sender.send(",
       ["sender.send("], {"sender.send(": 1}, (("sender.send(",),), ("sender.send(", b"x"), ("sender.send(", None), _consumed()]


def make(marker, lint="privacy", inherits=None, module="campaign_tool/synthetic_sender.py"):
    return publish_paths._entry("synthetic", module, "send", "post", "public", lint, marker, "worker", "synthetic", inherits=inherits)


class MarkerAdmissionTests(unittest.TestCase):
    def test_entry_refuses_vacuous_markers(self):
        for marker in BAD[:8]:
            with self.subTest(marker=repr(marker)):
                for kwargs in ({}, {"lint": "upstream", "inherits": "site-pages"}):
                    with self.assertRaises(ValueError) as caught:
                        make(marker, **kwargs)
                    self.assertEqual(str(caught.exception), MESSAGE)

    def test_marker_types_refuse_predictably(self):
        for marker in BAD[8:]:
            with self.subTest(marker=type(marker).__name__):
                with self.assertRaises(ValueError) as caught:
                    make(marker)
                self.assertEqual(str(caught.exception), MESSAGE)
                self.assertNotIn("sender", str(caught.exception))

    def test_valid_single_and_tuple_markers_are_preserved(self):
        for marker in ("x", "é≠ lint(", "  sender.send( ", ("x",), ("a(", "b(", "a("), ("  pad(  ",)):
            with self.subTest(marker=repr(marker)):
                entry = make(marker)
                self.assertIs(entry["marker"], marker)
        for entry in publish_paths.PUBLISH_PATHS:
            self.assertIs(publish_paths.check_marker(entry["marker"]), entry["marker"])

    def test_source_problem_refuses_mutated_bad_markers_before_io(self):
        missing = Path(tempfile.gettempdir()) / "rf0500-8-no-such-root"
        for base in (make("x"), make("x", lint="upstream", inherits="site-pages")):
            for marker in BAD:
                with self.subTest(marker=repr(marker)):
                    self.assertEqual(source_problem({**base, "marker": marker}, missing), "synthetic: invalid lint marker")

    def test_upstream_vacuous_marker_cannot_erase_missing_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "campaign_tool").mkdir()
            (root / "campaign_tool/synthetic_sender.py").write_text("def send():\n    pass\n", encoding="utf-8")
            drivers = SimpleNamespace(root=root)
            base = publish_paths.PUBLISH_PATHS[0]
            control = make("sender.send(", lint="upstream", inherits=base["id"])
            problems = lint_problems(drivers, [control])
            self.assertEqual(problems, ["synthetic: campaign_tool/synthetic_sender.py lost its lint call sender.send("])
            for marker in BAD:
                with self.subTest(marker=repr(marker)):
                    problems = lint_problems(drivers, [{**control, "marker": marker}])
                    self.assertEqual(problems, ["synthetic: invalid lint marker"])

    def test_tuple_requires_every_literal_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "campaign_tool").mkdir()
            module = root / "campaign_tool/synthetic_sender.py"
            entry = make(("one(", " two("))
            for text, expected in (("", "one("), ("one(\n", " two("), (" two(\n", "one("), ("two(\none(\n", " two("),
                                   ("one(\n two(\n", None)):
                module.write_text("def send(): pass\n" + text, encoding="utf-8")
                problem = source_problem(entry, root)
                if expected is None:
                    self.assertIsNone(problem)
                else:
                    self.assertEqual(problem, "synthetic: campaign_tool/synthetic_sender.py lost its lint call %s" % expected)


if __name__ == "__main__":
    unittest.main()
