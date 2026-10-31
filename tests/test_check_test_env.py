"""RF0835-3: tools/check_test_env.py reports drift from pinned test packages."""

import ast
import contextlib
from importlib.metadata import PackageNotFoundError
import io
from pathlib import Path
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tools import check_test_env as env  # noqa: E402

SPEC_TEXT = ("pypdf==6.10.0\nopenpyxl==3.1.5\net_xmlfile==2.0.0\ndetect-secrets==1.5.0\nextract-msg==0.56.1\n"
             "olefile==0.47\n# FA1607 comment\nPillow==12.3.0\n")
PINS = {env.normalise(name): pinned for name, pinned, _ in env.parse(SPEC_TEXT)}


def fake(versions):
    def version_of(name):
        if name not in versions:
            raise PackageNotFoundError(name)
        return versions[name]
    return version_of


class CheckTestEnvTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def run_tool(self, versions=PINS, text=None, path=None):
        argv = []
        if text is not None:
            path = self.dir / "req.txt"
            path.write_text(text, encoding="utf-8")
        if path is not None:
            argv = ["--requirements", str(path)]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = env.main(argv, version_of=fake(versions))
        return code, out.getvalue().splitlines(), err.getvalue()

    def test_default_file_all_ok(self):
        real = env.parse(env.DEFAULT.read_text(encoding="utf-8"))
        code, lines, err = self.run_tool({env.normalise(n): v for n, v, _ in real})
        self.assertEqual(code, 0, err)
        self.assertEqual(lines[:-1], [f"{name} ok {pinned}" for name, pinned, _ in real])
        self.assertTrue(lines[-1].startswith(f"check-test-env: {len(real)} pins, 0 differ (python "))

    def test_missing_and_mismatch_print_every_line(self):
        versions = dict(PINS, pypdf="6.21.4")
        del versions["detect-secrets"]
        code, lines, _ = self.run_tool(versions, text=SPEC_TEXT)
        self.assertEqual(code, 1)
        self.assertEqual(lines[0], "pypdf mismatch installed=6.21.4 pinned=6.10.0")
        self.assertIn("detect-secrets missing (pinned 1.5.0)", lines)
        self.assertIn("Pillow ok 12.3.0", lines)
        self.assertEqual(len(lines), 8)
        self.assertIn(": 7 pins, 2 differ", lines[-1])

    def test_names_are_normalised_for_lookup(self):
        seen = []
        env.check(env.parse("Extract_Msg==0.56.1\net_xmlfile==2.0.0\n"), lambda n: seen.append(n) or "x")
        self.assertEqual(seen, ["extract-msg", "et-xmlfile"])

    def test_comments_blanks_and_physical_line_numbers(self):
        text = "# head\n\n   \npypdf==6.10.0  # note\n\tolefile==0.47\t# tab note\nbad>=1\n"
        with self.assertRaisesRegex(ValueError, r"^line 6: unsupported requirement$"):
            env.parse(text)
        self.assertEqual(env.parse(text.rsplit("bad", 1)[0]), [("pypdf", "6.10.0", 4), ("olefile", "0.47", 5)])

    def test_unsupported_lines_exit_2(self):
        for bad in ("pkg>=1", 'pkg==1; python_version<"3.12"', "pkg[extra]==1", "-r other.txt",
                    "https://example.invalid/pkg.whl", "pkg", "pkg==1#x"):
            with self.subTest(bad=bad):
                code, lines, err = self.run_tool(text=f"# c\n{bad}\n")
                self.assertEqual((code, lines), (2, []))
                self.assertIn("line 2: unsupported requirement", err)
                self.assertNotIn("Traceback", err)

    def test_duplicate_after_normalisation_exit_2(self):
        code, lines, err = self.run_tool(text="Pillow==12.3.0\npillow==12.3.0\n")
        self.assertEqual((code, lines), (2, []))
        self.assertIn("line 2: duplicate pillow", err)

    def test_unreadable_paths_exit_2(self):
        for path in (self.dir / "absent.txt", self.dir):
            with self.subTest(path=path.name):
                code, lines, err = self.run_tool(path=path)
                self.assertEqual((code, lines), (2, []))
                self.assertTrue(err.startswith("check-test-env: "))
                self.assertNotIn("Traceback", err)

    def test_comment_only_file_is_not_a_failure(self):
        code, lines, _ = self.run_tool(text="# nothing pinned\n\n")
        self.assertEqual((code, len(lines)), (0, 1))
        self.assertIn(": 0 pins, 0 differ", lines[0])

    def test_versions_compared_exactly(self):
        for installed, pinned in (("6.10.0.post1", "6.10.0"), ("12.3.0", "12.3")):
            with self.subTest(installed=installed):
                code, lines, _ = self.run_tool({"pkg": installed}, text=f"pkg=={pinned}\n")
                self.assertEqual(code, 1)
                self.assertEqual(lines[0], f"pkg mismatch installed={installed} pinned={pinned}")

    def test_read_only(self):
        req = self.dir / "req.txt"
        req.write_text(SPEC_TEXT, encoding="utf-8")
        before = sorted((p.name, p.read_bytes()) for p in self.dir.iterdir())
        self.assertEqual(self.run_tool(versions={}, path=req)[0], 1)
        self.assertEqual(sorted((p.name, p.read_bytes()) for p in self.dir.iterdir()), before)
        tree = ast.parse(Path(env.__file__).read_text(encoding="utf-8"))
        imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                    for alias in node.names}
        imported |= {str(node.module).split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertEqual(imported, {"argparse", "importlib", "pathlib", "re", "sys"})
