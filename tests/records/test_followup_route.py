"""Synthetic routing acceptance; no drafts, outbound calls or real records."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import cli, followup_findings as ff, followup_route as fr, followup_state as fs
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


def finding(valid=True, fid="F1", request="r1", quote="synthetic cited fact", item="i1"):
    facts = [{"fact_id": "fact-1", "source_sha256": "a" * 64, "page": 2, "quote": quote}]
    return {"kind": "contradiction", "agency_key": "alpha-pd", "request_id": request if valid else None,
            "finding_id": fid, "finding_sha256": "b" * 64, "evidence_sha256": ff.evidence_digest(facts) if valid else None,
            "facts": facts if valid else [], "item_ids": [item] if valid else [], "valid": valid}


class ReviewedContradictionTests(unittest.TestCase):
    def route(self, s, found, asks=(ask(1, rating="answered"),), clock=None):
        args = (s, clock or {}, [], DAY)
        before = copy.deepcopy((s, found))
        out = fr.route(*args, items={"asks": list(asks)}, reviewed_findings=found)
        self.assertEqual((s, found), before)
        return out

    def test_reviewed_finding_selects_cited_reply_in_thread_after_residual_rules_only(self):
        for s in (state("answered", produced=[1]), state("waiting"), state("withheld", withheld=[1]),
                  state("partial", not_addressed=[1])):
            with self.subTest(state=s["state"]):
                r = self.route(s, [finding()])
                self.assertEqual((r["pattern"], r["vehicle"], r["handled_by"], r["reason"], r["asks_for"], r["extra_patterns"]),
                                 ("records_contradict_policy", "reply_in_thread", "engine", "reviewed_contradiction", [], []))
                self.assertEqual((r["planned_date"], r["date_basis"]), (DAY, fr.READY))
                self.assertEqual(r["finding"], ff.locator(finding()))
                self.assertNotIn("synthetic cited fact", json.dumps(r))  # full quotes stay out of the route output

    def test_first_match_precedence_is_unchanged_for_every_current_fixture(self):
        future = {"promised": {"date": "2026-11-26"}, "extended_due": "2026-11-20"}
        fixtures = [(state("closed", ["open_tier1", "extension_limit_exceeded"], withheld=[1]), []),
                    (state("closed", withheld=[1]), []), (state("clarification", withheld=[1]), [queue(kind="fee-reply")]),
                    (state("silence", withheld=[1]), [queue(kind="fee-reply")]),
                    (state("silence", ["extension_limit_exceeded"], withheld=[1]), []),
                    (state("extension", ["extension_limit_exceeded"], withheld=[1]), []),
                    (state("extension", ["extension_limit_exceeded"], withheld=[1]), [queue(kind="reply-extension")]),
                    (state("extension", withheld=[1]), []), (state("unknown", not_addressed=[1]), []),
                    (state("withheld", withheld=[1]), []), (state("answered", produced=[1]), []),
                    (state("silence", not_addressed=[1]), [])]
        for s, qs in fixtures:
            with self.subTest(state=s["state"], flags=s["flags"]):
                plain = fr.route(s, future, qs, DAY, items={"asks": [ask(1, rating="answered")]})
                with_finding = fr.route(s, future, qs, DAY, items={"asks": [ask(1, rating="answered")]},
                                        reviewed_findings=[finding()])
                number = fr._rule(s["state"], s["flags"], {q["kind"] for q in qs}, [1] if s["item_needs"] else [])
                if number in (8, 9):
                    self.assertEqual(with_finding["pattern"], "records_contradict_policy")
                else:
                    self.assertEqual(json.dumps(with_finding, sort_keys=True), json.dumps(plain, sort_keys=True))
                    self.assertNotIn("finding", with_finding)
                self.assertNotIn("finding", plain)

    def test_ineligible_findings_leave_the_legacy_route(self):
        s = state("answered", produced=[1])
        legacy = fr.route(s, {}, [], DAY, items={"asks": [ask(1, rating="answered")]})
        for label, found, asks in (("none", [], (ask(1, rating="answered"),)),
                                   ("not answered", [finding()], (ask(1, rating="missing"),)),
                                   ("other item", [finding(item="other")], (ask(1, rating="answered"),))):
            with self.subTest(label):
                self.assertEqual(self.route(s, found, asks), legacy)

    def test_unmapped_or_ambiguous_findings_are_manual(self):
        s = state("answered", produced=[1])
        r = self.route(s, [finding(valid=False)])
        self.assertEqual((r["handled_by"], r["reason"], r["pattern"], r["asks_for"]), ("manual", "contradiction_evidence_unavailable", None, []))
        r = self.route(s, [finding(), finding(fid="F2")])
        self.assertEqual((r["handled_by"], r["reason"], r["pattern"]), ("manual", "contradiction_findings_ambiguous", None))
        r = self.route(state("unknown", not_addressed=[1]), [finding()])
        self.assertEqual((r["handled_by"], r["reason"][:7]), ("manual", "rule 10"))


def denial(quote="We deny this request under Gov. Code section 9999.", kind="denied_whole", cites=None, page=1,
           sha="c" * 64, ident="d1", words=True):
    cites = [quote] if cites is None else cites
    return {"id": ident, "agency_key": "alpha-pd", "request_id": "r1", "kind": kind, "item_no": None,
            "agency_words": {"quote": quote, "page": page, "doc_sha256": sha} if words else None,
            "agency_citation": {"text": cites, "label": "agency's citation; UNVERIFIED by us"}}


class VagueAnswerRouteTests(unittest.TestCase):
    """FLK-REC-4: a cited denial the agency did not tie to items; precedence over silence and extension."""

    def route(self, s, entries, qs=(), clock=None, tables=(), findings=()):
        return fr.route(s, clock or {}, qs, DAY, items={"asks": [ask(1, rating="missing"), ask(2, rating="missing")]},
                        coverage_rows=tables, denial_entries=entries, reviewed_findings=findings)

    def test_whole_denial_with_citation_routes_vague_answer_with_nontext_locator(self):
        r = self.route(state("unknown", not_addressed=[1, 2]), [denial()])
        self.assertEqual((r["pattern"], r["vehicle"], r["handled_by"], r["asks_for"], r["extra_patterns"]),
                         ("vague_answer", "reply_in_thread", "engine", [1, 2], []))
        self.assertEqual((r["planned_date"], r["date_basis"]), (DAY, fr.READY))
        self.assertEqual(r["vague_answer_evidence"], [{"denial_id": "d1", "page": 1, "doc_sha256": "c" * 64,
                                                       "quote_sha256": fr.quote_digest(denial()["agency_words"]["quote"])}])
        self.assertNotIn("section 9999", json.dumps(r))  # no quote or citation prose in route output
        r = self.route(state("withheld", withheld=[1]), [denial(kind="withheld")])
        self.assertEqual((r["pattern"], r["asks_for"]), ("vague_answer", [1]))  # statute digits are not an item number

    def test_item_level_denial_never_routes_vague_answer(self):
        for quote in ("Item 2 is withheld under Gov. Code section 9999.", "Items 1, 2 and 3 are exempt under § 7923.600.",
                      "1. Withheld under Gov. Code section 9999.", "Request item #2 is denied under § 7923.600."):
            with self.subTest(quote=quote):
                r = self.route(state("withheld", withheld=[1]), [denial(quote, kind="withheld")])
                self.assertEqual((r["pattern"], r["handled_by"]), ("blanket_denial", "engine"))
                self.assertNotIn("vague_answer_evidence", r)
        r = self.route(state("unknown", not_addressed=[1]), [denial("Item 99 is withheld under § 7923.600.")])
        self.assertEqual((r["pattern"], r["handled_by"]), (None, "manual"))  # unknown item: not a whole-denial fallback

    def test_no_citation_or_incomplete_evidence_never_activates(self):
        base = state("unknown", not_addressed=[1])
        for label, entry in (("no words", denial(words=False)), ("empty citation", denial(cites=[])),
                             ("bare exempt", denial("The records are exempt.")),
                             ("search statement", denial("We conducted a search of our systems.")),
                             ("no records", denial("We have no responsive records.", cites=["no responsive records"])),
                             ("page 0", denial(page=0)), ("bool page", denial(page=True)),
                             ("short hash", denial(sha="c" * 63)), ("upper hash", denial(sha="C" * 64)),
                             ("truncated", denial("We deny this request under Gov. Code section 9999. " + "x" * 400))):
            with self.subTest(label):
                self.assertEqual(self.route(base, [entry])["handled_by"], "manual")
                self.assertIsNone(self.route(base, [entry])["pattern"])
        self.assertIsNone(self.route(base, [])["pattern"])

    def test_precedence_over_silence_and_extension_and_under_closed_clarification_fee(self):
        future = {"promised": {"date": "2026-11-26"}, "extended_due": "2026-11-20"}
        wins = [state("silence", withheld=[1]), state("silence", ["extension_limit_exceeded"], withheld=[1]),
                state("extension", ["extension_limit_exceeded"], withheld=[1]), state("extension", withheld=[1]),
                state("withheld", withheld=[1]), state("partial", not_addressed=[1])]
        for s in wins:
            with self.subTest(state=s["state"], flags=s["flags"]):
                self.assertEqual(self.route(s, [denial()], clock=future)["pattern"], "vague_answer")
                self.assertNotEqual(self.route(s, [], clock=future)["pattern"], "vague_answer")
        for s, qs in ((state("closed", ["open_tier1"], withheld=[1]), []), (state("closed", withheld=[1]), []),
                      (state("clarification", withheld=[1]), []),
                      (state("withheld", withheld=[1]), [queue(kind="fee-reply")])):
            with self.subTest(state=s["state"]):
                self.assertEqual(json.dumps(self.route(s, [denial()], qs, clock=future), sort_keys=True),
                                 json.dumps(self.route(s, [], qs, clock=future), sort_keys=True))
        asks = {"asks": [ask(1, rating="answered"), ask(2, rating="missing")]}
        s = state("withheld", withheld=[2])
        self.assertEqual(fr.route(s, {}, [], DAY, items=asks, denial_entries=[denial()],
                                  reviewed_findings=[finding()])["pattern"], "vague_answer")  # first match
        self.assertEqual(fr.route(s, {}, [], DAY, items=asks, reviewed_findings=[finding()])["pattern"],
                         "records_contradict_policy")

    def test_coverage_and_ambiguity(self):
        s = state("withheld", withheld=[1, 2])
        both = {"asks": [ask(1, rating="withheld"), ask(2, rating="withheld")]}
        cover = [queue(kind="appeal-denial")]
        covered = fr.route(s, {}, cover, DAY, items=both, coverage_rows=[table("1,2")], denial_entries=[denial()])
        self.assertNotEqual(covered["pattern"], "vague_answer")  # fully covered: existing disposition
        part = fr.route(s, {}, cover, DAY, items=both, coverage_rows=[table("1")], denial_entries=[denial()])
        self.assertEqual((part["pattern"], part["asks_for"], [c["queue_id"] for c in part["covered"]]),
                         ("vague_answer", [2], ["q1"]))
        bad = self.route(s, [denial()], [queue()], tables=[table("9")])
        self.assertEqual((bad["handled_by"], bad["reason"]), ("manual", "coverage_table_invalid"))
        two = self.route(s, [denial(ident="d1"), denial("We deny under § 7923.600.", ident="d2")])
        self.assertEqual((two["handled_by"], two["reason"]), ("manual", "vague_answer_evidence_ambiguous"))
        self.assertEqual(len(self.route(s, [denial(), denial()])["vague_answer_evidence"]), 1)  # identical collapse


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
