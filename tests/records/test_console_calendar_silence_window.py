"""RF0824-1: future follow-up reminders stay on the calendar while a passed source date is inside the silence window."""
import copy
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from campaign_tool.records import console_calendar as cc
from campaign_tool.records import console_home
from campaign_tool.records import followup_clock as fc
from tests.records.test_console_calendar import NOW, RUN, TODAY, build, clock, put, register, row, stub

SOURCE = "2026-11-03"


def settings(silence, **per_agency):
    base = {"autonomy": "draft", "notice_after_days": 10, "silence_after_days": silence}
    return {"invalid": False, "default": dict(base), "agencies": {k: {**base, **v} for k, v in per_agency.items()}}


def det(state="past_due_reminder", request="D", agency="alpha-pd", due=SOURCE, **kw):
    return row(agency, request, clock(state=state, due=due), **kw)


def prom(state="passed", request="P", agency="alpha-pd", date=SOURCE, **kw):
    return row(agency, request, clock(state="determined", determination_at="2026-11-01"),
               promised={"state": state, "date": date}, **kw)


def projected(rows, cfg, queue=()):
    items, notes = cc.build(stub(register(rows), list(queue), cfg), TODAY)
    return sorted((x["request"], x["date"].isoformat(), x["label"].split(" ")[0]) for x in items
                  if x["kind"] == "followup_draft_due"), notes


class SilenceWindow(unittest.TestCase):
    def test_passed_determination_wait_stays_projected(self):
        r, cfg = det(), settings(5)
        self.assertEqual(fc.plan(register([r]), cfg, [], as_of=TODAY.isoformat()), [])
        got, notes = projected([r], cfg)
        self.assertEqual(got, [("D", "2026-11-09", "reminder-no-response")])
        self.assertEqual([c["kind"] for c in fc.plan(register([r]), cfg, [], as_of="2026-11-09")], ["reminder-no-response"])
        self.assertNotIn("no follow-up projected", " ".join(notes))

    def test_passed_promise_wait_stays_projected(self):
        r, cfg = prom(), settings(5)
        self.assertEqual(fc.plan(register([r]), cfg, [], as_of=TODAY.isoformat()), [])
        got, notes = projected([r], cfg)
        self.assertEqual(got, [("P", "2026-11-09", "reminder-promised-date")])
        self.assertNotIn("no follow-up projected", " ".join(notes))

    def test_projection_survives_source_day_transition(self):
        for n in (0, 1, 5, 30):
            for shift, stage in ((-5, "before"), (-1, "before"), (0, "source"), (1, "after"), (3, "after")):
                src = (TODAY + timedelta(days=shift)).isoformat()
                due = (TODAY + timedelta(days=shift + 1 + n))
                for r, label, upcoming in (
                        (det("running" if shift >= 0 else "past_due_reminder", due=src), "reminder-no-response", None),
                        (prom("upcoming" if shift >= 0 else "passed", date=src), "reminder-promised-date", None)):
                    with self.subTest(n=n, shift=shift, kind=label):
                        got, _ = projected([r], settings(n))
                        if due > TODAY:
                            self.assertEqual(got, [(r["request_id"], due.isoformat(), label)])
                        else:
                            self.assertLessEqual(len(got), 1)
                            self.assertTrue(all(d == TODAY.isoformat() for _, d, _ in got))

    def test_agency_overrides_and_two_clocks_are_isolated(self):
        both = row("alpha-pd", "B", clock(state="past_due_reminder", due="2026-11-02"),
                   promised={"state": "passed", "date": SOURCE})
        other = det(request="B", agency="beta-so", name="BETA-SO")
        cfg = settings(5, **{"beta-so": {"silence_after_days": 10}})
        got, _ = projected([both, other], cfg)
        # determination 11-02 + 6 = 11-08; promise 11-03 + 6 = 11-09; beta-so 11-03 + 11 = 11-14
        self.assertEqual(got, [("B", "2026-11-08", "reminder-no-response"), ("B", "2026-11-09", "reminder-promised-date"),
                               ("B", "2026-11-14", "reminder-no-response")])

    def test_future_wait_projection_retains_refusals_and_queue_semantics(self):
        cfg = settings(5)
        closed = {**det(request="C"), "status": "closed"}
        issues = {**det(request="I"), "issues": ["determination_date_inconsistent"]}
        off = det(request="O", agency="gamma-pd", name="GAMMA")
        cfg["agencies"]["gamma-pd"] = {**cfg["default"], "autonomy": "off"}
        determined = row("alpha-pd", "X", clock(state="determined", determination_at="2026-11-01"))
        unknown = row("alpha-pd", "U", clock(state="unknown"))
        satisfied = prom(state="satisfied", request="S")
        malformed = det(request="M", due="not-a-date")
        got, notes = projected([closed, issues, off, determined, unknown, satisfied, malformed], cfg)
        self.assertEqual(got, [])
        self.assertIn("no follow-up projected for 6 requests", " ".join(notes))
        self.assertIn("follow-up drafting is off for this agency: GAMMA", " ".join(notes))
        ident = fc.item_id("alpha-pd", "D", "determination", SOURCE)
        q = {"id": "q", "item_id": ident, "kind": "reminder-no-response", "agency_id": "alpha-pd", "request_id": "D",
             "status": "pending", "updated_at": "2026-11-05T00:00:00Z"}
        got = [x for x in cc.build(stub(register([det()]), [q], cfg), TODAY)[0] if x["kind"] == "followup_draft_due"]
        self.assertEqual([(x["date"].isoformat(), "awaiting your approval (Home)" in x["label"]) for x in got],
                         [("2026-11-09", True)])

    def test_overflow_isolated_from_release_link_and_model_unchanged(self):
        from tests.records.test_console_calendar import link
        bad = det(due="9999-12-31")
        model = stub(register([bad]), [], settings(5))
        model["links"] = {"state": "ok", "doc": {"links": [link("2026-11-08")]}}
        before = copy.deepcopy(model)
        items, _ = cc.build(model, TODAY)
        self.assertEqual([x["kind"] for x in items if x["kind"] != "followup_draft_due"], ["link_expiry"])
        self.assertEqual(model, before)

    def test_wait_projection_reaches_month_ics_and_home_readonly(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            build(project, [det(), prom()])
            put(project / fc.SETTINGS_FILE, {"schema": fc.SETTINGS_SCHEMA,
                                             "agencies": {"alpha-pd": {"autonomy": "draft", "silence_after_days": 5}}})
            files = {str(p): (p.read_bytes(), p.stat().st_mode) for p in project.rglob("*")
                     if p.is_file() and not p.name.endswith(".lock")}
            model = cc.load(str(project), RUN, NOW)
            items = [e for e in cc.entries(model, TODAY) if e["kind"] == "followup_draft_due"]
            self.assertEqual(sorted((e["request"], e["date"].isoformat()) for e in items),
                             [("D", "2026-11-09"), ("P", "2026-11-09")])
            self.assertEqual(cc.to_ics(items, NOW).count("DTSTART;VALUE=DATE:20261109"), 2)
            page = cc.render(model, TODAY, TODAY.replace(day=1))
            self.assertIn("reminder-no-response", page)
            self.assertIn("reminder-promised-date", page)
            home = console_home.load(project, RUN, now=NOW)
            week = [(e["request"], e["date"].isoformat()) for e in home["calendar"]["rows"]
                    if e["kind"] == "followup_draft_due"]
            self.assertEqual(sorted(week), [("D", "2026-11-09"), ("P", "2026-11-09")])
            # horizon boundary: the same entries drop out when the horizon ends before the reminder
            short = [e for e in cc.entries(model, TODAY, horizon_days=3) if e["kind"] == "followup_draft_due"]
            self.assertEqual(short, [])
            after = {str(p): (p.read_bytes(), p.stat().st_mode) for p in project.rglob("*")
                     if p.is_file() and not p.name.endswith(".lock")}
            self.assertEqual(files, after)


if __name__ == "__main__":
    unittest.main()
