"""RF2340-4: malformed quoted vote CSV is refused before any outcome is recorded. Synthetic data only."""
import os
import tempfile
import unittest
from pathlib import Path

from campaign_tool.meeting_outcome import MeetingOutcomeError, read_votes
from tests import test_meeting_votes as votes_tests

BAD = {
    "unclosed_final_vote": 'name,vote\nAsh Example,"yes',
    "unclosed_final_vote_lf": 'name,vote\nAsh Example,"yes\n',
    "unclosed_final_vote_crlf": 'name,vote\r\nAsh Example,"yes\r\n',
    "junk_after_quote": 'name,vote\nAsh Example,"ye"s\n',
    "junk_after_name_quote": 'name,vote\n"Ash Example"x,yes\n',
    "junk_after_header_quote": '"name"x,vote\nAsh Example,yes\n',
    "bad_later_record": 'name,vote\nAsh Example,yes\nBirch Example,"no\n',
    "bad_later_junk": 'name,vote\nAsh Example,yes\n"Birch Example"x,no\n',
}


class DirectParsingTests(unittest.TestCase):
    def write(self, text):
        handle = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
        self.addCleanup(os.unlink, handle.name)
        handle.write(text.encode("utf-8"))
        handle.close()
        return handle.name

    def test_malformed_refused(self):
        for label, text in BAD.items():
            with self.subTest(label), self.assertRaises(MeetingOutcomeError) as caught:
                read_votes(self.write(text))
            self.assertIn("votes: not a valid UTF-8 CSV", str(caught.exception))

    def test_valid_line_endings_and_quoting(self):
        for eol in ("\n", "\r\n", "\r"):
            for final in (eol, ""):
                with self.subTest(eol=eol, final=bool(final)):
                    rows = read_votes(self.write(f'name,vote{eol}"Ash, ""A""",yes{final}'))
                    self.assertEqual(rows, [(1, ["name", "vote"]), (2, ['Ash, "A"', "yes"])])

    def test_quoted_multiline_is_one_record_with_newline_kept(self):
        rows = read_votes(self.write('name,vote\n"Ash\nExample",yes\n'))
        self.assertEqual(rows, [(1, ["name", "vote"]), (2, ["Ash\nExample", "yes"])])


class MalformedVoteOutcomeTests(votes_tests.MeetingVotesTests):
    def test_malformed_refused_before_lookup_even_for_unknown_meeting(self):
        for label, text in BAD.items():
            with self.subTest(label):
                message = self.refused(["votes: not a valid UTF-8 CSV"], votes=self.csv(text), meeting_id="synthetic-missing")
                self.assertNotIn("no staged meeting", message)

    def test_bad_later_record_writes_nothing(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.refused(["votes: not a valid UTF-8 CSV"], votes=self.csv(BAD["bad_later_record"]))
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, before)
        self.assertFalse((self.root / "public").exists())

    def test_cli_bad_csv_preserves_new_and_replaced_outcomes(self):
        bad = self.csv(BAD["junk_after_quote"])
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        done = self.cli("--votes", str(bad))
        self.assertEqual(done.returncode, 1)
        self.assertNotIn("Recorded", done.stdout)
        self.assertNotIn("Traceback", done.stderr)
        self.assertIn("votes: not a valid UTF-8 CSV", done.stderr + done.stdout)
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, before)
        self.record(self.csv("name,vote\nAsh Example,yes\n", "good.csv"))
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        done = self.cli("--votes", str(bad), "--replace")
        self.assertEqual(done.returncode, 1)
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, before)
        self.assertFalse((self.root / "public").exists())

    def test_valid_quoted_fields_still_record(self):
        self.record(self.csv('name,vote\n"Ash Example","yes"\r\n'))
        self.assertIn('"vote": "yes"', self.meeting.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
