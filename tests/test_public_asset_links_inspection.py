"""RF1550-3: an acknowledged asset whose metadata cannot be inspected is one fixed, path-free, reportable ContentError.
Synthetic data only; no network. Real chmod cases skip only when the platform does not enforce directory permissions."""
import errno
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.content import ContentError, load_content, load_site
from campaign_tool.content_check import content_problems
from campaign_tool.public_asset_links import validate_unlinked_assets_ok

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
LABEL = "site.json.unlinked_assets_ok"
CANNOT = LABEL + "[{}]: cannot inspect file in content/public-assets/"
MISSING = LABEL + "[{}]: file not found in content/public-assets/"
SYMLINK = LABEL + "[{}]: is or sits under a symlink; use a regular file in content/public-assets/"
MARKERS = ("MARKER-FILENAME", "MARKER-FILENAME2", "MARKER-STRERROR", "multiline", "é", "Errno", "Permission denied")


def hostile(code, name="MARKER-FILENAME"):
    return OSError(code, "MARKER-STRERROR\nmultiline é", name, None, "MARKER-FILENAME2")


class Inject:
    """Replace the metadata seam (Path.lstat): ``fail`` maps a path relative to the assets root ('' for the root) to an
    exception; every inspected path is recorded in ``seen``."""

    def __init__(self, root, fail):
        self.root, self.fail, self.seen = Path(root), fail, []
        self.real = Path.lstat

    def __enter__(self):
        def fake(path, *args, **kwargs):
            try:
                key = path.relative_to(self.root).as_posix()
            except ValueError:
                return self.real(path, *args, **kwargs)
            key = "" if key == "." else key
            self.seen.append(key)
            if key in self.fail:
                raise self.fail[key]
            return self.real(path, *args, **kwargs)
        self.patch = mock.patch.object(Path, "lstat", fake)
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()


class MetadataSeamTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-inspect-")
        self.addCleanup(temporary.cleanup)
        self.assets = Path(temporary.name) / "assets"
        (self.assets / "docs").mkdir(parents=True)
        (self.assets / "docs" / "h.pdf").write_bytes(b"x")
        (self.assets / "a.pdf").write_bytes(b"x")

    def test_metadata_oserrors_are_fixed_at_every_component(self):
        # "" is the assets root (inspected by the first entry); entry 0 is a.pdf, entry 1 is docs/h.pdf
        for where, index in (("", 0), ("docs", 1), ("docs/h.pdf", 1)):
            for code in (errno.EACCES, errno.EIO, errno.ENAMETOOLONG):
                with self.subTest(where=where, code=code), Inject(self.assets, {where: hostile(code)}) as seam:
                    with self.assertRaises(ContentError) as raised:
                        validate_unlinked_assets_ok(["a.pdf", "docs/h.pdf"], self.assets)
                    text = str(raised.exception)
                    self.assertEqual(text, CANNOT.format(index))
                    for marker in MARKERS:
                        self.assertNotIn(marker, text)
                    self.assertNotIn(str(self.assets), text)
                    self.assertIsNone(raised.exception.__cause__)
                    self.assertTrue(raised.exception.__suppress_context__)
                    self.assertEqual(seam.seen[-1], where)  # inspection stops at the failed component

    def test_missing_nonregular_and_symlink_diagnostics_are_preserved(self):
        root = self.assets
        (root / "plain").write_bytes(b"x")
        (root / "dir").mkdir()
        os.mkfifo(root / "fifo")
        (root / "docs" / "link").symlink_to(root / "docs" / "h.pdf")
        (root / "linkdir").symlink_to(root / "docs")
        (root / "dangling.pdf").symlink_to(root / "nope")
        cases = {
            "missing.pdf": MISSING, "nodir/x.pdf": MISSING, "docs/missing.pdf": MISSING, "plain/x.pdf": MISSING,
            "dir": MISSING, "fifo": MISSING, "docs/link": SYMLINK, "linkdir/h.pdf": SYMLINK, "dangling.pdf": SYMLINK,
        }
        for entry, template in cases.items():
            with self.subTest(entry=entry), self.assertRaises(ContentError) as raised:
                validate_unlinked_assets_ok(["a.pdf", entry], root)
            self.assertEqual(str(raised.exception), template.format(1))
        with self.subTest("root"):
            (root / "root-link").symlink_to(root)
            with self.assertRaises(ContentError) as raised:
                validate_unlinked_assets_ok(["a.pdf"], root / "root-link")
            self.assertEqual(str(raised.exception), SYMLINK.format(0))
            with self.assertRaises(ContentError) as raised:
                validate_unlinked_assets_ok(["a.pdf"], root / "no-such-root")
            self.assertEqual(str(raised.exception), MISSING.format(0))

    def test_syntax_duplicate_and_index_precedence(self):
        with Inject(self.assets, {"": hostile(errno.EACCES)}) as seam:
            self.assertEqual(validate_unlinked_assets_ok(["a.pdf", "docs/h.pdf"]), ["a.pdf", "docs/h.pdf"])
            self.assertEqual(seam.seen, [])  # syntax only: zero metadata calls
            for value in (["/bad", "a.pdf"], [f"f{n}.pdf" for n in range(51)], []):  # refused before any inspection
                with self.assertRaises(ContentError) as raised:
                    validate_unlinked_assets_ok(value, self.assets)
                self.assertNotIn("cannot inspect", str(raised.exception))
            self.assertEqual(seam.seen, [])
        with Inject(self.assets, {"docs": hostile(errno.EACCES)}):
            with self.assertRaises(ContentError) as raised:  # duplicate is found on entry 1, before docs is reached
                validate_unlinked_assets_ok(["a.pdf", "a.pdf", "docs/h.pdf"], self.assets)
            self.assertEqual(str(raised.exception), LABEL + "[1]: repeats an earlier entry")
        # entry 0 is inspected successfully first; entry 1 is the only index named; the first error in entry order wins
        with Inject(self.assets, {"docs": hostile(errno.EACCES)}):
            with self.assertRaises(ContentError) as raised:
                validate_unlinked_assets_ok(["a.pdf", "docs/h.pdf", "/bad"], self.assets)
            self.assertEqual(str(raised.exception), CANNOT.format(1))
            with self.assertRaises(ContentError) as raised:
                validate_unlinked_assets_ok(["a.pdf", "/bad", "docs/h.pdf"], self.assets)
            self.assertIn("[1]: use a plain relative path", str(raised.exception))

    def test_exception_boundary_does_not_swallow_programmer_errors(self):
        for failure in (RuntimeError("programmer"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__), Inject(self.assets, {"docs/h.pdf": failure}):
                with self.assertRaises(type(failure)):
                    validate_unlinked_assets_ok(["docs/h.pdf"], self.assets)
        with Inject(self.assets, {"": RuntimeError("never reached")}) as seam:
            self.assertEqual(validate_unlinked_assets_ok(["a.pdf"]), ["a.pdf"])
            self.assertEqual(seam.seen, [])

    def test_regular_file_controls_do_not_add_readability_policy(self):
        (self.assets / "docs" / "h.pdf").chmod(0)
        os.link(self.assets / "a.pdf", self.assets / "docs" / "linked.pdf")
        self.addCleanup(lambda: (self.assets / "docs" / "h.pdf").chmod(0o600))
        value = ["docs/h.pdf", "a.pdf"]
        result = validate_unlinked_assets_ok(value, self.assets)
        self.assertEqual(result, value)
        self.assertIsNot(result, value)  # a fresh list
        self.assertEqual(validate_unlinked_assets_ok(["docs/linked.pdf"], self.assets), ["docs/linked.pdf"])


class CampaignTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-inspect-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "SYNTHETIC-PRIVATE-PATH-MARKER"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.assets = self.root / "content" / "public-assets"
        (self.assets / "docs").mkdir(parents=True)
        (self.assets / "docs" / "handout.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        self.site = self.root / "content" / "site.json"
        self.document = json.loads(self.site.read_text(encoding="utf-8"))
        self.document["unlinked_assets_ok"] = ["docs/handout.pdf"]
        self.site.write_text(json.dumps(self.document), encoding="utf-8")

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args, "--directory", str(self.root)],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=300)

    def snapshot(self):
        out = {}
        for path in sorted(self.root.parent.rglob("*")):
            info = path.lstat()
            key = path.relative_to(self.root.parent).as_posix()
            if stat.S_ISREG(info.st_mode):
                out[key] = (info.st_mode, info.st_mtime_ns, path.read_bytes())
            else:
                out[key] = (info.st_mode, info.st_mtime_ns)
        return out

    def deny(self):
        """chmod the acknowledged file's parent to 000; skip when the platform still lets the file be inspected."""
        parent = self.assets / "docs"
        parent.chmod(0)
        self.addCleanup(parent.chmod, 0o755)
        try:
            (parent / "handout.pdf").lstat()
        except OSError:
            return parent
        parent.chmod(0o755)
        self.skipTest("directory search permission is not enforced here")

    def assert_path_free(self, text):
        self.assertNotIn("SYNTHETIC-PRIVATE-PATH-MARKER", text)
        self.assertNotIn("Errno", text)
        self.assertNotIn("Permission denied", text)
        self.assertNotIn("Check who owns the folder", text)

    def assert_fixed(self, text):
        self.assertIn(CANNOT.format(0), text)
        self.assert_path_free(text)

    def test_real_search_permission_denial_reaches_structured_cli(self):
        parent = self.deny()
        with self.assertRaises(ContentError) as raised:
            validate_unlinked_assets_ok(["docs/handout.pdf"], self.assets)
        self.assertEqual(str(raised.exception), CANNOT.format(0))
        with self.assertRaises(ContentError):
            load_site(self.document, self.assets)
        with self.assertRaises(ContentError):
            load_content(self.root)
        problems = content_problems(self.root)
        self.assertEqual(len(problems), 1, problems)
        self.assert_fixed(problems[0])
        text = self.cli("content-check")
        self.assertEqual((text.returncode, text.stderr), (3, ""), text.stdout)
        self.assert_fixed(text.stdout)
        self.assertTrue(text.stdout.startswith("content/: 1 problem(s)"))
        report = self.cli("content-check", "--json")
        self.assertEqual((report.returncode, report.stderr), (3, ""))
        document = json.loads(report.stdout)
        self.assert_path_free(report.stdout)
        self.assertEqual(len(document["problems"]), 1)
        self.assertEqual(document["problems"][0], {"file": LABEL + "[0]", "message": "cannot inspect file in content/public-assets/"})
        self.assertEqual(parent.stat().st_mode & 0o777, 0)

    def test_build_refuses_before_first_or_replacement_output(self):
        self.deny()
        for args in (("build",), ("build", "--check")):
            with self.subTest(args=args):
                result = self.cli(*args)
                self.assertEqual((result.returncode, result.stdout), (1, ""))
                self.assertEqual(result.stderr.strip(), "Stopped: " + CANNOT.format(0))
                self.assert_fixed(result.stderr)
                self.assertNotIn("written to", result.stderr + result.stdout)
                self.assertFalse((self.root / "public").exists())
                self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".public")], [])
        # an existing public tree is preserved across a repeated refusal
        (self.root / "public").mkdir()
        (self.root / "public" / "sentinel.txt").write_text("keep", encoding="utf-8")
        for _ in range(2):
            self.assertEqual(self.cli("build").returncode, 1)
        self.assertEqual((self.root / "public" / "sentinel.txt").read_text(encoding="utf-8"), "keep")
        self.assertEqual(sorted(p.name for p in (self.root / "public").iterdir()), ["sentinel.txt"])

    def test_refusal_is_read_only_and_recovery_uses_same_inputs(self):
        parent = self.deny()
        before = self.snapshot()
        for _ in range(2):
            self.assertEqual(self.cli("content-check", "--json").returncode, 3)
            self.assertEqual(self.cli("build", "--check").returncode, 1)
        self.assertEqual(self.snapshot(), before)  # nothing created, rewritten or repaired by the refusals
        parent.chmod(0o755)  # only the test-owned directory mode is restored
        self.assertEqual(validate_unlinked_assets_ok(["docs/handout.pdf"], self.assets), ["docs/handout.pdf"])
        self.assertEqual(content_problems(self.root), [])
        checked = self.cli("build", "--check")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(self.cli("content-check").returncode, 0)


if __name__ == "__main__":
    unittest.main()
