"""`status --list` refuses SQLite query/decoder failures with the fixed message only (synthetic campaign only).

A non-STRICT TEXT column can hold bytes that are not UTF-8. sqlite3 then raises an OperationalError whose
message quotes the stored value, so the listing must translate it rather than echo private receipt text.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import ledger_list
from campaign_tool.cli import database
from campaign_tool.sources_cmd import add_batch

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
FIXED = "private/ledger.sqlite is not a readable ledger"
STOPPED = "Stopped: " + FIXED + "\n"
CANARY = "SYNTHETIC_PRIVATE_CANARY_8"
BAD = CANARY.encode() + b"\xff"
WHEN = "2026-10-20T09:00:00-07:00"
AGENCY = "ca-cedar-police"
COLUMNS = ("source_id", "original_name", "received_at")


def digest(label):
    return hashlib.sha256(f"synthetic RF1530-8 record {label}; not a real document\n".encode()).hexdigest()


def cli(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=120)


def tree(root):
    """Every path under root with bytes, mode and mtime: refusal must leave all of it unchanged."""
    state = {}
    for path in sorted(Path(root).rglob("*")):
        info = os.lstat(path)
        state[str(path.relative_to(root))] = (path.read_bytes() if path.is_file() else None,
                                              info.st_mode, info.st_mtime_ns)
    return state


class QueryErrorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-list-query-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.ledger = self.root / "private" / "ledger.sqlite"

    def receive(self, rows):
        """rows: (label, source_id, received_at, original_name); a bytes value is stored as invalid TEXT."""
        db = database(self.root)
        with db:
            for number, (label, source, received, name) in enumerate(rows):
                db.execute("INSERT OR IGNORE INTO objects VALUES (?, 1, ?)", (digest(label), WHEN))
                values = [f"r-{label}-{number}", digest(label), source, received, name]
                marks = ["CAST(? AS TEXT)" if isinstance(v, bytes) else "?" for v in values]
                db.execute(f"INSERT INTO receipts VALUES ({', '.join(marks)})", values)
        db.close()

    def healthy(self, count=2, source="src-a"):
        return [(f"h{n}", source, f"2026-10-20T16:{n:02d}:00+00:00", f"Healthy {n}.pdf") for n in range(count)]

    def bad_row(self, column, label="bad", source="src-a"):
        row = {"source_id": source, "received_at": "2026-10-20T17:00:00+00:00", "original_name": "Bad.pdf"}
        row[column] = BAD
        return (label, row["source_id"], row["received_at"], row["original_name"])

    def assert_stored_invalid_text(self, column):
        db = sqlite3.connect(self.ledger)
        try:
            kind, raw = db.execute(f"SELECT typeof({column}), CAST({column} AS BLOB) FROM receipts "
                                   f"WHERE object_sha256 = ?", (digest("bad"),)).fetchone()
        finally:
            db.close()
        self.assertEqual((kind, raw), ("text", BAD))

    def assert_fixed_refusal(self, **arguments):
        with self.assertRaises(ValueError) as caught:
            ledger_list.list_receipts(self.root, **arguments)
        self.assertEqual(str(caught.exception), FIXED)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def assert_cli_refusal(self, *extra):
        result = cli("status", "--directory", str(self.root), "--list", *extra)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (1, "", STOPPED))

    def test_invalid_utf8_text_refuses_without_private_value(self):
        for column in COLUMNS:
            with self.subTest(column):
                self.ledger.unlink(missing_ok=True)
                self.receive(self.healthy() + [self.bad_row(column)])
                self.assert_stored_invalid_text(column)
                with self.assertRaises(sqlite3.OperationalError) as raw:  # the disclosure being contained
                    db = sqlite3.connect(self.ledger)
                    try:
                        db.execute(f"SELECT {column} FROM receipts").fetchall()
                    finally:
                        db.close()
                self.assertIn(CANARY, str(raw.exception))
                self.assert_fixed_refusal()

    def test_actual_cli_has_only_fixed_stopped_line(self):
        for column in COLUMNS:
            with self.subTest(column):
                self.ledger.unlink(missing_ok=True)
                self.receive(self.healthy() + [self.bad_row(column)])
                self.assert_cli_refusal()
        self.ledger.unlink()
        name = "Cédula ñandú 記録 \u2603.pdf"
        self.receive([("u", "src-ü", "2026-10-20T16:00:00+00:00", name)])
        result = cli("status", "--directory", str(self.root), "--list")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual([(r["source_id"], r["original_name"]) for r in report["receipts"]], [("src-ü", name)])

    def test_execute_and_fetch_errors_close_snapshot(self):
        real = ledger_list._snapshot
        opened = []

        class Spy:
            def __init__(self, db):
                self.db, self.closed = db, 0

            def execute(self, *args):
                if self.closed:
                    raise AssertionError("snapshot reused after close")
                return self.db.execute(*args)

            def close(self):
                self.closed += 1
                self.db.close()

        def spying(path):
            opened.append(Spy(real(path)))
            return opened[-1]

        self.receive(self.healthy() + [self.bad_row("original_name")])
        with mock.patch.object(ledger_list, "_snapshot", spying):
            self.assert_fixed_refusal()  # materialization (fetchall) failure
        db = sqlite3.connect(self.ledger)
        db.execute("ALTER TABLE receipts DROP COLUMN original_name")
        db.commit()
        db.close()
        with mock.patch.object(ledger_list, "_snapshot", spying):
            self.assert_fixed_refusal()  # execution failure: no such column
        with mock.patch.object(ledger_list, "_snapshot", lambda path: opened.append(Spy(mock.Mock(
                execute=mock.Mock(side_effect=RuntimeError("unexpected"))))) or opened[-1]):
            with self.assertRaisesRegex(RuntimeError, "unexpected"):
                ledger_list.list_receipts(self.root)
        self.assertEqual([spy.closed for spy in opened], [1, 1, 1])

    def test_selected_bad_text_cannot_hide_behind_registration_or_cap(self):
        self.receive(self.healthy(3) + [self.bad_row("original_name")])
        with mock.patch.object(ledger_list, "_registered", return_value={digest("bad")}):
            self.assert_fixed_refusal(not_in_sources=True)
        with mock.patch.object(ledger_list, "MAX_ROWS", 1):
            self.assert_fixed_refusal()

    def test_sql_excluded_bad_row_keeps_healthy_selection(self):
        self.receive([("a", "src-a", "2026-10-20T16:00:00+00:00", "Policy A.pdf"),
                      ("b", "src-b", "2026-10-01T16:00:00+00:00", BAD)])
        for arguments in ({"source_id": "src-a"}, {"since": "2026-10-10"}):
            with self.subTest(**arguments):
                report = ledger_list.list_receipts(self.root, **arguments)
                self.assertEqual(report, {"receipts": [{"sha256_prefix": digest("a")[:12], "bytes": 1,
                                                        "source_id": "src-a", "original_name": "Policy A.pdf",
                                                        "received_at": "2026-10-20T16:00:00+00:00"}], "count": 1})
        self.assert_fixed_refusal()
        self.assertEqual(ledger_list.list_receipts(self.root, source_id="no-such"), {"receipts": [], "count": 0})
        self.ledger.unlink()
        self.assertEqual(ledger_list.list_receipts(self.root), {"receipts": [], "count": 0})
        database(self.root).close()
        self.assertEqual(ledger_list.list_receipts(self.root), {"receipts": [], "count": 0})

    def test_source_batch_refusal_precedes_every_write(self):
        self.receive(self.healthy(2, "prod-2026-10") + [self.bad_row("original_name", source="prod-2026-10")])
        before = tree(self.root)
        with mock.patch("campaign_tool.sources_cmd._full_hashes") as full, \
                mock.patch("campaign_tool.sources_cmd._batch_write") as write:
            with self.assertRaises(ValueError) as caught:
                add_batch(self.root, "prod-2026-10", AGENCY, WHEN)
        self.assertEqual(str(caught.exception), FIXED)
        full.assert_not_called()
        write.assert_not_called()
        for extra in ((), ("--dry-run",)):
            with self.subTest(extra=extra):
                result = cli("sources", "add-batch", "--directory", str(self.root), "--source-id", "prod-2026-10",
                             "--agency-id", AGENCY, "--received-at", WHEN, *extra)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (1, "", STOPPED))
        self.assertEqual(tree(self.root), before)

    def test_failure_then_healthy_retry_preserves_disk_and_resources(self):
        self.receive(self.healthy())
        good = self.ledger.read_bytes()
        self.receive([self.bad_row("source_id", label="late")])
        before = tree(self.root)
        for _ in range(3):
            self.assert_fixed_refusal()
        self.assert_cli_refusal()
        self.assertEqual(tree(self.root), before)
        self.assertEqual([p.name for p in self.ledger.parent.iterdir() if p.name.startswith("ledger")],
                         ["ledger.sqlite"])
        self.ledger.write_bytes(good)  # the test, not production code, restores the healthy fixture
        self.assertEqual(ledger_list.list_receipts(self.root)["count"], 2)

    def test_unicode_and_earlier_refusals_stay_compatible(self):
        self.receive([("u", "src-a", "2026-10-20T16:00:00+00:00", "Ünïcødé\x07\nname.pdf"),
                      ("u", "src-a", "2026-10-20T16:01:00+00:00", "x" * 300),
                      ("z", "src-a", "2026-10-20T16:02:00+00:00", ""),
                      self.bad_row("original_name")])
        # Earlier flag/date/library/sidecar/size refusals still win over the query failure.
        for arguments, message in (({"source_id": ""}, "--source-id: use 1-200 characters"),
                                   ({"since": "yesterday"}, ledger_list.SINCE_ERROR)):
            with self.assertRaises(ValueError) as caught:
                ledger_list.list_receipts(self.root, **arguments)
            self.assertEqual(str(caught.exception), message)
        sidecar = Path(f"{self.ledger}-wal")
        sidecar.write_bytes(b"")
        with self.assertRaises(ValueError) as caught:
            ledger_list.list_receipts(self.root)
        self.assertEqual(str(caught.exception), ledger_list.SIDECAR_ERROR)
        sidecar.unlink()
        with mock.patch.object(ledger_list, "MAX_LEDGER_BYTES", 1):
            with self.assertRaises(ValueError) as caught:
                ledger_list.list_receipts(self.root)
        self.assertEqual(str(caught.exception), ledger_list.LEDGER_SIZE_ERROR)
        # Without the malformed row, Unicode/control/repeated/long/empty names list exactly as before.
        db = sqlite3.connect(self.ledger)
        db.execute("DELETE FROM receipts WHERE object_sha256 = ?", (digest("bad"),))
        db.commit()
        db.close()
        report = ledger_list.list_receipts(self.root)
        self.assertEqual([r["original_name"] for r in report["receipts"]],
                         ["Ünïcødé??name.pdf", "x" * 200, ""])
        self.assertEqual([r["sha256_prefix"] for r in report["receipts"]],
                         [digest("u")[:12], digest("u")[:12], digest("z")[:12]])
        self.assertEqual(report["count"], 3)


if __name__ == "__main__":
    unittest.main()
