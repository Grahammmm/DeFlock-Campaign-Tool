"""Malformed nested response items are refused as a whole before any tier is computed (RF1450-8)."""
import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import item_tiers as it
from campaign_tool.records import playbook as pb
from campaign_tool.records import precedent as pr

CANARY = "CANARY-SECRET-9"
REFUSAL = "records items: refused response_items_invalid (shape)\n"


def good():
    return {"schema": it.SCHEMA_IN, "run_id": "r1", "requests": [
        {"agency_key": "example-pd", "request_id": "R-1", "flags": ["f"],
         "asks": [{"ask_id": "a1", "text": "Search logs for January", "rating": "answered",
                   "evidence": [{"page": 1}], "flags": ["g"]}]}]}


def mutated(fn):
    rep = good()
    fn(rep)
    return rep


def req(rep):
    return rep["requests"][0]


def ask(rep):
    return rep["requests"][0]["asks"][0]


def set_ask(key, value):
    return lambda rep: ask(rep).__setitem__(key, value)


def set_req(key, value):
    return lambda rep: req(rep).__setitem__(key, value)


BAD = {
    "request_null": lambda r: r["requests"].__setitem__(0, None),
    "request_scalar": lambda r: r["requests"].__setitem__(0, CANARY),
    "request_list": lambda r: r["requests"].__setitem__(0, [CANARY]),
    "ask_null": lambda r: req(r).__setitem__("asks", [None]),
    "ask_scalar": lambda r: req(r).__setitem__("asks", [CANARY]),
    "ask_list": lambda r: req(r).__setitem__("asks", [[CANARY]]),
    "text_list": set_ask("text", [CANARY]),
    "text_false": set_ask("text", False),
    "text_zero": set_ask("text", 0),
    "text_dict": set_ask("text", {"k": CANARY}),
    "rating_list": set_ask("rating", [CANARY]),
    "rating_false": set_ask("rating", False),
    "rating_zero": set_ask("rating", 0),
    "rating_dict": set_ask("rating", {"k": 1}),
    "asks_false": set_req("asks", False),
    "asks_zero": set_req("asks", 0),
    "asks_empty_string": set_req("asks", ""),
    "asks_empty_dict": set_req("asks", {}),
    "run_id_list": lambda r: r.__setitem__("run_id", [CANARY]),
    "agency_key_list": set_req("agency_key", [CANARY]),
    "request_id_int": set_req("request_id", 7),
    "ask_id_int": set_ask("ask_id", 7),
    "req_flags_string": set_req("flags", CANARY),
    "req_flags_dict": set_req("flags", {"k": CANARY}),
    "req_flags_bool": set_req("flags", False),
    "req_flags_nonstring": set_req("flags", [1]),
    "ask_flags_string": set_ask("flags", CANARY),
    "ask_flags_nonstring": set_ask("flags", [None]),
    "evidence_scalar": set_ask("evidence", 1),
    "evidence_string": set_ask("evidence", CANARY),
    "evidence_scalar_entry": set_ask("evidence", [CANARY]),
    "evidence_null_entry": set_ask("evidence", [None]),
}


class ShapeTests(unittest.TestCase):
    def setUp(self):
        self.book = pb.load()

    def test_nested_request_ask_text_and_rating_types_refuse(self):
        for name, fn in BAD.items():
            with self.subTest(case=name):
                rep = mutated(fn)
                before = copy.deepcopy(rep)
                with self.assertRaises(pb.PlaybookError) as cm:
                    it.tier_report(rep, self.book)
                self.assertEqual((cm.exception.code, cm.exception.detail), ("response_items_invalid", "shape"))
                self.assertEqual(rep, before)

    def test_falsey_wrong_asks_never_becomes_not_asked(self):
        for value in (False, 0, "", {}):
            with self.subTest(value=value), self.assertRaises(pb.PlaybookError):
                it.tier_report(mutated(set_req("asks", value)), self.book)
        for fn in (set_req("asks", []), set_req("asks", None), lambda r: req(r).pop("asks")):
            out = it.tier_report(mutated(fn), self.book)
            self.assertEqual(out["requests"][0]["asks"], [])
            self.assertEqual(out["requests"][0]["logs_and_audits"]["line"], "logs: not asked; audits: not asked")

    def test_optional_identity_flag_and_evidence_shapes(self):
        rep = good()
        req(rep).update(agency_key=None, request_id=None, flags=None)
        rep["run_id"] = None
        ask(rep).update(ask_id=None, text=None, rating=None, flags=None, evidence=None)
        out = it.tier_report(rep, self.book)
        r = out["requests"][0]
        self.assertEqual((out["run_id"], r["agency_key"], r["request_id"], r["flags"]), (None, None, None, []))
        self.assertEqual(r["asks"][0]["text"], "")
        self.assertEqual((r["asks"][0]["evidence"], r["asks"][0]["flags"]), ([], []))
        rep = good()
        ask(rep)["evidence"] = [{"anything": [1, None], "quote": 5}]
        self.assertEqual(it.tier_report(rep, self.book)["requests"][0]["asks"][0]["evidence"],
                         [{"anything": [1, None], "quote": 5}])

    def test_partial_reports_unknown_ratings_and_full_valid_output_unchanged(self):
        rep = {"schema": it.SCHEMA_IN, "requests": [{"asks": [{"text": "policy", "rating": "who-knows"},
                                                               {"text": "search logs", "rating": "answered"}]}]}
        before = copy.deepcopy(rep)
        out = it.tier_report(rep, self.book)
        self.assertEqual(rep, before)
        self.assertEqual([(a["tier"], a["state"], a["rating"]) for a in out["requests"][0]["asks"]],
                         [(1, "produced", "answered"), (2, "not rated", "who-knows")])
        self.assertEqual(out["requests"][0]["logs_and_audits"]["line"], "logs: produced; audits: not asked")
        full = it.tier_report(good(), self.book)
        self.assertEqual(full["requests"][0], {
            "agency_key": "example-pd", "request_id": "R-1", "flags": ["f"],
            "asks": [{"ask_id": "a1", "text": "Search logs for January", "tier": 1,
                      "tier_name": full["requests"][0]["asks"][0]["tier_name"], "rating": "answered",
                      "state": "produced", "evidence": [{"page": 1}], "flags": ["g"]}],
            "logs_and_audits": full["requests"][0]["logs_and_audits"]})
        self.assertEqual(full["requests"][0]["logs_and_audits"]["line"], "logs: produced; audits: not asked")
        self.assertEqual(full["run_id"], "r1")

    def test_root_schema_detail_unchanged(self):
        with self.assertRaises(pb.PlaybookError) as cm:
            it.tier_report({"schema": "x", "requests": []}, self.book)
        self.assertEqual(cm.exception.detail, "schema")


class CliTests(unittest.TestCase):
    def run_cli(self, module, *argv):
        out, err = io.StringIO(), io.StringIO()
        old = sys.argv
        sys.argv = ["records", *argv]
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = module.main()
        finally:
            sys.argv = old
        return code, out.getvalue(), err.getvalue()

    def write(self, rep, name="items.json"):
        d = Path(tempfile.mkdtemp())
        path = d / name
        path.write_text(json.dumps(rep), encoding="utf-8")
        return path

    def test_cli_shape_refusal_is_fixed_and_content_free(self):
        second_bad = good()
        second_bad["requests"].append(None)
        cases = [mutated(BAD[n]) for n in ("request_null", "text_list", "asks_false", "rating_list")] + [second_bad]
        for rep in cases:
            for extra in ((), ("--json",)):
                path = self.write(rep)
                raw = path.read_bytes()
                code, out, err = self.run_cli(it, str(path), *extra)
                self.assertEqual((code, out, err), (2, "", REFUSAL))
                self.assertEqual(path.read_bytes(), raw)

    def test_precedent_consumer_receives_playbook_error(self):
        path = self.write(mutated(BAD["text_list"]))
        register = path.parent / "precedents.csv"
        register.write_text(",".join(pr.HEADER) + "\n" +
                            'city-alpha,logs,"monthly CSV exports with plate numbers redacted",2026-09-16,"ref 1"\n',
                            encoding="utf-8")
        code, out, err = self.run_cli(pr, str(path), "--register", str(register))
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err, "records precedent: refused response_items_invalid (shape)\n")


if __name__ == "__main__":
    unittest.main()
