"""Excessively nested automation metadata degrades to unknown; synthetic data only."""
import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from campaign_tool.records import console_automation as ca, pipeline_health
from tests.records.test_console_jobs import fake, timer, service
from tests.records.test_pipeline_health import NOW, report_for


def deep(kind):
    # The C scanner's limit is independent of sys.getrecursionlimit(); 100k exceeds it on 3.12-3.14.
    depth = max(sys.getrecursionlimit() + 500, 100000)
    text = ("[" * depth + "]" * depth) if kind == "array" else ('{"a":' * depth + "1" + "}" * depth)
    assert len(text) < pipeline_health.MAX_JSON_BYTES
    return text.encode()


class DeepMetadata(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "run-report.json"
        (self.root / "alerts").mkdir()
        self.alert = self.root / "alerts" / "health-disk_space-low_space.json"
        self.runner = fake({"records-run.timer": timer(), "records-run.service": service()})

    def good_report(self):
        self.path.write_text(json.dumps(report_for(NOW - timedelta(minutes=5))))

    def good_alarm(self):
        self.alert.write_text(json.dumps({"schema": pipeline_health.ALERT_SCHEMA, "state": "warn",
                                          "check": "disk_space", "reason_code": "low_space"}))

    def model(self):
        return ca.load(self.path, units=["records-run.timer"], runner=self.runner, now=NOW)

    def test_deep_run_report_is_unknown(self):
        for kind in ("array", "object"):
            self.path.write_bytes(deep(kind))
            rep = ca.report(self.path, NOW)
            self.assertEqual((rep["state"], rep["code"], rep["data"]),
                             ("unknown", "run_report_missing_invalid_or_future", None))

    def test_deep_alarm_is_unknown(self):
        for kind in ("array", "object"):
            self.alert.write_bytes(deep(kind))
            alarm = ca.alarms(self.path)
            self.assertEqual((alarm["state"], alarm["rows"]), ("unknown", []))

    def test_page_survives_one_corrupt_source(self):
        self.good_report()
        self.alert.write_bytes(deep("array"))
        model = self.model()
        self.assertEqual((model["report"]["state"], model["alarms"]["state"]), ("reported", "unknown"))
        self.assertIn("UNKNOWN (missing or unreadable)", ca.render(model))
        self.path.write_bytes(deep("object"))
        self.good_alarm()
        model = self.model()
        self.assertEqual((model["report"]["state"], model["alarms"]["state"]), ("unknown", "read"))
        page = ca.render(model)
        self.assertIn("Run report unavailable", page)
        self.assertIn("low_space", page)

    def test_normal_metadata_unchanged(self):
        self.good_report()
        self.good_alarm()
        model = self.model()
        self.assertEqual((model["report"]["state"], model["alarms"]["rows"]),
                         ("reported", [("disk_space", "low_space", "warn")]))
        self.path.write_bytes(b"{" + b" " * pipeline_health.MAX_JSON_BYTES + b"}")
        self.assertEqual(ca.report(self.path, NOW)["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
