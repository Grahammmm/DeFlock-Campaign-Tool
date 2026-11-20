"""DF1117L-07: the comparison worksheet is a blank, private qualification form plus reviewed citation candidates (synthetic only)."""
import ast
import contextlib
import inspect
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from campaign_tool.records import cli, comparison_worksheet as ws
from campaign_tool.records.outcomes import comparison as c
from tests.flock.test_facts import snapshot
from tests.flock.test_facts_review import ADDED, DOC, facts
from tests.records.test_outcome_register import TODAY, data, request
from campaign_tool.records.outcomes.register import build_register

REG = ws.REGISTER_NAME

REPO = Path(__file__).resolve().parents[2]
TABLE = c.WORKSHEET_FIELDS
PRACTICE = {"native_coverage_complete", "policy_version", "policy_modality", "exceptions_resolved"}
EXTRAS = {"usage_policy_published": {"has_website", "conspicuous_online"}, "retention": {"data_layer"},
          "sharing_practice": PRACTICE | {"event_type"}, "audit_practice": PRACTICE,
          "retention_practice": PRACTICE | {"data_layer", "policy_data_layer", "observed_days", "min_days", "max_days"}}
# Keys _fact_row reads that are not editable inputs: the container itself.
ALLOWED = {"value": "the value container read as fact.get('value'); projected as the per-field value object"}


def fact_row(n, fact_type, quote="synthetic quote %d", added_by="ana"):
    row = dict(zip(facts.V1, ["Alpha", "REQ-SYN-1", fact_type, "value %d" % n, DOC, str(n), quote % n if "%d" in quote else quote]))
    row.update(kind="record", status="unreviewed", effective_from="2026-01-01" if n == 1 else "", effective_to="", added_by=added_by, added_at=ADDED)
    row["fact_id"] = facts.fact_id(row)
    return row


def read_keys(source):
    """String keys read from the fact/value containers: .get('k'), ['k'], tuple-driven comprehensions and keys.append('k')."""
    found = set()
    for node in ast.walk(ast.parse(textwrap.dedent(source))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
            owner, arg = node.func.value, node.args[0]
            if node.func.attr == "get" and isinstance(owner, ast.Name) and owner.id in ("value", "fact") and isinstance(arg, ast.Constant):
                found.add(arg.value)
            if node.func.attr == "append" and isinstance(owner, ast.Name) and owner.id == "keys" and isinstance(arg, ast.Constant):
                found.add(arg.value)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in ("value", "fact") \
                and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            found.add(node.slice.value)
        if isinstance(node, (ast.GeneratorExp, ast.ListComp)):
            for gen in node.generators:
                reads = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
                            and isinstance(n.func.value, ast.Name) and n.func.value.id in ("value", "fact")
                            and isinstance(n.args[0], ast.Name) and n.args[0].id == gen.target.id for n in ast.walk(node.elt))
                if reads and isinstance(gen.iter, ast.Tuple):
                    found.update(e.value for e in gen.iter.elts if isinstance(e, ast.Constant))
    return found


class WorksheetTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store, self.out_dir = self.dir / "store", self.dir / "out"
        self.out_dir.mkdir()
        self.long = "Q" * 500
        rows = [fact_row(1, "retention", self.long), fact_row(2, "retention_days"), fact_row(3, "vendor"),
                fact_row(4, "custodian"), fact_row(5, "safeguards"), fact_row(6, "audit_reports")]
        self.ids = [r["fact_id"] for r in rows]
        facts.write_atomic(self.store / "agencies" / "alpha" / "FACTS.csv", rows)
        for ids, to in ((self.ids[:3] + self.ids[5:], "checked"), (self.ids[3:4], "disputed")):  # row 5 stays unreviewed
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(facts.main(["review", "--store", str(self.store), "--agency", "Alpha", "--ids", ",".join(ids),
                                             "--set", to, "--reviewer", "ben"]), 0)
        agency = lambda aid, **kw: {"agency_id": aid, **kw}  # noqa: E731
        self.agency = agency
        self.comparison = self.dir / "comparison.json"
        self.make([request("REQ-1", "alpha", "EXT-1")])

    def make(self, reqs, inventory=None, mutate=None, extra=()):
        """Write a real producer register (and a comparison whose inventory is `inventory`) beside each other."""
        reg = data()
        reg["agencies"] = [{"id": "alpha", "name": "Alpha PD"}, {"id": "beta", "name": "Beta PD"}]
        reg["requests"] = list(reqs)
        report = build_register(reg, list(extra), as_of=TODAY)
        if mutate:
            mutate(report)
        (self.dir / REG).write_text(json.dumps(report))
        inventory = [r["id"] for r in reqs if r["agency_id"] == "alpha"] if inventory is None else inventory
        report = c.compare([self.agency("alpha", requests=[{"request_id": i} for i in inventory]), self.agency("beta-no-facts")],
                           run_id="syn-run", as_of="2026-04-30")
        self.comparison.write_text(json.dumps(report))

    def items(self, reqs, **kw):
        self.make(reqs, **kw)
        (self.out_dir / "worksheet.json").unlink(missing_ok=True)
        code, _, err = self.run_cli()
        self.assertEqual((code, err), (0, ""))
        requests = json.loads((self.out_dir / "worksheet.json").read_text())["requests"]
        return {i["request_id"]: i for i in requests["items"]}, requests["mismatches"]

    def run_cli(self, *extra, out=None, store=None, comparison=None):
        argv = ["records", "comparison-worksheet", "--comparison", str(comparison or self.comparison),
                "--facts-store", str(store or self.store), "--out", str(out or self.out_dir / "worksheet.json"), *extra]
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def sheet(self):
        self.assertEqual(self.run_cli()[0], 0)
        return json.loads((self.out_dir / "worksheet.json").read_text())

    # Contract: only checked facts are listed, mapped by the fixed table; disputed/unreviewed are counted, never shown.
    # Bug caught: listing an unreviewed fact, a wrong mapping, an uncut quote or leaking withheld/source text to stdout.
    def test_worksheet_lists_only_checked_candidates_and_counts_withheld(self):
        before = snapshot(self.dir)
        code, out, err = self.run_cli()
        self.assertEqual((code, err), (0, ""))
        for secret in (self.long, DOC, "REQ-SYN-1", *self.ids):
            self.assertNotIn(secret, out)
        self.assertIn("candidates=3 unmapped=1 withheld_disputed=1 withheld_unreviewed=1", out)
        path = self.out_dir / "worksheet.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        after = snapshot(self.dir)
        self.assertEqual({k: v for k, v in after.items() if k != "out/worksheet.json"}, before)  # store and comparison untouched
        alpha = json.loads(path.read_text())["agencies"][0]
        listed = {f: [x["fact_id"] for x in v["candidates"]] for f, v in alpha["facts"].items() if v["candidates"]}
        self.assertEqual(listed, {"retention": sorted(self.ids[:2]), "audit_practice": [self.ids[5]]})
        self.assertEqual([x["fact_id"] for x in alpha["unmapped"]], [self.ids[2]])
        self.assertEqual(alpha["withheld"], {"disputed": 1, "unreviewed": 1})
        text = path.read_text()
        self.assertNotIn(self.ids[3], text)  # disputed and unreviewed are never listed
        self.assertNotIn(self.ids[4], text)
        first = next(x for x in alpha["facts"]["retention"]["candidates"] if x["fact_id"] == self.ids[0])
        self.assertEqual(first, {"fact_id": self.ids[0], "kind": "record", "fact_type": "retention", "effective_from": "2026-01-01",
                                 "effective_to": None, "source_sha256_prefix": DOC[:12], "page": 1, "verbatim_quote": "Q" * 400})

    # Contract: inventory comes from comparison.json rows alone and the blank form qualifies nothing.
    # Bug caught: a placeholder request, a duplicate agency, a prefilled value/role/attestation, or a blank that compiles to a rating.
    def test_worksheet_preserves_inventory_including_agency_without_facts_and_requests(self):
        doc = self.sheet()
        self.assertEqual((doc["schema"], doc["run_id"], doc["reviewer_role"], doc["review_date"]), (ws.SCHEMA, "syn-run", "", ""))
        self.assertEqual((doc["requests"]["mismatches"], [i["request_id"] for i in doc["requests"]["items"]]), ([], ["REQ-1"]))
        self.assertEqual([a["agency_id"] for a in doc["agencies"]], ["alpha", "beta-no-facts"])
        beta = doc["agencies"][1]
        self.assertEqual((set(beta["facts"]), beta["regulated_roles"], beta["jurisdiction"], beta["unmapped"]), (set(c.CRITERIA), [], None, []))
        sheets = []
        for agency in doc["agencies"]:
            facts_in = {f: {k: v[k] for k in ("state", "counterevidence", "custodian_attested", "applicability_confirmed", "citations", "value")}
                        for f, v in agency["facts"].items()}
            self.assertTrue(all(x["custodian_attested"] is False and x["applicability_confirmed"] is False and x["citations"] == []
                                and all(v is None for v in x["value"].values()) for x in facts_in.values()))
            sheets.append({"agency_id": agency["agency_id"], "regulated_roles": agency["regulated_roles"],
                           "jurisdiction": agency["jurisdiction"], "facts": facts_in})
        statuses = {r["status"] for r in c.compare(sheets, run_id="x", as_of="2026-04-30")["rows"]}
        self.assertEqual(statuses, {"unknown"})

    # Contract: nothing is created from a refused run. Bug caught: overwriting an occupied path or following a dangling link,
    # reading a store in the checkout, writing into a project, accepting a pending-journal store or a duplicate-key comparison.
    def test_refuses_occupied_output_and_checkout_store(self):
        taken = self.out_dir / "worksheet.json"
        taken.write_bytes(b"keep")
        dangling = self.out_dir / "dangling.json"
        dangling.symlink_to(self.out_dir / "nowhere")
        self.assertEqual(self.run_cli()[0], 1)
        self.assertEqual(self.run_cli(out=dangling)[0], 1)
        self.assertEqual((taken.read_bytes(), os.path.lexists(self.out_dir / "nowhere")), (b"keep", False))
        taken.unlink()
        for store in (REPO, REPO / "tools"):
            self.assertEqual(self.run_cli(store=store), (1, "", "comparison_worksheet_fact_store_in_checkout\n"))
        project = self.dir / "proj" / "run-outputs" / "r1"
        project.mkdir(parents=True)
        self.assertEqual(self.run_cli(out=project / "w.json"), (1, "", "comparison_worksheet_output_location_refused\n"))
        self.assertEqual(self.run_cli(out=self.store / "w.json")[0], 1)
        bad = self.dir / "dup.json"
        bad.write_text('{"schema": "records-agency-comparison-v1", "schema": "x", "rows": []}')
        self.assertEqual(self.run_cli(comparison=bad)[2], "comparison_worksheet_comparison_unreadable\n")
        (self.store / "agencies" / "alpha" / ".FACTS-TXN.json").write_text("{}")
        self.assertEqual(self.run_cli()[2], "comparison_worksheet_fact_store_refused\n")
        self.assertEqual([p.name for p in self.out_dir.iterdir()], ["dangling.json"])

    def test_missing_facts_still_refuses_pending_transaction_and_busy_store(self):
        import fcntl
        folder = self.store / "agencies" / "beta-no-facts"
        folder.mkdir()
        journal = folder / ".FACTS-TXN.json"
        journal.write_text("{}")
        self.assertEqual(self.run_cli(), (1, "", "comparison_worksheet_fact_store_refused\n"))
        journal.unlink()
        report = c.compare([{"agency_id": "beta-no-facts"}], run_id="syn-run", as_of="2026-04-30")
        self.comparison.write_text(json.dumps(report))
        fd = os.open(self.store, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with mock.patch.object(ws, "_tool", wraps=ws._tool) as load_tool:
            tool = ws._tool()
            tool.LOCK_WAIT = 0
            load_tool.return_value = tool
            self.assertEqual(self.run_cli(), (1, "", "comparison_worksheet_fact_store_busy\n"))
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.assertEqual(self.run_cli()[0], 0)

    def test_refuses_symlink_in_store_path(self):
        link = self.dir / "store-link"
        link.symlink_to(self.store, target_is_directory=True)
        self.assertEqual(self.run_cli(store=link), (1, "", "comparison_worksheet_fact_store_unsafe\n"))
        parent_link = self.dir / "parent-link"
        parent_link.symlink_to(self.dir, target_is_directory=True)
        self.assertEqual(self.run_cli(store=parent_link / "store"), (1, "", "comparison_worksheet_fact_store_unsafe\n"))
        self.assertFalse((self.out_dir / "worksheet.json").exists())

    def test_output_creation_error_is_fixed_refusal(self):
        real_open = os.open
        target = self.out_dir / "worksheet.json"
        def fail_output(path, flags, *args, **kwargs):
            if Path(path) == target and flags & os.O_CREAT:
                raise PermissionError("synthetic private path")
            return real_open(path, flags, *args, **kwargs)
        with mock.patch.object(os, "open", side_effect=fail_output):
            self.assertEqual(self.run_cli(), (1, "", "comparison_worksheet_output_write_failed\n"))
        self.assertFalse(target.exists())
        def race_output(path, flags, *args, **kwargs):
            if Path(path) == target and flags & os.O_CREAT:
                target.write_bytes(b"other writer")
            return real_open(path, flags, *args, **kwargs)
        with mock.patch.object(os, "open", side_effect=race_output):
            self.assertEqual(self.run_cli(), (1, "", "comparison_worksheet_output_write_failed\n"))
        self.assertEqual(target.read_bytes(), b"other writer")

    # Contract: one typed table beside CRITERIA drives every key. Bug caught: a predicate or typed extra dropped from the blank.
    def test_projects_all_criteria_predicates_and_typed_extras_blank(self):
        sheet = self.sheet()["agencies"][1]["facts"]
        self.assertEqual(set(sheet), set(c.CRITERIA))
        for name, (_, _, predicates) in c.CRITERIA.items():
            entry, value = sheet[name], TABLE[name]["value"]
            self.assertEqual(set(value), set(predicates) | EXTRAS.get(name, set()), name)
            self.assertEqual(entry["value"], {k: None for k in value})
            self.assertEqual((entry["state"], entry["counterevidence"], entry["candidates"]), ("unresolved", [], []))
            self.assertEqual(set(entry["descriptors"]["fact"]), {"state", "counterevidence", "custodian_attested", "applicability_confirmed", "citations"})
            for key, d in {**value, **TABLE[name]["fact"]}.items():
                self.assertTrue(d["hint"].strip() and "\n" not in d["hint"], key)
            self.assertTrue(all(value[p]["type"] == "bool" for p in predicates))
        self.assertEqual(TABLE["usage_policy_published"]["value"]["conspicuous_online"]["required"], "only when has_website is true")
        types = {k: d["type"] for k, d in TABLE["retention_practice"]["value"].items()}
        self.assertEqual((types["observed_days"], types["min_days"], types["policy_modality"], types["data_layer"]),
                         ("int", "int_or_null", "enum", "string"))
        self.assertEqual(TABLE["sharing_practice"]["value"]["event_type"]["values"], ["disclosure", "configured_access"])
        self.assertEqual(TABLE["retention_practice"]["value"]["min_days"]["required"], "one bound at least")

    # Contract: _fact_row/_qualified may only read keys the table declares. Bug caught: a new comparison key that the worksheet omits.
    def test_fact_row_reads_only_declared_keys(self):
        declared = set(TABLE["retention_practice"]["fact"]) | {k for t in TABLE.values() for k in t["value"]} | set(ALLOWED)
        source = inspect.getsource(c._fact_row) + inspect.getsource(c._qualified)
        read = read_keys(source)
        self.assertTrue({"state", "citations", "value", "conspicuous_online", "observed_days", "event_type"} <= read)
        self.assertEqual(read - declared, set())
        mutated = source.replace("value = fact.get(\"value\")", "value = fact.get(\"value\")\n    value.get(\"novel_key\")")
        self.assertNotEqual(mutated, source)
        self.assertEqual(read_keys(mutated) - declared, {"novel_key"})
        self.assertTrue(all(reason for reason in ALLOWED.values()))

    # DF1117L-08: request timing is proposed from the sibling register only; receipt/calendar/notice stay custodian-owned.
    def test_proposes_recorded_receipt_and_points_to_register_row(self):
        items, mismatches = self.items([request("r9", "beta", "X"), request("REQ-1", "alpha", "EXT-1", received_at="2030-01-03")])
        item = items["REQ-1"]
        self.assertEqual((item["received_on"], item["submitted_on"], item["submitted_note"]), ("2030-01-03", None, None))
        self.assertEqual(mismatches, [{"kind": "register_only", "agency_id": "beta", "request_id": "r9", "register_entries": [2]}])
        self.assertEqual(item["register_source"], {"artifact": REG, "entry": 1})  # canonical ledger id matched, not the external ref
        self.assertEqual((item["calendar_start"], item["calendar_end"], item["holidays"], item["citations"]), (None, None, None, []))

    def test_submission_proxy_is_labelled_not_receipt(self):
        item = self.items([request("REQ-1", "alpha", "EXT-1")])[0]["REQ-1"]
        self.assertEqual((item["received_on"], item["submitted_on"], item["submitted_note"]),
                         (None, "2030-01-01", "submitted, receipt not recorded"))

    def test_recorded_determination_is_made(self):
        items = self.items([request("REQ-1", "alpha", "E1", determination_at="2030-01-04"),
                            request("REQ-2", "alpha", "E2", determination_due_at="2030-01-09", production_at="2030-01-05")])[0]
        self.assertEqual((items["REQ-1"]["determination_state"], items["REQ-1"]["determination_on"]), ("made", "2030-01-04"))
        self.assertEqual((items["REQ-2"]["determination_state"], items["REQ-2"]["determination_on"]), ("pending", None))

    def test_no_recorded_determination_is_pending(self):
        item = self.items([request("REQ-1", "alpha", "E1", status="complete", production_at="2030-01-05")])[0]["REQ-1"]
        self.assertEqual((item["determination_state"], item["determination_on"]), ("pending", None))

    def test_only_recorded_extension_proposes_days(self):
        reqs = [request(f"REQ-{n}", "alpha", f"E{n}", **kw) for n, kw in enumerate(
            [{"extension": "yes"}, {"extension": "no"}, {"extension": "maybe"}, {"determination_due_at": "2030-01-25"}], 1)]
        items = self.items(reqs)[0]
        got = [(items[r["id"]]["extension_claimed"], items[r["id"]]["extension_days"]) for r in reqs]
        self.assertEqual(got, [(True, 14), (None, None), (None, None), (None, None)])
        self.assertEqual(items["REQ-3"]["register_issues"], ["extension_unresolved"])
        self.assertEqual((items["REQ-1"]["extension_notice_on"], items["REQ-1"]["extension_notice_qualified"]), (None, None))

    def test_invalid_register_dates_are_not_promoted(self):
        proxy = self.items([request("REQ-1", "alpha", "E1", received_at="not-a-date")])[0]["REQ-1"]
        self.assertEqual((proxy["received_on"], proxy["submitted_on"], proxy["register_issues"]), (None, "2030-01-01", ["invalid_date:received_at"]))

        def corrupt(report):
            report["requests"][0]["request_date"] = "garbage-date"
            report["requests"][0]["deadline"]["determination_at"] = "2030-13-45"
            report["requests"][1]["request_date"] = "2031-02-02"  # after register as-of
            report["requests"][1]["deadline"]["determination_at"] = "2031-01-01"
        items = self.items([request("REQ-1", "alpha", "E1", received_at="2030-01-03"), request("REQ-2", "alpha", "E2")], mutate=corrupt)[0]
        for item in items.values():
            self.assertEqual((item["received_on"], item["submitted_on"], item["determination_state"], item["determination_on"]),
                             (None, None, "pending", None))
        text = (self.out_dir / "worksheet.json").read_text()
        self.assertTrue(all(raw not in text for raw in ("garbage-date", "2030-13-45", "2031-01-01", "2031-02-02", "not-a-date")))

    # Contract: a date issue withholds a proposal only when it concerns the field that proposal rests on (receipt vs proxy).
    # Bug caught: a malformed submitted_at dropping a valid recorded receipt, or an invalid received_at not stopping a receipt.
    def test_date_issues_withhold_only_the_basis_they_concern(self):
        # Real producer row: valid recorded receipt, malformed submitted_at (carries invalid_date:submitted_at).
        item = self.items([request("REQ-1", "alpha", "E1", received_at="2030-01-03", submitted_at="not-a-date")])[0]["REQ-1"]
        self.assertEqual((item["received_on"], item["submitted_on"], item["register_issues"]), ("2030-01-03", None, ["invalid_date:submitted_at"]))

        def inject(report):
            report["requests"][0]["issues"].append("invalid_date:received_at")  # receipt basis: its own issue withholds
            report["requests"][1]["issues"].append("source_conflict:received_at")  # proxy basis: the receipt field is not its basis
            report["requests"][2]["issues"].append("source_conflict:submitted_at")  # proxy basis: its own conflict withholds
        items = self.items([request("REQ-1", "alpha", "E1", received_at="2030-01-03"), request("REQ-2", "alpha", "E2"),
                            request("REQ-3", "alpha", "E3")], mutate=inject)[0]
        self.assertEqual([(items[r]["received_on"], items[r]["submitted_on"]) for r in ("REQ-1", "REQ-2", "REQ-3")],
                         [(None, None), (None, "2030-01-01"), (None, None)])

    def test_inventory_mismatches_never_become_proposals(self):
        def duplicate(report):
            row = next(r for r in report["requests"] if r["ledger_request_id"] == "AMBIG")
            report["requests"].append(dict(row))
            report["request_count"] += 1
        reqs = [request("REQ-1", "alpha", "SAME"), request("REG-ONLY", "alpha", "E2"), request("W1", "beta", "SAME"),
                request("AMBIG", "alpha", "E4")]
        items, mismatches = self.items(reqs, inventory=["REQ-1", "SAME", "AMBIG", "INV-ONLY", "W1"], mutate=duplicate)
        self.assertEqual(sorted(items), ["REQ-1"])  # an external ref never stands in for a ledger id, nor a beta id for alpha
        got = [(m["kind"], m["agency_id"], m["request_id"], m["register_entries"]) for m in mismatches]
        self.assertEqual(got, [("ambiguous_register", "alpha", "AMBIG", [2, 5]), ("comparison_only", "alpha", "INV-ONLY", []),
                               ("register_only", "alpha", "REG-ONLY", [1]), ("comparison_only", "alpha", "SAME", []),
                               ("comparison_only", "alpha", "W1", []), ("register_only", "beta", "W1", [4])])
        self.assertFalse(any("received_on" in m for m in mismatches))

    def test_no_timing_proposal_confirms_or_qualifies(self):
        reqs = [request("REQ-1", "alpha", "E1", received_at="2030-01-03", determination_at="2030-01-04", extension="yes"),
                request("REQ-2", "alpha", "E2", received_at="bad")]
        items = self.items(reqs)[0]
        for item in items.values():
            self.assertTrue(all(item[k] is None for k in ("receipt_confirmed", "calendar_confirmed", "extension_notice_qualified",
                                                          "applicability_confirmed", "extension_notice_on", "calendar_start", "calendar_end", "holidays")))
            self.assertEqual((item["citations"], item["counterevidence"]), ([], []))
        before = snapshot(self.dir)
        self.assertEqual(self.run_cli(out=self.out_dir / "second.json")[0], 0)
        self.assertEqual({k: v for k, v in snapshot(self.dir).items() if not k.startswith("out/")}, {k: v for k, v in before.items() if not k.startswith("out/")})

    def test_missing_or_misplaced_register_is_refused_before_output(self):
        elsewhere = self.dir / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "comparison.json").write_text(self.comparison.read_text())
        self.assertEqual(self.run_cli(comparison=elsewhere / "comparison.json"), (1, "", "comparison_worksheet_register_unreadable\n"))
        (elsewhere / REG).symlink_to(self.dir / REG)
        self.assertEqual(self.run_cli(comparison=elsewhere / "comparison.json")[2], "comparison_worksheet_register_unreadable\n")
        (self.dir / REG).write_text('{"schema": "records-request-register-v1", "schema": "x", "requests": []}')
        self.assertEqual(self.run_cli()[2], "comparison_worksheet_register_unreadable\n")
        self.assertEqual(list(self.out_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
