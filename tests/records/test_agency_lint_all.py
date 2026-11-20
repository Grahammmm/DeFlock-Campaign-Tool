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
        (self.agency / "asks.tsv").write_text("text\n")
        (self.agency / "events.tsv").write_text("event\n")
        (self.agency / "pages" / "DOC1_p001.txt").write_text("page")
        self.package = self.root / "package.md"
        self.package.write_text("pkg")
        self.draft = self.root / "draft.md"
        self.draft.write_text(CLEAN)

    def run_lint(self, fakes=None, draft=None, package=True, agency=None, team=True, allow=True):
        fakes = {"lint_asks": fake(), "lint_cites": fake(), "lint_quotes": fake(), "lint_out": fake(), **(fakes or {})}
        argv = [str(draft or self.draft), "--agency-dir", str(agency or self.agency)]
        if package:
            argv += ["--package", str(self.package)]
        if team:
            argv += ["--team-list", "team.txt"]
        if allow:
            argv += ["--allow", "allow.txt"]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(lint_all.importlib, "import_module", lambda name, pkg=None: fakes[name.lstrip(".")]):
            with redirect_stdout(out), redirect_stderr(err):
                code = lint_all.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_each_lint_failing_alone_shows_only_that_lint(self):
        names = {"lint_asks": "asks", "lint_cites": "cites", "lint_quotes": "quotes", "lint_out": "out"}
        for module, name in names.items():
            with self.subTest(module=module):
                bad = fake(1, {"lint_quotes": "12:4 R7 %s\n13:1 R7\n" % CANARY, "lint_out": "12 L8_TEAM\n13 L10_LABEL\n"}.get(module, "draft line 12 ask 2: x\ndraft line 15: y\n"))
                code, out, _ = self.run_lint({module: bad})
                self.assertEqual(code, 1)
                lines = out.splitlines()
                self.assertEqual([l.split(":")[0] for l in lines[:5]], ["asks", "cites", "quotes", "out", "banned phrases"])
                failing = [l for l in lines[:5] if "FAIL" in l]
                first = "12:4" if module == "lint_quotes" else "12"
                self.assertEqual(failing, ["%s: FAIL, 2 hits, first %s" % (name, first)])
                self.assertEqual(lines[-1], "agency-lint: FAIL")
                self.assertNotIn(CANARY, out)

    def test_clean_draft_exits_0(self):
        code, out, _ = self.run_lint()
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["asks: pass", "cites: pass", "quotes: pass", "out: pass", "banned phrases: pass", "agency-lint: PASS"])

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

    def test_out_without_team_list_or_allow_is_skipped_not_pass(self):
        boom = fake(exc=AssertionError("called"))
        for kwargs, reason in (({"team": False, "allow": False}, "no team list"), ({"team": False}, "no team list"),
                               ({"allow": False}, "no allow file")):
            with self.subTest(reason=reason):
                code, out, _ = self.run_lint({"lint_out": boom}, **kwargs)
                self.assertEqual(code, 3)
                self.assertIn("out: skipped: " + reason, out)
                self.assertNotIn("out: pass", out)
                self.assertNotIn("error", out)
                self.assertEqual(out.splitlines()[-1], "agency-lint: INCOMPLETE (1 skipped)")

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
        code, out, _ = self.run_lint({"lint_out": fake(2)})
        self.assertEqual(code, 1)
        self.assertIn("out: error", out)

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
        self.assertEqual(cli.COMMANDS["agency-lint-out"], ("agency_run.lint_out", "main"))

    def test_wrapper_argument_error_is_fixed_and_value_free(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(SystemExit) as raised:
                lint_all.main([str(self.draft), "--agency-dir", str(self.agency), "--" + CANARY])
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(err.getvalue().strip(), "refused agency_lint_input")
        self.assertNotIn(CANARY, out.getvalue() + err.getvalue())


class RealAgencyLintAllTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.agency = self.root / "agency"
        (self.agency / "pages").mkdir(parents=True)
        (self.agency / "pages" / "D1_p001.txt").write_text("Unrelated page text.\n")
        for name, header in (("inventory.tsv", "doc_id\ttitle\trequest_ref"),
                             ("asks.tsv", "request_ref\ttext\trating"),
                             ("events.tsv", "request_ref\tevent_type")):
            (self.agency / name).write_text(header + "\n")
        self.package = self.root / "package.json"
        self.package.write_text('{"records_law": {}}')
        self.draft = self.root / "draft.md"
        self.draft.write_text("1. Please send existing records.\n")

    def run_real(self, package=None, team=False):
        argv = [str(self.draft), "--agency-dir", str(self.agency), "--package", str(package or self.package)]
        if team:
            (self.root / "team.txt").write_text("Jordan Testname\n")
            (self.root / "allow.txt").write_text("records@agency.example.org\n")
            argv += ["--team-list", str(self.root / "team.txt"), "--allow", str(self.root / "allow.txt")]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = lint_all.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_missing_each_actual_input_is_skipped(self):
        for name in ("inventory.tsv", "asks.tsv", "events.tsv"):
            with self.subTest(name=name):
                path = self.agency / name
                saved = path.read_bytes()
                path.unlink()
                code, out, err = self.run_real()
                self.assertEqual(code, 3, out + err)
                self.assertIn("asks: skipped: no " + name.removesuffix(".tsv"), out)
                self.assertIn("agency-lint: INCOMPLETE", out)
                self.assertNotIn("error", out)
                path.write_bytes(saved)
        empty_package = self.root / "empty-package"
        empty_package.mkdir()
        code, out, err = self.run_real(empty_package)
        self.assertEqual(code, 3, out + err)
        self.assertIn("cites: skipped: no package", out)
        self.assertNotIn("error", out)

    def test_clean_real_run_and_failed_quote_preserve_limitation(self):
        code, out, err = self.run_real(team=True)
        self.assertEqual(code, 0, out + err)
        self.assertIn("quotes: pass; text check only; no visual check", out)
        self.assertIn("out: pass", out)
        self.assertIn("agency-lint: PASS", out)
        code, out, err = self.run_real()
        self.assertEqual(code, 3, out + err)
        self.assertIn("out: skipped: no team list", out)
        self.draft.write_text("1. Please send existing records.\nJordan Testname\n")
        code, out, err = self.run_real(team=True)
        self.assertEqual(code, 1, out + err)
        self.assertIn("out: FAIL, 1 hits, first 2", out)
        self.assertNotIn("Jordan", out + err)
        self.draft.write_text("1. Please send existing records.\n")
        self.draft.write_text('1. Please send records.\n"made up page wording" (D1 p.1),\n'
                              '"fabricated message wording" (msg M1),\n')
        code, out, err = self.run_real()
        self.assertEqual(code, 1, out + err)
        self.assertIn("quotes: FAIL, 1 hits", out)
        self.assertIn("message quotes not checked against pages", out)
        self.assertIn("no visual check", out)
        self.assertNotIn("fabricated message wording", out + err)

    def test_message_quote_coverage_is_not_misrepresented(self):
        self.draft.write_text('1. Please send existing records.\n"this is fabricated wording" (msg M1),\n')
        code, out, err = self.run_real()
        self.assertEqual(code, 3, out + err)
        self.assertIn("message quotes not checked against pages", out)
        self.assertIn("no visual check", out)
        self.assertIn("agency-lint: INCOMPLETE", out)
        self.assertNotIn("fabricated wording", out + err)
        self.assertNotIn("M1", out + err)


if __name__ == "__main__":
    unittest.main()
