"""Two actual offline campaign journeys, with outbound connections forbidden."""
import socket
import unittest
from unittest.mock import patch
from tools.rehearse_localities import rehearse


class LocalityRehearsalTests(unittest.TestCase):
    def test_two_isolated_localities_preserve_bytes_and_readiness_holds(self):
        with patch.object(socket, "create_connection", side_effect=AssertionError("offline only")):
            report = rehearse()
        self.assertEqual([r["county"] for r in report["localities"]],
                         ["San Luis Obispo", "Alameda"])
        self.assertFalse(report["live_pilot_completed"])
        for locality in report["localities"]:
            self.assertEqual(locality["delivery_receipts"], 2)
            self.assertFalse(locality["production_ready"])
            self.assertFalse(locality["jurisdiction_verified"])
