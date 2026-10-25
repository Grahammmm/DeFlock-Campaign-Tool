"""FC10-LAW-JURISDICTION: package_path accepts only the whole jurisdiction grammar.

campaign_tool.law.package_path must accept exactly the two-letter country,
hyphen, two-letter state form (lowercase ASCII), and refuse anything else with
ValueError before it constructs a path, checks existence or reads a file. In
particular a trailing newline ("us-ca\\n") must not slip through a prefix
match and name a different directory.

Synthetic cases only (tests/fixtures/fc10-law-jurisdiction/cases.json), found
relative to this file. Temporary package trees are built under the tempfile
default root, which the dedicated acceptance runner points inside its own
invocation directory. Ordinary discovery changes no ambient setting:

    python3 -m unittest tests.records.test_fc10_law_jurisdiction -v

Script mode (for a fresh cwd) accepts --temp-root DIR, applied before the
product import, then ordinary unittest arguments.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "fc10-law-jurisdiction" / "cases.json"

if __name__ == "__main__":
    # Script mode only: choose the temporary root and the import root before
    # importing product code. Discovery leaves sys.path and tempfile untouched.
    if "--temp-root" in sys.argv:
        position = sys.argv.index("--temp-root")
        temp_root = Path(sys.argv[position + 1]).absolute()
        del sys.argv[position:position + 2]
        temp_root.mkdir(parents=True, exist_ok=True)
        tempfile.tempdir = str(temp_root)
    sys.path.insert(0, str(REPO_ROOT))

from campaign_tool import law  # noqa: E402

CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))
DIMENSIONS = CASES["dimensions"]
PREFIX = CASES["error_prefix"]


def decode(value):
    """Fixture values are JSON; {"$bytes": text} stands for text encoded as bytes."""
    if isinstance(value, dict) and set(value) == {"$bytes"}:
        return value["$bytes"].encode("utf-8")
    return value


def values(dimension):
    return [decode(value) for value in DIMENSIONS[dimension]["values"]]


class FilesystemSpy:
    """Record (without blocking) Path filesystem probes made by law functions."""

    NAMES = ("is_file", "exists", "is_dir", "read_text", "read_bytes", "open", "stat")

    def __init__(self):
        self.calls = []

    def __enter__(self):
        self.patches = []
        for name in self.NAMES:
            original = getattr(Path, name)

            def spy(path, *args, _name=name, _original=original, **kwargs):
                self.calls.append((_name, str(path)))
                return _original(path, *args, **kwargs)

            patcher = mock.patch.object(Path, name, spy)
            patcher.start()
            self.patches.append(patcher)
        return self

    def __exit__(self, *exc):
        for patcher in reversed(self.patches):
            patcher.stop()
        return False


class JurisdictionGrammarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.real_package = law.JURISDICTIONS / "us-ca" / "package.json"
        cls.package_bytes = cls.real_package.read_bytes()

    def setUp(self):
        holder = tempfile.TemporaryDirectory(prefix="fc10-law-")
        self.addCleanup(holder.cleanup)
        self.base = Path(holder.name)

    def plant(self, name):
        """Write a valid synthetic copy of the us-ca package under ``name``."""
        directory = self.base / name
        directory.mkdir()
        (directory / "package.json").write_bytes(self.package_bytes)
        return directory

    def assert_refused(self, value, base=None):
        with self.assertRaises(ValueError) as caught:
            law.package_path(value, base)
        self.assertEqual(str(caught.exception), PREFIX + repr(value))

    # -- dimensions ---------------------------------------------------------
    def test_fixture_dimensions_all_covered(self):
        expected = {name[len("test_"):] for name in dir(self) if name.startswith("test_")}
        expected.discard("fixture_dimensions_all_covered")
        self.assertEqual(set(DIMENSIONS), expected)
        for name, spec in DIMENSIONS.items():
            self.assertTrue(spec["values"], name)

    def test_ordinary_us_ca_and_other_two_letter_pairs(self):
        for value in values("ordinary_us_ca_and_other_two_letter_pairs"):
            with self.subTest(value=value):
                self.assertEqual(law.package_path(value), law.JURISDICTIONS / value / "package.json")
                self.assertEqual(law.package_path(value, self.base),
                                 self.base / value / "package.json")
                self.assertEqual(law.package_status(value, self.base), ("missing", None))
        self.assertEqual(law.package_status("us-ca"), ("draft", None))

    def test_trailing_newline_cr_crlf(self):
        for value in values("trailing_newline_cr_crlf"):
            with self.subTest(value=value):
                self.assert_refused(value)
                self.assert_refused(value, self.base)

    def test_tabs_unicode_space(self):
        for value in values("tabs_unicode_space"):
            with self.subTest(value=value):
                self.assert_refused(value)
                self.assert_refused(value, self.base)

    def test_leading_space(self):
        for value in values("leading_space"):
            with self.subTest(value=value):
                self.assert_refused(value)

    def test_embedded_linebreak(self):
        for value in values("embedded_linebreak"):
            with self.subTest(value=value):
                self.assert_refused(value)
                self.assert_refused(value, self.base)

    def test_suffix_prefix(self):
        for value in values("suffix_prefix"):
            with self.subTest(value=value):
                self.assert_refused(value)

    def test_uppercase(self):
        for value in values("uppercase"):
            with self.subTest(value=value):
                self.assert_refused(value)

    def test_nonstring(self):
        for value in values("nonstring"):
            with self.subTest(value=value):
                self.assert_refused(value)

    def test_package_status_invalid_is_missing(self):
        for value in values("package_status_invalid_is_missing"):
            with self.subTest(value=value):
                if isinstance(value, str):
                    self.plant(value)  # A matching directory must not rescue it.
                self.assertEqual(law.package_status(value, self.base),
                                 ("missing", PREFIX + repr(value)))
        self.plant("us-ca")
        self.assertEqual(law.package_status("us-ca", self.base), ("draft", None))

    def test_no_read_on_invalid(self):
        for value in values("no_read_on_invalid"):
            with self.subTest(value=value):
                self.plant(value)
                with FilesystemSpy() as spy:
                    with self.assertRaises(ValueError) as caught:
                        law.load_package(value, self.base)
                    status = law.package_status(value, self.base)
                self.assertEqual(str(caught.exception), PREFIX + repr(value))
                self.assertEqual(status, ("missing", PREFIX + repr(value)))
                self.assertEqual(spy.calls, [])
        # Control: the spy does observe probes for an accepted jurisdiction.
        self.plant("us-ca")
        with FilesystemSpy() as spy:
            self.assertEqual(law.load_package("us-ca", self.base)["jurisdiction"], "us-ca")
        self.assertTrue(any(name == "is_file" for name, _ in spy.calls))

    def test_explicit_base_preserved(self):
        for value in values("explicit_base_preserved"):
            with self.subTest(value=value):
                for base in (self.base, str(self.base)):
                    self.assertEqual(law.package_path(value, base),
                                     self.base / value / "package.json")
        self.plant("us-ca")
        self.assertEqual(law.package_status("us-ca", str(self.base)), ("draft", None))
        loaded = law.load_package("us-ca", self.base)
        self.assertEqual(loaded["jurisdiction"], "us-ca")
        self.assertEqual(law.package_path("us-ca", None), law.JURISDICTIONS / "us-ca" / "package.json")


if __name__ == "__main__":
    unittest.main()
