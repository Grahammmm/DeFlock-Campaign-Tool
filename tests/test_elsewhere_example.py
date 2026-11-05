"""DF1107-05: examples/elsewhere.json, the organizer-rechecked starter, checked offline through the DF1107-04 validator and guard.

Nothing here fetches a URL; sockets are blocked. Source authority is evidenced by the table in docs/SITE-CONTENT.md, not by tests.
"""
import copy
import json
import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool.content import ContentError
from campaign_tool.content_elsewhere import load_elsewhere, read_elsewhere
from campaign_tool.legal_claims import elsewhere_notes

REPO = Path(__file__).resolve().parents[1]
STARTER = REPO / "examples" / "elsewhere.json"
WRAPPER = REPO / "tests" / "fixtures" / "no_network_run.py"
TODAY = date(2026, 11, 4)


def entries():
    return json.loads(STARTER.read_text(encoding="utf-8"))


def problems_for(document, today=TODAY):
    found = []
    load_elsewhere(document, today=today, problems=found)
    return found


class StarterDataTests(unittest.TestCase):
    def setUp(self):
        guard = mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network used"))
        guard.start()
        self.addCleanup(guard.stop)

    def test_starter_passes_the_shared_validator(self):
        data = entries()
        self.assertTrue(8 <= len(data) <= 20, len(data))
        self.assertEqual(problems_for(data), [])
        self.assertEqual(len(load_elsewhere(data, today=TODAY)), len(data))
        for number, entry in enumerate(data):
            self.assertTrue(entry["source_url"].startswith("https://"), number)
            self.assertLessEqual(entry["decided_on"], entry["checked_on"], number)

    def test_curated_wording_raises_no_legal_notes_and_is_scanned_unmodified(self):
        data = entries()
        self.assertEqual(elsewhere_notes(data), [])
        # positive control: the same scan flags an unqualified violation by index, without printing the text
        flagged = copy.deepcopy(data)
        flagged[2]["as_stated"] = "The agency violated SB 34."
        notes = elsewhere_notes(flagged)
        self.assertEqual(notes, ["review-required: elsewhere.json[2].as_stated: legal-claim wording (violation-verb)"])
        self.assertNotIn("SB 34", notes[0])
        # an official source_url never exempts an entry from the scan
        self.assertTrue(flagged[2]["source_url"].startswith("https://"))

    def test_starter_count_bounds(self):
        data = entries()
        seven = data[:7]
        twenty_one = [dict(data[i % len(data)], decided_on=f"2026-01-{i + 1:02d}", checked_on="2026-11-04") for i in range(21)]
        self.assertFalse(8 <= len(seven) <= 20)  # a starter of seven fails the starter-count rule ...
        self.assertEqual(problems_for(seven), [])  # ... while the general validator still accepts 1-100
        self.assertFalse(8 <= len(twenty_one) <= 20)
        self.assertEqual(problems_for(twenty_one), [])
        for size in (8, 20):
            fixture = [dict(data[i % len(data)], decided_on=f"2026-01-{i + 1:02d}") for i in range(size)]
            self.assertTrue(8 <= len(fixture) <= 20)
            self.assertEqual(problems_for(fixture), [], size)

    def test_mutations_fail_with_field_and_index_context(self):
        base = entries()
        cases = {
            "missing official source_url": (lambda e: (e[1].pop("source_url"), e[1].update(news_url="https://news.example.test/x")),
                                            "elsewhere.json[1]: missing field(s) ['source_url']"),
            "unsupported kind": (lambda e: e[0].update(kind="banned"), "elsewhere.json[0].kind"),
            "future checked_on": (lambda e: e[3].update(checked_on="2026-11-05"), "elsewhere.json[3].checked_on: 2026-11-05 is in the future"),
            "invalid checked_on": (lambda e: e[3].update(checked_on="2026-02-30"), "elsewhere.json[3].checked_on"),
            "decided after checked": (lambda e: e[4].update(checked_on="2025-01-01"), "elsewhere.json[4].decided_on"),
            "as_stated too long": (lambda e: e[2].update(as_stated="x" * 201), "elsewhere.json[2].as_stated"),
            "duplicate identity": (lambda e: e.append(dict(e[0], as_stated="Another wording.")), f"elsewhere.json[{len(base)}]: duplicate of elsewhere.json[0]"),
            "http source": (lambda e: e[5].update(source_url="http://example.test/x"), "elsewhere.json[5].source_url"),
        }
        for name, (mutate, expected) in cases.items():
            with self.subTest(name):
                data = copy.deepcopy(base)
                mutate(data)
                found = problems_for(data)
                self.assertTrue(any(line.startswith(expected) for line in found), found)

    def test_every_entry_cites_its_own_record(self):
        # a place may repeat (two Redmond decisions) but never a whole identity or a second copy of the same wording
        data = entries()
        self.assertEqual(len({entry["as_stated"] for entry in data}), len(data))
        self.assertTrue(all("news_url" not in entry or entry["news_url"] != entry["source_url"] for entry in data))


class StarterIsOptInTests(unittest.TestCase):
    def cli(self, *args):
        result = subprocess.run([sys.executable, "-B", str(WRAPPER), *map(str, args)], cwd=REPO, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("NETWORK_FORBIDDEN", result.stderr)

    def test_init_and_content_init_never_insert_the_starter(self):
        before = STARTER.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self.cli("init", "--directory", tmp / "new", "--name", "Synthetic Humboldt", "--county", "Humboldt", "--state", "CA")
            self.cli("content", "init", "--directory", tmp / "new")
            self.assertTrue((tmp / "new" / "content").is_dir())
            self.assertEqual(sorted(p.name for p in (tmp / "new").rglob("elsewhere*")), [])
            self.assertIsNone(read_elsewhere(tmp / "new", TODAY))  # absent file stays the feature-off state
            # only an explicit manual copy enables it, and the shared reader then accepts it
            (tmp / "new" / "content" / "elsewhere.json").write_bytes(before)
            self.assertEqual(len(read_elsewhere(tmp / "new", TODAY)), len(entries()))
        self.assertEqual(STARTER.read_bytes(), before)  # nothing edited the starter
        self.assertFalse((REPO / "examples" / "fictional-campaign" / "content" / "elsewhere.json").exists())

    def test_symlinked_copy_is_refused_not_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            content = Path(tmp) / "content"
            content.mkdir()
            (content / "elsewhere.json").symlink_to(STARTER)
            with self.assertRaises(ContentError):
                read_elsewhere(tmp, TODAY)


if __name__ == "__main__":
    unittest.main()
