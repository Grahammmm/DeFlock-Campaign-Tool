"""A failed systemctl show must never yield an ok/RUNNING row from its stdout. Fake runners only."""
import unittest

from campaign_tool.records import console_automation as ca, console_jobs as cj
from tests.records.test_console_jobs import service, timer

CANARY = "CANARY-RAW-OUTPUT"


def runner_for(units, codes=None):
    """units: name -> props dict (absent = not-found). codes: name -> returncode (default 0)."""
    calls = []
    codes = codes or {}

    def runner(args):
        calls.append(list(args))
        assert args[0] == "show", args
        props = units.get(args[1])
        code = codes.get(args[1], 0)
        if props is None:
            return code, "LoadState=not-found\n"
        return code, "".join("%s=%s\n" % kv for kv in props.items()) + "Description=" + CANARY + "\n"
    runner.calls = calls
    return runner


HEALTHY = {"a.timer": timer(Unit="a.service"), "a.service": service()}


class FailedQuery(unittest.TestCase):
    def test_healthy_control_is_ok(self):
        self.assertEqual(cj.inspect("a.timer", runner_for(HEALTHY))["state"], "ok")

    def test_failed_timer_show_cannot_report_ok(self):
        run = runner_for(HEALTHY, {"a.timer": 1})
        row = cj.inspect("a.timer", run)
        self.assertEqual(row["state"], "unknown")
        self.assertEqual([c[1] for c in run.calls], ["a.timer"])

    def test_failed_service_show_cannot_report_ok(self):
        row = cj.inspect("a.timer", runner_for(HEALTHY, {"a.service": 3}))
        self.assertEqual(row["state"], "unknown")

    def test_failed_show_status_matrix(self):
        for bad in (1, -1, 127, None, "0", 0.0, True, False):
            with self.subTest(status=bad):
                self.assertIsNone(cj._show(lambda a, b=bad: (b, "LoadState=loaded\n"), "a.timer", ("LoadState",)))
        self.assertEqual(cj._show(lambda a: (0, "LoadState=loaded\n"), "a.timer", ("LoadState",)),
                         {"LoadState": "loaded"})

    def test_nonzero_not_found_is_unknown(self):
        row = cj.inspect("x.timer", runner_for({}, {"x.timer": 4}))
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(cj.inspect("x.timer", runner_for({}))["state"], "not_installed")

    def test_both_read_only_views_keep_failed_query_unknown(self):
        run = runner_for({"a.timer": timer(Unit="a.service"), "a.service": service(ActiveState="active")},
                         dict.fromkeys(("a.timer", "a.service", *ca.SERVICES), 1))
        html = cj.render(["a.timer"], run)
        self.assertNotIn(CANARY, html)
        self.assertNotIn(">Passed", html)
        model = ca.load(None, units=["a.timer"], runner=run)
        self.assertEqual(model["timers"][0]["state"], "unknown")
        self.assertTrue(all(s["state"] == "UNKNOWN" for s in model["services"]))
        self.assertNotIn(CANARY, repr(model))
        self.assertTrue(all(c[0] == "show" for c in run.calls))
        self.assertEqual(ca._service(ca.SERVICES[0], run)["state"], "UNKNOWN")
        ok = ca._service(ca.SERVICES[0], runner_for({ca.SERVICES[0]: service()}))
        self.assertEqual(ok["state"], "INACTIVE")


if __name__ == "__main__":
    unittest.main()
