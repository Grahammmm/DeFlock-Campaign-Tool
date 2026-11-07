"""Synthetic route -> case -> real playbook drafter acceptance; no external IO."""
import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest

from campaign_tool.records import case_builder as cb, followup_drafter as fd
from campaign_tool.records import followup_route as fr, followup_state as fs, playbook as pb

DAY = "2026-11-06"


def ask(n, text, rating):
    return {"ask_id": f"ask-{n}", "text": text, "rating": rating}


def request(closed=False):
    return {"agency_key": "alpha-pd", "agency": "Alpha PD", "request_id": "OLD-1", "closed": closed,
            "events": [{"type": "sent", "date": "2026-10-01"},
                       {"type": "denial", "date": "2026-11-02"},
                       {"type": "closed", "date": "2026-11-03", "superseded": not closed},
                       {"type": "closed", "date": "2026-11-04", "superseded": True}]}


def items(*asks):
    return {"agency_key": "alpha-pd", "request_id": "OLD-1", "asks": list(asks)}


class CaseTests(unittest.TestCase):
    def chain(self, req, data, queue=(), **kwargs):
        state = fs.classify(req, data["asks"], DAY)
        route = fr.route(state, {}, queue, DAY, items=data)
        before = copy.deepcopy((route, req, data, kwargs))
        case = cb.build(route, req, data, **kwargs)
        self.assertEqual((route, req, data, kwargs), before)
        out = fd.draft(pb.load(), case)
        self.assertFalse(out["sent"])
        self.assertTrue(out["review_required"])
        return route, case, out

    def questions_once(self, out, patterns, slots=None):
        letter = out["body"].split(fd.NOTES_RULE)[0]
        for pattern in patterns:
            for question in pb.pattern(pb.load(), pattern)["questions"]:
                filled = fd._fill(question, slots or {}, [])
                self.assertEqual(letter.count(filled), 1, filled)
        return letter

    def test_mixed_withheld_and_audits_keep_all_questions_once(self):
        data = items(ask(1, "ALPR search logs", "withheld"),
                     ask(2, "Completed audits of ALPR use", "no_records_stated"))
        route, case, out = self.chain(request(), data)
        self.assertEqual((route["pattern"], route["extra_patterns"], route["asks_for"]),
                         ("blanket_denial", ["audit_no_documents"], [1, 2]))
        self.assertEqual(out["extra_patterns"], ["audit_no_documents"])
        letter = self.questions_once(out, ["blanket_denial", "audit_no_documents"])
        for n in (1, 2):
            self.assertIn(f"Item {n} asked for", letter)
        keys = [c["key"] for c in out["citations"]]
        self.assertEqual(len(keys), len(set(keys)))
        for p in route["extra_patterns"]:
            entry = pb.pattern(pb.load(), p)
            self.assertIn(entry["goal"], out["body"])
            for line in entry["do_not"]:
                self.assertIn(line, out["body"])

    def test_covered_denial_is_absent_and_inactive_rows_do_not_cover(self):
        one = dict(ask(1, "ALPR search logs", "withheld"), evidence=[{"item_id": "i1"}])
        data = items(one, ask(2, "Completed audits of ALPR use", "no_records_stated"))
        queue = {"id": "q1", "kind": "appeal-denial", "status": "pending", "evidence": {"item_id": "i1"}}
        _, case, out = self.chain(request(), data, [queue])
        self.assertEqual([a["number"] for a in case["asks"]], [2])
        self.assertNotIn(one["text"], out["body"])
        for status in ("cancelled", "expired"):
            with self.subTest(status=status):
                _, case, _ = self.chain(request(), data, [dict(queue, status=status)])
                self.assertEqual([a["number"] for a in case["asks"]], [1, 2])

    def test_partial_production_names_received_without_reasking(self):
        _, case, out = self.chain(request(), items(ask(1, "Vendor agreement", "answered"),
                                                  ask(2, "ALPR search logs", "missing")))
        self.assertEqual(case["response_type"], "partial_production")
        self.assertEqual([a["number"] for a in case["asks"]], [2])
        self.assertIn("Items recorded as received; not requested again: Item 1: Vendor agreement", out["body"])
        self.assertNotIn("Item 1 asked for", out["body"])

    def test_closed_route_verbatim_and_mixed_audit_questions(self):
        for audits in (False, True):
            with self.subTest(audits=audits):
                data = items(ask(2, "Retention policy", "no_records_stated"),
                             ask(1, "ALPR search logs", "missing"), ask(3, "Vendor agreement", "answered"))
                if audits:
                    data["asks"].append(ask(4, "Completed audits of ALPR use", "no_records_stated"))
                route, case, out = self.chain(request(True), data)
                self.assertEqual((route["pattern"], route["vehicle"]), ("closed_new_request", "new_request"))
                self.assertEqual(case["response_type"], "closed_new_request")
                self.assertEqual(out["pattern"], "closed_new_request")
                letter = self.questions_once(out, ["closed_new_request"] + (["audit_no_documents"] if audits else []), case["slots"])
                self.assertIn("This is a new request. It refers to request OLD-1", letter)
                self.assertIn("2026-11-03", letter)
                self.assertLess(letter.index("Item 1 asked for"), letter.index("Item 2 asked for"))
                self.assertNotIn("Item 3 asked for", letter)
                self.assertNotIn("no audits", letter.lower())
                if audits:
                    self.assertEqual(out["extra_patterns"], ["audit_no_documents"])

    def test_prior_rows_drop_only_produced_pending_and_leave_contact_missing(self):
        data = items(ask(1, "Vendor agreement", "missing"), ask(2, "Retention policy", "missing"),
                     ask(3, "ALPR search logs", "missing"), ask(4, "Completed audits", "missing"))
        rows = [{"item_text": a["text"], "ask_id": "unrelated-ledger-id", "state": status}
                for a, status in zip(data["asks"], ("produced", "pending", "withheld", "not_answered"))]
        route = {"pattern": "partial_production", "asks_for": [1, 2, 3, 4]}
        case = cb.build(route, request(), data, asks_rows=rows,
                        policy_ref={"policy_id": "Policy 1", "section": "1"})
        out = fd.draft(pb.load(), case)
        self.assertEqual([a["number"] for a in out["open_asks"]], [3, 4])
        self.assertEqual([d["number"] for d in out["dropped"]], [1, 2])
        self.assertNotIn("prior_asks_note", case["slots"])
        self.assertEqual((case["request_date"], case["response_date"]), ("2026-10-01", "2026-11-02"))
        for key in ("recipient", "contact", "sender"):
            self.assertNotIn(key, case)
        self.assertEqual(out["status"], "needs edit")
        self.assertEqual(cb.build(route, request(), data)["slots"]["prior_asks_note"], cb.PRIOR_NOTE)

    def test_refuses_unmapped_duplicate_missing_and_wrong_scope(self):
        route = {"pattern": "closed_new_request", "asks_for": [1]}
        data = items(ask(1, "ALPR search logs", "missing"))
        controls = [(dict(route, pattern=p), data, "route_pattern_unmapped")
                    for p in ("closed_new_requests", "closed", None)]
        controls += [(dict(route, extra_patterns=["not-a-pattern"]), data, "route_pattern_unmapped"),
                     (dict(route, extra_patterns=["closed_new_request"]), data, "route_pattern_duplicate"),
                     (dict(route, asks_for=[1, 1]), data, "route_asks_invalid"),
                     (dict(route, asks_for=[]), data, "route_asks_invalid"),
                     (dict(route, asks_for=[2]), data, "route_asks_missing"),
                     (dict(route, handled_by="manual"), data, "route_not_actionable"),
                     (route, dict(data, request_id="stale"), "case_request_mismatch"),
                     (route, items(data["asks"][0], data["asks"][0]), "case_duplicate_ask")]
        for r, d, code in controls:
            with self.subTest(code=code), self.assertRaises(cb.CaseError) as err:
                cb.build(r, request(), d)
            self.assertEqual(err.exception.code, code)

    def test_synthetic_run_staged_ledger_read_only_and_stale_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "run-outputs" / "r1" / "outcomes"
            out.mkdir(parents=True)
            req = request(True)
            data = items(ask(1, "ALPR search logs", "missing"), ask(2, "Retention policy", "missing"))
            report = {"schema": "records-timeline-v1", "run_id": "r1", "as_of": DAY, "requests": [req]}
            ratings = {"schema": "records-response-items-v1", "run_id": "r1", "requests": [data]}
            (out / "timeline.json").write_text(json.dumps(report))
            (out / "response-items.json").write_text(json.dumps(ratings))
            (out.parent / "CURRENT-REQUEST-REGISTER.json").write_text(json.dumps(
                {"schema": "records-request-register-v1", "as_of": DAY, "requests": [req]}))
            path = root / "agency-asks" / "alpha-pd.tsv"
            path.parent.mkdir()
            with path.open("w", newline="") as stream:
                writer = csv.DictWriter(stream, cb.ASK_COLUMNS, delimiter="\t")
                writer.writeheader()
                writer.writerow({"ask_id": "prior-1", "item_text": "Retention policy", "state": "pending"})
            before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
            case = cb.build_for_run(root, "r1", "alpha-pd", "OLD-1")
            self.assertEqual([a["number"] for a in fd.draft(pb.load(), case)["open_asks"]], [1])
            self.assertEqual(before, {p: p.read_bytes() for p in root.rglob("*") if p.is_file()})
            path.unlink()
            self.assertEqual(cb.build_for_run(root, "r1", "alpha-pd", "OLD-1")["slots"]["prior_asks_note"], cb.PRIOR_NOTE)
            path.symlink_to(out / "timeline.json")
            with self.assertRaises(ValueError):
                cb.build_for_run(root, "r1", "alpha-pd", "OLD-1")
            path.unlink()
            path.write_text("wrong\tcolumns\n")
            with self.assertRaises(cb.CaseError):
                cb.build_for_run(root, "r1", "alpha-pd", "OLD-1")
            for run, agency, rid in (("../r1", "alpha-pd", "OLD-1"), ("r1", "../alpha-pd", "OLD-1"),
                                     ("r1", "alpha-pd", "absent")):
                with self.subTest(run=run, agency=agency, rid=rid), self.assertRaises(ValueError):
                    cb.build_for_run(root, run, agency, rid)
            ratings["run_id"] = "stale"
            (out / "response-items.json").write_text(json.dumps(ratings))
            with self.assertRaises(ValueError):
                cb.build_for_run(root, "r1", "alpha-pd", "OLD-1")
