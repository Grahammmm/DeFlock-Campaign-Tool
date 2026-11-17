"""RF1831-4: social --check windows stay inside the supported calendar (synthetic only)."""
import json
import socket
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from campaign_tool import social_check as SC
from campaign_tool.social import build_event_social, build_social
from tests.test_social_check_folders import NOW, FolderCheckCase, outcome
from tests.test_social_events import event

UTC = timezone.utc
MAX_NOW = datetime(9999, 12, 30, tzinfo=UTC)


def meeting(starts, on=None, status="scheduled", participation=True):
    return {"status": status, "starts_at": starts, "participation": {} if participation else None,
            "outcome": None if on is None else {"on": on}}


def ctx(meetings=None, now=NOW, events=None):
    return SimpleNamespace(meetings=lambda: (meetings or {}, now), events=lambda: events or {}, event_now=now)


class Helpers(unittest.TestCase):
    def test_meeting_and_event_eligibility_preserve_inclusive_instants(self):
        top = datetime(9999, 12, 31, tzinfo=UTC)
        for window in (1, 14, 60):
            for now in (NOW, top - timedelta(days=window), top - timedelta(days=window) + timedelta(hours=1), top):
                for delta in (timedelta(0), timedelta(days=window), timedelta(days=window, seconds=1),
                              timedelta(seconds=-1)):
                    try:
                        stamp = (now + delta).isoformat()
                    except OverflowError:
                        continue
                    want = timedelta(0) <= delta <= timedelta(days=window)
                    got = SC._eligible_meetings(ctx({"a": meeting(stamp)}, now), window)
                    self.assertEqual(list(got), ["a"] if want else [], (now, window, delta))
                    got = SC._eligible_events(ctx(events={"a": {"starts_at": stamp}}, now=now), window)
                    self.assertEqual(list(got), ["a"] if want else [], (now, window, delta))
        self.assertEqual(list(SC._eligible_events(ctx(events={"a": {"starts_at": "2026-11-10T04:00:00-08:00"}}), 1)), ["a"])
        self.assertEqual(SC._eligible_events(ctx(events={"a": {"starts_at": "2026-11-11T04:00:01-08:00"}}), 1), {})

    def test_outcome_windows_at_calendar_min_use_day_ages(self):
        for now in (datetime(1, 1, 1, tzinfo=UTC), datetime(1, 3, 1, 9, tzinfo=UTC), NOW, MAX_NOW):
            for window in (1, 14, 60):
                for age in (-1, 0, window - 1, window):
                    try:
                        day = now.date() - timedelta(days=age)
                    except OverflowError:
                        continue
                    want = 0 <= age < window
                    m = {"a": meeting("2030-01-01T00:00:00Z", on=day.isoformat())}
                    self.assertEqual(bool(SC._eligible_outcomes(ctx(m, now), window)), want, (now, window, age))
                    meta = {"within_days": window, "outcome": m["a"]["outcome"]}
                    self.assertEqual(SC._stale_outcome(ctx(m, now), "a", meta) != [], not want, (now, window, age))
                    self.assertEqual(SC._stale_outcome(ctx(m, now), "a", None) != [], not 0 <= age < 14, (now, window, age))

    def test_stale_event_own_window_and_unknown_default_survive_endpoint(self):
        late = {"a": {"starts_at": "9999-12-31T23:59:59Z"}}
        top = datetime(9999, 12, 31, tzinfo=UTC)
        meta = {"starts_at": "9999-12-31T23:59:59Z", "within_days": 1}
        self.assertEqual(SC._stale_event(ctx(events=late, now=top), "a", meta), [])
        self.assertEqual(SC._stale_event(ctx(events=late, now=top), "a", None), [])
        self.assertEqual(SC._stale_event(ctx(events=late, now=MAX_NOW), "a", meta), ["event left its 1-day window"])
        self.assertEqual(SC._stale_event(ctx(events=late, now=MAX_NOW - timedelta(days=30)), "a", None),
                         ["event is outside the default 14-day window and its original window is unknown; rebuild with social --events"])
        self.assertEqual(SC._stale_event(ctx(events=late, now=top), "a", dict(meta, starts_at="9999-12-30T00:00:00Z")),
                         ["rescheduled (was 9999-12-30T00:00:00Z, now 9999-12-31T23:59:59Z)"])


class Folders(FolderCheckCase):
    def setUp(self):
        super().setUp()
        (self.root / "content/events.json").write_text(json.dumps([event("one", "9999-12-31T00:00:00Z", ends_at="9999-12-31T01:00:00Z")]), encoding="utf-8")
        build_social(self.root)
        (self.social / "events").mkdir()
        (self.social / "events/stray.md").write_text("unrelated organizer note", encoding="utf-8")

    def test_event_checker_at_calendar_max_keeps_in_range_event(self):
        folder = self.folder("events", MAX_NOW)
        self.assertEqual(([(d["slug"], d["state"]) for d in folder["drafts"]], folder["stale"], folder["problems"]),
                         ([("one", "missing")], ["stray"], 2))

    def test_real_cli_at_upper_boundary_returns_report_not_traceback(self):
        before = self.tree()
        from campaign_tool import cli, social
        import io, sys
        from contextlib import redirect_stderr, redirect_stdout
        real = social.check_social
        argv = ["campaign_tool", "social", "--directory", str(self.root), "--check"]
        out, err, code = io.StringIO(), io.StringIO(), 0
        with patch.object(sys, "argv", argv), patch.object(social, "check_social", lambda root, now=None: real(root, now=MAX_NOW)), \
                patch.object(socket, "socket", side_effect=AssertionError("network")), redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as stop:
                code = stop.code
        report = json.loads(out.getvalue())
        self.assertEqual((code, report["posted"], err.getvalue()), (3, False, ""))
        self.assertEqual(report["folders"]["kit/social/events"]["problems"], 2)
        self.assertEqual(self.tree(), before)

    def test_ordinary_clock_reports_are_identical(self):
        (self.root / "content/events.json").write_text(json.dumps([event("one"), event("far", "2026-12-20T00:00:00Z")]),
                                                       encoding="utf-8")
        (self.social / "events/stray.md").unlink()
        build_event_social(self.root, now=NOW)
        folder = self.folder("events")
        self.assertEqual(folder["problems"], 0)
        self.assertEqual(self.states("events"), {"one": ("ok", [])})
        later = self.folder("events", NOW + timedelta(days=1))
        self.assertEqual(later["stale"], [])
        self.assertEqual(self.states("events", NOW + timedelta(days=1)), {"one": ("ok", [])})
        self.assertEqual(self.folder("events", NOW + timedelta(days=3))["stale"], ["one"])


if __name__ == "__main__":
    unittest.main()
