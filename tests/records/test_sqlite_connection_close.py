"""Ledger initialization and the legacy intake bridge close their connections.

A sqlite3.Connection used as a context manager only commits or rolls back; it is
not closed. Left open, it lingers until garbage collection, where Python 3.13+
reports it as a ResourceWarning inside whichever later test happens to trigger
the collector (FA65's silent negative-control check on main, 2026-10-29).
"""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from campaign_tool.records.ledger import store
from campaign_tool.records.runner import adapters
from tests.records import test_runner

REAL_CONNECT = sqlite3.connect


def connections_opened_by(module):
    """Patch sqlite3.connect and record connections opened directly by module."""
    opened = []

    def connect(*args, **kwargs):
        connection = REAL_CONNECT(*args, **kwargs)
        if sys._getframe(1).f_globals.get("__name__") == module.__name__:
            opened.append(connection)
        return connection

    return opened, mock.patch.object(sqlite3, "connect", connect)


class ConnectionsClosed(unittest.TestCase):
    def assert_all_closed(self, opened, minimum):
        self.assertGreaterEqual(len(opened), minimum)
        for connection in opened:
            with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
                connection.execute("SELECT 1")

    def test_initialize_closes_every_connection_and_keeps_wal(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "private" / "ledger.sqlite"
            opened, patch = connections_opened_by(store)
            with patch:
                store.initialize(path)
            self.assert_all_closed(opened, 2)
            with closing(sqlite3.connect(path)) as check:
                self.assertEqual(check.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_legacy_intake_preserve_closes_its_connection(self):
        fixture = test_runner.RunnerTests("test_backend_promotion_gap_explicit")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        opened, patch = connections_opened_by(adapters)
        with patch:
            result = fixture.invoke(backend=adapters.LegacyIntakeBackend(fixture.delta.root, fixture.delta.out))
        self.assertEqual(result["messages_preserved"], 1)
        self.assert_all_closed(opened, 1)


if __name__ == "__main__":
    unittest.main()
