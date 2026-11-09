"""Synthetic routing acceptance; no drafts, outbound calls or real records."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import cli, followup_route as fr, followup_state as fs
from campaign_tool.records.outcomes import timeline

DAY = "2026-11-06"
SENTINEL = "ASK-TEXT-SENTINEL-1"


def state(name="partial", flags=(), **needs):
    return {"state": name, "flags": list(flags), "item_needs": needs,
            "evidence": {"item_numbers": sorted(n for values in needs.values() for n in values)}}


def ask(n, item="i1", rating="unclear", quote=SENTINEL):
    return {"ask_id": f"ask-{n}", "text": SENTINEL, "rating": rating,
            "evidence": [{"item_id": item, "page": 1, "quote": quote}]}


def queue(ident="q1", kind="followup-missing-records", item="i1", quote=SENTINEL, status="pending"):
    return {"id": ident, "kind": kind, "status": status, "agency_id": "alpha-pd", "request_id": "r1",
            "evidence": {"item_id": item, "findings": [{"rating": "gap", "page": 1, "quote": quote}]}}


def table(numbers="1", qid="q1"):
    return {"queue_id": qid, "ask_numbers": numbers, "confirmed_by": "synthetic-operator", "note": SENTINEL}


class RouteTests(unittest.TestCase):
    def check(self, s, qs=(), asks=(), tables=(), clock=None, rule=8, owner="engine"):
        inputs = (s, clock or {}, qs, {"asks": list(asks)}, tables)
        before = copy.deepcopy(inputs)
        result = fr.route(s, inputs[1], qs, DAY, items=inputs[3], coverage_rows=tables)
        self.assertEqual(inputs, before)
        opened = {n for _, k in fr.CANDIDATES for n in s["item_needs"].get(k, [])}
        active = [q for q in qs if q["status"] in ("pending", "approved")]
        self.assertEqual(fr._rule(s["state"], s["flags"], {q["kind"] for q in active}, opened), rule)
        self.assertEqual(result["handled_by"], owner)
        self.assertNotIn(SENTINEL, json.dumps(result))
        if rule in (1, 6, 8):
            if owner == "manual":
                self.assertEqual(result["asks_for"], [])
                self.assertEqual(result["covered"], [])
            else:
                partition = result["asks_for"] + [n for row in result["covered"] for n in row["items"]]
                self.assertEqual(sorted(partition), sorted(opened))
                self.assertEqual(len(partition), len(set(partition)))
        return result

    def test_first_match_table_and_precedence(self):
        future = {"promised": {"date": "2026-11-26"}, "extended_due": "2026-11-20"}
        fixtures = [
            (state("closed", ["open_tier1", "extension_limit_exceeded"], withheld=[1]), [], 1, "engine"),
            (state("closed", withheld=[1]), [], 2, "none"),
            (state("clarification", withheld=[1]), [queue(kind="fee-reply")], 3, "manual"),
            (state("silence", withheld=[1]), [queue(kind="fee-reply")], 4, "received_items"),
            (state("silence", ["extension_limit_exceeded"], withheld=[1]), [], 5, "followup_clock"),
            (state("extension", ["extension_limit_exceeded"], withheld=[1]), [], 6, "engine"),
            (state("extension", withheld=[1]), [], 7, "none"),
            (state("withheld", withheld=[1]), [], 8, "engine"),
            (state("no_records", other_no_records=[1]), [], 8, "engine"),
            (state("partial", not_addressed=[1]), [], 8, "engine"),
            (state("answered", produced=[1]), [], 9, "none"),
            (state("waiting", not_addressed=[1]), [], 9, "none"),
            (state("partial"), [], 9, "none"),
            (state("unknown", not_addressed=[1]), [], 10, "manual"),
        ]
        for s, qs, number, owner in fixtures:
            with self.subTest(rule=number, state=s["state"]):
                r = self.check(s, qs, rule=number, owner=owner, clock=future)
                if number == 6:
                    self.assertEqual((r["pattern"], r["planned_date"], r["date_basis"]), ("extension", DAY, fr.EXTENSION))
                if number == 7:
                    self.assertEqual(r["planned_date"], "2026-11-26")
        r = self.check(state("extension", ["extension_limit_exceeded"], withheld=[1]),
                       [queue(kind="reply-extension")], clock=future, rule=6, owner="received_items")
        self.assertEqual(r["covered"], [])
        r = self.check(state("silence", not_addressed=[1]), rule=5, owner="followup_clock",
                       clock={"followup_due": [{"due_date": "2026-11-04"}], "notice_due": "2026-11-15"})
        self.assertEqual((r["planned_date"], r["date_basis"]), ("2026-11-15", fr.CLOCK))
        r = self.check(state("silence", not_addressed=[1]), rule=5, owner="followup_clock",
                       clock={"followup_due": [{"due_date": "2026-11-04"}]})
        self.assertEqual(r["planned_date"], "2026-11-04")

    def test_mixed_patterns_and_closed_vehicle(self):
        s = state("withheld", withheld=[1], audit_no_records=[2])
        r = self.check(s)
        self.assertEqual((r["pattern"], r["extra_patterns"], r["asks_for"]),
                         ("blanket_denial", ["audit_no_documents"], [1, 2]))
        r = self.check(s, [queue(kind="appeal-denial")], [ask(1, rating="withheld")])
        self.assertEqual((r["pattern"], r["asks_for"]), ("audit_no_documents", [2]))
        self.assertEqual(r["covered"], [{"pattern": "blanket_denial", "queue_id": "q1", "items": [1], "basis": "item_evidence"}])
        r = self.check(state(produced=[1], not_addressed=[2], other_no_records=[3]))
        self.assertEqual((r["pattern"], r["extra_patterns"], r["asks_for"]), ("no_records", ["partial_production"], [2, 3]))
        r = self.check(state("closed", ["open_tier1"], withheld=[1], audit_no_records=[2]), rule=1)
        self.assertEqual((r["pattern"], r["extra_patterns"], r["vehicle"]), ("closed_new_request", ["audit_no_documents"], "new_request"))

    def test_partial_full_unknown_and_operator_coverage(self):
        s = state(not_addressed=[1, 2])
        asks = [ask(1), ask(2, quote="different synthetic quote")]
        r = self.check(s, [queue()], asks)
        self.assertEqual((r["pattern"], r["asks_for"]), ("partial_production", [2]))
        self.assertEqual(r["covered"], [{"pattern": "partial_production", "queue_id": "q1", "items": [1], "basis": "item_evidence"}])
        self.check(s, [queue()], [ask(1), ask(2)], owner="received_items")
        unknown = [queue(quote="unmapped synthetic quote")]
        r = self.check(s, unknown, asks, owner="manual")
        self.assertEqual((r["reason"], r["unreconciled"]), ("coverage_unreconciled", ["q1"]))
        r = self.check(s, unknown, asks, [table()])
        self.assertEqual((r["asks_for"], r["covered"][0]["basis"]), ([2], "operator_table"))
        self.assertEqual(self.check(s, unknown, asks, [table("none")])["asks_for"], [1, 2])
        for tables in ([table("3")], [table(qid="stale")], [table(), table()], [table("1,1")],
                       [dict(table(), confirmed_by="")], [table("0")]):
            r = self.check(s, unknown, asks, tables, owner="manual")
            self.assertEqual(r["reason"], "coverage_table_invalid")

    def test_denial_scope_split_unmapped_and_mixed_unknown(self):
        s = state("withheld", withheld=[1, 2, 3])
        asks = [ask(1, rating="withheld"), ask(2, "i2", "withheld"), ask(3, rating="exemption_cited")]
        r = self.check(s, [queue(kind="appeal-denial")], asks)
        self.assertEqual(r["asks_for"], [2])
        self.assertEqual(r["covered"][0]["items"], [1, 3])
        for qs, data in (([queue(kind="appeal-denial", item="other")], asks),
                         ([queue(kind="appeal-denial")], [dict(asks[0], evidence=asks[0]["evidence"] + asks[1]["evidence"])]),
                         ([queue(kind="appeal-denial"), queue("q2", item="other")], asks)):
            r = self.check(s, qs, data, owner="manual")
            self.assertEqual(r["reason"], "coverage_unreconciled")
        split = dict(ask(1), evidence=ask(1)["evidence"] + ask(2, "i2")["evidence"])
        self.check(state(not_addressed=[1]), [queue()], [split], owner="manual")
        # A split ask blocks mapping even when a different ask matches the gap.
        split["ask_id"] = "ask-2"
        split["evidence"][0]["quote"] = "different synthetic quote"
        self.check(state(not_addressed=[1, 2]), [queue()], [ask(1), split], owner="manual")
        # Every gap scope unit must map, not just one of them.
        q = queue()
        q["evidence"]["findings"].append({"rating": "gap", "page": 2, "quote": "unmapped"})
        self.check(state(not_addressed=[1]), [q], [ask(1)], owner="manual")

    def test_cancelled_duplicates_stale_and_noncovering_kinds(self):
        s = state(not_addressed=[1])
        for status in ("cancelled", "expired", "rejected"):
            self.assertEqual(self.check(s, [queue(status=status)])["asks_for"], [1])
            self.assertEqual(self.check(s, [queue(status=status)], tables=[table()], owner="manual")["reason"], "coverage_table_invalid")
        for kind in ("reply-extension", "reminder-no-response"):
            self.assertEqual(self.check(s, [queue(kind=kind)])["covered"], [])
        self.check(s, [queue(kind="fee-reply")], rule=4, owner="received_items")
        self.check(s, [queue(status="approved")], [ask(1)], owner="received_items")
        self.check(s, [queue(), queue("q2")], [ask(1)], owner="manual")
        with self.assertRaises(ValueError):
            fr.route(s, {}, [queue(), queue()], DAY)
        with self.assertRaises(ValueError):
            fr.route(s, {}, [], "invalid")
        r = fr.route(state("closed", ["open_tier1"], produced=[1]), {}, [], DAY)
        self.assertEqual(r["asks_for"], [1])
        r = fr.route(state("extension", ["extension_limit_exceeded"], produced=[1]), {}, [], DAY)
        self.assertEqual((r["pattern"], r["asks_for"]), ("extension", []))


class RunTests(unittest.TestCase):
    def test_cli_read_only_scope_and_stale_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "run-outputs" / "r1" / "outcomes"
            out.mkdir(parents=True)
            req = {"agency_key": "alpha-pd", "request_id": "r1", "events": [], "clocks": {}}
            report = {"schema": "records-timeline-v1", "run_id": "r1", "as_of": DAY, "requests": [req]}
            (out / "timeline.json").write_text(json.dumps(report))
            register = {"schema": "records-request-register-v1", "as_of": DAY, "requests": [req]}
            (out.parent / "CURRENT-REQUEST-REGISTER.json").write_text(json.dumps(register))
            ratings = {"schema": "records-response-items-v1", "run_id": "r1", "requests": [dict(req, asks=[ask(1, rating="withheld")])]}
            (out / "response-items.json").write_text(json.dumps(ratings))
            (root / "followup-coverage").mkdir()
            tab = root / "followup-coverage" / "alpha-pd.tsv"
            tab.write_text("queue_id\task_numbers\tconfirmed_by\tnote\nq1\tnone\tsynthetic-operator\tnote\n")
            q = queue(kind="appeal-denial")
            before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
            with patch.object(fr.approval_queue, "load_queue", return_value=[q, dict(queue("other"), agency_id="beta-pd")]):
                stdout = io.StringIO()
                with patch("sys.argv", ["records", "followup-route", "--project", tmp, "--run-id", "r1"]), contextlib.redirect_stdout(stdout):
                    self.assertEqual(cli.main(), 0)
                result = json.loads(stdout.getvalue())
                self.assertEqual(result["asks_for"], [1])
                self.assertTrue(result["review_required"])
            self.assertEqual(before, {p: p.read_bytes() for p in root.rglob("*") if p.is_file()})
            tab.unlink()
            self.assertEqual(fr.route_run(root, "r1")[0]["asks_for"], [1])
            tab.symlink_to(out / "timeline.json")
            with self.assertRaises(ValueError):
                fr.route_run(root, "r1")
            tab.unlink()
            for bad in ("../r1", "absent"):
                with self.assertRaises((ValueError, OSError)):
                    fr.route_run(root, bad)
            ratings["run_id"] = "stale"
            (out / "response-items.json").write_text(json.dumps(ratings))
            with patch("sys.argv", ["followup-route", "--project", tmp, "--run-id", "r1"]), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(fr.main(), 2)
            self.assertEqual(err.getvalue().strip(), "followup_route_invalid_input")


class SameDayReplyRunTests(unittest.TestCase):
    AS_OF = "2030-02-01"
    SEND = "2030-01-22T09:00:00Z"

    def build(self, root, reply_day, rating):
        """Real timeline over synthetic receipts; two agencies share one request id."""
        rows = [{"agency_key": key, "agency": key.upper(), "request_id": "REQ-1", "ledger_request_id": "led-" + key,
                 "status": "open", "asked": "1. Search logs", "issues": [],
                 "deadline": {"state": "past_due_reminder", "due": "2030-01-20", "basis": "recorded_receipt"},
                 "promised": {"state": "none", "date": None}} for key in ("alpha-pd", "beta-pd")]
        register = {"schema": "records-request-register-v1", "as_of": self.AS_OF, "requests": rows}
        outbox = [{"agency_id": row["agency_key"], "request_id": "REQ-1", "state": "sent", "kind": "send_followup",
                   "sent_at": self.SEND, "idempotency_key": "send-" + row["agency_key"],
                   "message_id": "<%s@example.test>" % row["agency_key"]} for row in rows]
        received = {"items": [{"agency_id": "alpha-pd", "request_id": "REQ-1", "item_id": "item-1",
                               "kind": "response", "received_at": reply_day + "T10:00:00Z", "deadlines": []}]}
        report = timeline.build_timeline(register, received, [], outbox, [], run_id="r1")
        asks = [{"ask_id": "ask-1", "text": "Search logs", "rating": rating, "evidence": []}]
        ratings = {"schema": "records-response-items-v1", "run_id": "r1",
                   "requests": [{"agency_key": key, "request_id": "REQ-1", "asks": asks}
                                for key in ("alpha-pd", "beta-pd")]}
        out = root / "run-outputs" / "r1"
        (out / "outcomes").mkdir(parents=True)
        (out / "outcomes" / "timeline.json").write_text(json.dumps(report))
        (out / "outcomes" / "response-items.json").write_text(json.dumps(ratings))
        (out / "CURRENT-REQUEST-REGISTER.json").write_text(json.dumps(register))
        return report

    @staticmethod
    def snapshot(root):
        return sorted((str(p.relative_to(root)), p.stat().st_mode, p.read_bytes() if p.is_file() else None)
                      for p in [root, *root.rglob("*")])

    def test_same_run_same_day_reply_routes_by_item_needs(self):
        cases = (("2030-01-21", "withheld", "silence", "followup_clock"),
                 ("2030-01-22", "withheld", "blanket_denial", "engine"),
                 ("2030-01-23", "withheld", "blanket_denial", "engine"),
                 ("2030-01-21", "answered", "silence", "followup_clock"),
                 ("2030-01-22", "answered", None, "none"),
                 ("2030-01-23", "answered", None, "none"))
        for reply_day, rating, pattern, owner in cases:
            with self.subTest(reply=reply_day, rating=rating), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                report = self.build(root, reply_day, rating)
                dates = {r["agency_key"]: [e["date"] for e in r["events"]] for r in report["requests"]}
                self.assertEqual(dates["alpha-pd"], sorted(["2030-01-22", reply_day]))
                self.assertEqual(dates["beta-pd"], ["2030-01-22"])
                before = self.snapshot(root)
                states = {r["agency_key"]: r["state"] for r in fs.classify_run(root, "r1")["requests"]}
                routes = {r["agency_key"]: r for r in fr.route_run(root, "r1")}
                self.assertEqual(self.snapshot(root), before)
                self.assertFalse(any(name in ("approvals", "outbox", "receipts")
                                     for name, _, _ in self.snapshot(root)))
                self.assertEqual(states["alpha-pd"], "silence" if pattern == "silence" else rating)
                self.assertEqual((routes["alpha-pd"]["pattern"], routes["alpha-pd"]["handled_by"]), (pattern, owner))
                # The other agency's send-only request keeps the no-response route.
                self.assertEqual(states["beta-pd"], "silence")
                self.assertEqual((routes["beta-pd"]["pattern"], routes["beta-pd"]["handled_by"]),
                                 ("silence", "followup_clock"))
