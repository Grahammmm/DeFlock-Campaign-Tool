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
        report = c.compare([agency("alpha", requests=[{"request_id": "REQ-1"}]), agency("beta-no-facts")], run_id="syn-run", as_of="2026-04-30")
        self.comparison = self.dir / "comparison.json"
        self.comparison.write_text(json.dumps(report))

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
        self.assertEqual(doc["requests"], {"items": [{"agency_id": "alpha", "request_id": "REQ-1"}], "mismatches": []})
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


if __name__ == "__main__":
    unittest.main()
