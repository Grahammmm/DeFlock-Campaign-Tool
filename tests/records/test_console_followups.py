"""FLK-REC-8: the per-ask machine reading beside the follow-up draft, from live reader to sealed export to both pages."""
import copy
import json
import os
import time
from pathlib import Path
from unittest import mock

from campaign_tool.records import approval_queue as aq
from campaign_tool.records import console_export, console_followups as cf, console_snapshot, followup_route, followup_state
from campaign_tool.records.outcomes import timeline
from tests.records.test_console_export import tree_state
from tests.records.test_console_model import RUN
from tests.records.test_console_snapshot import Panel

AS_OF = "2030-02-01"
SEND = "2030-01-22T09:00:00Z"
ASK_SENTINEL, QUOTE_SENTINEL = "ASKTEXTSENTINEL", "AGENCYQUOTESENTINEL"
MIXED = [("Search logs " + ASK_SENTINEL, "answered"), ("Policy " + ASK_SENTINEL, "withheld"),
         ("Annual audits " + ASK_SENTINEL, "no_records_stated"), ("Contracts " + ASK_SENTINEL, "no_records_stated"),
         ("Training " + ASK_SENTINEL, "missing")]
WITHHELD = [("Policy " + ASK_SENTINEL, "withheld")]


def write_run(live, requests, run=RUN, rated=True):
    """Real timeline over synthetic receipts for [(agency, request, ledger, [(ask text, rating)])]; returns the paths."""
    rows = [{"agency_key": a, "agency": a.upper(), "request_id": r, "ledger_request_id": led, "status": "open",
             "asked": "1. Search logs", "issues": [],
             "deadline": {"state": "past_due_reminder", "due": "2030-01-20", "basis": "recorded_receipt"},
             "promised": {"state": "none", "date": None}} for a, r, led, _ in requests]
    register = {"schema": "records-request-register-v1", "as_of": AS_OF, "requests": rows}
    outbox = [{"agency_id": a, "request_id": r, "state": "sent", "kind": "send_followup", "sent_at": SEND,
               "idempotency_key": "send-%s-%s" % (a, r), "message_id": "<%s-%s@example.test>" % (a, r)}
              for a, r, _, _ in requests]
    received = {"items": [{"agency_id": a, "request_id": r, "item_id": "item-%s-%s" % (a, r), "kind": "response",
                           "received_at": "2030-01-23T10:00:00Z", "deadlines": [], "quote": QUOTE_SENTINEL}
                          for a, r, _, _ in requests]}
    report = timeline.build_timeline(register, received, [], outbox, [], run_id=run)
    ratings = {"schema": "records-response-items-v1", "run_id": run, "requests": [
        {"agency_key": a, "request_id": r, "asks": [{"ask_id": "ask-%d" % n, "text": text, "rating": rating,
                                                       "evidence": [{"quote": QUOTE_SENTINEL}]}
                                                      for n, (text, rating) in enumerate(asks, 1)]}
        for a, r, _, asks in requests]}
    out = live / "run-outputs" / run
    (out / "outcomes").mkdir(parents=True, exist_ok=True)
    paths = {"timeline": out / "outcomes" / "timeline.json", "ratings": out / "outcomes" / "response-items.json",
             "register": out / "CURRENT-REQUEST-REGISTER.json"}
    paths["timeline"].write_text(json.dumps(report))
    if rated:
        paths["ratings"].write_text(json.dumps(ratings))
    paths["register"].write_text(json.dumps(register))
    return paths


class Reading(Panel):
    REQUESTS = [("caltrans", "req-1", "led-1", MIXED), ("caltrans", "req-2", "led-2", WITHHELD),
                ("alpha-pd", "req-1", "led-a", WITHHELD)]

    def setUp(self):
        super().setUp()
        self.paths = write_run(self.live, self.REQUESTS)

    def row(self, reading, agency, request):
        return next(r for r in reading["requests"] if (r["agency_key"], r["request_id"]) == (agency, request))

    def pin(self, dest):
        return console_snapshot.Pin(str(dest), RUN, aq._project_id(self.live), "exports/x", "now")

    def make_dest(self):
        dest = self.tmp / ("exp%d" % len(list(self.tmp.glob("exp*"))))
        dest.mkdir(mode=0o700)
        console_export.build(str(self.live), RUN, str(dest))
        return dest

    def test_mixed_answer_live_export_snapshot_parity(self):
        before = tree_state(self.live)
        reading = cf.read(self.live, RUN)
        self.assertEqual(tree_state(self.live), before)
        self.assertEqual(reading["state"], "ok")
        state = next(r for r in followup_state.classify_run(self.live, RUN)["requests"]
                     if (r["agency_key"], r["request_id"]) == ("caltrans", "req-1"))
        route = next(r for r in followup_route.route_run(self.live, RUN) if (r["agency_key"], r["request_id"]) == ("caltrans", "req-1"))
        row = self.row(reading, "caltrans", "req-1")
        self.assertEqual(row["item_needs"], {"produced": [1], "withheld": [2], "audit_no_records": [3],
                                             "other_no_records": [4], "not_addressed": [5]})
        self.assertEqual((row["display_state"], row["flags"], row["item_needs"]), (state["state"], state["flags"], state["item_needs"]))
        self.assertEqual((row["pattern"], row["reason"], row["ledger_request_id"]), (route["pattern"], route["reason"], "led-1"))
        self.assertEqual(row["state_source"] + row["route_source"], "okok")
        dest = self.make_dest()
        exported = json.loads((dest / "console-model" / "followup-reading.json").read_text())
        self.assertEqual({k: v for k, v in exported.items() if k not in ("schema", "source_project_id")}, reading)
        self.assertEqual(console_snapshot.SnapshotSource(self.pin(dest)).followup_reading(RUN), exported)
        self.assertEqual(console_snapshot.LiveSource(self.live).followup_reading(RUN), reading)
        page = cf.render(exported, "caltrans", "req-1")
        for text in (cf.LABEL, "Asks produced</dt><dd>1<", "Asks withheld</dt><dd>2<", "Asks audit no records</dt><dd>3<",
                     "Asks other no records</dt><dd>4<", "Asks not addressed</dt><dd>5<", row["display_state"]):
            self.assertIn(text, page)
        for blob in (json.dumps(exported), page):
            for sentinel in (ASK_SENTINEL, QUOTE_SENTINEL, "evidence"):
                self.assertNotIn(sentinel, blob)

    def test_vague_answer_live_export_snapshot_preserves_other_requests(self):
        quote = "We deny this request under Gov. Code section 9999."
        reason = "vague_answer; cited denial not tied to items (machine reading, not a finding)"
        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous):
                self.paths = write_run(self.live, self.REQUESTS)
                ratings = json.loads(self.paths["ratings"].read_text())
                request = next(r for r in ratings["requests"]
                               if (r["agency_key"], r["request_id"]) == ("caltrans", "req-2"))
                request["asks"][0]["evidence"] = [{"quote": quote, "page": 1, "doc_sha256": "a" * 64}]
                if ambiguous:
                    second = copy.deepcopy(request["asks"][0])
                    second["ask_id"] = "ask-2"
                    second["evidence"][0]["page"] = 2
                    request["asks"].append(second)
                self.paths["ratings"].write_text(json.dumps(ratings))
                before = tree_state(self.live)
                routes = followup_route.route_run(self.live, RUN)
                route = next(r for r in routes if (r["agency_key"], r["request_id"]) == ("caltrans", "req-2"))
                expected = (None, "vague_answer_evidence_ambiguous") if ambiguous else ("vague_answer", reason)
                self.assertEqual((route["pattern"], route["reason"]), expected)
                reading = console_snapshot.LiveSource(self.live).followup_reading(RUN)
                self.assertEqual(reading["state"], "ok")
                self.assertEqual(tree_state(self.live), before)
                ordinary = self.row(reading, "caltrans", "req-1")
                self.assertEqual(ordinary["item_needs"], {"produced": [1], "withheld": [2], "audit_no_records": [3],
                                                          "other_no_records": [4], "not_addressed": [5]})
                dest = self.make_dest()
                exported = json.loads((dest / "console-model" / "followup-reading.json").read_text())
                self.assertEqual({k: v for k, v in exported.items() if k not in ("schema", "source_project_id")}, reading)
                snapshot = console_snapshot.SnapshotSource(self.pin(dest)).followup_reading(RUN)
                self.assertEqual(snapshot, exported)
                for doc in (reading, exported, snapshot):
                    row = self.row(doc, "caltrans", "req-2")
                    self.assertEqual((row["state_source"], row["route_source"]), ("ok", "ok"))
                    self.assertEqual((row["pattern"], row["reason"]), expected)
                    self.assertEqual(row["item_needs"]["withheld"], [1, 2] if ambiguous else [1])
                    self.assertEqual(self.row(doc, "caltrans", "req-1"), ordinary)
                    page = cf.render(doc, "caltrans", "req-2")
                    self.assertIn(expected[1], page)
                    self.assertIn("Asks withheld</dt><dd>" + ("1, 2" if ambiguous else "1") + "<", page)
                    for sentinel in (quote, ASK_SENTINEL, QUOTE_SENTINEL):
                        self.assertNotIn(sentinel, json.dumps(doc) + page)

    def test_coverage_unreconciled_is_visible_not_guessed(self):
        aq.add_draft(self.live, item_id="%064x" % 9, kind="appeal-denial", agency_id="caltrans", request_id="req-2",
                     body="Appeal.", evidence={"source": "test", "item_id": "item-x"}, source_run_id=RUN)
        reading = cf.read(self.live, RUN)
        row = self.row(reading, "caltrans", "req-2")
        self.assertEqual((row["reason"], row["pattern"], row["state_source"], row["route_source"]),
                         ("coverage_unreconciled", None, "ok", "ok"))
        self.assertEqual(row["item_needs"]["withheld"], [1])
        page = cf.render(reading, "caltrans", "req-2")
        for text in (cf.LABEL, "coverage_unreconciled", "Asks withheld</dt><dd>1<", "Route pattern</dt><dd>none<"):
            self.assertIn(text, page)
        self.assertNotIn("unknown", page)

    def test_partition_unknown_when_required_source_unavailable(self):
        good = {k: p.read_text() for k, p in self.paths.items()}
        for name, mutate in (("register", lambda p: p.unlink()), ("timeline", lambda p: p.write_text("{")),
                             ("ratings", lambda p: p.write_text(good["ratings"].replace(RUN, "run_20260101T000000Z_00000000"))),
                             ("register", lambda p: p.write_text(good["register"].replace(AS_OF, "2030-01-01"))),
                             ("ratings", lambda p: p.unlink())):
            with self.subTest(name):
                mutate(self.paths[name])
                reading = cf.read(self.live, RUN)
                self.assertEqual(reading, {"state": "unavailable", "run_id": RUN, "code": "followup_reading_unavailable"})
                page = cf.render(reading, "caltrans", "req-1")
                self.assertIn("unknown", page)
                self.assertNotIn("Asks produced", page)
                for key, path in self.paths.items():
                    path.write_text(good[key])
        self.assertEqual(cf.read(self.live, "../x")["state"], "unavailable")
        rated = json.loads(good["ratings"])
        rated["requests"] = [r for r in rated["requests"] if r["request_id"] != "req-2" or r["agency_key"] != "caltrans"]
        self.paths["ratings"].write_text(json.dumps(rated))
        row = self.row(cf.read(self.live, RUN), "caltrans", "req-2")
        self.assertEqual((row["state_source"], row["display_state"], row["item_needs"], row["route_source"]),
                         ("unavailable", None, None, "unavailable"))
        self.assertIn("unknown", cf.render({"state": "ok", "run_id": RUN, "as_of": AS_OF, "requests": [row]}, "caltrans", "req-2"))

    def test_route_failure_preserves_state(self):
        (self.live / "followup-coverage").mkdir()
        (self.live / "followup-coverage" / "caltrans.tsv").write_text("bad\tcolumns\n")
        reading = cf.read(self.live, RUN)
        self.assertEqual(reading["state"], "ok")
        row = self.row(reading, "caltrans", "req-1")
        self.assertEqual((row["state_source"], row["route_source"], row["pattern"], row["reason"]), ("ok", "unavailable", None, None))
        self.assertEqual(row["item_needs"]["withheld"], [2])
        page = cf.render(reading, "caltrans", "req-1")
        self.assertIn("Asks withheld</dt><dd>2<", page)
        self.assertIn("Route reason</dt><dd>unknown<", page)

    def test_same_request_id_two_agencies_and_ledger_alias(self):
        reading = cf.read(self.live, RUN)
        a, b = (cf.render(reading, key, "req-1") for key in ("alpha-pd", "caltrans"))
        self.assertIn("Asks withheld</dt><dd>1<", a)
        self.assertIn("Asks withheld</dt><dd>2<", b)
        self.assertEqual(cf.find(reading, "caltrans", "led-1")["request_id"], "req-1")  # explicit same-agency alias
        self.assertEqual(cf.find(reading, "caltrans", "req-1", "led-1")["request_id"], "req-1")
        for agency, request, ledger in (("caltrans", "led-a", None), ("caltrans", "nope", None), ("beta-pd", "req-1", None),
                                        ("caltrans", "req-1", "led-2")):
            self.assertIsNone(cf.find(reading, agency, request, ledger), (agency, request, ledger))
        self.assertIn("Reading: unknown", cf.render(reading, "caltrans", "nope"))
        self.assertIn("Reading: unknown", cf.render(None, "caltrans", "req-1"))

    def test_forged_or_malformed_readings_fail_closed(self):
        reading = cf.read(self.live, RUN)
        self.assertIsNotNone(cf.validate(reading, RUN))
        self.assertIsNone(cf.validate(reading, "run_20260101T000000Z_00000000"))

        def forged(mutate):
            doc = copy.deepcopy(reading)
            mutate(doc["requests"])
            return cf.validate(doc, RUN)

        def put(key, value, index=0):
            return lambda rows: rows[index].__setitem__(key, value)
        needs = lambda g, v: (lambda rows: rows[0]["item_needs"].__setitem__(g, v))
        cases = (put("reason", "free text from a letter"), put("pattern", "invented"), put("flags", ["made_up"]),
                 put("reason", "vague_answer; cited denial not tied to items (machine reading, not a finding) " + QUOTE_SENTINEL),
                 put("reason", "vague_answer_evidence_ambiguous " + QUOTE_SENTINEL),
                 put("extra", "text"), put("display_state", "bogus"), needs("produced", [0]), needs("produced", [True]),
                 needs("produced", [-1]), needs("withheld", [1, 1]), needs("withheld", [1]), needs("produced", [2, 1]),
                 lambda rows: rows.append(copy.deepcopy(rows[0])), lambda rows: rows[0].pop("reason"),
                 lambda rows: rows[0]["item_needs"].pop("withheld"), put("state_source", "maybe"))
        for number, mutate in enumerate(cases):
            self.assertIsNone(forged(mutate), number)
        hostile = copy.deepcopy(reading)
        hostile["run_id"] = hostile["as_of"] = "<script>alert(1)</script>"
        page = cf.render(hostile, "caltrans", "req-1")
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_old_export_missing_model_is_not_in_this_export(self):
        def drop(dest):
            (dest / "console-model" / "followup-reading.json").unlink()
        self.make_export(RUN, drop)
        self.up()
        agency = self.get("/agency?agency=caltrans")
        review = self.get("/review?id=" + self.pending_id)
        for page in (agency, review):
            self.assertIn(cf.LABEL, page)
            self.assertIn("not in this export", page)
        before = tree_state(self.root)
        with mock.patch.object(cf, "read", side_effect=AssertionError("live read")):
            self.get("/agency?agency=caltrans")
            self.get("/review?id=" + self.pending_id)
        self.assertEqual(tree_state(self.root), before)

    def test_present_corrupt_or_mismatched_model_is_unknown_not_missing(self):
        path = lambda dest: dest / "console-model" / "followup-reading.json"
        good = {"schema": console_export.MODEL_SCHEMA, "source_project_id": aq._project_id(self.live), **cf.read(self.live, RUN)}
        cases = (lambda dest: path(dest).write_text("{"),
                 lambda dest: path(dest).write_text(json.dumps({**good, "run_id": "run_20260101T000000Z_00000000"})),
                 lambda dest: path(dest).write_text(json.dumps({**good, "source_project_id": "0" * 16})),
                 lambda dest: path(dest).write_text(json.dumps({**good, "requests": [{"agency_key": "caltrans"}]})))
        for number, mutate in enumerate(cases):
            with self.subTest(number):
                time.sleep(1.1)  # installed export names carry a one-second timestamp
                self.make_export(RUN, mutate)
                dest = max((self.root / "exports").iterdir())
                reading = console_snapshot.SnapshotSource(self.pin(dest)).followup_reading(RUN)
                self.assertEqual(reading["state"], "unavailable")
                self.assertNotIn("not in this export", cf.render(reading, "caltrans", "req-1"))
                self.assertIn("unknown", cf.render(reading, "caltrans", "req-1"))

    def test_snapshot_never_reopens_live_sources_and_pages_agree(self):
        reading = cf.read(self.live, RUN)
        self.make_export()
        self.up()
        boom = AssertionError("live source touched")
        with mock.patch.object(cf, "read", side_effect=boom), mock.patch.object(followup_route, "route_run", side_effect=boom), \
                mock.patch.object(console_snapshot.LiveSource, "followup_reading", side_effect=boom):
            agency = self.get("/agency?agency=caltrans")
            review = self.get("/review?id=" + self.pending_id)
        for page in (agency, review):
            self.assertIn(cf.LABEL, page)
            self.assertIn("Asks withheld</dt><dd>2<", page)
            self.assertIn("Asks not addressed</dt><dd>5<", page)
            self.assertNotIn(ASK_SENTINEL.replace("ASK", "ZZ"), page)
        self.assertIn(cf.render(reading, "caltrans", "req-1", ledger_request_id="led-1"), agency)
        self.assertIn(cf.render(reading, "caltrans", "req-1", draft_run_id=RUN), review)
        self.assertIn(cf.render(reading, "caltrans", "req-2", ledger_request_id="led-2"), agency)

    def test_review_notes_a_draft_from_another_run(self):
        reading = cf.read(self.live, RUN)
        page = cf.render(reading, "caltrans", "req-1", draft_run_id="run_20260101T000000Z_00000000")
        self.assertIn("Selected run " + RUN, page)
        self.assertIn("This draft was created from run run_20260101T000000Z_00000000", page)
        self.assertNotIn("created from", cf.render(reading, "caltrans", "req-1", draft_run_id=RUN))
        self.assertIn("Reading: unknown", cf.render(reading, "caltrans", "req-9", draft_run_id="x"))

    def test_no_new_post_or_decision_effects(self):
        reading = cf.read(self.live, RUN)
        page = cf.render(reading, "caltrans", "req-1") + cf.render({"state": "not_in_export"}, "caltrans", "req-1") + cf.render(None, "a", "b")
        for tag in ("<form", "<input", "<button", "<textarea", "hidden", "POST", "signature"):
            self.assertNotIn(tag, page)
        source = Path(cf.__file__).read_text()
        for word in ("do_POST", "approve(", "send_one", "open(", "write"):
            self.assertNotIn(word, source)
        self.make_dest()
        self.make_export()
        self.up()
        after_export = tree_state(self.live)
        self.assertEqual([p for p in os.listdir(self.live) if "followup" in p], [])
        self.get("/agency?agency=caltrans")
        self.get("/review?id=" + self.pending_id)
        status, _ = self.request("POST", "/followup-reading", {"csrf_token": self.server.csrf_token})  # type: ignore[union-attr]
        self.assertNotEqual(status, 200)
        self.assertEqual(tree_state(self.live), after_export)
