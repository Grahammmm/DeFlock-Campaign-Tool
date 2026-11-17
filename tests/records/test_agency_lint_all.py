import hashlib
import io
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from campaign_tool.records import cli
from campaign_tool.records.agency_run import lint_all

CANARY = "canary-secret"
CLEAN = "Please produce existing records.\nSecond line.\nThird line.\n"


def fake(code=0, out="", exc=None):
    def main(argv=None):
        if exc is not None:
            raise exc
        print(out, end="")
        return code
    return types.SimpleNamespace(main=main, calls=[])


class AgencyLintAllTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.agency = self.root / "agency"
        (self.agency / "pages").mkdir(parents=True)
        (self.agency / "inventory.tsv").write_text("doc\n")
        (self.agency / "pages" / "DOC1_p001.txt").write_text("page")
        self.package = self.root / "package.md"
        self.package.write_text("pkg")
        self.draft = self.root / "draft.md"
        self.draft.write_text(CLEAN)

    def run_lint(self, fakes=None, draft=None, package=True, agency=None):
        fakes = {"lint_asks": fake(), "lint_cites": fake(), "lint_quotes": fake(), **(fakes or {})}
        argv = [str(draft or self.draft), "--agency-dir", str(agency or self.agency)]
        if package:
            argv += ["--package", str(self.package)]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(lint_all.importlib, "import_module", lambda name, pkg=None: fakes[name.lstrip(".")]):
            with redirect_stdout(out), redirect_stderr(err):
                code = lint_all.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_each_lint_failing_alone_shows_only_that_lint(self):
        names = {"lint_asks": "asks", "lint_cites": "cites", "lint_quotes": "quotes"}
        for module, name in names.items():
            with self.subTest(module=module):
                bad = fake(1, "12:4 R7 %s\n13:1 R7\n" % CANARY if module == "lint_quotes" else "draft line 12 ask 2: x\ndraft line 15: y\n")
                code, out, _ = self.run_lint({module: bad})
                self.assertEqual(code, 1)
                lines = out.splitlines()
                self.assertEqual([l.split(":")[0] for l in lines[:4]], ["asks", "cites", "quotes", "banned phrases"])
                failing = [l for l in lines[:4] if "FAIL" in l]
                first = "12:4" if module == "lint_quotes" else "12"
                self.assertEqual(failing, ["%s: FAIL, 2 hits, first %s" % (name, first)])
                self.assertEqual(lines[-1], "agency-lint: FAIL")
                self.assertNotIn(CANARY, out)

    def test_clean_draft_exits_0(self):
        code, out, _ = self.run_lint()
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["asks: pass", "cites: pass", "quotes: pass", "banned phrases: pass", "agency-lint: PASS"])

    def test_missing_inputs_are_skipped_not_pass(self):
        (self.agency / "pages" / "DOC1_p001.txt").unlink()
        code, out, _ = self.run_lint(package=False)
        self.assertEqual(code, 3)
        self.assertIn("quotes: skipped: no pages", out)
        self.assertIn("cites: skipped: no package", out)
        self.assertEqual(out.splitlines()[-1], "agency-lint: INCOMPLETE (2 skipped)")
        (self.agency / "inventory.tsv").unlink()
        code, out, _ = self.run_lint()
        self.assertIn("asks: skipped: no inventory", out)
        self.assertEqual(code, 3)

    def test_skipped_lint_is_never_called(self):
        (self.agency / "pages" / "DOC1_p001.txt").unlink()
        boom = fake(exc=AssertionError("called"))
        code, out, _ = self.run_lint({"lint_quotes": boom})
        self.assertIn("quotes: skipped: no pages", out)
        self.assertNotIn("error", out)

    def test_error_and_systemexit_are_errors_without_text(self):
        code, out, err = self.run_lint({"lint_asks": fake(exc=ValueError(CANARY)), "lint_cites": fake(exc=SystemExit(2))})
        self.assertEqual(code, 1)
        self.assertIn("asks: error", out)
        self.assertIn("cites: error", out)
        self.assertNotIn(CANARY, out + err)

    def test_code_2_from_a_lint_is_error(self):
        code, out, _ = self.run_lint({"lint_quotes": fake(2)})
        self.assertEqual(code, 1)
        self.assertIn("quotes: error", out)

    def test_banned_phrase_reports_line_and_col_only(self):
        self.draft.write_text("one\ntwo\n  We issue a litigation hold now.\n")
        code, out, _ = self.run_lint()
        self.assertEqual(code, 1)
        self.assertIn("banned phrases: FAIL, 1 hits, first 3:14", out)
        self.assertNotIn("litigation", out)

    def test_no_hit_lines_but_exit_1_reports_zero_hits(self):
        code, out, _ = self.run_lint({"lint_cites": fake(1, "no place here\n")})
        self.assertEqual(code, 1)
        self.assertIn("cites: FAIL, 0 hits", out)

    def test_inputs_unchanged_and_bad_input_refused(self):
        def digest():
            return [hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(self.root.rglob("*")) if p.is_file()]
        before = digest()
        self.run_lint()
        self.assertEqual(digest(), before)
        for kwargs in ({"draft": self.root / "missing.md"}, {"agency": self.root / "nope"}):
            code, out, err = self.run_lint(**kwargs)
            self.assertEqual(code, 2)
            self.assertEqual(out.strip(), "refused agency_lint_input")
            self.assertNotIn(str(self.root), out + err)

    def test_cli_registered(self):
        self.assertEqual(cli.COMMANDS["agency-lint"], ("agency_run.lint_all", "main"))


if __name__ == "__main__":
    unittest.main()
