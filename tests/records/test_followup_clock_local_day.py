"""RF1405-1: follow-up waits count from the reminder approval's Pacific day, the register's own calendar.

Synthetic data only: invented agencies, a throwaway test SSH key and temporary project roots. Nothing sends.
"""
import copy
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from campaign_tool.records import approval_queue as aq
from campaign_tool.records import console_calendar as cc
from campaign_tool.records import followup_clock as fc
from tests import approval_signing
from tests.records import test_console_calendar as calfx  # fixture helpers only; no TestCase is imported

RUN = "run_1"
APPROVED_AT = "2030-01-23T00:30:00Z"  # 2030-01-22 16:30 PST: after UTC midnight, before Pacific midnight
SETTINGS = {"invalid": False, "default": {"autonomy": "draft", "notice_after_days": 10, "silence_after_days": 0},
            "agencies": {}}
NOTICE_DAY = "2030-02-01"


def rows():
    return [calfx.row("alpha-pd", "D", calfx.clock(state="past_due_reminder", due="2030-01-10")),
            calfx.row("alpha-pd", "P", calfx.clock(state="determined", determination_at="2030-01-05", due="2030-01-10"),
                      promised={"state": "passed", "date": "2030-01-12"})]


def reg(as_of="2030-01-21", requests=None):
    return {"schema": "records-request-register-v1", "as_of": as_of, "requests": requests or rows(), "limits": []}


def kinds(found):
    return sorted((c["request_id"], c["kind"]) for c in found)


def approved_row(request="D", clock_kind="determination", clock_date="2030-01-10", at: object = APPROVED_AT,
                 status="approved"):
    reminder = fc.REMINDERS[0 if clock_kind == "determination" else 1]
    return {"id": "q-" + request, "item_id": fc.item_id("alpha-pd", request, clock_kind, clock_date), "kind": reminder,
            "agency_id": "alpha-pd", "request_id": request, "status": status, "updated_at": at}


class LocalDay(unittest.TestCase):
    def setUp(self):
        self.signer = approval_signing.Signer()
        self.addCleanup(self.signer.close)
        trust = self.signer.trust()
        trust.__enter__()
        self.addCleanup(trust.__exit__, None, None, None)

    def project(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def signed(self, project):
        """Queue both reminders the planner asks for, then sign each approval at APPROVED_AT."""
        register = reg()
        found = fc.plan(register, SETTINGS, [], as_of=register["as_of"])
        self.assertEqual(kinds(found), [("D", fc.REMINDERS[0]), ("P", fc.REMINDERS[1])])
        with mock.patch.object(aq, "_now", return_value="2030-01-21T20:00:00Z"):
            for candidate in found:
                body, evidence = fc._render(candidate, register, RUN)
                aq.add_draft(project, item_id=candidate["item_id"], kind=candidate["kind"], agency_id="alpha-pd",
                             request_id=candidate["request_id"], body=body, evidence=evidence, source_run_id=RUN)
        with mock.patch.object(aq, "_now", return_value=APPROVED_AT):
            for row in aq.load_queue(project):
                signature = self.signer.sign(aq.approval_payload(project, row["id"]))
                aq.approve(project, row["id"], {"kind": "ssh", "signature": signature})
        queue = aq.load_queue(project)
        self.assertEqual([(r["status"], r["updated_at"]) for r in queue], [("approved", APPROVED_AT)] * 2)
        return queue

    def test_signed_utc_evening_approval_uses_register_day(self):
        queue = self.signed(self.project())
        register = reg()
        self.assertEqual(fc._day(APPROVED_AT), date(2030, 1, 22))
        self.assertEqual([e["notice_due"] for e in fc.clock_table(register, SETTINGS, queue, as_of="2030-01-25")],
                         [NOTICE_DAY, NOTICE_DAY])
        self.assertEqual(kinds(fc.plan(register, SETTINGS, queue, as_of="2030-01-31")),
                         [("D", fc.REMINDERS[0]), ("P", fc.REMINDERS[1])])
        found = fc.plan(register, SETTINGS, queue, as_of=NOTICE_DAY)
        self.assertEqual(kinds(found), [("D", fc.NOTICE), ("D", fc.REMINDERS[0]), ("P", fc.NOTICE), ("P", fc.REMINDERS[1])])
        self.assertEqual({c["reminder_date"] for c in found}, {"2030-01-22"})
        for notice in (c for c in found if c["kind"] == fc.NOTICE):
            body, _ = fc._render(notice, reg(NOTICE_DAY), RUN)
            self.assertIn("This follows our reminder of 2030-01-22.", body)
            self.assertNotIn("2030-01-23", body)

    def test_equivalent_instants_and_dst_have_one_notice_day(self):
        cases = {  # literal Pacific days, written out by hand
            "2030-01-23T00:30:00Z": "2030-01-22", "2030-01-23T00:30:00+00:00": "2030-01-22",
            "2030-01-22T16:30:00-08:00": "2030-01-22", "2030-01-23T09:30:00+09:00": "2030-01-22",
            "2030-01-23T07:59:59Z": "2030-01-22", "2030-01-23T08:00:00Z": "2030-01-23",
            "2030-01-23T00:00:00.5-08:00": "2030-01-23",
            "2030-07-15T06:59:59Z": "2030-07-14", "2030-07-15T07:00:00Z": "2030-07-15",  # summer, PDT
            "2030-03-10T07:59:59Z": "2030-03-09", "2030-03-10T08:00:00Z": "2030-03-10",  # spring-forward day
            "2030-03-10T10:00:00Z": "2030-03-10", "2030-03-11T06:59:59Z": "2030-03-10", "2030-03-11T07:00:00Z": "2030-03-11",
            "2030-11-03T06:59:59Z": "2030-11-02", "2030-11-03T07:00:00Z": "2030-11-03",  # fall-back day
            "2030-11-03T09:30:00Z": "2030-11-03", "2030-11-04T07:59:59Z": "2030-11-03", "2030-11-04T08:00:00Z": "2030-11-04",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(fc._day(text), date.fromisoformat(expected))
        register = reg()
        spellings = ("2030-01-23T00:30:00Z", "2030-01-23T00:30:00+00:00", "2030-01-22T16:30:00-08:00",
                     "2030-01-23T09:30:00+09:00", "2030-01-22")
        tables = [fc.clock_table(register, SETTINGS, [approved_row(at=s)], as_of="2030-01-25") for s in spellings]
        self.assertEqual([t[0]["notice_due"] for t in tables], [NOTICE_DAY] * len(spellings))
        plans = [kinds(fc.plan(register, SETTINGS, [approved_row(at=s)], as_of=NOTICE_DAY)) for s in spellings]
        self.assertEqual(plans, [[("D", fc.NOTICE), ("D", fc.REMINDERS[0]), ("P", fc.REMINDERS[1])]] * len(spellings))

    def test_calendar_and_ics_use_same_notice_date(self):
        project = self.project()
        queue = self.signed(project)
        before = calfx.snapshot(project)
        register, settings = reg(), copy.deepcopy(SETTINGS)
        model = calfx.stub(register, queue, settings)
        frozen = copy.deepcopy(model)
        items = cc.entries(model, date(2030, 1, 25))
        notices = sorted((e["request"], e["date"]) for e in items if e["kind"] == "followup_draft_due" and fc.NOTICE in e["label"])
        self.assertEqual(notices, [("D", date(2030, 2, 1)), ("P", date(2030, 2, 1))])
        table = {e["request_id"]: e["notice_due"] for e in fc.clock_table(register, settings, queue, as_of="2030-01-25")}
        self.assertEqual(table, {"D": NOTICE_DAY, "P": NOTICE_DAY})
        first = min(day for day in ("2030-01-30", "2030-01-31", "2030-02-01", "2030-02-02")
                    if fc.NOTICE in {c["kind"] for c in fc.plan(register, settings, queue, as_of=day)})
        self.assertEqual(first, NOTICE_DAY)
        text = cc.to_ics([e for e in items if fc.NOTICE in e["label"]], datetime(2030, 1, 25, 18, tzinfo=timezone.utc))
        self.assertEqual(text.count("DTSTART;VALUE=DATE:20300201"), 2)
        self.assertNotIn("DTSTART;VALUE=DATE:20300202", text)
        self.assertEqual(model, frozen)
        self.assertEqual(calfx.snapshot(project), before)

    def test_complete_dates_and_bad_timestamps(self):
        self.assertEqual(fc._day("2030-01-22"), date(2030, 1, 22))
        self.assertEqual(fc._day("2030-02-28"), date(2030, 2, 28))
        bad = (None, True, False, 20300122, 2030.0, ["2030-01-22"], {"d": "2030-01-22"}, b"2030-01-22",
               date(2030, 1, 22), "", "2030-01-23T00:30:00", "2030-01-23 00:30:00", "2030-02-30", "2030-02-30T00:30:00Z",
               "2030-01-23T00:30:00+25:00", "2030-01-23T00:30:00+0800", "2030-01-23T00:30:00+08",
               "2030-01-23T00:30:00 Z", "2030-01-23T24:30:00Z", "2030-01-22x", "2030-01-22 ", " 2030-01-22",
               "2030-01-22\n", "2030-01-23T00:30:00Zjunk", "2030-01-23T00:30:00Z\n", "20300122",
               "\uff12\uff10\uff13\uff10-01-22", "0001-01-01T00:00:00Z", "9999-12-31T23:59:59-12:00")
        for value in bad:
            with self.subTest(value=value):
                self.assertIsNone(fc._day(value))
        register = reg()
        for value in ("2030-01-23T00:30:00", "2030-01-23T00:30:00Zjunk", "0001-01-01T00:00:00Z", None, True):
            with self.subTest(updated_at=value):
                queue = [approved_row(at=value)]
                self.assertIsNone(fc.clock_table(register, SETTINGS, queue, as_of="2030-01-25")[0]["notice_due"])
                self.assertNotIn(fc.NOTICE, {c["kind"] for c in fc.plan(register, SETTINGS, queue, as_of="2031-01-01")})
        model = calfx.stub(register, [approved_row(at="2030-01-23T00:30:00Zjunk")], SETTINGS)
        self.assertFalse([e for e in cc.entries(model, date(2030, 1, 25)) if fc.NOTICE in e["label"]])

    def test_notice_guards_and_clock_identity_unchanged(self):
        register = reg()
        for status in ("pending", "rejected"):  # an edited draft folds back to pending
            with self.subTest(status=status):
                found = fc.plan(register, SETTINGS, [approved_row(status=status)], as_of="2030-03-01")
                self.assertNotIn(fc.NOTICE, {c["kind"] for c in found})
        base = rows()[0]
        guarded = {"closed": dict(base, status="closed"), "issue": dict(base, issues=["determination_date_inconsistent"])}
        for name, row in guarded.items():
            with self.subTest(guard=name):
                found = fc.plan(reg(requests=[row]), SETTINGS, [approved_row()], as_of="2030-03-01")
                self.assertEqual(found, [])
        off = {**SETTINGS, "agencies": {"alpha-pd": {"autonomy": "off"}}}
        self.assertEqual(fc.plan(register, off, [approved_row()], as_of="2030-03-01"), [])
        moved = reg(requests=[calfx.row("alpha-pd", "D", calfx.clock(state="past_due_reminder", due="2030-01-24"))])
        stale = approved_row()  # approved for the pre-extension 2030-01-10 clock
        self.assertNotEqual(fc.item_id("alpha-pd", "D", "determination", "2030-01-24"), stale["item_id"])
        found = fc.plan(moved, SETTINGS, [stale], as_of="2030-03-01")
        self.assertEqual([c["kind"] for c in found], [fc.REMINDERS[0]])
        self.assertEqual(found[0]["item_id"], fc.item_id("alpha-pd", "D", "determination", "2030-01-24"))
        self.assertIsNone(fc.clock_table(moved, SETTINGS, [stale], as_of="2030-01-25")[0]["notice_due"])

    def test_queue_replay_does_not_rewrite_approved_reminder(self):
        for _ in range(2):  # two separate project roots: no shared state between them
            project = self.project()
            queue = self.signed(project)
            journal = project / "approvals" / "queue.jsonl"
            signed = journal.read_bytes()
            calfx.put(project / fc.SETTINGS_FILE, {"schema": fc.SETTINGS_SCHEMA, "default": {"autonomy": "draft"}})
            path = project / "run-outputs" / RUN / fc.REGISTER_NAME
            calfx.put(path, reg("2030-01-31"))
            self.assertEqual(fc.queue_followups(project, RUN)["created"], 0)
            self.assertEqual(journal.read_bytes(), signed)
            calfx.put(path, reg(NOTICE_DAY))
            first = fc.queue_followups(project, RUN)
            self.assertEqual((first["created"], first["existing"]), (2, 2))
            after = journal.read_bytes()
            self.assertTrue(after.startswith(signed))
            again = fc.queue_followups(project, RUN)
            self.assertEqual((again["created"], again["existing"]), (0, 4))
            self.assertEqual(journal.read_bytes(), after)
            now = {r["id"]: r for r in aq.load_queue(project)}
            for old in queue:
                self.assertEqual(now[old["id"]], old)
            notices = [r for r in now.values() if r["kind"] == fc.NOTICE]
            self.assertEqual([r["status"] for r in notices], ["pending", "pending"])
            self.assertTrue(all("This follows our reminder of 2030-01-22." in r["body"] for r in notices))
            names = {p.name for p in project.rglob("*")}
            self.assertFalse({n for n in names if "outbox" in n or "receipt" in n or "sent" in n})
            self.assertEqual(json.loads(path.read_text())["as_of"], NOTICE_DAY)


if __name__ == "__main__":
    unittest.main()
