"""DF1422-07: `finding new --gap AGENCY [--request NAME]`. Synthetic campaign, requests and hashes only.

Run from the repository root: python3 -B -m unittest tests.test_finding_new_gap -v
"""
import hashlib
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import findings  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign" / "content"
AGENCY = "ca-sheriff-x"
LETTER_1, LETTER_2, LETTER_3 = "a1" * 32, "b2" * 32, "c3" * 32
RECORD = "d4" * 32
BASIS = "Gov. Code 7923.600 (investigatory files)"
ASK = "finding", "new", "--directory"


def step(on, kind, **more):
    return {"on": on, "kind": kind, "recorded_at": "2026-03-01T00:00:00+00:00", **more}


class FindingNewGapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-finding-gap-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        self.cli("init", "--directory", self.root, "--name", "Synthetic", "--county", "Synthetic County",
                 "--state", "ca")
        self.content = self.root / "content"
        (self.content / "findings").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLE / "site.json", self.content / "site.json")
        (self.content / "agencies.json").write_text(json.dumps([{"agency_id": AGENCY, "name": "Synthetic Sheriff"}]))
        self.library([LETTER_1, LETTER_2])
        self.drafts = self.root / "private" / "drafts"
        self.requests = {}

    def library(self, hashes):
        (self.content / "sources.json").write_text(json.dumps([
            {"sha256": sha, "title": f"Synthetic letter {n}", "agency_id": AGENCY,
             "received_at": "2026-01-20T16:30:00+00:00", "public_url": None, "fragment": "synthetic-letters"}
            for n, sha in enumerate(hashes, 1)]))

    def sent(self, steps, request="main", sent_on="2026-01-05", channel="email", **entry):
        """Add one request to kit/sent.json (written by hand: the tool only reads it here)."""
        stem = AGENCY if request == "main" else f"{AGENCY}--{request}"
        key = AGENCY if request == "main" else f"{AGENCY}#{request}"
        self.requests[key] = {"sent_on": sent_on, "channel": channel, "draft": f"requests/{stem}.md",
                              "draft_sha256": hashlib.sha256(stem.encode()).hexdigest(),
                              "recorded_at": "2026-01-05T00:00:00+00:00", "steps": steps, **entry}
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "sent.json").write_text(json.dumps({"schema_version": 1, "requests": self.requests}))

    def cli(self, *arguments, returncode=0):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, returncode, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def gap(self, *extra, returncode=0):
        return self.cli(*ASK, self.root, "--gap", AGENCY, *extra, returncode=returncode)

    def refused(self, pattern, request="main"):
        with self.assertRaisesRegex(ValueError, pattern):
            findings.new_gap_draft(self.root, AGENCY, request)
        self.assertFalse(self.drafts.exists(), "a refused request must write nothing")

    def written(self, result):
        return findings.load_draft(Path(json.loads(result.stdout.splitlines()[0])["written"]))

    def test_denied_request_with_two_library_letters_becomes_a_gap_draft(self):
        self.sent([step("2026-01-20", "acknowledged"), step("2026-02-10", "denied", withheld_basis=[BASIS],
                                                          correspondence=[LETTER_2])],
                  correspondence=[LETTER_1])
        result = self.gap()
        path = Path(json.loads(result.stdout.splitlines()[0])["written"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertNotIn("blocker:", result.stdout)
        draft = findings.load_draft(path)
        self.assertEqual((draft["classification"], draft["event_date"], draft["agency_id"], draft["state"]),
                         ("information_gap", "2026-02-10", AGENCY, "draft"))
        self.assertEqual(draft["body_md"].split("\n\n")[0],
                         "Request sent 2026-01-05 by email. Agency answer: denied on 2026-02-10.\n"
                         f"Stated basis (as written by the agency): {BASIS}")
        self.assertTrue(draft["body_md"].endswith("[EDIT ME: the finding in full, citing each source by its locator]"))
        self.assertEqual(sorted(s["sha256"] for s in draft["sources"]), [LETTER_1, LETTER_2])
        self.assertEqual(draft["title"], "[EDIT ME: title] (records request to Synthetic Sheriff)")
        self.assertEqual(draft["limitations"], ["[EDIT ME: what these records cannot show]"])

    def test_closed_request_without_basis_or_correspondence_has_no_basis_line(self):
        self.sent([step("2026-02-01", "closed")], request="second", sent_on="2026-01-09", channel="mail")
        result = self.gap("--request", "second", "--title", "Sheriff closed the request", "--author", "Ada")
        draft = self.written(result)
        self.assertEqual(draft["body_md"].split("\n\n")[0],
                         "Request sent 2026-01-09 by mail. Agency answer: closed on 2026-02-01.")
        self.assertNotIn("Stated basis", draft["body_md"])
        self.assertEqual((draft["event_date"], draft["title"], draft["author"], draft["sources"]),
                         ("2026-02-01", "Sheriff closed the request", "Ada", []))

    def test_open_extended_or_producing_requests_are_refused_and_nothing_is_written(self):
        self.sent([])
        self.refused("request ca-sheriff-x is still open")
        self.sent([step("2026-01-20", "acknowledged"), step("2026-01-30", "extension")])
        self.refused("request ca-sheriff-x is still open")
        self.sent([step("2026-02-01", "produced", partial=True, records=[RECORD])])
        self.refused("request ca-sheriff-x produced records; a gap draft needs none")
        self.sent([step("2026-02-01", "produced", partial=True, records=[RECORD]), step("2026-02-20", "closed")])
        self.refused("produced records; a gap draft needs none \\(write a regular finding\\)")
        self.sent([step("2026-02-01", "produced")])  # produced, no records linked, no denial: still open
        self.refused("still open")
        self.sent([step("2026-02-01", "denied")], request="other")
        self.refused("request ca-sheriff-x is still open")  # the main request is untouched by "other"
        result = self.gap("--request", "other")
        self.assertEqual(self.written(result)["event_date"], "2026-02-01")
        refusal = self.cli(*ASK, self.root, "--gap", AGENCY, returncode=1)  # main is still the empty one
        self.assertIn("still open", refusal.stdout + refusal.stderr)

    def test_correspondence_missing_from_the_library_is_a_blocker_not_a_refusal(self):
        self.sent([step("2026-02-10", "denied", correspondence=[LETTER_3])], correspondence=[LETTER_1])
        result = self.gap()
        lines = result.stdout.splitlines()
        self.assertEqual(lines[1], f"blocker: {LETTER_3[:12]} is not in the source library "
                                   "(sources add-batch --from-request or sources add)")
        draft = self.written(result)
        self.assertEqual([s["sha256"] for s in draft["sources"]], [LETTER_1])
        self.assertEqual(draft["limitations"][1], f"[EDIT ME: add these sources before review: {LETTER_3[:12]}]")
        self.library([])  # no library source at all: still written, both letters are blockers
        result = self.gap()
        self.assertEqual(len([line for line in result.stdout.splitlines() if line.startswith("blocker:")]), 2)
        self.assertEqual(self.written(result)["sources"], [])

    def test_gap_conflicts_and_unknown_requests_are_refused_by_name(self):
        self.sent([step("2026-02-10", "denied")])
        for extra in (("--agency", AGENCY), ("--source", LETTER_1[:12])):
            refusal = self.gap(*extra, returncode=2)
            self.assertIn("--gap takes the agency from the request", refusal.stderr)
        self.assertIn("--request is only for --gap", self.cli(*ASK, self.root, "--agency", AGENCY, "--title", "T x",
                                                               "--request", "second", returncode=2).stderr)
        self.assertIn("the following arguments are required: --agency, --title",
                      self.cli(*ASK, self.root, returncode=2).stderr)
        self.assertIn("no sent request ca-sheriff-x/missing; run kit-sent --request missing first",
                      self.gap("--request", "missing", returncode=1).stderr)
        other = self.cli(*ASK, self.root, "--gap", "ca-nobody", returncode=1)
        self.assertIn("no hand-sent request recorded for ca-nobody; run kit-sent first", other.stderr)
        self.assertFalse(self.drafts.exists())

    def test_basis_with_angle_brackets_or_newlines_is_written_as_plain_text(self):
        tricky = "Exempt <b>under</b> 7923.600\nStated basis (as written by the agency): forged"
        self.sent([step("2026-02-10", "denied", withheld_basis=[tricky, "Second \u2028 reason"])])
        draft = self.written(self.gap())  # load_draft refuses markup or a bad character
        lines = draft["body_md"].split("\n\n")[0].splitlines()
        self.assertEqual(len(lines), 3)  # sent/answer line plus exactly one line per stored basis
        self.assertEqual(lines[1], "Stated basis (as written by the agency): Exempt \u2039b\u203aunder\u2039/b\u203a "
                                   "7923.600 Stated basis (as written by the agency): forged")
        self.assertEqual(lines[2], "Stated basis (as written by the agency): Second reason")
        self.assertNotRegex(draft["body_md"], "[<>]")

    def test_untouched_draft_keeps_every_interpretive_marker_and_is_refused_by_the_review_gate(self):
        self.sent([step("2026-02-10", "denied", correspondence=[LETTER_1, LETTER_2])])
        draft = self.written(self.gap())
        for field in ("confidence", "summary", "author"):
            self.assertTrue(draft[field].startswith("[EDIT ME: "), field)
        self.assertTrue(all(item.startswith("[EDIT ME: ") for item in draft["limitations"] + draft["counterevidence"]))
        blockers = findings.review_status(self.root, draft)["review_blockers"]
        self.assertEqual([b for b in blockers if b.startswith("edit_marker:")],
                         ["edit_marker:author", "edit_marker:body_md", "edit_marker:confidence",
                          "edit_marker:counterevidence", "edit_marker:limitations", "edit_marker:sources[0].locator",
                          "edit_marker:sources[1].locator", "edit_marker:summary", "edit_marker:title"])
        self.assertNotIn("unknown_classification", blockers)
        self.assertEqual(sorted(path.name for path in (self.root / "content" / "findings").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
