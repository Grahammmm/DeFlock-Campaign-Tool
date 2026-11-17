import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records.agency_run import lint_asks

INVENTORY = "doc_id\ttitle\trequest_ref\nD1\tTransparency Portal Export.pdf\tPRA 26-901\nD2\tShort.pdf\t26-901\n"
ASKS = ("request_ref\ttext\trating\n"
        "PRA 26-901\tPlease send the sharing agreements with neighboring agencies\tanswered\n"
        "PRA 26-901\tPlease send the vendor onboarding checklist for the cameras\tmissing\n"
        "PRA 26-901\tPlease send the quarterly hotlist reconciliation reports\twithheld\n"
        "context\tUnrelated 26-555 background note\tanswered\n")
EVENTS = ("request_ref\tevent_type\n26-901\tdetermination\n26-777 (third party)\tdetermination\ncontext\tclosed\n")
GOLDEN = """Re: PRA 26-901

1. Please send the policy history for 2022.
2. Please send the audit trail for 2024.
3. Please send the Transparency Portal Export for January.
4. Please send the sharing agreements with neighboring agencies.

See also 26-777 for the other agency.
We are still waiting on 26-901.
"""
CLEAN = """Re: PRA 26-901

> 1. Please send the audit trail for 2024.
2. Please send the policy history for 2022,
   other than the Transparency Portal Export.
"""


class LintAsksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "agency"
        self.dir.mkdir()
        self.write("inventory.tsv", INVENTORY)
        self.write("asks.tsv", ASKS)
        self.write("events.tsv", EVENTS)

    def write(self, name, text, base=None):
        (base or self.dir).joinpath(name).write_text(text, encoding="utf-8")

    def run_lint(self, letter, *extra):
        draft = Path(self.tmp.name) / "draft.md"
        draft.write_text(letter, encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = lint_asks.main(["--draft", str(draft), "--agency-dir", str(self.dir), *extra])
        return code, out.getvalue(), err.getvalue()

    def test_sheriff_v2_classes_golden(self):
        code, out, _ = self.run_lint(GOLDEN)
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), [
            "draft line 3 ask 1: R9_tier_order",
            "draft line 5 ask 3: R7_named_held_doc",
            "draft line 6 ask 4: R1_repeat_ask",
            "draft line 8 ask -: R8_foreign_ref",
            "draft line 9 ask -: R8_custodian_answered"])

    def test_clean_letter_exits_zero(self):
        code, out, _ = self.run_lint(CLEAN)
        self.assertEqual((code, out), (0, ""))
        code, out, _ = self.run_lint(CLEAN, "--json")
        self.assertEqual(json.loads(out), {"ok": True, "hits": []})

    def test_r1_boundary(self):
        text = "Re: 26-901\n\n1. %s\n"
        # five of eight distinct 4+ letter words shared: 5/8 >= 0.6 hits; four of seven under.
        self.assertEqual(self.run_lint(text % "Please send vendor onboarding checklist for cameras")[0], 1)
        self.assertEqual(self.run_lint(text % "Please send vendor onboarding checklist unrelated lanes notes")[0], 0)
        self.assertEqual(self.run_lint(text % "Please send the quarterly hotlist reconciliation reports")[0], 0)

    def test_r7_exclusion_phrases(self):
        base = "Re: 26-901\n\n1. Please send the Transparency Portal Export. %s\n"
        self.assertIn("R7_named_held_doc", self.run_lint(base % "")[1])
        for phrase in ("Other than that.", "Please do not resend it."):
            self.assertEqual(self.run_lint(base % phrase)[0], 0, phrase)
        self.assertEqual(self.run_lint("Re: 26-901\n\n1. Please send Short today\n")[0], 0)

    def test_r8_third_party_and_context_rows_ignored(self):
        for ref in ("26-777", "26-555"):
            code, out, _ = self.run_lint("Re: 26-901\n\n1. Please send the audit trail, see %s.\n" % ref)
            self.assertIn("R8_foreign_ref", out)

    def test_r9_order_cases(self):
        def run(*items):
            body = "".join("%d. %s\n" % (i, t) for i, t in enumerate(items, 1))
            return self.run_lint("Re: 26-901\n\n" + body)[1].splitlines()
        self.assertEqual(run("Please send the policy history", "Please send the audit trail"),
                         ["draft line 3 ask 1: R9_tier_order"])
        self.assertEqual(run("Please send the audit trail", "Please send the policy history"), [])
        self.assertEqual(run("Please send the contract", "Please send the policy history"), [])
        self.assertEqual(run("Please send greetings", "Please send the audit trail"), [])

    def test_refusals(self):
        letter = "Re: 26-901\n\n1. Please send the audit trail.\n"
        (self.dir / "asks.tsv").unlink()
        self.assertEqual(self.run_lint(letter)[::2], (2, "agency-lint-asks: agency_file_missing:asks.tsv\n"))
        self.write("asks.tsv", "text\trating\nx\tmissing\n")
        self.assertIn("column_missing:asks.tsv:request_ref", self.run_lint(letter)[2])
        self.write("asks.tsv", ASKS)
        self.assertIn("no_asks", self.run_lint("Re: 26-901\nNo numbered asks.\n")[2])
        draft = Path(self.tmp.name) / "draft.md"
        draft.write_text(letter)
        link = Path(self.tmp.name) / "link.md"
        os.symlink(draft, link)
        for target, body in ((link, None), (draft, "x" * (256 * 1024 + 1))):
            if body:
                draft.write_text(body)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = lint_asks.main(["--draft", str(target), "--agency-dir", str(self.dir)])
            self.assertEqual((code, err.getvalue()), (2, "agency-lint-asks: draft_invalid\n"))

    def test_output_has_no_letter_text_inputs_unchanged_and_json_shape(self):
        canary = "ZEBRACANARY"
        letter = GOLDEN.replace("policy history", "policy history " + canary)
        files = [self.dir / n for n in ("inventory.tsv", "asks.tsv", "events.tsv")]
        before = [(f.read_bytes(), f.stat().st_mtime_ns) for f in files]
        code, out, err = self.run_lint(letter)
        jcode, jout, _ = self.run_lint(letter, "--json")
        self.assertNotIn(canary, out + err + jout)
        self.assertEqual(before, [(f.read_bytes(), f.stat().st_mtime_ns) for f in files])
        lines = ["draft line %d ask %s: %s" % (h["line"], h["ask"], h["code"]) for h in json.loads(jout)["hits"]]
        self.assertEqual((code, jcode, lines), (1, 1, out.splitlines()))


if __name__ == "__main__":
    unittest.main()
