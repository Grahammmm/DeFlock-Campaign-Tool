"""status --check: malformed ledger byte counts are invalid_ledger_row rows (RF0825-6).

Synthetic bytes in a temporary campaign only. Run from the repository root:
python3 -B -m unittest tests.test_store_check_byte_count -q
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import store_check  # noqa: E402
from tests import test_store_check as fixtures  # noqa: E402  (alias only: FIRST/SECOND/sha, no TestCase rerun)

FIRST, SECOND, sha = fixtures.FIRST, fixtures.SECOND, fixtures.sha
CANARY = "SYNTHETIC-PRIVATE-COUNT-CANARY"


def bad(digest):
    return {"sha256": digest, "problem": "invalid_ledger_row"}


class Hostile:
    """A count whose every conversion, comparison and display protocol raises."""

    def _boom(self, *args):
        raise AssertionError("invalid byte count was converted, compared or displayed")
    __int__ = __index__ = __float__ = __eq__ = __ne__ = __lt__ = __le__ = __gt__ = __ge__ = _boom
    __repr__ = __str__ = __format__ = __bytes__ = __hash__ = __add__ = __radd__ = _boom


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-store-count-")
        self.addCleanup(temporary.cleanup)
        self.addCleanup(os.umask, os.umask(0o077))
        self.base = Path(temporary.name)
        self.root = self.base / "campaign"
        self.objects = self.root / "private" / "objects"
        self.ledger = self.root / "private" / "ledger.sqlite"

    def cli(self, command, *arguments, code=0):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "campaign_tool", command, "--directory", str(self.root),
             *map(str, arguments)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def campaign(self):
        """Real initialised, ingested synthetic campaign with two objects."""
        self.cli("init", "--name", "Synthetic Campaign", "--county", "Synthetic", "--state", "CA")
        for data, name in ((FIRST, "first.txt"), (SECOND, "second.txt")):
            (self.base / name).write_bytes(data)
            self.cli("ingest", "--file", self.base / name, "--source-id", "synthetic-source")

    def set_count(self, data, value):
        db = sqlite3.connect(self.ledger)
        with db:
            db.execute("UPDATE objects SET byte_count = ? WHERE sha256 = ?", (value, sha(data)))
        kind = db.execute("SELECT typeof(byte_count) FROM objects WHERE sha256 = ?", (sha(data),)).fetchone()[0]
        db.close()
        return kind

    def snapshot(self):
        found = {}
        for path in sorted(self.root.rglob("*")):
            info = os.lstat(path)
            body = path.read_bytes() if path.is_file() and not path.is_symlink() else None
            found[str(path.relative_to(self.root))] = (info.st_mode, info.st_mtime_ns, body)
        return found


class SqliteAndCliTests(Base):
    def test_sqlite_text_blob_real_and_negative_counts_are_invalid_rows(self):
        self.campaign()
        cases = ((CANARY, "text"), (b"\x00\xffblob", "blob"), (2.5, "real"), (-1, "integer"))
        order = sorted([sha(FIRST), sha(SECOND)])
        for value, kind in cases:
            with self.subTest(kind=kind):
                self.set_count(FIRST, len(FIRST))
                self.assertEqual(self.set_count(FIRST, value), kind)
                self.set_count(SECOND, 3)  # valid integer, wrong size: the peer is still really verified
                result = store_check.check_store(self.root)
                json.dumps(result)
                self.assertEqual(result["checked_objects"], 2)
                expected = {sha(FIRST): bad(sha(FIRST)),
                            sha(SECOND): {"sha256": sha(SECOND), "problem": "size_mismatch",
                                          "expected": 3, "actual": len(SECOND)}}
                self.assertEqual(result["problems"], [expected[digest] for digest in order])
                self.set_count(SECOND, len(SECOND))

    def test_real_cli_bad_counts_are_complete_private_problem_reports(self):
        self.campaign()
        plain = self.cli("status").stdout
        for value in (CANARY, b"\x00\xffblobbytes", 2.5, -1):
            with self.subTest(value=type(value).__name__):
                self.set_count(FIRST, value)
                before = self.snapshot()
                result = self.cli("status", "--check", code=3)
                self.assertNotIn("Traceback", result.stderr)
                self.assertNotIn("Stopped", result.stdout + result.stderr)
                self.assertTrue(result.stdout.startswith(plain))
                decoder = json.JSONDecoder()
                counts, end = decoder.raw_decode(result.stdout)
                block, _ = decoder.raw_decode(result.stdout[end:].lstrip())
                self.assertEqual(counts["stored_objects"], 2)
                self.assertEqual(block["check"], {"checked_objects": 2, "problems": [bad(sha(FIRST))]})
                for text in (CANARY, "blobbytes", "b'", "2.5", "-1"):
                    self.assertNotIn(text, result.stdout + result.stderr)
                self.assertEqual(self.snapshot(), before)  # check-only: nothing created, rewritten or chmodded
                self.set_count(FIRST, len(FIRST))
        self.cli("status", "--check", code=0)


class RowsTests(Base):
    def setUp(self):
        super().setUp()
        self.objects.mkdir(parents=True, mode=0o700)

    def put(self, data):
        (self.objects / sha(data)).write_bytes(data)
        return sha(data)

    def check(self, rows):
        return store_check.check_store(self.root, rows=rows)

    def test_rows_counts_require_exact_nonnegative_int(self):
        one = self.put(b"x")
        values = [True, False, 1.0, 0.0, 1.5, float("nan"), float("inf"), float("-inf"), None, "1", " 1", b"1",
                  -1, Hostile()]
        for value in values:
            with self.subTest(value=type(value).__name__ + ("" if isinstance(value, Hostile) else str(value))):
                rows = [(one, value)]
                with mock.patch.object(store_check, "_sha256") as hashing:
                    result = self.check(rows)
                hashing.assert_not_called()
                self.assertEqual(result, {"checked_objects": 1, "problems": [bad(one)]})
                self.assertIs(rows[0][1], value)  # caller data untouched
        generated = self.check(iter([(one, True), (one, 1)]))
        self.assertEqual(generated["checked_objects"], 2)
        self.assertEqual(generated["problems"], [bad(one)])  # the exact int row for the same object is clean

    def test_invalid_count_precedes_object_lookup_and_hashing(self):
        absent = sha(b"absent")
        plain = self.put(b"plain object")
        linked = sha(b"linked")
        os.symlink(self.base / "nowhere", self.objects / linked)
        piped = sha(b"piped")
        os.mkfifo(self.objects / piped, 0o600)
        peer = self.put(FIRST)
        rows = [(digest, "7") for digest in (absent, plain, linked, piped)] + [(peer, len(FIRST))]
        seen = []
        real_lstat, real_open = os.lstat, os.open

        def lstat(path, *args, **kwargs):
            seen.append(("lstat", Path(path).name))
            return real_lstat(path, *args, **kwargs)

        def open_(path, *args, **kwargs):
            seen.append(("open", Path(path).name))
            return real_open(path, *args, **kwargs)
        with mock.patch.object(store_check.os, "lstat", lstat), mock.patch.object(store_check.os, "open", open_), \
                mock.patch.object(store_check, "_sha256", wraps=store_check._sha256) as hashing:
            result = self.check(rows)
        self.assertEqual([name for _, name in seen], [peer, peer, peer])  # only the valid peer is looked at
        self.assertEqual(hashing.call_count, 1)
        self.assertEqual(result["problems"], [bad(d) for d in (absent, plain, linked, piped)])
        self.assertEqual(result["checked_objects"], 5)

    def test_bad_count_hash_membership_does_not_invent_orphan(self):
        first, second = self.put(FIRST), self.put(SECOND)
        orphan = self.put(b"synthetic orphan")
        (self.objects / ".intake-abc").write_bytes(b"temp")
        (self.objects / "notes.txt").write_bytes(b"note")
        os.chmod(self.objects / second, 0o644)
        rows = [(second, None), (first, 1.5), (second, len(SECOND) + 1)]
        before = list(rows)
        result = self.check(rows)
        self.assertEqual(rows, before)
        self.assertEqual(result["checked_objects"], 3)
        self.assertEqual(result["problems"][:2], [bad(second), bad(first)])  # row order, one problem per row
        self.assertEqual(result["problems"][2], {"sha256": second, "problem": "size_mismatch",
                                                 "expected": len(SECOND) + 1, "actual": len(SECOND)})
        tail = result["problems"][3:]
        self.assertEqual(tail[:3], [{"name": ".intake-abc", "problem": "leftover_temp"},
                                    {"name": orphan, "problem": "no_ledger_row"},
                                    {"name": "notes.txt", "problem": "unexpected_entry"}])
        self.assertEqual([p["problem"] for p in tail[3:]], ["loose_mode"])
        self.assertEqual(sum(p.get("name") in (first, second) for p in result["problems"]), 0)

    def test_zero_exact_and_large_integer_counts_preserve_budget(self):
        exact = b"0123456789abcdefghijklmn"
        good = {self.put(b""): 0, self.put(exact): len(exact), self.put(b"x"): 1}
        empty, long, one = good

        def run(**override):  # every stored object is a row, so only the overridden one can be a finding
            return self.check([(d, override.get(d, n)) for d, n in good.items()])["problems"]
        self.assertEqual(run(), [])
        reads = []
        real_read = os.read

        def read(fd, count):
            reads.append(count)
            return real_read(fd, count)
        with mock.patch.object(store_check, "CHUNK", 8), mock.patch.object(store_check.os, "read", read):
            self.assertEqual(run(), [])
        self.assertEqual(reads, [1, 8, 8, 8, 1, 2, 1])  # empty; three whole chunks + EOF byte; one byte
        self.assertTrue(all(type(count) is int for count in reads))
        for wrong in (len(exact) + 1, 10 ** 30):
            with self.subTest(expected=wrong), mock.patch.object(store_check, "_sha256", wraps=store_check._sha256) \
                    as hashing:
                self.assertEqual(run(**{long: wrong}), [{"sha256": long, "problem": "size_mismatch",
                                                         "expected": wrong, "actual": len(exact)}])
                self.assertEqual(hashing.call_count, 2)  # the two other objects only
        for equivalent in (True, 1.0):
            with self.subTest(equivalent=repr(equivalent)), \
                    mock.patch.object(store_check, "_sha256", wraps=store_check._sha256) as hashing:
                self.assertEqual(run(**{one: equivalent}), [bad(one)])
                self.assertEqual(hashing.call_count, 2)

    def test_invalid_hash_and_existing_findings_stay_compatible(self):
        clean, short, mismatch = self.put(FIRST), self.put(SECOND), self.put(b"synthetic third")
        (self.objects / mismatch).write_bytes(b"synthetic THIRD")
        missing = sha(b"missing")
        with mock.patch.object(store_check.os, "lstat", wraps=os.lstat) as lstat:
            result = self.check([("../../campaign.json", CANARY), (None, -1), (clean, len(FIRST)),
                                 (short, len(SECOND) - 1), (mismatch, len(b"synthetic third")),
                                 (missing, 4)])
        self.assertEqual(result["problems"][:2], [{"sha256": "../../campaign.json", "problem": "invalid_ledger_row"},
                                                  {"sha256": "None", "problem": "invalid_ledger_row"}])
        self.assertEqual(result["problems"][2:], [
            {"sha256": short, "problem": "size_mismatch", "expected": len(SECOND) - 1, "actual": len(SECOND)},
            {"sha256": mismatch, "problem": "hash_mismatch"},
            {"sha256": missing, "problem": "missing_file"}])
        self.assertNotIn("../../campaign.json", [call.args[0].name for call in lstat.call_args_list])


class PreservationTests(Base):
    def test_malformed_count_checks_preserve_campaign_and_outside_sentinel(self):
        self.campaign()
        sentinel = self.base / "outside-sentinel"
        sentinel.write_bytes(FIRST)
        os.utime(sentinel, ns=(10 ** 9, 10 ** 9))
        digest = sha(b"linked to outside")
        os.symlink(sentinel, self.objects / digest)
        self.set_count(SECOND, CANARY)
        db = sqlite3.connect(self.ledger)
        with db:
            db.execute("INSERT INTO objects VALUES (?, ?, ?)", (digest, b"\x01", "t"))
        db.close()
        outside = (sentinel.read_bytes(), os.lstat(sentinel).st_mtime_ns, os.lstat(sentinel).st_mode)
        before = self.snapshot()
        api = store_check.check_store(self.root)
        self.assertEqual(api["problems"][:2], [bad(d) for d in sorted((sha(SECOND), digest))])
        self.assertEqual(self.snapshot(), before)
        result = self.cli("status", "--check", code=3)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((sentinel.read_bytes(), os.lstat(sentinel).st_mtime_ns, os.lstat(sentinel).st_mode), outside)
        self.assertEqual(sorted(path.name for path in self.objects.iterdir()),
                         sorted([sha(FIRST), sha(SECOND), digest]))


if __name__ == "__main__":
    unittest.main()
