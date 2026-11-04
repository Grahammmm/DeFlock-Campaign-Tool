"""BF-df-historical: tools/historical_coverage.py, earliest evidenced dates per agency, gaps shown as gaps, no inferred date. Synthetic store."""
import ast
import contextlib
import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import legal_claims
from campaign_tool.records import public_scan

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "historical_coverage.py"
SCHEMA = json.loads((ROOT / "schemas" / "historical-coverage-v1.json").read_text(encoding="utf-8"))
V2 = ["agency", "request", "fact_type", "value", "source_sha256", "page", "verbatim_quote", "fact_id", "kind",
      "effective_from", "effective_to", "added_by", "added_at", "status"]
PH = ["policy_row_id", "agency", "policy_id", "version_label", "doc_sha256", "footer_version_date", "adoption_evidence",
      "adoption_sha256", "governs_from", "governs_to", "added_by", "added_at", "status"]
A, B, C, D, E, F, G = ("%x" % n * 64 for n in range(1, 8))
PLANTED = ("clerk@city.example.gov", "(805) 555-1234", "7ABC123", "Pat Example", "planted sentence about cameras")
BANNED = re.compile(r"violat|unlawful|illegal|non-?complian|breach", re.I)


def load_tool():
    spec = importlib.util.spec_from_file_location("_hc_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MOD = load_tool()
FACTS = MOD._module("flock_facts.py")
POLICY = MOD._module("flock_law_policy.py")


def fact(agency, ftype, value, sha, page, start="", quote=None):
    row = dict(zip(V2, [agency, "REQ-1", ftype, value, sha, str(page), PLANTED[4] if quote is None else quote, "", "record",
                        start, "", "tester", "2026-01-01", "checked"]))
    row["fact_id"] = FACTS.fact_id(row)
    return [row[k] for k in V2]


def policy(agency, doc, start, evidence="verified"):
    row = dict(zip(PH, ["", agency, "ALPR-1", "v1", doc, "2026-01-01", evidence, "b" * 64, start, "", "tester", "2026-01-01", "checked"]))
    row["policy_row_id"] = POLICY.derived_id(row)
    return [row[k] for k in PH]


def write_csv(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        csv.writer(handle, lineterminator="\n").writerows([header, *rows])


def store_tree(root):
    seen = {}
    for p in sorted(Path(root).rglob("*")):
        seen[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir"
    return seen


def check(node, schema, root=SCHEMA):
    """A small subset validator: enough for the keywords the schema file uses. Returns the list of problems."""
    if "$ref" in schema:
        schema = root["$defs"][schema["$ref"].rsplit("/", 1)[1]]
    if "oneOf" in schema:
        return [] if sum(not check(node, s, root) for s in schema["oneOf"]) == 1 else ["oneOf"]
    if "anyOf" in schema:
        return [] if any(not check(node, s, root) for s in schema["anyOf"]) else ["anyOf"]
    kinds = {"object": dict, "array": list, "string": str, "integer": int, "null": type(None)}
    want = schema.get("type")
    if want and not any(isinstance(node, kinds[t]) for t in ([want] if isinstance(want, str) else want)):
        return ["type %r" % (want,)]
    if "const" in schema and node != schema["const"]:
        return ["const"]
    if "enum" in schema and node not in schema["enum"]:
        return ["enum"]
    if "pattern" in schema and isinstance(node, str) and not re.search(schema["pattern"], node):
        return ["pattern"]
    out = []
    if isinstance(node, dict):
        out += ["missing " + k for k in schema.get("required", []) if k not in node]
        if schema.get("additionalProperties") is False:
            out += ["extra " + k for k in node if k not in schema.get("properties", {})]
        for k, sub in schema.get("properties", {}).items():
            out += check(node[k], sub, root) if k in node else []
    if isinstance(node, list) and "items" in schema:
        for item in node:
            out += check(item, schema["items"], root)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.store, self.work = self.tmp / "store", self.tmp / "work"
        self.work.mkdir()
        self.agencies = self.store / "agencies"
        self.agencies.mkdir(parents=True)
        self.fixture()

    def put(self, slug, facts=None, policies=None):
        if facts is not None:
            write_csv(self.agencies / slug / "FACTS.csv", V2, facts)
        if policies is not None:
            write_csv(self.agencies / slug / "POLICY.csv", PH, policies)

    def fixture(self):
        # alpha-city: all three evidenced, installation also in a later document and policy in a second row
        self.put("alpha-city", [fact("alpha-city", "installation", "in operation", A, 1, "2010-05-01"),
                                fact("alpha-city", "installation", "in operation", C, 4, "2012-01-01"),
                                fact("alpha-city", "vendor", "contract", B, 2, "2011-03-01"),
                                fact("alpha-city", "retention_days", "365", C, 3, "2013-01-01")],
                 [policy("alpha-city", D, "2012-06-01")])
        # beta-town: installation only
        self.put("beta-town", [fact("beta-town", "installation", "in operation", A, 1, "2015-01-15")], [])
        # gamma-ville: its only document has no extractable text
        self.put("gamma-ville", [fact("gamma-ville", "document", "UNREADABLE", E, 1, "", "")])
        # delta-county: an unreadable vendor-typed row, an installation row with no date
        self.put("delta-county", [fact("delta-county", "vendor", "UNREADABLE", F, 1, "", ""),
                                  fact("delta-county", "installation", "in operation", G, 2, "")])
        # echo-park: no documents at all, in the same county as alpha-city
        (self.agencies / "echo-park").mkdir()

    def build(self, *extra, out="out"):
        target = self.work / out
        err, text = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(text):
            code = MOD.main(["build", "--store", str(self.store), "--out", str(target), *extra])
        return code, text.getvalue(), err.getvalue(), target

    def report(self, *extra, out="out"):
        code, _, _, target = self.build(*extra, out=out)
        return code, json.loads((target / "historical-coverage.json").read_text(encoding="utf-8")), target

    def dims(self, doc, slug):
        return next(a for a in doc["agencies"] if a["agency"] == slug)


class FiveAgencies(Base):
    def test_all_three_evidenced(self):
        code, doc, _ = self.report()
        self.assertEqual(code, 0)
        row = self.dims(doc, "alpha-city")["dimensions"]
        self.assertEqual({k: (v["status"], v["date"], v["basis"]) for k, v in row.items()},
                         {"installation": ("evidenced", "2010-05-01", "document_date"),
                          "vendor": ("evidenced", "2011-03-01", "contract_term_start"),
                          "policy": ("evidenced", "2012-06-01", "in_force_statement")})
        self.assertEqual((row["installation"]["source_sha256"], row["installation"]["page"]), (A, 1))
        self.assertEqual(row["policy"]["source_sha256"], D)
        self.assertEqual(self.dims(doc, "alpha-city")["gaps"], [])

    def test_installation_without_policy_document(self):
        _, doc, _ = self.report()
        row = self.dims(doc, "beta-town")["dimensions"]
        self.assertEqual(row["installation"]["date"], "2015-01-15")
        self.assertEqual(row["policy"], {"status": "unevidenced", "reason": "no-document"})
        self.assertEqual(row["vendor"], {"status": "unevidenced", "reason": "no-document"})

    def test_only_document_has_no_text(self):
        _, doc, _ = self.report()
        for dim in self.dims(doc, "gamma-ville")["dimensions"].values():
            self.assertEqual(dim, {"status": "unevidenced", "reason": "no-text"})

    def test_unreadable_and_undated_are_distinct(self):
        _, doc, _ = self.report()
        row = self.dims(doc, "delta-county")["dimensions"]
        self.assertEqual(row["vendor"]["reason"], "document-unreadable")
        self.assertEqual(row["installation"]["reason"], "no-date-in-document")

    def test_no_documents_at_all(self):
        _, doc, _ = self.report()
        row = self.dims(doc, "echo-park")
        self.assertEqual({d["reason"] for d in row["dimensions"].values()}, {"no-document"})
        self.assertEqual(row["gaps"], [])

    def test_rows_files_status_line_and_totals(self):
        code, out, _, target = self.build()
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "historical | agencies=5 evidenced=1 partial=1 unevidenced=3 no-text=2 | "
                                      "private; review-required; not a legal finding")
        doc = json.loads((target / "historical-coverage.json").read_text(encoding="utf-8"))
        self.assertEqual(len(doc["agencies"]), 5)
        self.assertTrue(all(sorted(a["dimensions"]) == ["installation", "policy", "vendor"] for a in doc["agencies"]))
        self.assertEqual(check(doc, SCHEMA), [])
        self.assertTrue((target / "historical-coverage-report.md").read_text(encoding="utf-8").endswith(out))

    def test_four_reasons_are_distinct(self):
        _, doc, _ = self.report()
        reasons = {d["reason"] for a in doc["agencies"] for d in a["dimensions"].values() if d["status"] == "unevidenced"}
        self.assertEqual(reasons, {"no-document", "no-date-in-document", "no-text", "document-unreadable"})


class NoInference(Base):
    def held_hashes(self):
        found = set()
        for csv_file in self.agencies.rglob("*.csv"):
            found |= set(re.findall(r"[0-9a-f]{64}", csv_file.read_text(encoding="utf-8")))
        return found

    def test_every_date_traces_to_a_held_hash(self):
        _, doc, _ = self.report("--as-of", "2020-01-01")
        held, seen = self.held_hashes(), 0
        for row in doc["agencies"]:
            evidenced = {d["date"] for d in row["dimensions"].values() if d["status"] == "evidenced"}
            for dim in row["dimensions"].values():
                for cite in ([dim] + dim.get("corroborating", [])) if dim["status"] == "evidenced" else []:
                    self.assertIn(cite["source_sha256"], held)
                    self.assertEqual(cite["document_id"], cite["source_sha256"])
                    seen += 1
            for gap in row["gaps"]:  # a gap starts the period after this agency's own earliest evidence and never earlier
                self.assertTrue(evidenced)
                self.assertGreater(date.fromisoformat(gap["from"]), date.fromisoformat(min(evidenced)))
        self.assertEqual(seen, 5)

    def test_cross_agency_files_are_never_read(self):
        opened = []
        real = open

        def spy(path, *args, **kwargs):
            opened.append(str(path))
            return real(path, *args, **kwargs)

        recorded = []
        with mock.patch("builtins.open", spy):
            row, _ = MOD.build_agency(self.store, "alpha-city", recorded.append)
        prefix = str(self.agencies / "alpha-city") + os.sep
        self.assertTrue(opened)
        self.assertTrue(all(p.startswith(prefix) for p in opened), opened)
        self.assertEqual(sorted(p.name for p in recorded), ["FACTS.csv", "POLICY.csv"])
        self.assertEqual(row["dimensions"]["installation"]["status"], "evidenced")
        row, _ = MOD.build_agency(self.store, "echo-park")
        self.assertEqual({d["status"] for d in row["dimensions"].values()}, {"unevidenced"})

    def test_earliest_wins_and_later_documents_corroborate(self):
        _, doc, _ = self.report()
        inst = self.dims(doc, "alpha-city")["dimensions"]["installation"]
        self.assertEqual((inst["date"], inst["source_sha256"]), ("2010-05-01", A))
        self.assertEqual([(c["date"], c["source_sha256"], c["page"]) for c in inst["corroborating"]], [("2012-01-01", C, 4)])
        self.put("alpha-city", [fact("alpha-city", "installation", "in operation", C, 4, "2012-01-01"),
                                fact("alpha-city", "installation", "in operation", A, 1, "2010-05-01")], [])
        _, again, _ = self.report(out="again")
        self.assertEqual(self.dims(again, "alpha-city")["dimensions"]["installation"], inst)

    def test_partial_dates_stay_at_their_precision(self):
        self.put("foxtrot-bay", [fact("foxtrot-bay", "installation", "in operation", A, 1, "2009"),
                                 fact("foxtrot-bay", "vendor", "contract", B, 2, "2011-03")],
                 [policy("foxtrot-bay", D, "2012-06-01")])
        _, doc, target = self.report("--agency", "foxtrot-bay", "--agency", "beta-town")
        self.assertIn("| vendor | evidenced | 2011-03 |", (target / "historical-coverage-report.md").read_text(encoding="utf-8"))
        row = self.dims(doc, "foxtrot-bay")
        self.assertEqual([row["dimensions"][k]["date"] for k in ("installation", "vendor")], ["2009", "2011-03"])
        self.assertEqual(row["sequence"]["interval_days"], None)
        self.assertEqual(row["sequence"]["installation"], "2009")
        self.assertEqual(doc["as_of"], "2015-01-15")
        self.assertEqual(row["gaps"], [])
        self.put("golf-bay", [fact("golf-bay", "installation", "in operation", A, 1, "2009")], [])
        _, doc, _ = self.report("--agency", "golf-bay", "--as-of", "2012-03-04", out="golf")
        self.assertEqual(self.dims(doc, "golf-bay")["gaps"][0], {"from": "2010", "to": "2012-03-04", "dimension": "policy",
                                                                 "reason": "no-document"})

    def test_invalid_rows_are_not_evidence(self):
        bad = fact("alpha-city", "installation", "in operation", "z" * 64, 1, "2001-01-01")
        self.put("alpha-city", [bad, fact("alpha-city", "installation", "in operation", A, 1, "2010-05-01")], [])
        code, doc, _ = self.report()
        self.assertEqual(code, 1)
        row = self.dims(doc, "alpha-city")
        self.assertEqual(row["dimensions"]["installation"]["date"], "2010-05-01")
        self.assertTrue(any("source_sha256" in e for e in row["errors"]))

    def test_unverified_policy_is_not_evidence(self):
        self.put("beta-town", None, [policy("beta-town", D, "", "unverified")])
        _, doc, _ = self.report()
        self.assertEqual(self.dims(doc, "beta-town")["dimensions"]["policy"], {"status": "unevidenced", "reason": "no-date-in-document"})


class Sequence(Base):
    def test_installation_before_policy(self):
        _, doc, target = self.report()
        seq = self.dims(doc, "alpha-city")["sequence"]
        self.assertEqual(seq["interval_days"], (date(2012, 6, 1) - date(2010, 5, 1)).days)
        self.assertEqual((seq["installation"], seq["policy"]), ("2010-05-01", "2012-06-01"))
        self.assertIsNone(self.dims(doc, "beta-town")["sequence"])
        text = (target / "historical-coverage-report.md").read_text(encoding="utf-8")
        self.assertIn("### Dates in sequence", text)
        self.assertIn("These are dates from documents the campaign holds; this is not a claim that any agency violated a law.", text)
        self.assertIn("762 days apart", text)

    def test_no_generated_sentence_characterises_the_interval(self):
        _, doc, target = self.report("--as-of", "2020-01-01")
        fixed = MOD.SEQUENCE_SENTENCE
        generated = (target / "historical-coverage-report.md").read_text(encoding="utf-8").replace(fixed, "")
        generated += json.dumps(doc).replace(fixed, "")
        self.assertIsNone(BANNED.search(generated))
        self.assertEqual(legal_claims.legal_claims(generated), [])
        self.assertTrue(set(MOD.BASIS.values()) <= {"document_date", "effective_date", "in_force_statement", "contract_term_start"})


class Gaps(Base):
    def test_gap_runs_from_day_after_earliest_to_as_of(self):
        _, doc, _ = self.report("--as-of", "2020-01-01")
        gaps = self.dims(doc, "beta-town")["gaps"]
        self.assertEqual(gaps, [{"from": "2015-01-16", "to": "2020-01-01", "dimension": "policy", "reason": "no-document"},
                                {"from": "2015-01-16", "to": "2020-01-01", "dimension": "vendor", "reason": "no-document"}])
        self.assertEqual(self.dims(doc, "gamma-ville")["gaps"], [])

    def test_default_as_of_is_latest_evidenced_date_without_a_clock(self):
        _, doc, _ = self.report()
        self.assertEqual(doc["as_of"], "2015-01-15")
        _, doc, _ = self.report("--agency", "alpha-city", out="one")
        self.assertEqual(doc["as_of"], "2012-06-01")
        _, doc, _ = self.report("--agency", "echo-park", out="none")
        self.assertIsNone(doc["as_of"])

    def test_as_of_before_the_evidence_gives_no_gap(self):
        _, doc, _ = self.report("--as-of", "2014-01-01")
        self.assertEqual(self.dims(doc, "beta-town")["gaps"], [])


class Privacy(Base):
    def test_planted_values_never_appear(self):
        self.put("alpha-city", [fact("alpha-city", "installation", PLANTED[0] + " " + PLANTED[3], A, 1, "2010-05-01",
                                     " ".join(PLANTED))], [])
        code, out, _, target = self.build()
        self.assertEqual(code, 0)
        for path in target.iterdir():
            text = path.read_text(encoding="utf-8")
            for secret in PLANTED + ("555", "7ABC"):
                self.assertNotIn(secret, text)
        scan = subprocess.run([sys.executable, "-B", str(ROOT / "tools" / "flock_privacy_scan.py"), str(target)],
                              capture_output=True, text=True, check=False, timeout=60)
        self.assertEqual(scan.returncode, 0, scan.stdout)
        self.assertIn("findings=0", scan.stdout)

    def test_stray_copy_fails_the_public_tree_check_and_names_it(self):
        _, _, target = self.report()
        repo = self.tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=60)
        for name in os.listdir(target):
            (repo / name).write_bytes((target / name).read_bytes())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = public_scan.main(["--root", str(repo)])
        self.assertEqual(code, 1)
        self.assertTrue({MOD.JSON_NAME, MOD.MD_NAME} <= public_scan.DENIED_NAMES)
        for name in os.listdir(target):
            self.assertIn(name, out.getvalue())


class Refusals(Base):
    def refused(self, out, *extra):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = MOD.main(["build", "--store", str(self.store), "--out", str(out), *extra])
        self.assertEqual(code, 2, err.getvalue())
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)
        self.assertFalse(os.path.lexists(out))

    def test_out_that_exists(self):
        (self.work / "there").mkdir()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(MOD.main(["build", "--store", str(self.store), "--out", str(self.work / "there")]), 2)
        self.assertEqual(os.listdir(self.work / "there"), [])

    def test_out_inside_repo_and_inside_store_agencies(self):
        self.refused(ROOT / "hc-never-created-out")
        self.refused(self.agencies / "alpha-city" / "out")

    def test_parent_symlink_and_relative_paths(self):
        (self.tmp / "real").mkdir()
        (self.tmp / "link").symlink_to(self.tmp / "real")
        self.refused(self.tmp / "link" / "out")
        self.assertEqual(os.listdir(self.tmp / "real"), [])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(MOD.main(["build", "--store", str(self.store), "--out", "relative"]), 2)
            self.assertEqual(MOD.main(["build", "--store", str(self.store), "--out", str(self.work / "x"), "--as-of", "2020-13-01"]), 2)
            self.assertEqual(MOD.main(["build", "--store", str(self.store), "--out", str(self.work / "x"), "--agency", "nowhere"]), 2)
            self.assertEqual(MOD.main(["build", "--store", str(self.store)]), 2)
            self.assertEqual(MOD.main([]), 2)
        self.assertFalse((self.work / "x").exists())

    def test_modes(self):
        _, _, target = self.report()
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        for path in target.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["historical-coverage-report.md", "historical-coverage.json"])

    def test_failed_source_exits_one_and_still_writes(self):
        (self.agencies / "echo-park" / "FACTS.csv").write_text("not,the,header\n", encoding="utf-8")
        code, out, _, target = self.build()
        self.assertEqual(code, 1)
        self.assertTrue((target / "historical-coverage.json").is_file())
        self.assertIn("FACTS.csv: header does not match FACTS.csv", (target / "historical-coverage-report.md").read_text(encoding="utf-8"))


class Store(Base):
    def test_store_is_byte_identical_after_every_run(self):
        before = store_tree(self.store)
        self.build()
        self.build("--json", out="two")
        self.build("--agency", "alpha-city", "--as-of", "2020-01-01", out="three")
        self.assertEqual(store_tree(self.store), before)

    def test_static_no_network_subprocess_site_or_store_write(self):
        tree = ast.parse(TOOL.read_text(encoding="utf-8"))
        names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertFalse(names & {"socket", "urllib", "http", "requests", "subprocess", "smtplib", "ssl", "ftplib"})
        text = TOOL.read_text(encoding="utf-8")
        self.assertNotIn("site_build", text)
        opens = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "open"]
        for call in opens:  # the only builtin open is a read of the store; the output goes through os.open O_EXCL
            self.assertTrue(len(call.args) < 2 and all(k.arg != "mode" for k in call.keywords))
        self.assertIn("O_EXCL | os.O_NOFOLLOW", text)


class Report(Base):
    def test_report_prints_the_saved_files(self):
        _, doc, target = self.report()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(MOD.main(["report", str(target)]), 0)
        self.assertEqual(out.getvalue(), (target / "historical-coverage-report.md").read_text(encoding="utf-8"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(MOD.main(["report", str(target), "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue()), doc)

    def test_report_refuses_a_folder_without_the_file(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(MOD.main(["report", str(self.work)]), 2)
        (self.work / "historical-coverage.json").write_text("[1]", encoding="utf-8")
        with contextlib.redirect_stderr(err):
            self.assertEqual(MOD.main(["report", str(self.work)]), 2)

    def test_help_runs(self):
        done = subprocess.run([sys.executable, "-B", str(TOOL), "--help"], capture_output=True, text=True, check=False, timeout=60)
        self.assertEqual(done.returncode, 0)
        self.assertIn("build", done.stdout)


if __name__ == "__main__":
    unittest.main()
