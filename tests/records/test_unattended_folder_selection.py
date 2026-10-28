"""Synthetic regression for explicit empty IMAP folder selection."""

import unittest
from unittest.mock import patch

from tests.records import test_run_safety as fixtures


class FolderSelectionTests(fixtures.SafetyFixture, unittest.TestCase):
    def configured_intake(self, folders):
        # Reuse the canonical backend/ledger fixture with an initially empty
        # mailbox. No message or checkpoint has been attempted by this setup.
        _, intake, safety = fixtures.GuardedIntakeTests.intake(self, {})
        intake.config["folders"] = folders
        return intake, safety, fixtures.FakeIMAP({"Archive": {"uidvalidity": 17, "messages": {1: fixtures.synthetic_email()}}})

    def test_explicit_empty_folders_never_fetch_or_advance(self):
        intake, safety, client = self.configured_intake([])
        with patch.object(intake, "client_factory", return_value=client), \
                patch.object(client, "list", wraps=client.list) as visibility, \
                patch.object(client, "select", wraps=client.select) as select, \
                patch.object(client, "uid", wraps=client.uid) as uid, \
                patch.object(intake, "_checkpoint", wraps=intake._checkpoint) as checkpoint, \
                patch.object(intake, "_save", wraps=intake._save) as save:
            report = intake.run()
        visibility.assert_called_once()
        select.assert_not_called()
        uid.assert_not_called()
        checkpoint.assert_not_called()
        save.assert_not_called()
        self.assertEqual(report["folders"], [])
        self.assertEqual(report["attempted"], 0)
        self.assertEqual(report["deferred"], 0)
        self.assertFalse(report["limit_reached"])
        self.assertEqual(report["failures"], 0)

    def test_null_folders_preserve_all_available_folder_behavior(self):
        intake, safety, client = self.configured_intake(None)
        with patch.object(intake, "client_factory", return_value=client), \
                patch.object(client, "select", wraps=client.select) as select, \
                patch.object(client, "uid", wraps=client.uid) as uid:
            report = intake.run()
        select.assert_called_once()
        self.assertGreaterEqual(uid.call_count, 2)
        self.assertEqual(report["attempted"], 1)
        self.assertEqual(report["failures"], 0)


if __name__ == "__main__":
    unittest.main()
