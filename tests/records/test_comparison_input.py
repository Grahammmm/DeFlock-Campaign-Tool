"""DF1117L-09: a filled worksheet becomes comparison input only through checks; every success runs through the real loader and compare()."""
import contextlib
import copy
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

from campaign_tool.records import cli, comparison_worksheet as ws
from campaign_tool.records.outcomes import comparison as c
from tests.flock.test_facts import snapshot
from tests.flock.test_facts_review import facts
from tests.records.test_comparison_worksheet import fact_row

CANARY = "CANARY-TEXT"
ROLES = ["operator", "end_user", "public_agency"]


def full(field, good=True):
    """A completely filled synthetic value for one field that compares to meets (good) or gap."""
    value = {}
    for key, spec in c.WORKSHEET_FIELDS[field]["value"].items():
        value[key] = {"bool": True, "string": "images", "enum": spec.get("values", [None])[0], "int": 30, "int_or_null": None}[spec["type"]]
    if field == "retention_practice":
        value.update(min_days=10, max_days=60, observed_days=30 if good else 90)
    elif not good:
        value[c.CRITERIA[field][2][0] if field != "usage_policy_published" else "written_public"] = False
    if field == "usage_policy_published":
        value["has_website"] = True
    if field == "sharing_practice":
        value["event_type"] = "disclosure"
    return value


class CompileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store, self.out_dir = self.dir / "store", self.dir / "out"
        self.out_dir.mkdir()
        rows = [fact_row(n, "custodian") for n in (1, 2, 3, 4)]
        self.f1, self.f2, self.f3, self.f4 = (r["fact_id"] for r in rows)
        beta = fact_row(7, "custodian")
        beta["agency"] = "Beta"
        beta["fact_id"] = facts.fact_id(beta)
        self.beta = beta["fact_id"]
        facts.write_atomic(self.store / "agencies" / "alpha" / "FACTS.csv", rows)
        facts.write_atomic(self.store / "agencies" / "beta" / "FACTS.csv", [beta])
        self.review("Alpha", [self.f1, self.f2, self.f3], "checked")
        self.review("Beta", [self.beta], "checked")
        self.comparison = self.dir / "comparison.json"
        report = c.compare([{"agency_id": "alpha", "requests": [{"request_id": "REQ-1"}]}, {"agency_id": "beta", "requests": [{"request_id": "REQ-2"}]}],
                           run_id="syn-run", as_of="2026-04-30")
        self.comparison.write_text(json.dumps(report))
        self.doc = {"schema": ws.SCHEMA, "run_id": "syn-run", "as_of": "2026-04-30", "reviewer_role": "custodian", "review_date": "2026-04-30",
                    "agencies": [{"agency_id": "alpha", "regulated_roles": list(ROLES), "jurisdiction": "us-ca", "facts": {}}],
                    "requests": {"items": [], "mismatches": []}}

    def review(self, agency, ids, to):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(facts.main(["review", "--store", str(self.store), "--agency", agency, "--ids", ",".join(ids), "--set", to, "--reviewer", "ben"]), 0)

    def entry(self, field, value=None, state="observed", **more):
        base = {"state": state, "counterevidence": [], "custodian_attested": True, "applicability_confirmed": True,
                "citations": [{"fact_id": self.f1}], "value": full(field) if value is None else value}
        return {**base, **more}

    def put(self, field, **kw):
        self.doc["agencies"][0]["facts"] = {field: self.entry(field, **kw)}

    def add(self, field, **kw):
        self.doc["agencies"][0]["facts"][field] = self.entry(field, **kw)

    def run_cli(self, doc=None, out="in.json"):
        path = self.dir / "worksheet.json"
        path.write_text(json.dumps(self.doc if doc is None else doc))
        argv = ["records", "comparison-input", "--worksheet", str(path), "--facts-store", str(self.store),
                "--comparison", str(self.comparison), "--out", str(self.out_dir / out)]
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def ok(self, out="in.json"):
        """Compile, load the emitted bytes with the real loader, run compare(): (rows by field, request rows, input, stdout)."""
        code, stdout, stderr = self.run_cli(out=out)
        self.assertEqual((code, stderr), (0, ""))
        loaded, _ = c._load_bytes((self.out_dir / out).read_bytes())
        rows = c.compare(loaded["agencies"], run_id="syn-run", as_of="2026-04-30")["rows"]
        return {r["field"]: r for r in rows if r["agency_id"] == "alpha"}, loaded["agencies"][0], stdout

    def refused(self, code, mutate=None):
        before = snapshot(self.dir)
        if mutate:
            mutate(self.doc)
        status, stdout, stderr = self.run_cli()
        self.assertEqual((status, stdout), (1, ""))
        self.assertTrue(stderr.startswith("comparison_input_%s" % code), stderr)
        self.assertNotIn(CANARY, stderr)
        self.assertFalse((self.out_dir / "in.json").exists())
        after = snapshot(self.dir)
        after.pop("worksheet.json"), before.pop("worksheet.json", None)
        self.assertEqual(after, before)
        return stderr

    # Contract: each CRITERIA field, fully filled and cited, compiles to meets or gap with the exact fact-id/page projection.
    def test_every_criterion_compiles_meets_and_gap(self):
        for field in c.CRITERIA:
            for good in (True, False):
                with self.subTest(field=field, good=good):
                    self.doc["agencies"][0]["facts"] = {field: self.entry(field, full(field, good))}
                    rows, agency, out = self.ok("%s-%s.json" % (field, good))
                    self.assertEqual(rows[field]["status"], "meets" if good else "gap")
                    self.assertEqual(agency["facts"][field]["citations"], [{"source_id": self.f1, "locator": "page 1"}])
                    self.assertEqual(out, "agency=alpha emitted=1 incomplete=0\n")

    def test_retention_practice_bounds_modality_and_layers(self):
        cases = {"inside": ({}, "meets", "supplied_policy_shall_match"), "outside_should": ({"observed_days": 90, "policy_modality": "should"}, "gap", "supplied_policy_should_discrepancy"),
                 "max_only": ({"min_days": None, "observed_days": 5}, "meets", "supplied_policy_shall_match"),
                 "max_only_over": ({"min_days": None, "observed_days": 61}, "gap", "supplied_policy_shall_discrepancy"),
                 "layers_differ": ({"policy_data_layer": "plate reads"}, "unknown", "retention_bounds_or_data_layer_unresolved")}
        for name, (change, status, reason) in cases.items():
            with self.subTest(name):
                self.put("retention_practice", value={**full("retention_practice"), **change})
                rows, agency, out = self.ok(name + ".json")
                self.assertEqual((rows["retention_practice"]["status"], rows["retention_practice"]["reason"]), (status, reason))
                self.assertEqual(out, "agency=alpha emitted=1 incomplete=0\n")  # mismatched layers are emitted as attested, not refused
                self.assertEqual("min_days" in agency["facts"]["retention_practice"]["value"], "min_days" not in change)

    def test_configured_access_online_exceptions_and_counterevidence(self):
        cases = [("sharing_practice", {"event_type": "configured_access"}, {}, "configured_access_is_not_actual_disclosure"),
                 ("audit_practice", {"exceptions_resolved": False}, {}, "practice_scope_policy_or_exceptions_unresolved"),
                 ("custodian", {}, {"counterevidence": [{"fact_id": self.f2}]}, "incomplete_or_contradictory_evidence")]
        for field, change, more, reason in cases:
            with self.subTest(field):
                self.doc["agencies"][0]["facts"] = {field: self.entry(field, {**full(field), **change}, **more)}
                rows, agency, _ = self.ok(field + ".json")
                self.assertEqual((rows[field]["status"], rows[field]["reason"]), ("unknown", reason))
        for written, status in ((True, "meets"), (False, "gap")):  # no website: written_public decides
            self.put("usage_policy_published", value={**full("usage_policy_published"), "has_website": False, "conspicuous_online": None, "written_public": written})
            rows, agency, _ = self.ok("nosite-%s.json" % written)
            self.assertEqual(rows["usage_policy_published"]["status"], status)
            self.assertNotIn("conspicuous_online", agency["facts"]["usage_policy_published"]["value"])

    # Contract: any required key still null omits the field (counted incomplete) so it stays unknown, never false or meets.
    def test_missing_required_keys_are_incomplete_and_unknown(self):
        cases = [("usage_policy_published", "conspicuous_online"), ("retention", "data_layer"), ("sharing_practice", "event_type"),
                 ("retention_practice", "data_layer"), ("retention_practice", "policy_data_layer"), ("retention_practice", "observed_days"),
                 ("audit_practice", "policy_version"), ("audit_practice", "policy_modality"), ("audit_practice", "native_coverage_complete"),
                 ("audit_practice", "exceptions_resolved"), ("audit_practice", "process_followed"), ("access_logs", "purpose")]
        for field, key in cases:
            with self.subTest(field=field, key=key):
                self.put(field, value={**full(field), key: None})
                rows, agency, out = self.ok("%s-%s.json" % (field, key))
                self.assertEqual((rows[field]["status"], rows[field]["reason"], field in agency["facts"]), ("unknown", "missing_or_unqualified_evidence", False))
                self.assertEqual(out, "agency=alpha emitted=0 incomplete=1\n")
        self.put("retention_practice", value={**full("retention_practice"), "min_days": None, "max_days": None})
        self.assertEqual(self.ok("bounds.json")[2], "agency=alpha emitted=0 incomplete=1\n")  # no default bound is supplied

    def test_unattested_omitted_and_nonobserved_has_no_value(self):
        self.add("custodian", custodian_attested=False)
        self.add("safeguards", applicability_confirmed=False)
        self.add("accuracy_correction", citations=[], custodian_attested=False)
        for state in ("missing", "redacted", "unresolved"):
            self.add("authorized_purposes", state=state, value={"purposes_documented": None})
            rows, agency, out = self.ok(state + ".json")
            self.assertEqual(agency["facts"], {"authorized_purposes": {"state": state, "applicability_confirmed": True, "citations": [{"source_id": self.f1, "locator": "page 1"}]}})
            self.assertEqual((rows["authorized_purposes"]["reason"], rows["custodian"]["status"]), ("missing_redacted_or_unresolved_fact", "unknown"))
            self.assertEqual(out, "agency=alpha emitted=1 incomplete=0\n")

    def test_refuses_bad_values_and_cross_key_conflicts(self):
        def edit(field, **change):
            return lambda doc: doc["agencies"][0]["facts"].__setitem__(field, self.entry(field, {**full(field), **change}))
        cases = {"predicate_non_bool": edit("custodian", title_documented="yes"), "days_bool": edit("retention_practice", observed_days=True),
                 "days_negative": edit("retention_practice", observed_days=-1), "days_non_int": edit("retention_practice", observed_days=1.5),
                 "blank_string": edit("retention", data_layer=" "), "bad_modality": edit("audit_practice", policy_modality="must"),
                 "bad_event_type": edit("sharing_practice", event_type="other"), "inverted_bounds": edit("retention_practice", min_days=70),
                 "online_without_website": edit("usage_policy_published", has_website=False, conspicuous_online=True),
                 "unknown_value_key": edit("custodian", **{CANARY: True})}
        for code, mutate in cases.items():
            with self.subTest(code):
                self.refused(code, mutate)
                self.setUp()
        with self.subTest("attested_without_citation"):
            self.refused("attested_without_citation", lambda d: d["agencies"][0]["facts"].__setitem__("custodian", self.entry("custodian", citations=[])))
        self.setUp()
        with self.subTest("position"):
            self.assertEqual(self.refused("days_negative", edit("retention_practice", observed_days=-1)), "comparison_input_days_negative agency=1 field=retention_practice key=observed_days\n")

    def test_refuses_bad_review_and_inventory(self):
        item = lambda **kw: {"agency_id": "alpha", "request_id": "REQ-1", **kw}  # noqa: E731
        cases = {"bad_role": lambda d: d["agencies"][0].update(regulated_roles=["operator", CANARY]), "wrong_jurisdiction": lambda d: d["agencies"][0].update(jurisdiction="us-ny"),
                 "duplicate_agency": lambda d: d["agencies"].append(copy.deepcopy(d["agencies"][0])), "unknown_agency": lambda d: d["agencies"][0].update(agency_id=CANARY),
                 "duplicate_request": lambda d: d["requests"].update(items=[item(), item()]), "unknown_request": lambda d: d["requests"].update(items=[item(request_id="REQ-9")]),
                 "request_wrong_agency": lambda d: d["requests"].update(items=[item(agency_id="beta")]),
                 "missing_reviewer_role": lambda d: d.update(reviewer_role=" "), "missing_review_date": lambda d: d.update(review_date=""),
                 "bad_date": lambda d: d.update(review_date="2026-13-01"), "run_mismatch": lambda d: d.update(run_id="other"),
                 "receipt_without_date": lambda d: d["requests"].update(items=[item(receipt_confirmed=True)]),
                 "calendar_without_start": lambda d: d["requests"].update(items=[item(calendar_confirmed=True, calendar_end="2026-12-31")]),
                 "calendar_without_end": lambda d: d["requests"].update(items=[item(calendar_confirmed=True, calendar_start="2026-01-01")]),
                 "extension_without_notice_date": lambda d: d["requests"].update(items=[item(extension_notice_qualified=True)]),
                 "flag_non_bool": lambda d: d["requests"].update(items=[item(receipt_confirmed="true", received_on="2026-04-01")])}
        for code, mutate in cases.items():
            with self.subTest(code):
                self.refused(code, mutate)
                self.setUp()

    def test_refuses_unhashable_or_misshapen_containers_without_crashing(self):
        self.refused("unknown_agency", lambda d: d["agencies"][0].update(agency_id=[]))
        self.setUp()
        self.refused("unknown_request_key", lambda d: d.update(requests=[{"agency_id": "alpha"}]))

    def test_request_timing_compiles_through_compare_and_unconfirmed_stays_unknown(self):
        proposal = {"register_source": {"entry": 1}, "register_issues": [], "hint": CANARY, "submitted_on": "2026-04-01"}
        confirmed = {"agency_id": "alpha", "request_id": "REQ-1", "received_on": "2026-04-01", "receipt_confirmed": True, "calendar_confirmed": True,
                     "calendar_start": "2026-01-01", "calendar_end": "2026-12-31", "holidays": [], "determination_state": "made", "determination_on": "2026-04-10",
                     "extension_claimed": False, "applicability_confirmed": True, "citations": [{"fact_id": self.f2, "page": 2}], **proposal}
        self.doc["requests"]["items"] = [confirmed]
        rows = c.compare(c._load_bytes((self.run_ok("t.json")).read_bytes())[0]["agencies"], run_id="syn-run", as_of="2026-04-30")["rows"]
        self.assertEqual([r["status"] for r in rows if r["field"] == "cpra_response_deadline"], ["meets"])
        self.assertNotIn(CANARY, (self.out_dir / "t.json").read_text())
        self.assertNotIn("register_source", (self.out_dir / "t.json").read_text())
        self.doc["requests"]["items"] = [{**confirmed, "receipt_confirmed": None, "calendar_confirmed": None}]
        rows = c.compare(c._load_bytes(self.run_ok("u.json").read_bytes())[0]["agencies"], run_id="syn-run", as_of="2026-04-30")["rows"]
        self.assertEqual([r["status"] for r in rows if r["field"] == "cpra_response_deadline"], ["unknown"])

    def run_ok(self, out):
        self.assertEqual(self.run_cli(out=out)[0], 0)
        return self.out_dir / out

    def test_unchecked_missing_cross_agency_and_disputed_after_worksheet(self):
        for name, fid in (("missing_fact", "0" * 16), ("unreviewed_fact", self.f4), ("cross_agency_fact", self.beta)):
            with self.subTest(name):
                self.refused("citation_not_checked", lambda d: d["agencies"][0]["facts"].__setitem__("custodian", self.entry("custodian", citations=[{"fact_id": fid}])))
                self.setUp()
        with self.subTest("page_mismatch"):
            self.refused("citation_page_mismatch", lambda d: d["agencies"][0]["facts"].__setitem__("custodian", self.entry("custodian", citations=[{"fact_id": self.f1, "page": 9}])))
        self.setUp()
        self.put("custodian")
        self.assertEqual(self.run_cli(out="first.json")[0], 0)  # worksheet bytes stay unchanged; the store changes underneath it
        self.review("Alpha", [self.f1], "disputed")
        self.refused("citation_not_checked")

    def test_deterministic_private_output_no_overwrite(self):
        self.put("custodian")
        self.add("retention", value={**full("retention"), "data_layer": None})  # incomplete is a count, not a refusal
        self.assertEqual(self.run_cli(out="a.json")[1], "agency=alpha emitted=1 incomplete=1\n")
        self.assertEqual(self.run_cli(out="b.json")[0], 0)
        a, b = (self.out_dir / n for n in ("a.json", "b.json"))
        self.assertEqual((a.read_bytes(), stat.S_IMODE(a.stat().st_mode)), (b.read_bytes(), 0o600))
        before = snapshot(self.dir)
        self.assertEqual(self.run_cli(out="a.json")[0], 1)
        self.assertEqual(snapshot(self.dir), before)
        project = self.dir / "proj" / "run-outputs" / "r1"
        project.mkdir(parents=True)
        for path in (project / "i.json", self.store / "i.json"):
            self.assertEqual(self.run_cli(out=str(path))[0], 1)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
