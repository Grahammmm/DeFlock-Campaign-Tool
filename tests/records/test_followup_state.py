"""Synthetic response-state acceptance; no campaign records or writes by the classifier."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import cli, followup_state as fs

DAY = "2026-11-06"
SENTINEL = "ITEM-TEXT-SENTINEL-1"


def event(kind, day="2026-11-02", **extra):
    return {"type": kind, "date": day, "source": {"event_id": "evt-1", "actor": SENTINEL},
            "summary": SENTINEL, **extra}


def ask(number=1, text="Search logs", rating="withheld"):
    return {"ask_id": "ask-%d" % number, "text": text + " " + SENTINEL, "rating": rating,
            "evidence": [{"quote": SENTINEL, "item_id": "item-1"}]}


def request(**extra):
    return {"agency_key": "alpha-pd", "request_id": "r1", "events": [],
            "clocks": {}, "issues": [], **extra}


class ClassifyTests(unittest.TestCase):
    def check(self, req, items, state):
        before = copy.deepcopy((req, items))
        result = fs.classify(req, items, DAY)
        self.assertEqual(result["state"], state)
        self.assertEqual((req, items), before)
        numbers = [n for values in result["item_needs"].values() for n in values]
        self.assertEqual(len(numbers), len(set(numbers)))
        self.assertEqual(sorted(numbers), result["evidence"]["item_numbers"])
        self.assertNotIn(SENTINEL, json.dumps(result))
        return result

    def test_all_summary_states(self):
        overdue = [{"kind": "statutory_date_passed", "date": "2026-11-03"}]
        fixtures = [
            (request(closed=True), [ask()], "closed"),
            (request(events=[event("clarification requested")]), None, "clarification"),
            (request(events=[event("sent")], overdue_marks=overdue), None, "silence"),
            (request(events=[event("extension notice", new_dates=["2026-11-10"])]), [ask()], "extension"),
            (request(), [ask()], "withheld"),
            (request(), [ask(rating="no_records_stated")], "no_records"),
            (request(), [ask(rating="answered"), ask(2, "policy", "unclear")], "partial"),
            (request(), [ask(rating="answered")], "answered"),
            (request(clocks={"state": "running", "due": "2026-11-10"}), None, "waiting"),
            (request(), None, "unknown"),
            (request(issues=["conflicting_dates"]), [ask(rating="answered")], "unknown"),
        ]
        for req, items, state in fixtures:
            with self.subTest(state=state):
                self.check(req, items, state)
        self.assertIn("open_tier1", self.check(request(closed=True), [ask()], "closed")["flags"])
        self.assertIn("register_issues", self.check(request(issues=["missing"]), None, "unknown")["flags"])

    def test_mixed_answer_partitions(self):
        result = self.check(request(), [ask(), ask(2, "Annual audits", "no_records_stated")], "withheld")
        self.assertEqual(result["item_needs"]["withheld"], [1])
        self.assertEqual(result["item_needs"]["audit_no_records"], [2])
        self.assertIn("audit_items_no_records", result["flags"])
        result = self.check(request(), [ask(3, "contracts", "no_records_stated"),
                                       ask(2, "policy", "missing"), ask(rating="answered")], "partial")
        self.assertEqual(result["item_needs"], {"withheld": [], "audit_no_records": [],
                         "other_no_records": [3], "not_addressed": [2], "produced": [1]})
        self.assertEqual(result["open_items"], [2, 3])
        self.check(request(), [ask(rating="exemption_cited")], "withheld")

    def test_no_records_without_ratings_retains_original_scope(self):
        req = request(events=[event("no records")], asked="1. Annual audits; 2. Audit logs; 3. Policy")
        result = self.check(req, None, "no_records")
        self.assertEqual(result["item_needs"]["audit_no_records"], [1])
        self.assertEqual(result["item_needs"]["other_no_records"], [2, 3])
        self.assertIn("items_not_rated", result["flags"])
        self.assertIn("audit_items_no_records", result["flags"])
        self.assertEqual(result["evidence"]["events"][0],
                         {"type": "no records", "date": "2026-11-02", "ids": {"event_id": "evt-1"}})
        self.check(req, [ask(rating="answered")], "no_records")

    def test_ack_after_latest_send_prevents_silence_but_old_ack_does_not(self):
        req = request(events=[event("sent", "2026-11-01"), event("acknowledged")],
                      overdue_marks=[{"kind": "promised_date_passed", "date": "2026-11-03"}])
        self.check(req, None, "unknown")
        req["events"].append(event("our follow-up sent", "2026-11-04"))
        self.check(req, None, "silence")
        req["events"].append(event("acknowledged", "2026-11-05", superseded=True))
        self.check(req, None, "silence")
        req["overdue_marks"][0]["date"] = "2026-11-07"
        self.check(req, None, "unknown")

    def test_expired_cancelled_stale_extensions_and_clarification(self):
        for extra in ({"new_dates": ["2026-11-05"]},
                      {"new_dates": ["2026-11-10"], "superseded": True}):
            self.check(request(events=[event("extension notice", **extra)]), [ask()], "withheld")
        self.check(request(events=[event("extension notice", new_dates=["2026-11-10"]),
                                   event("production", "2026-11-04")]), [ask()], "withheld")
        req = request(events=[event("promised date", "2026-11-10", new_dates=["2026-11-10"])],
                      extension_chain={"exceeds_14_days": True})
        self.assertIn("extension_limit_exceeded", self.check(req, [ask()], "extension")["flags"])
        self.check(request(events=[event("clarification requested"), event("production", "2026-11-03")]),
                   [ask(rating="answered")], "answered")
        self.check(request(status="clarification requested"), None, "clarification")

    def test_invalid_dates_and_duplicate_asks_refused_duplicate_events_deduplicated(self):
        with self.assertRaises(ValueError):
            fs.classify(request(), None, "invalid")
        with self.assertRaises(ValueError):
            fs.classify(request(), [ask(), ask()], DAY)
        with self.assertRaises(ValueError):
            fs.classify(request(), [{"ask_id": "bad"}], DAY)
        result = self.check(request(events=[event("sent"), event("sent")]), None, "unknown")
        self.assertEqual(len(result["evidence"]["events"]), 1)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "run-outputs" / "r1" / "outcomes"
        self.out.mkdir(parents=True)
        self.report = {"schema": "records-timeline-v1", "run_id": "r1", "as_of": DAY,
                       "requests": [request(events=[event("no records")])]}
        self.write("timeline.json", self.report)
        (self.out.parent / "CURRENT-REQUEST-REGISTER.json").write_text(json.dumps({"requests": [
            request(asked="1. Annual audits; 2. Policy")]}))

    def write(self, name, data):
        (self.out / name).write_text(json.dumps(data))

    def test_run_cli_read_only_and_joins_scoped_by_agency(self):
        self.write("response-items.json", {"schema": "records-response-items-v1", "run_id": "r1",
                  "requests": [{"agency_key": "beta-pd", "request_id": "r1", "asks": [ask()]}]})
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = fs.classify_run(self.root, "r1")
        self.assertEqual(result["requests"][0]["item_needs"]["audit_no_records"], [1])
        stdout = io.StringIO()
        with patch("sys.argv", ["records", "followup-state", "--project", str(self.root), "--run-id", "r1"]), contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(json.loads(stdout.getvalue()), result)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        self.assertTrue(result["review_required"])

    def test_missing_ratings_and_same_run_rating(self):
        self.assertIn("items_not_rated", fs.classify_run(self.root, "r1")["requests"][0]["flags"])
        self.report["requests"][0]["events"] = []
        self.write("timeline.json", self.report)
        self.write("response-items.json", {"schema": "records-response-items-v1", "run_id": "r1",
                  "requests": [request(asks=[ask(rating="answered")])]})
        self.assertEqual(fs.classify_run(self.root, "r1")["requests"][0]["state"], "answered")

    def test_stale_duplicate_malformed_and_symlink_inputs_refused(self):
        with self.assertRaises(ValueError):
            fs.classify_run(self.root, "../r1")
        for change in ({"run_id": "old"}, {"requests": [request(), request()]}, {"as_of": "bad"}):
            self.write("timeline.json", {**self.report, **change})
            with self.assertRaises(ValueError):
                fs.classify_run(self.root, "r1")
        self.write("timeline.json", self.report)
        self.write("response-items.json", {"schema": "records-response-items-v1", "run_id": "old", "requests": []})
        with self.assertRaises(ValueError):
            fs.classify_run(self.root, "r1")
        (self.out / "response-items.json").unlink()
        (self.out / "response-items.json").symlink_to(self.out / "timeline.json")
        with self.assertRaises(ValueError):
            fs.classify_run(self.root, "r1")
        with patch("sys.argv", ["followup-state", "--project", str(self.root), "--run-id", "r1"]), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(fs.main(), 2)
        self.assertEqual(err.getvalue().strip(), "followup_state_invalid_input")
