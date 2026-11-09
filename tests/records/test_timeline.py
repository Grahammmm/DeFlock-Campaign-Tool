"""Per-run request timeline (REC-TL-1): merge, marks, extension chain, IO refusals (synthetic only)."""
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest

from campaign_tool import outbox as outbox_mod
from campaign_tool.records import approval_queue
from campaign_tool.records.outcomes import timeline as tl

AS_OF = "2026-11-05"


def clock(**kw):
    base = {"state": "running", "basis": "recorded_receipt", "base_due": "2026-11-09", "extended_due": "2026-11-23",
            "due": "2026-11-09", "recorded_due": None, "determination_at": None, "extension_recorded": False}
    return {**base, **kw}


def row(request="R-1", agency="ag1", ledger="L-1", deadline=None, action="wait", **kw):
    return {"agency": "Agency One", "agency_key": agency, "request_id": request, "ledger_request_id": ledger,
            "deadline": deadline or clock(), "next_action": action, "status": "open", "issues": [],
            "production_at": None, **kw}


def reg(*rows):
    return {"as_of": AS_OF, "requests": list(rows)}


def build(register, received=None, drafts=(), outbox=(), manual=()):
    return tl.build_timeline(register, received, list(drafts), list(outbox), list(manual), run_id="run1")


def only(report):
    return report["requests"][0]


def manual(event_id, kind, day, **kw):
    return {"event_id": event_id, "agency_id": "ag1", "request_id": "R-1", "type": kind, "date": day,
            "actor": "G", **kw}


class MergeTests(unittest.TestCase):
    def test_overdue_mark_is_labelled_and_running_clock_is_not_marked(self):
        late = only(build(reg(row(deadline=clock(state="past_due_reminder", due="2026-10-20")))))
        self.assertEqual([m["kind"] for m in late["overdue_marks"]], ["statutory_date_passed"])
        self.assertEqual(late["overdue_marks"][0]["label"], tl.OVERDUE_LABEL)
        self.assertEqual(late["overdue_marks"][0]["basis"], "recorded_receipt")
        self.assertEqual(only(build(reg(row())))["overdue_marks"], [])

    def test_determined_and_closed_requests_are_not_overdue(self):
        done = row(deadline=clock(state="determined", determination_at="2026-10-20"))
        self.assertEqual(only(build(reg(done)))["overdue_marks"], [])
        closed = row(deadline=clock(state="past_due_reminder"))
        out = only(build(reg(closed), manual=[manual("m1", "closed", "2026-11-01")]))
        self.assertEqual((out["overdue_marks"], out["closed"]), ([], True))

    def test_unknown_clock_is_unknown_not_overdue(self):
        out = only(build(reg(row(deadline=clock(state="unknown", basis="unknown", due=None)))))
        self.assertEqual([m["kind"] for m in out["overdue_marks"]], ["unknown"])
        self.assertEqual(out["next_action"]["owner"], "coordinator")

    def test_register_only_promised_date_is_merged_and_marked(self):
        passed = only(build(reg(row(promised={"state": "passed", "date": "2026-11-04"}))))
        self.assertEqual([m["kind"] for m in passed["overdue_marks"]], ["promised_date_passed"])
        self.assertEqual(passed["overdue_marks"][0]["label"], tl.OVERDUE_LABEL)
        ev = [e for e in passed["events"] if e["type"] == "promised date"]
        self.assertEqual([(e["date"], e["source"]["kind"]) for e in ev], [("2026-11-04", "register")])
        future = only(build(reg(row(promised={"state": "upcoming", "date": "2026-11-20"}))))
        self.assertEqual(future["overdue_marks"], [])
        met = only(build(reg(row(promised={"state": "none", "date": "2026-11-04"}))))
        self.assertEqual((met["overdue_marks"], [e for e in met["events"] if e["type"] == "promised date"]), ([], []))

    def test_promised_date_passed_marked_but_future_promise_is_not(self):
        past = only(build(reg(row()), manual=[manual("m1", "promised date", "2026-10-30", new_date="2026-11-04")]))
        self.assertIn("promised_date_passed", [m["kind"] for m in past["overdue_marks"]])
        self.assertTrue(all(m["label"] == tl.OVERDUE_LABEL for m in past["overdue_marks"]))
        future = only(build(reg(row()), manual=[manual("m1", "promised date", "2026-10-30", new_date="2026-11-20")]))
        self.assertEqual(future["overdue_marks"], [])
        answered = only(build(reg(row()), manual=[manual("m1", "promised date", "2026-10-30", new_date="2026-11-04"),
                                                  manual("m2", "production", "2026-11-03")]))
        self.assertEqual(answered["overdue_marks"], [])

    def test_extension_chain_flag_and_corrected_notice(self):
        got = only(build(reg(row()), manual=[manual("m1", "extension notice", "2026-11-02", new_date="2026-11-23"),
                                             manual("m2", "extension notice", "2026-11-20", new_date="2026-12-10")]))
        chain = got["extension_chain"]
        self.assertEqual((chain["count"], chain["exceeds_14_days"], chain["label"]),
                         (2, True, tl.EXTENSION_LABEL))
        within = only(build(reg(row()), manual=[manual("m1", "extension notice", "2026-11-02", new_date="2026-11-23")]))
        self.assertFalse(within["extension_chain"]["exceeds_14_days"])
        fixed = only(build(reg(row()), manual=[
            manual("m1", "extension notice", "2026-11-02", new_date="2026-12-10"),
            manual("m2", "extension notice", "2026-11-03", new_date="2026-11-23", corrects="m1")]))
        self.assertFalse(fixed["extension_chain"]["exceeds_14_days"])
        listed = {e["source"]["event_id"]: e["superseded"] for e in fixed["events"]}
        self.assertEqual(listed, {"m1": True, "m2": False})

    def test_received_extension_item_feeds_chain_and_stage(self):
        item = {"item_id": "i1", "agency_id": "ag1", "request_id": "L-1", "kind": "extension",
                "received_at": "2026-11-02T10:00:00Z", "comparison_status": "unresolved",
                "deadlines": [{"date": "2026-12-01"}]}
        out = only(build(reg(row()), {"items": [item]}))
        self.assertEqual(out["stage"], "extended")
        self.assertTrue(out["extension_chain"]["exceeds_14_days"])

    def test_stage_ladder_from_mixed_sources(self):
        sent = {"idempotency_key": "k1", "kind": "send_request", "request_id": "L-1", "agency_id": "ag1",
                "sent_at": "2026-10-20T00:00:00Z", "state": "sent", "message_id": "<m@x>"}
        self.assertEqual(only(build(reg(row()), outbox=[sent]))["stage"], "sent")
        draft = {"id": "d1", "kind": "followup-missing-records", "status": "pending", "agency_id": "ag1",
                 "request_id": "L-1", "created_at": "2026-11-01T00:00:00Z"}
        got = only(build(reg(row()), outbox=[sent], drafts=[draft]))
        self.assertEqual(got["stage"], "follow_up_drafted")
        follow = {**sent, "idempotency_key": "k2", "kind": "send_followup", "sent_at": "2026-11-02T00:00:00Z"}
        self.assertEqual(only(build(reg(row()), outbox=[sent, follow], drafts=[draft]))["stage"], "follow_up_sent")
        rejected = {**draft, "status": "rejected"}
        self.assertEqual(only(build(reg(row()), outbox=[sent], drafts=[rejected]))["stage"], "sent")
        nothing = only(build(reg(row())))
        self.assertEqual(nothing["stage"], "not_started")

    def test_analysed_requires_a_non_default_comparison_status(self):
        item = {"item_id": "i1", "agency_id": "ag1", "request_id": "L-1", "kind": "records",
                "received_at": "2026-11-02T10:00:00Z", "comparison_status": "partial", "deadlines": []}
        self.assertEqual(only(build(reg(row()), {"items": [item]}))["stage"], "analysed")
        item["comparison_status"] = "unresolved"
        self.assertEqual(only(build(reg(row()), {"items": [item]}))["stage"], "produced")

    def test_next_action_owner_follows_the_approval_journal(self):
        draft = {"id": "d1", "kind": "fee-reply", "status": "pending", "agency_id": "ag1", "request_id": "L-1",
                 "created_at": "2026-11-01T00:00:00Z"}
        pending = only(build(reg(row()), drafts=[draft]))["next_action"]
        self.assertEqual(pending["owner"], "G")
        self.assertIn("approve", pending["action"])
        approved = only(build(reg(row()), drafts=[{**draft, "status": "approved"}]))["next_action"]
        self.assertIn("sending stays manual", approved["action"])
        self.assertEqual(only(build(reg(row(action="follow up"))))["next_action"]["owner"], "coordinator")
        self.assertEqual(only(build(reg(row())))["next_action"]["owner"], "agency")

    def test_several_drafts_for_one_request_do_not_break_the_timeline(self):
        base = {"kind": "fee-reply", "status": "pending", "agency_id": "ag1", "request_id": "L-1",
                "created_at": "2026-11-01T00:00:00Z"}
        two = [{**base, "id": "d2"}, {**base, "id": "d1"}]
        self.assertEqual(only(build(reg(row()), drafts=two))["next_action"]["owner"], "G")
        two = [{**d, "status": "approved"} for d in two]
        self.assertIn("sending stays manual", only(build(reg(row()), drafts=two))["next_action"]["action"])

    def test_manual_determination_clears_the_statutory_overdue_mark(self):
        late = row(deadline=clock(state="past_due_reminder"))
        self.assertEqual(only(build(reg(late)))["overdue_marks"][0]["kind"], "statutory_date_passed")
        out = only(build(reg(late), manual=[manual("m1", "determination", "2026-11-02")]))
        self.assertEqual(out["overdue_marks"], [])
        out = only(build(reg(late), manual=[manual("m1", "determination", "2026-11-02"),
                                            manual("m2", "call", "2026-11-03", corrects="m1")]))
        self.assertEqual(out["overdue_marks"][0]["kind"], "statutory_date_passed")

    def test_events_are_dated_oldest_first_and_unmatched_are_counted(self):
        stray = {"idempotency_key": "kx", "kind": "send_request", "request_id": "OTHER", "agency_id": "ag1",
                 "sent_at": "2026-10-01T00:00:00Z", "state": "sent", "message_id": None}
        report = build(reg(row()), outbox=[stray], manual=[manual("m2", "call", "2026-11-03"),
                                                           manual("m1", "portal check", "2026-11-01")])
        self.assertEqual([e["date"] for e in only(report)["events"]], ["2026-11-01", "2026-11-03"])
        self.assertEqual(report["unmatched"]["outbox"], 1)
        self.assertEqual(report["unmatched"]["manual"], 0)

    def test_bad_as_of_is_refused(self):
        with self.assertRaises(ValueError):
            tl.build_timeline({"as_of": "soon", "requests": []}, None, [], [], [], run_id="r")


class ProductionAdmissionTests(unittest.TestCase):
    def prod(self, **kw):
        return only(build(reg(row(request_date="2026-10-20", production_at="2026-11-01", **kw))))

    def test_register_production_event_requires_admitted_day(self):
        valid = self.prod()
        self.assertEqual(valid["stage"], "produced")
        self.assertEqual([e["date"] for e in valid["events"] if e["type"] == "production"], ["2026-11-01"])
        for value in ("2026-11-06", "2026-10-19", "soon", "2026-02-30"):
            with self.subTest(value=value):
                promise = {"state": "passed", "date": "2026-11-04"}
                legacy = row(request_date="2026-10-20", production_at=value, promised=promise)
                before = json.dumps(legacy, sort_keys=True)
                out = only(build(reg(legacy)))
                self.assertEqual(json.dumps(legacy, sort_keys=True), before)  # input not mutated
                self.assertFalse([e for e in out["events"] if e["type"] == "production"])
                self.assertNotEqual(out["stage"], "produced")
                self.assertEqual([m["kind"] for m in out["overdue_marks"]], ["promised_date_passed"])
                self.assertEqual(out["issues"], ["invalid_date:production_at" if value in ("soon", "2026-02-30")
                                                 else "production_date_inconsistent"])
        # An issue the register already carries is not duplicated; existing issues are kept.
        kept = only(build(reg(row(request_date="2026-10-20", production_at="2026-11-06",
                                  issues=["production_date_inconsistent", "x"]))))
        self.assertEqual(kept["issues"], ["production_date_inconsistent", "x"])
        # Boundaries inclusive; unknown request date adds no lower bound.
        for value, start in (("2026-10-20", "2026-10-20"), ("2026-11-05", "2026-10-20"), ("1999-01-01", None)):
            out = only(build(reg(row(request_date=start, production_at=value))))
            self.assertEqual(out["stage"], "produced")
        # Independent event sources keep their own contract.
        manual_prod = only(build(reg(row(request_date="2026-10-20", production_at="2026-11-06")),
                                 manual=[manual("m1", "production", "2026-11-06")]))
        self.assertEqual(manual_prod["stage"], "produced")
        received = {"items": [{"agency_id": "ag1", "request_id": "L-1", "item_id": "i1", "kind": "records",
                               "received_at": "2026-10-10", "deadlines": []}]}
        self.assertEqual(only(build(reg(row(request_date="2026-10-20")), received=received))["stage"], "produced")


def make_project(tmp, register=None, received=None):
    project = Path(tmp) / "proj"
    run_dir = project / "run-outputs" / "run1"
    (run_dir / "outcomes").mkdir(parents=True, mode=0o700)
    if register is not None:
        (run_dir / "CURRENT-REQUEST-REGISTER.json").write_text(json.dumps(register), encoding="utf-8")
    if received is not None:
        (run_dir / "outcomes" / "received-items.json").write_text(json.dumps(received), encoding="utf-8")
    return project


class IoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_writes_private_file_and_replay_replaces_with_new_manual_event(self):
        project = make_project(self.tmp.name, reg(row()))
        tl.write_run_timeline(project, "run1")
        target = project / "run-outputs" / "run1" / "outcomes" / "timeline.json"
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        first = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual((first["schema"], first["request_count"]), (tl.SCHEMA, 1))
        (project / "manual-events.jsonl").write_text(json.dumps(manual("m1", "call", "2026-11-03")) + "\n",
                                                     encoding="utf-8")
        tl.write_run_timeline(project, "run1")
        second = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(second["requests"][0]["events"][0]["source"]["actor"], "G")

    def test_missing_register_writes_nothing(self):
        project = make_project(self.tmp.name)
        self.assertEqual(tl.write_run_timeline(project, "run1")["skipped"], "inputs_missing")
        self.assertFalse((project / "run-outputs" / "run1" / "outcomes" / "timeline.json").exists())

    def test_bad_run_id_and_symlinked_target_are_refused(self):
        project = make_project(self.tmp.name, reg(row()))
        with self.assertRaises(OSError):
            tl.write_run_timeline(project, "../x")
        target = project / "run-outputs" / "run1" / "outcomes" / "timeline.json"
        os.symlink(Path(self.tmp.name) / "elsewhere", target)
        with self.assertRaises(OSError):
            tl.write_run_timeline(project, "run1")

    def test_manual_events_refuse_unknown_type_missing_actor_and_duplicates(self):
        path = Path(self.tmp.name) / "m.jsonl"
        good = manual("m1", "call", "2026-11-03")
        for bad in ({**good, "type": "invented"}, {**good, "actor": " "}, {**good, "date": "tomorrow"}):
            path.write_text(json.dumps(bad) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                tl.read_manual_events(path)
        path.write_text((json.dumps(good) + "\n") * 2, encoding="utf-8")
        with self.assertRaises(ValueError):
            tl.read_manual_events(path)
        path.write_text(json.dumps(good) + "\n", encoding="utf-8")
        self.assertEqual(len(tl.read_manual_events(path)), 1)
        self.assertEqual(tl.read_manual_events(Path(self.tmp.name) / "absent.jsonl"), [])

    def test_outbox_receipts_are_sent_rows_only_and_carry_no_body(self):
        path = Path(self.tmp.name) / "outbox" / "outbox.sqlite"
        box = outbox_mod.Outbox(path)
        box.db.execute("INSERT INTO outbox (idempotency_key, kind, request_id, agency_id, scope_version, channel,"
                       " draft_json, state, sent_at, provider_receipt, created_at, updated_at)"
                       " VALUES ('k1','send_request','L-1','ag1',1,'email','SECRET BODY','sent','2026-10-20T00:00:00Z',"
                       "'{\"provider_message_id\": \"<m@x>\"}','t','t'), ('k2','send_request','L-2','ag1',1,'email',"
                       "'SECRET BODY','approved',NULL,NULL,'t','t')")
        box.close()
        got = tl.read_outbox_receipts(path)
        self.assertEqual([(r["idempotency_key"], r["message_id"]) for r in got], [("k1", "<m@x>")])
        self.assertNotIn("SECRET", json.dumps(got))
        self.assertEqual(tl.read_outbox_receipts(Path(self.tmp.name) / "none.sqlite"), [])

    def test_approval_journal_row_reaches_the_timeline_without_its_body(self):
        project = make_project(self.tmp.name, reg(row()), {"schema": "records-received-item-v1", "run_id": "run1",
                                                           "items": []})
        os.chmod(project, 0o700)
        draft = {"item_id": "i1", "kind": "fee-reply", "agency_id": "ag1", "request_id": "L-1",
                 "body": "PRIVATE BODY TEXT", "evidence": {}, "source_run_id": "run1"}
        with approval_queue._Store(project, True) as store:
            store.append([approval_queue._event("draft", approval_queue.draft_id("i1", "fee-reply"), 0, "tester",
                                                snapshot=draft)])
        report = tl.write_run_timeline(project, "run1")
        self.assertEqual(only(report)["next_action"]["owner"], "G")
        self.assertNotIn("PRIVATE BODY TEXT", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
