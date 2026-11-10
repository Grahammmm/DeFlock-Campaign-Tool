"""`sources add-batch`: one ingest source_id's unlisted receipts in one validated write (synthetic campaign only)."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.cli import database
from campaign_tool.content import ContentError, load_content
from campaign_tool.sources_cmd import add_batch

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
FULL_HASH = re.compile(r"[0-9a-f]{64}")
WHEN = "2026-10-20T09:00:00-07:00"
AGENCY = "ca-cedar-police"


def digest(label):
    return hashlib.sha256(f"synthetic DF1822-03 record {label}; not a real document\n".encode()).hexdigest()


def cli(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=120)


class SourcesAddBatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-sources-batch-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.sources = self.root / "content" / "sources.json"

    def receive(self, rows):
        """rows: (label, source_id, original_name); synthetic ledger rows, no record bytes stored."""
        db = database(self.root)
        with db:
            for number, (label, source_id, name) in enumerate(rows):
                db.execute("INSERT OR IGNORE INTO objects VALUES (?, 1, ?)", (digest(label), WHEN))
                db.execute("INSERT INTO receipts VALUES (?, ?, ?, ?, ?)",
                           (f"r{number}-{source_id}", digest(label), source_id, f"2026-10-20T16:{number % 60:02d}:00+00:00",
                            name))
        db.close()

    def list_existing(self, label):
        document = json.loads(self.sources.read_text(encoding="utf-8"))
        document.append({**document[0], "sha256": digest(label)})
        self.sources.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    def batch(self, source_id="prod-2026-10", agency_id=AGENCY, received_at=WHEN, **extra):
        return add_batch(self.root, source_id, agency_id, received_at, **extra)

    def assertRefused(self, message, **arguments):
        before = self.sources.read_bytes()
        with self.assertRaises(ValueError) as caught:
            self.batch(**arguments)
        self.assertIn(message, str(caught.exception))
        self.assertEqual(self.sources.read_bytes(), before)
        self.assertEqual(list(self.sources.parent.glob(".sources-*")), [])

    def three(self):
        self.receive([("a", "prod-2026-10", "Policy A.pdf"), ("b", "prod-2026-10", "Contract B.pdf"),
                      ("c", "prod-2026-10", "Audit C.pdf"), ("d", "other-prod", "Other D.pdf")])
        self.list_existing("b")

    def test_three_receipts_one_listed_adds_two_and_keeps_existing_bytes(self):
        self.three()
        original = self.sources.read_text(encoding="utf-8")
        result = cli("sources", "add-batch", "--directory", str(self.root), "--source-id", "prod-2026-10",
                     "--agency-id", AGENCY, "--received-at", WHEN)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(FULL_HASH.search(result.stdout), "a full 64-hex hash was printed")
        self.assertEqual(json.loads(result.stdout),
                         {"added": [digest("a")[:12], digest("c")[:12]], "already_listed": 1, "dry_run": False})
        written = self.sources.read_text(encoding="utf-8")
        self.assertTrue(written.startswith(original.rstrip()[:-1].rstrip()), "existing entry bytes changed")
        document = json.loads(written)
        self.assertEqual(document[:3], json.loads(original))
        self.assertEqual([entry["sha256"] for entry in document[3:]], [digest("a"), digest("c")])
        self.assertEqual(document[3], {"sha256": digest("a"), "title": "Policy A [EDIT ME: title]", "agency_id": AGENCY,
                                       "received_at": WHEN, "public_url": None, "fragment": "cedar-police-records",
                                       "pages": None, "note": "added in batch from prod-2026-10"})
        self.assertEqual(len(load_content(self.root)["sources"]), 5)
        self.assertEqual(list(self.sources.parent.glob(".sources-*")), [])
        self.assertFalse((self.root / "public").exists(), "nothing is built or published")

    def test_dry_run_reports_the_same_and_leaves_bytes_and_mtime(self):
        self.three()
        before, stamp = self.sources.read_bytes(), self.sources.stat().st_mtime_ns
        self.assertEqual(self.batch(dry_run=True),
                         {"added": [digest("a")[:12], digest("c")[:12]], "already_listed": 1, "dry_run": True})
        self.assertEqual((self.sources.read_bytes(), self.sources.stat().st_mtime_ns), (before, stamp))

    def test_unknown_source_bad_agency_and_offsetless_time_are_refused(self):
        self.three()
        self.assertRefused("no receipts for source_id", source_id="prod-2026-11")
        self.assertRefused("no receipts for source_id", source_id="prod-2026-1")  # exact match only
        self.assertRefused("not one of the campaign's agencies", agency_id="ca-nowhere-police")
        self.assertRefused("not one of the campaign's agencies", agency_id="CA Cedar Police")
        self.assertRefused("must include a time zone offset", received_at="2026-10-20T09:00:00")
        self.assertRefused("UTC offset", received_at="last tuesday")
        self.assertRefused("must include a time zone offset", received_at="2026-10-20")

    def test_fragment_needed_for_an_agency_without_one_and_library_reserved(self):
        self.three()
        self.assertRefused("give --fragment", agency_id="ca-cedar-county-sheriff")
        self.assertRefused("'library' is the page anchor", fragment="library")
        self.assertRefused("'library' is the page anchor", agency_id="ca-cedar-county-sheriff", fragment="library")
        result = self.batch(agency_id="ca-cedar-county-sheriff", fragment="sheriff-production")
        self.assertEqual(len(result["added"]), 2)
        self.assertEqual({entry["fragment"] for entry in json.loads(self.sources.read_text())[3:]},
                         {"sheriff-production"})

    def test_two_existing_fragments_for_the_agency_need_an_explicit_fragment(self):
        self.three()
        document = json.loads(self.sources.read_text(encoding="utf-8"))
        document[1]["fragment"] = "cedar-police-contracts"
        self.sources.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        self.assertRefused("2 existing fragments; give --fragment")

    def test_symlinked_or_oversized_library_is_refused(self):
        self.three()
        real = self.root / "sources-real.json"
        shutil.copy(self.sources, real)
        os.unlink(self.sources)
        self.sources.symlink_to(real)
        with self.assertRaises(ContentError) as caught:
            self.batch()
        self.assertIn("symlinks are not read or replaced", str(caught.exception))
        self.assertTrue(self.sources.is_symlink())
        os.unlink(self.sources)
        big = b"[" + b" " * (6 * 1024 * 1024) + b"]"
        self.sources.write_bytes(big)
        self.assertRefused("file too large")

    def test_markup_control_characters_and_long_names_are_cleaned_and_marked(self):
        self.receive([("m", "prod-x", "<b>Flock</b> <script>x()</script>contract.pdf"),
                      ("k", "prod-x", "minutes\x00\x1b[31m\u202e final.docx"),
                      ("l", "prod-x", "L" * 300 + ".pdf"), ("e", "prod-x", ".pdf"), ("n", "prod-x", "dir/sub\\Notes v2")])
        result = self.batch(source_id="prod-x")
        self.assertEqual(len(result["added"]), 5)
        titles = [entry["title"] for entry in json.loads(self.sources.read_text())[2:]]
        for title in titles:
            self.assertTrue(title.endswith(" [EDIT ME: title]"), title)
            self.assertNotRegex(title, r"[<>\x00-\x1f\x7f\u202e]")
        self.assertEqual(titles[0], "Flock x() contract [EDIT ME: title]")
        self.assertEqual(titles[2], "L" * 120 + " [EDIT ME: title]")
        self.assertEqual(titles[3], ".pdf [EDIT ME: title]")
        self.assertEqual(titles[4], "Notes v2 [EDIT ME: title]")
        self.assertEqual(len(load_content(self.root)["sources"]), 7)

    def test_more_than_five_hundred_new_records_refused_and_no_full_hash_printed(self):
        self.receive([(f"bulk-{number}", "bulk", f"record {number}.pdf") for number in range(501)])
        self.assertRefused("501 new records; at most 500 per run", source_id="bulk")
        result = cli("sources", "add-batch", "--directory", str(self.root), "--source-id", "bulk",
                     "--agency-id", AGENCY, "--received-at", WHEN)
        self.assertEqual(result.returncode, 1)
        self.assertIn("at most 500 per run", result.stderr)
        self.assertIsNone(FULL_HASH.search(result.stdout + result.stderr), "a full 64-hex hash was printed")
        self.assertEqual(len(json.loads(self.sources.read_text())), 2)

    def test_five_hundred_are_accepted_in_one_write(self):
        self.receive([(f"bulk-{number}", "bulk", f"record {number}.pdf") for number in range(500)])
        self.assertEqual(len(self.batch(source_id="bulk")["added"]), 500)
        self.assertEqual(len(load_content(self.root)["sources"]), 502)

    def test_second_run_adds_nothing_and_duplicate_receipts_add_one_entry(self):
        self.receive([("a", "prod-2026-10", "Policy A.pdf"), ("a", "prod-2026-10", "Policy A copy.pdf"),
                      ("c", "prod-2026-10", "Audit C.pdf")])
        self.assertEqual(self.batch()["added"], [digest("a")[:12], digest("c")[:12]])
        after, stamp = self.sources.read_bytes(), self.sources.stat().st_mtime_ns
        self.assertEqual(self.batch(), {"added": [], "already_listed": 2, "dry_run": False})
        self.assertEqual((self.sources.read_bytes(), self.sources.stat().st_mtime_ns), (after, stamp))

    def test_listed_in_another_fragment_counts_as_already_listed(self):
        self.three()
        document = json.loads(self.sources.read_text(encoding="utf-8"))
        document[2]["fragment"] = "cedar-elsewhere"
        document[2]["agency_id"] = "ca-cedar-city-council"
        self.sources.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        self.assertEqual(self.batch()["already_listed"], 1)

    def test_batch_oversize_preserves_library(self):
        from campaign_tool import ledger_list
        self.three()
        ledger = self.root / "private" / "ledger.sqlite"
        before = (self.sources.stat().st_mode, self.sources.stat().st_mtime_ns, ledger.read_bytes(),
                  ledger.stat().st_mtime_ns, sorted(os.listdir(ledger.parent)))
        with mock.patch.object(ledger_list, "MAX_LEDGER_BYTES", ledger.stat().st_size - 1):
            self.assertRefused(ledger_list.LEDGER_SIZE_ERROR)
            self.assertRefused(ledger_list.LEDGER_SIZE_ERROR, dry_run=True)
        self.assertEqual(before, (self.sources.stat().st_mode, self.sources.stat().st_mtime_ns, ledger.read_bytes(),
                                  ledger.stat().st_mtime_ns, sorted(os.listdir(ledger.parent))))
        self.assertEqual(self.batch(dry_run=True)["added"], [digest("a")[:12], digest("c")[:12]])  # under the limit again

    def test_no_ledger_or_invalid_library_is_refused(self):
        self.assertRefused("no receipts for source_id")
        self.receive([("a", "prod-2026-10", "Policy A.pdf")])
        self.sources.write_text('[{"sha256": "x"}]\n', encoding="utf-8")
        self.assertRefused("sources.json[0]")


if __name__ == "__main__":
    unittest.main()
