"""DF2237-12: newsletter --from-content lists recorded meeting outcomes under Decisions. Synthetic; nothing is sent."""
import copy
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.newsletter.draft import MAX_MEETINGS, DraftError, build_draft  # noqa: E402
from campaign_tool.newsletter.from_content import manifest_from_content  # noqa: E402
from tests.test_newsletter import MANIFEST, NOW as WORKER_NOW  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
CFG = {"name": "Synthetic Campaign", "county": "Synthetic"}
# sha256 prefixes of build_draft(MANIFEST, now=WORKER_NOW) on main before DF2237-12 (html, text).
PRE_CHANGE = ("c69f11fac9536e5b", "99cdc93e3021dda1")
DECISION = {"body": "Synthetic Board", "on": "2026-10-06", "result": "denied", "vote": None,
            "source_url": "https://records.example.invalid/minutes/1", "meeting_path": "/meetings/x.html"}


def meeting(meeting_id, body, day, outcome=None):
    document = {"id": meeting_id, "body": body, "starts_at": day + "T18:00:00-07:00",
                "agenda_url": "https://records.example.invalid/agenda/" + meeting_id, "agenda_item": "Item 4"}
    return {**document, "outcome": outcome} if outcome else document


def outcome(result, on, verified_at, vote=None):
    return {"result": result, "on": on, "source_url": f"https://records.example.invalid/minutes/{on}",
            "verified_by": "Synthetic Organizer", "verified_at": verified_at, **({"vote": vote} if vote else {})}


class DecisionsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-newsletter-decisions-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root)
        folder = self.root / "content" / "meetings"
        # File order (a-, m-, z-) is the reverse of outcome order, so the sort is by `on`.
        for name, document in (
                ("a-late", meeting("board-late", "Synthetic Board Late", "2026-10-06",
                                   outcome("denied", "2026-10-06", "2026-10-08"))),
                ("m-pending", meeting("board-pending", "Synthetic Board Pending", "2026-10-20")),
                ("z-early", meeting("board-early", "Synthetic Board Early", "2026-09-08",
                                    outcome("approved", "2026-09-08", "2026-09-10", vote="4-1")))):
            (folder / (name + ".json")).write_text(json.dumps(document))

    def draft(self, since=None):
        manifest = manifest_from_content(self.root, CFG, since)
        return manifest, build_draft(manifest, now=NOW)

    def section(self, text):
        self.assertEqual(text.count("## Decisions"), 1)
        return text.split("## Decisions\n", 1)[1].split("\n\n", 1)[0].splitlines()

    def test_since_lists_only_outcomes_recorded_on_or_after_it(self):
        manifest, draft = self.draft("2026-10-01T00:00:00+00:00")
        self.assertEqual(manifest["decisions"], [{
            "body": "Synthetic Board Late", "on": "2026-10-06", "result": "denied", "vote": None,
            "source_url": "https://records.example.invalid/minutes/2026-10-06", "meeting_path": "/meetings/board-late.html"}])
        self.assertEqual(self.section(draft["text"]), [
            "- **Synthetic Board Late**, Oct 06, 2026: Denied [Official record](https://records.example.invalid/minutes/2026-10-06)"])
        self.assertEqual(draft["counts"]["decisions"], 1)
        self.assertIn('<a href="https://records.example.invalid/minutes/2026-10-06"', draft["html"])
        self.assertNotIn("Synthetic Board Early", draft["text"])
        # verified the day of `since` still counts; the day before does not
        self.assertEqual(len(self.draft("2026-10-08T00:00:00+00:00")[0]["decisions"]), 1)
        self.assertEqual(self.draft("2026-10-09T00:00:00+00:00")[0]["decisions"], [])

    def test_without_since_every_recorded_outcome_in_date_order(self):
        _, draft = self.draft()
        self.assertEqual(self.section(draft["text"]), [
            "- **Synthetic Board Early**, Sep 08, 2026: Approved (vote 4-1) "
            "[Official record](https://records.example.invalid/minutes/2026-09-08)",
            "- **Synthetic Board Late**, Oct 06, 2026: Denied [Official record](https://records.example.invalid/minutes/2026-10-06)"])
        self.assertEqual(draft["counts"]["decisions"], 2)
        self.assertNotIn("## Upcoming meetings", draft["text"])  # past meetings are never "upcoming"

    def test_meeting_without_outcome_is_never_described_as_decided(self):
        manifest, draft = self.draft()
        self.assertNotIn("Synthetic Board Pending", draft["text"] + draft["html"])
        self.assertNotIn("Cedar City Council", draft["text"])  # the example's own past meeting, no outcome
        self.assertEqual(draft["excluded"]["meetings_past"], len(manifest["meetings"]))
        for path in self.root.glob("content/meetings/*.json"):
            document = json.loads(path.read_text())
            document.pop("outcome", None)
            path.write_text(json.dumps(document))
        manifest, draft = self.draft()
        self.assertEqual(manifest["decisions"], [])
        self.assertNotIn("decisions", draft["counts"])
        for word in ("Decisions", "decided", "voted", "Official record", "Approved", "Denied"):
            self.assertNotIn(word, draft["text"])

    def test_worker_manifest_without_decisions_is_byte_identical(self):
        before = build_draft(MANIFEST, now=WORKER_NOW)
        prefixes = tuple(hashlib.sha256(before[key].encode()).hexdigest()[:16] for key in ("html", "text"))
        self.assertEqual(prefixes, PRE_CHANGE)
        self.assertEqual(before["counts"], {"findings": 2, "meetings": 1})
        for value in (None, []):
            after = build_draft({**MANIFEST, "decisions": value}, now=WORKER_NOW)
            self.assertEqual((after["html"], after["text"], after["counts"]),
                             (before["html"], before["text"], before["counts"]))

    def test_malformed_decisions_refused_naming_the_entry(self):
        good = dict(DECISION)
        cases = (("not a list", {"body": "x"}, r"^decisions must be a list"),
                 ("string", "approved", r"^decisions must be a list"),
                 ("entry", [good, "approved"], r"^decisions\[1\]: must be an object"),
                 ("result", [good, {**good, "result": "passed"}], r"^decisions\[1\]\.result"),
                 ("http", [{**good, "source_url": "http://records.example.invalid/m"}], r"^decisions\[0\]\.source_url"),
                 ("javascript", [good, {**good, "source_url": "javascript:alert(1)"}], r"^decisions\[1\]\.source_url"),
                 ("body", [{**good, "body": "B" * 201}], r"^decisions\[0\]\.body: longer than 200"),
                 ("empty body", [{**good, "body": " "}], r"^decisions\[0\]\.body"),
                 ("vote", [{**good, "vote": "v" * 41}], r"^decisions\[0\]\.vote: longer than 40"),
                 ("date", [{**good, "on": "2026-10-6"}], r"^decisions\[0\]\.on"),
                 ("bad day", [{**good, "on": "2026-02-30"}], r"^decisions\[0\]\.on"),
                 ("timestamp", [{**good, "on": "2026-10-06T00:00:00Z"}], r"^decisions\[0\]\.on"))
        for name, value, message in cases:
            with self.subTest(name), self.assertRaisesRegex(DraftError, message):
                build_draft({**MANIFEST, "decisions": value}, now=WORKER_NOW)
        # an entry past the display cap is still validated
        with self.assertRaisesRegex(DraftError, rf"^decisions\[{MAX_MEETINGS + 1}\]\.result"):
            build_draft({**MANIFEST, "decisions": [good] * (MAX_MEETINGS + 1) + [{**good, "result": "x"}]},
                        now=WORKER_NOW)

    def test_cli_writes_nothing_for_bad_decisions(self):
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({**MANIFEST, "decisions": [{**DECISION, "source_url": "javascript:x"}]}))
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.newsletter", "--directory", str(self.root),
                                 "--manifest", str(manifest)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Stopped: decisions[0].source_url", result.stderr)
        self.assertFalse((self.root / "kit").exists())
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.newsletter", "--directory", str(self.root),
                                 "--from-content", "--since", "2026-10-01"], cwd=REPO_ROOT, capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["counts"]["decisions"], 1)
        self.assertIn("Synthetic Board Late**, Oct 06, 2026: Denied", (self.root / "kit" / "newsletter-draft.txt").read_text())

    def test_body_markup_escaped_like_meeting_bodies(self):
        body = "Board **x** & co"
        manifest = copy.deepcopy(MANIFEST)
        manifest["meetings"][0]["body"] = body
        manifest["decisions"] = [{**DECISION, "body": body}]
        html = build_draft(manifest, now=WORKER_NOW)["html"]
        rendered = re.findall(r"<li>(.*?), (?:Oct|Wed)", html)
        self.assertEqual(len(rendered), 2, html)
        self.assertEqual(rendered[0], rendered[1])
        self.assertIn("&amp;", rendered[0])
        for markup in ("<img src=x>", "a <b>bold</b> body"):
            for key in ("meetings", "decisions"):
                broken = copy.deepcopy(manifest)
                broken[key][0]["body"] = markup
                with self.subTest(key=key, markup=markup), self.assertRaisesRegex(DraftError, r"\.body: plain text"):
                    build_draft(broken, now=WORKER_NOW)

    def test_more_than_the_cap_ends_with_and_n_more(self):
        entries = [{**DECISION, "body": f"Synthetic Board {n:02d}"} for n in range(MAX_MEETINGS + 3)]
        draft = build_draft({**MANIFEST, "decisions": entries}, now=WORKER_NOW)
        lines = self.section(draft["text"])
        self.assertEqual(len(lines), MAX_MEETINGS + 1)
        self.assertEqual(lines[-1], "- and 3 more on the site.")
        self.assertNotIn(f"Synthetic Board {MAX_MEETINGS:02d}", draft["text"])
        self.assertEqual(draft["counts"]["decisions"], MAX_MEETINGS + 3)
        text = draft["text"]
        self.assertLess(text.index("## New findings"), text.index("## Decisions"))
        self.assertLess(text.index("## Decisions"), text.index("## Upcoming meetings"))


if __name__ == "__main__":
    unittest.main()
