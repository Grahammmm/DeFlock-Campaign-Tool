import hashlib
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from campaign_tool.records.agency_run import lint_out
from campaign_tool.records.draft_templates import _FOLLOWUP_FOOTER

TEAM = ["Jordan Testname", "jtest@team.example.net", "805-555-0142"]
ALLOW = ["records@agency.example.org", "(805) 555-0100"]
DRAFT = "\n".join([
    "Dear Records Unit,",                                 # 1
    "Please send records.",                               # 2
    "Sincerely,",                                         # 3
    "  jordan   TESTNAME",                                # 4 team name, case/whitespace varied
    "Call 805-555-0199 or write mail@other.example.org",  # 5 unallowed phone and email
    "Or records@AGENCY.example.org / 1-805-555-0100.",    # 6 allowed contacts, varied
    "[EDIT ME: date]",                                    # 7 draft residue
    "See D3 and UNVERIFIED and INBOX:9 and AB-1234.",     # 8 internal marks
    "",
])


class AgencyLintOutTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def write(self, name, text):
        path = self.root / name
        path.write_bytes(text if isinstance(text, bytes) else text.encode())
        return str(path)

    def run_out(self, draft, team=TEAM, allow=ALLOW, extra=()):
        argv = ["--draft", self.write("draft.md", draft),
                "--team-list", self.write("team.txt", "# private\n" + "\n".join(team) + "\n"),
                "--allow", self.write("allow.txt", "\n".join(allow) + "\n"), *extra]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = lint_out.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_team_signature_and_unallowed_contacts(self):
        # Contract: team strings and unlisted contacts fail with line+rule only. Bugs caught: case-sensitive or
        # single-space matching, the allow file excusing a team entry, or any team entry or path echoed.
        code, out, err = self.run_out(DRAFT, allow=ALLOW + ["jtest@team.example.net", "805-555-0142"])
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines()[:3], ["4 L8_TEAM", "5 L8_EMAIL", "5 L8_PHONE"])
        for entry in TEAM + [str(self.root)]:
            self.assertNotIn(entry.lower(), (out + err).lower())
        code, out, _ = self.run_out("Thanks.\nJordan\n  Testname\nmail me at JTEST@team.example.net\n",
                                    allow=ALLOW + ["jtest@team.example.net"])
        self.assertEqual((code, out), (1, "2 L8_TEAM\n4 L8_TEAM\n"))

    def test_allowed_official_contacts_are_clean(self):
        # Contract: whole-match allow comparison. Bug caught: a domain or substring match excusing another address.
        code, out, err = self.run_out("Write to records@AGENCY.example.org or records [at] agency [dot] example.org,\n"
                                      "call 1-805-555-0100 or 805.555.0100.\n")
        self.assertEqual((code, out, err), (0, "", ""))
        code, out, _ = self.run_out("records@agency.example.org and other@agency.example.org\n")
        self.assertEqual((code, out), (1, "1 L8_EMAIL\n"))

    def test_labeled_allowed_contacts_are_clean(self):
        # Contract: an allowed address is accepted whatever label, bracket or mailto: sits against it; the label is never part
        # of the address. Bug caught: the EMAIL match swallowing "Contact:" into the local part so an allowed contact fails L8_EMAIL.
        for text in ("Contact:records@agency.example.org", "Email:records@agency.example.org.", "mailto:records@agency.example.org",
                     "Contact:(records@agency.example.org)", "Reply-to:<records@AGENCY.example.org>,", "Contact:records [at] agency [dot] example.org"):
            with self.subTest(text=text):
                self.assertEqual(lint_out.scan(text, ["Jordan Testname"], ["records@agency.example.org"]), [])

    def test_labeled_unallowed_contacts_still_fail(self):
        # Contract: stripping the label keeps whole-address matching. Bugs caught: a label or the allowed address excusing a
        # different local part or domain, and a labeled team contact escaping L8_TEAM.
        allow = ["records@agency.example.org"]
        for text in ("Contact:other@agency.example.org", "Contact:xrecords@agency.example.org", "Contact:records@agency.example.org.evil.net",
                     "Contact:records@other.example.org"):
            with self.subTest(text=text):
                self.assertEqual(lint_out.scan(text, ["Jordan Testname"], allow), [(1, "L8_EMAIL")])
        self.assertEqual(lint_out.scan("Contact:jtest@team.example.net", ["jtest@team.example.net"], allow + ["jtest@team.example.net"]),
                         [(1, "L8_TEAM")])

    def test_residue_locations(self):
        # Contract: L10 rows carry the right line. Bug caught: residue found only on the whole text, or reported at line 1.
        code, out, _ = self.run_out(DRAFT)
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines()[3:], ["7 L10_EDIT_PLACEHOLDER", "8 L10_INTERNAL_ID", "8 L10_LABEL", "8 L10_ROW_ID"])
        code, out, _ = self.run_out("Hello.\nOk.\n" + _FOLLOWUP_FOOTER)  # multi-line marker: located where it completes
        self.assertEqual((code, out), (1, "4 L10_DRAFT_FOOTER\n"))

    def test_input_errors_never_echo(self):
        # Contract: bad input exits 2 with one fixed line. Bug caught: argparse or exception text echoing a value.
        canary = "canary-zz9"
        for case in (dict(extra=["--" + canary]), dict(allow=["not-a-contact " + canary]), dict(team=[])):
            with self.subTest(case=list(case)):
                code, out, err = self.run_out("Hello.\n", **case)
                self.assertEqual((code, out, err.strip()), (2, "", lint_out.REFUSED))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            missing = lint_out.main(["--draft", str(self.root / canary), "--team-list", self.write("t", "x"), "--allow", self.write("a", "")])
            bad_utf8 = lint_out.main(["--draft", self.write("bad", b"\xff\xfe\xfa"), "--team-list", self.write("t", "x"), "--allow", self.write("a", "")])
        self.assertEqual((missing, bad_utf8), (2, 2))
        self.assertNotIn(canary, out.getvalue() + err.getvalue())
        paths = [self.root / n for n in ("draft.md", "team.txt", "allow.txt")]
        self.run_out(DRAFT)
        before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
        with redirect_stdout(io.StringIO()):
            lint_out.main(["--draft", str(paths[0]), "--team-list", str(paths[1]), "--allow", str(paths[2])])
        self.assertEqual([hashlib.sha256(p.read_bytes()).hexdigest() for p in paths], before)


if __name__ == "__main__":
    unittest.main()
