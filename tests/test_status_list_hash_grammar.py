"""`status --list` refuses malformed stored digest tokens before projection (synthetic campaign only)."""
import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import ledger_list, sources_cmd
from campaign_tool.cli import database
from campaign_tool.content import ContentError

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
FIXED = "private/ledger.sqlite is not a readable ledger"
WHEN = "2026-10-20T09:00:00-07:00"
AGENCY = "ca-cedar-police"
CANARY = "CANARYSECRET"


def good(label):
    return hashlib.sha256(f"synthetic RF1751-1 record {label}\n".encode()).hexdigest()


GOOD = good("a")
BAD = {
    "empty": "",
    "short12": GOOD[:12],
    "short63": GOOD[:63],
    "long65": GOOD + "a",
    "nonhex": "g" * 64,
    "upper": GOOD.upper(),
    "lead_space": " " + GOOD[:63],
    "trail_space": GOOD[:63] + " ",
    "control": GOOD[:30] + "\x07" + GOOD[31:],
    "trailing_lf": GOOD + "\n",
    "unicode_lookalike": "\uff41" + GOOD[1:],
    "canary_tail": GOOD[:40] + CANARY.lower() + GOOD[52:],
}


def cli(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=120)


class HashGrammarTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-hash-grammar-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.ledger = self.root / "private" / "ledger.sqlite"
        self.number = 0

    def put(self, token, source="source-one", received=None):
        """One object plus one receipt carrying the same (possibly malformed) digest token."""
        self.number += 1
        db = database(self.root)
        with db:
            db.execute("INSERT OR IGNORE INTO objects VALUES (?, 1, ?)", (token, WHEN))
            db.execute("INSERT INTO receipts VALUES (?, ?, ?, ?, ?)",
                       (f"r{self.number}", token, source, received or f"2026-10-20T16:{self.number:02d}:00+00:00",
                        f"file{self.number}.txt"))
        db.close()

    def inventory(self):
        return {str(p.relative_to(self.root)): (p.read_bytes() if p.is_file() else None, p.stat().st_mode,
                                                p.stat().st_mtime_ns)
                for p in sorted(self.root.rglob("*"))}

    def assertRefusal(self, **kwargs):
        before = self.inventory()
        with self.assertRaises(ValueError) as caught:
            ledger_list.list_receipts(self.root, **kwargs)
        self.assertEqual(str(caught.exception), FIXED)
        self.assertEqual(self.inventory(), before)

    def test_malformed_digest_tokens_refuse_whole_listing(self):
        for label, token in BAD.items():
            with self.subTest(label):
                self.setUp()
                self.put(good("before"))
                self.put(token)
                self.put(good("after"))
                self.assertRefusal()

    def test_same_prefix_does_not_make_short_digest_valid(self):
        self.put(GOOD)
        self.put(GOOD[:12])
        self.assertRefusal()

    def test_cli_malformed_digest_is_value_free_refusal(self):
        for label in ("short12", "canary_tail", "upper"):
            with self.subTest(label):
                self.setUp()
                self.put(good("x"))
                self.put(BAD[label])
                before = self.inventory()
                result = cli("status", "--directory", str(self.root), "--list")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "Stopped: " + FIXED + "\n")
                self.assertNotIn(CANARY.lower(), result.stderr)
                self.assertNotIn(str(self.root), result.stderr)
                self.assertEqual(self.inventory(), before)

    def test_bad_digest_beyond_cap_still_refuses(self):
        self.put(good("one"), received="2026-10-20T10:00:00+00:00")
        self.put(good("two"), received="2026-10-20T11:00:00+00:00")
        self.put(BAD["nonhex"], received="2026-10-20T12:00:00+00:00")
        with mock.patch.object(ledger_list, "MAX_ROWS", 1):
            self.assertRefusal()

    def test_sql_excluded_malformed_digest_does_not_expand_scope(self):
        self.put(good("ok"), source="source-one", received="2026-10-20T10:00:00+00:00")
        self.put(BAD["short12"], source="source-two", received="2026-10-19T10:00:00+00:00")
        report = ledger_list.list_receipts(self.root, source_id="source-one")
        self.assertEqual(report["count"], 1)
        self.assertEqual(report["receipts"][0]["sha256_prefix"], good("ok")[:12])
        report = ledger_list.list_receipts(self.root, since="2026-10-20")
        self.assertEqual(report["count"], 1)
        self.assertRefusal(source_id="source-two")
        self.assertRefusal(since="2026-10-19")

    def test_missing_ledger_and_no_matching_rows_keep_empty_shape(self):
        self.assertEqual(ledger_list.list_receipts(self.root), {"receipts": [], "count": 0})
        self.put(good("ok"))
        self.assertEqual(ledger_list.list_receipts(self.root, source_id="nothing-here"),
                         {"receipts": [], "count": 0})

    def test_source_batch_refuses_before_prefix_resolution_or_write(self):
        self.put(good("ok"), source="prod")
        self.put(BAD["short12"], source="prod")
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                before = self.inventory()
                with mock.patch.object(sources_cmd, "_full_hashes") as hashes, \
                        mock.patch.object(sources_cmd, "_batch_write") as write:
                    with self.assertRaises(ValueError) as caught:
                        sources_cmd.add_batch(self.root, "prod", AGENCY, WHEN, dry_run=dry_run)
                self.assertNotIsInstance(caught.exception, ContentError)
                self.assertEqual(str(caught.exception), FIXED)
                hashes.assert_not_called()
                write.assert_not_called()
                self.assertEqual(self.inventory(), before)

    def test_canonical_digest_controls_keep_exact_projection(self):
        digits = "0123456789" * 6 + "0123"
        letters = "abcdef" * 10 + "abcd"
        shared = [GOOD[:12] + "0" * 52, GOOD[:12] + "f" * 52]
        tokens = [digits, letters] + shared
        for token in tokens:
            self.put(token)
        self.put(tokens[0])  # duplicate receipt on one object
        report = ledger_list.list_receipts(self.root)
        self.assertEqual([r["sha256_prefix"] for r in report["receipts"]], [t[:12] for t in tokens + [tokens[0]]])
        self.assertEqual(report["count"], 5)
        with mock.patch.object(ledger_list, "MAX_ROWS", 2):
            capped = ledger_list.list_receipts(self.root)
        self.assertEqual((capped["count"], capped["more"]), (2, 3))

    def test_refusal_and_success_preserve_ledger_and_cleanup(self):
        self.put(good("ok"))
        before = self.inventory()
        self.assertEqual(ledger_list.list_receipts(self.root)["count"], 1)
        self.assertEqual(self.inventory(), before)
        self.put(BAD["trailing_lf"])
        self.assertRefusal()
        names = {p.name for p in self.ledger.parent.iterdir()}
        self.assertIn("ledger.sqlite", names)
        self.assertFalse(any(n.endswith(("-journal", "-wal", "-shm")) for n in names))


if __name__ == "__main__":
    unittest.main()
