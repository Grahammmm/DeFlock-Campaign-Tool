import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from campaign_tool.records import cli
from campaign_tool.records.agency_run import lint_precedent

AGENCIES = ("agency\taliases\tnotes\n"
            "Agency A\tA Sheriff|The A Office\tx\n"
            "Agency B\tB PD|Agency B Police Department\t\n"
            "Agency C\tC Police\t\n"
            "Agency A Valley\tValley PD\t\n")
REFUSAL, CLEAN, NOTE = ("Agency B produced six months of search logs, so please do the same.\n",
                        "Please produce search logs showing access by the Agency B Police Department to your agency's data.\n",
                        "Agencies like Agency B keep logs for a year.\n")


class AgencyLintPrecedentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def run_lint(self, draft, agencies=AGENCIES, key="Agency A", extra=()):
        (self.root / "draft.md").write_bytes(draft if isinstance(draft, bytes) else draft.encode())
        (self.root / "agencies.tsv").write_text(agencies)
        argv = ["--draft", str(self.root / "draft.md"), "--agencies", str(self.root / "agencies.tsv"), "--agency", key, *extra]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = lint_precedent.main(argv)
        return code, out.getvalue(), err.getvalue()

    # Contract: the three AGENCY-LINT-LIST L9 fixtures. Bug caught: a name-only check treats all three alike.
    def test_l9_fixtures(self):
        self.assertEqual(self.run_lint(REFUSAL), (1, "1 refusal L9_RELEASE_PRECEDENT\n", ""))
        self.assertEqual(self.run_lint(CLEAN), (0, "", ""))
        self.assertEqual(self.run_lint(NOTE), (0, "1 note L9_COMPARISON\n", ""))
        self.assertEqual(cli.COMMANDS["agency-lint-precedent"], ("agency_run.lint_precedent", "main"))

    # Contract: aliases, case, spacing, one refusal per sentence, addressee never flagged, whole-word and longest-name matching.
    def test_alias_case_addressee_and_multiple_names(self):
        cases = [("agency b pd PRODUCED logs.", 1, "1 refusal L9_RELEASE_PRECEDENT\n"),
                 ("AGENCY   B police  department has disclosed logs.", 1, "1 refusal L9_RELEASE_PRECEDENT\n"),
                 ("Agency B and C Police provided logs, and C police released more.", 1, "1 refusal L9_RELEASE_PRECEDENT\n"),
                 ("As C Police did, please share them.", 1, "1 refusal L9_RELEASE_PRECEDENT\n"),
                 ("A Sheriff released logs, and Agency A produced more; the A Office made public its logs.", 0, ""),
                 ("The WEB PD released logs.", 0, ""),
                 ("Agency A Valley released logs.", 1, "1 refusal L9_RELEASE_PRECEDENT\n"),
                 ("Agency A also keeps logs, like The A Office.", 0, ""),
                 ("Searches run by Agency B and also by Agency C.", 0, "1 note L9_COMPARISON\n")]
        for text, code, out in cases:
            with self.subTest(text=text):
                self.assertEqual(self.run_lint(text), (code, out, ""))

    # Contract: quotes never refuse; cues are never borrowed across sentences, quotes or soft-wrapped lines.
    def test_blockquote_and_sentence_boundaries(self):
        note = "2 note L9_QUOTED_PRECEDENT\n"
        self.assertEqual(self.run_lint("Intro.\n> Agency B produced six months of logs.\n"), (0, note, ""))
        self.assertEqual(self.run_lint("Intro.\n  > > Agency B produced six months of logs.\n"), (0, note, ""))
        self.assertEqual(self.run_lint("Intro.\nAgency B produced six months of logs.\n"), (1, "2 refusal L9_RELEASE_PRECEDENT\n", ""))
        self.assertEqual(self.run_lint("Please share logs of searches run by Agency B. Others produced logs.\n"), (0, "", ""))
        self.assertEqual(self.run_lint("> Agency C produced logs.\nPlease share logs of searches run by Agency B.\n"), (0, "1 note L9_QUOTED_PRECEDENT\n", ""))
        self.assertEqual(self.run_lint("Records\nthat Agency B\nreleased last year.\n\nAgency C is named.\n"), (1, "2 refusal L9_RELEASE_PRECEDENT\n", ""))
        self.assertEqual(self.run_lint("Please share access by Agency B; Agency B also produced logs.\n")[1], "1 refusal L9_RELEASE_PRECEDENT\n")

    # Contract: an abbreviation period inside a sentence is not a sentence end, so the release cue still binds to the named agency.
    # Bug caught: splitting at "Inc." / "e.g." left the name and "released" in different sentences and the refusal vanished.
    def test_abbreviation_periods_do_not_split_sentences(self):
        refusal = (1, "1 refusal L9_RELEASE_PRECEDENT\n", "")
        for text in ("Agency B Inc. released logs.", "Agency B, e.g. released logs.", "Agency B (i.e. B PD) released logs.",
                     "Agency B Corp. and Agency C Ltd. produced logs."):
            with self.subTest(text=text):
                self.assertEqual(self.run_lint(text), refusal)
        # a real sentence end still separates cues, and an abbreviation does not hide a following sentence
        self.assertEqual(self.run_lint("Please share searches run by Agency B Inc. Others released logs."), (0, "", ""))
        self.assertEqual(self.run_lint("Intro Inc. here. Agency B released logs."), refusal)

    # Contract: a Markdown lazy continuation line (no ">" directly after a quote line) stays inside the quote: a note, never a refusal.
    # Bug caught: splitting it into an unquoted run lost the quote's name/cue pairing (no finding) or raised a refusal for quoted text.
    def test_lazy_blockquote_continuation(self):
        note = (0, "1 note L9_QUOTED_PRECEDENT\n", "")
        self.assertEqual(self.run_lint("> Agency B\nreleased logs.\n"), note)
        self.assertEqual(self.run_lint("> Agency B\n> kept\nreleased logs.\n"), note)
        # a blank line, a list item or a heading ends the quote, so the following text is the author's own
        self.assertEqual(self.run_lint("> Agency B\n\nreleased logs.\n"), (0, "", ""))
        self.assertEqual(self.run_lint("> Quote.\n# Agency B released logs.\n"), (1, "2 refusal L9_RELEASE_PRECEDENT\n", ""))
        self.assertEqual(self.run_lint("> Quote.\n- Agency B released logs.\n"), (1, "2 refusal L9_RELEASE_PRECEDENT\n", ""))

    # Contract: only a quoted paragraph line can be lazily continued; text after a quoted heading, thematic break, fence or fenced code
    # is outside the blockquote, so an authored release precedent there stays a refusal.
    # Bug caught: treating any quoted block as lazy-continuable downgraded the author's own release sentence to a quote note.
    def test_lazy_continuation_needs_a_quoted_paragraph(self):
        for quote in ("> # Heading", "> ---", "> ***", "> ```", "> ```\n> code\n> ```", "> ```\n> code", "> ~~~\n> code\n> ~~~"):
            with self.subTest(quote=quote):
                line = quote.count("\n") + 2
                self.assertEqual(self.run_lint(quote + "\nAgency B released logs.\n"), (1, "%d refusal L9_RELEASE_PRECEDENT\n" % line, ""))
        # a quoted paragraph after a quoted heading or closed fence is a paragraph again and can be continued lazily
        self.assertEqual(self.run_lint("> # Heading\n> Agency B\nreleased logs.\n"), (0, "2 note L9_QUOTED_PRECEDENT\n", ""))
        self.assertEqual(self.run_lint("> ```\n> code\n> ```\n> Agency B\nreleased logs.\n"), (0, "4 note L9_QUOTED_PRECEDENT\n", ""))

    # Contract: bad input exits 2 with one fixed line and never echoes a value; a good run prints no name and writes nothing.
    def test_bad_input_never_echoes(self):
        fixed = (2, "", "refused agency_lint_precedent_input\n")
        self.assertEqual(self.run_lint(REFUSAL, key="Canary Unknown"), fixed)
        for bad in ("agency\nAgency A\nagency a\n", "agency\taliases\nAgency A\n", "name\nAgency A\n", ""):
            with self.subTest(bad=bad):
                self.assertEqual(self.run_lint(REFUSAL, agencies=bad), fixed)
        self.assertEqual(self.run_lint(b"\xff\xfe bad", ), fixed)
        self.assertEqual(self.run_lint(REFUSAL, extra=("--canary-arg",)), fixed)
        self.assertEqual(self.run_lint(b"x" * (lint_precedent.MAX_DRAFT + 1)), fixed)
        (self.root / "link.md").symlink_to(self.root / "draft.md")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(lint_precedent.main(["--draft", str(self.root / "link.md"), "--agencies", str(self.root / "agencies.tsv"), "--agency", "Agency A"]), 2)
            self.assertEqual(lint_precedent.main(["--draft", str(self.root / "gone.md"), "--agencies", str(self.root / "agencies.tsv"), "--agency", "Agency A"]), 2)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", "refused agency_lint_precedent_input\n" * 2))
        code, out, err = self.run_lint(REFUSAL + "BOM and CRLF.\r\n")
        self.assertEqual((self.root / "agencies.tsv").read_text(), AGENCIES)
        self.assertEqual((code, out, err), (1, "1 refusal L9_RELEASE_PRECEDENT\n", ""))


if __name__ == "__main__":
    unittest.main()
