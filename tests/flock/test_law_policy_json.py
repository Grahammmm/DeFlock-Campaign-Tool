"""flock_law_policy governing|gaps|coverage|stale|review|changes ... --json: flock-law-policy-<report>-v1 (RF2020-3)."""
import contextlib
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_law_policy.py"
sys.path.insert(0, str(ROOT))
from campaign_tool import law  # noqa: E402


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LP, FACTS = module(TOOL, "_law_policy_json_t"), module(ROOT / "tools" / "flock_facts.py", "_facts_json_t")
RULES = sorted(law.load_package("us-ca")["rules"], key=lambda r: r["rule_id"])  # derived at test time, no fixed count


def hexof(word):  # 64-hex values are computed, never written as literals
    return hashlib.sha256(word.encode()).hexdigest()


DUTY, URL, ADDER, EVIDENCE, QUOTE = "PLANTED-DUTY-rf3", "https://oag.ca.gov/planted-url-rf3", "planted-adder-rf3", \
    "PLANTED-EVIDENCE-rf3", "PLANTED-QUOTE-rf3"
TSHA, ASHA = hexof("planted-text-rf3"), hexof("planted-adoption-rf3")
ON, TODAY, NOTE = "2025-06-01", "2026-11-02", "research view, not a legal conclusion"
A, B, G = "Alpha Police Department", "Beta Sheriff", "Gamma PD"
SA, SB, SG = "alpha-police-department", "beta-sheriff", "gamma-pd"


def run(*args):
    done = subprocess.run([sys.executable, "-B", str(TOOL), *args], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout, done.stderr


def law_row(rule, accessed="2026-10-01", status="checked", to=""):
    return {"rule_id": rule["rule_id"], "section": "Sec 1", "duty": DUTY, "official_url": URL, "text_sha256": TSHA,
            "accessed": accessed, "effective_from": rule["effective_from"], "effective_to": to, "review": rule["review"],
            "added_by": ADDER, "added_at": "2026-01-01", "status": status}


def doc(label):
    return hexof("planted-doc-" + label)


def pol(agency, label, start, end="", pid="P", status="checked", added="2026-01-01", unverified=False):
    r = {"agency": agency, "policy_id": pid, "version_label": label, "doc_sha256": doc(label), "footer_version_date": "2019-12-01",
         "adoption_evidence": "unverified" if unverified else EVIDENCE, "adoption_sha256": "" if unverified else ASHA,
         "governs_from": "" if unverified else start, "governs_to": end, "added_by": ADDER, "added_at": added, "status": status}
    return {"policy_row_id": LP.derived_id(r), **r}


def fact(agency, label, fact_type, value):
    r = dict(agency=agency, request="R-1", fact_type=fact_type, value=value, source_sha256=doc(label), page="1",
             verbatim_quote=f"{QUOTE} {value}", kind="policy", effective_from="", effective_to="", added_by=ADDER,
             added_at="2026-01-01", status="unreviewed")
    r["fact_id"] = FACTS.fact_id(r)
    return r


def kv(line):  # the k=v tokens of a count line
    return {k: int(v) if v.isdigit() else v for k, v in re.findall(r"(\w+)=(\S+)", line.split(" | ")[0])}


TEXT = {  # report: [(regex, kind)]; groups become the comparable tuple after the kind
    "governing": [(r"governing \| (\S+) \| (.+) \| (.+) \| governs (\S+)\.\.(\S+) \| policy_row (\S+)$", "governing"),
                  (r"governing \| (\S+) \| none on \S+ \(adopted versions exist for other dates\)$", "other_dates"),
                  (r"governing \| (\S+) \| none established \(adoption unverified for all (\d+) rows\)$", "unverified_only")],
    "gaps": [(r"(\S+) \| (.+) \| gap (\S+)\.\.(\S+)$", "gap"), (r"(\S+) \| (.+) \| closed (\S+) \(nothing governs on \S+\)$", "closed"),
             (r"(\S+) \| (.+) \| not established \((\d+) unverified\)$", "unestablished")],
    "coverage": [(r"coverage \| (\S+) \| no LAW row \| review (.+)$", "missing"),
                 (r"coverage \| (\S+) \| not in effect on \S+ \((\d+) rows\)$", "not_in_effect"),
                 (r"coverage \| (\S+) \| only disputed rows \((\d+)\)$", "disputed_only")],
    "stale": [(r"(stale|future) \| (\S+) \| accessed (\S+) \| (-?\d+) days \| (\w+) \| " + NOTE + "$", None)],
    "review": [(r"review \| law \| (\S+) \| (\w+) \| added (\S+) \((?:(\d+) days|future)\)$", "law"),
               (r"review \| policy \| (\S+) \| (\S+) \| (.+) \| (\w+) \| added (\S+) \((?:(\d+) days|future)\)$", "policy")],
    "changes": [(r"changes \| (\S+) \| (.+) \| (.+) -> (.+) \| no policy facts for (.+)$", "no_facts"),
                (r"changes \| (\S+) \| (.+) \| (.+) -> (.+) \| (added|removed) (\S+) (\S+)$", None),
                (r"changes \| (\S+) \| (.+) \| (.+) -> (.+) \| changed (\S+) (\S+) -> (\S+)$", "changed")]}


def from_text(report, line):
    for rx, kind in TEXT[report]:
        m = re.fullmatch(rx, line)
        if m:
            g = tuple(int(x) if x and re.fullmatch(r"-?\d+", x) else x for x in m.groups())
            return g if kind is None else (kind, *g)
    raise AssertionError(f"{report}: unparsed {line!r}")


def from_json(report, r):
    k, ids = r.get("kind"), lambda key: ";".join(r[key])
    return {"governing": lambda: (k, r["agency"]) + ((r["policy_id"], r["version_label"], r["governs_from"], r["governs_to"] or "open",
                                                     r["policy_row_id"]) if k == "governing" else (r["unverified"],) if r["unverified"] else ()),
            "gaps": lambda: (k, r["agency"], r["policy_id"]) + ((r["from"], r["to"]) if k == "gap" else (r["to"],) if k == "closed"
                                                                else (r["unverified"],)),
            "coverage": lambda: (k, r["rule_id"]) + ((r["review"],) if k == "missing" else (r["rows"],)),
            "stale": lambda: (k, r["rule_id"], r["accessed"], r["age_days"], r["status"]),
            "review": lambda: ("law", r["rule_id"], r["status"], r["added_at"], r["age_days"]) if r["table"] == "law" else
            ("policy", r["agency"], r["policy_row_id"], f"{r['policy_id']} {r['version_label']}", r["status"], r["added_at"], r["age_days"]),
            "changes": lambda: ((k,) if k in ("no_facts", "changed") else ()) + (r["agency"], r["policy_id"], r["from_version"], r["to_version"]) + (
                (r["version"],) if k == "no_facts" else (k, r["fact_type"], ids("new_fact_ids" if k == "added" else "old_fact_ids"))
                if k in ("added", "removed") else (r["fact_type"], ids("old_fact_ids"), ids("new_fact_ids")))}[report]()


class LawPolicyJsonTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "store"
        self.store.mkdir()
        self.outputs = []

    def write(self, path, header, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="") as fh:
            csv.writer(fh, lineterminator="\n").writerows([header, *[[r[k] for k in header] for r in rows]])

    def build(self, law_rows, agencies):  # agencies: {name: (policy rows, facts)}
        self.write(self.store / "LAW.csv", LP.LAW_HEADER, law_rows)
        for name, (rows, facts) in agencies.items():
            self.write(self.store / "agencies" / LP.slug(name) / "POLICY.csv", LP.POLICY_HEADER, rows)
            if facts:
                self.write(self.store / "agencies" / LP.slug(name) / "FACTS.csv", FACTS.V2, facts)

    def findings_store(self):
        law_rows = [law_row(RULES[0], accessed="2020-01-01", status="disputed"),
                    law_row(RULES[1], accessed="2027-01-01", status="unreviewed", to=RULES[1]["effective_from"])]
        self.build(law_rows, {
            A: ([pol(A, "a1", "2024-01-01", "2024-03-31"), pol(A, "a2", "2024-06-01", status="unreviewed", added="2027-02-01"),
                 pol(A, "q1", "2020-01-01", "2020-12-31", pid="Q", status="disputed"), pol(A, "q2", "2021-01-01", pid="Q")],
                [fact(A, "a1", "retention", "30 days"), fact(A, "a2", "retention", "90 days"), fact(A, "a2", "sharing", "County")]),
            B: ([pol(B, "b1", "", unverified=True, status="unreviewed")], None),
            G: ([pol(G, "g1", "2023-01-01", "2024-12-31")], None)})

    def both(self, *args):  # text and --json runs of one report: same exit, empty stderr, one stable JSON line
        code, out, err = run(*args)
        jcode, jout, jerr = run(*args, "--json")
        self.assertEqual((jcode, err, jerr), (code, "", ""), args)
        self.assertEqual(run(*args, "--json")[1], jout, "not stable across runs")
        self.assertEqual((jout.count("\n"), jout.endswith("}\n")), (1, True), jout)
        got = json.loads(jout)
        self.assertEqual(jout, json.dumps(got, sort_keys=True, ensure_ascii=True) + "\n")
        self.assertEqual((got["schema"], got["note"]), (f"flock-law-policy-{args[0]}-v1", NOTE))
        lines = out.splitlines()
        self.outputs.append(jout)
        return code, got, lines

    def check_same(self, report, got, lines, n_tail=1):
        self.assertEqual([from_json(report, r) for r in got["rows"]], [from_text(report, x) for x in lines[:-n_tail]])
        text = kv(lines[-1])
        self.assertEqual({k: text[k] for k in got["counts"] if k in text}, {k: v for k, v in got["counts"].items() if k in text})

    def test_six_reports_rows_and_counts_match_text(self):
        self.findings_store()
        s = str(self.store)
        code, got, lines = self.both("governing", s, "--on", ON)
        self.assertEqual((code, got["on"], [r["kind"] for r in got["rows"]]), (0, ON, ["governing", "governing", "unverified_only", "other_dates"]))
        self.check_same("governing", got, lines, n_tail=2)
        live, total = map(int, re.fullmatch(r"law in effect on \S+: (\d+) of (\d+) rows", lines[-2]).groups())
        self.assertEqual((got["counts"]["law_in_effect"], got["counts"]["law_rows"], got["counts"]["agencies"]), (live, total, 3))
        code, got, lines = self.both("gaps", s, "--on", ON)
        self.assertEqual((code, [r["kind"] for r in got["rows"]]), (3, ["gap", "unestablished", "closed"]))
        self.assertEqual(got["rows"][0], {"agency": SA, "policy_id": "P", "kind": "gap", "from": "2024-04-01", "to": "2024-05-31",
                                          "unverified": None})
        self.check_same("gaps", got, lines)
        code, got, lines = self.both("coverage", s, "--on", "2099-01-01")
        kinds = {r["rule_id"]: r["kind"] for r in got["rows"] if r["kind"] != "missing"}
        self.assertEqual((code, got["on"], kinds), (3, "2099-01-01", {RULES[0]["rule_id"]: "disputed_only",
                                                                       RULES[1]["rule_id"]: "not_in_effect"}))
        self.assertEqual(got["counts"]["missing"], len(RULES) - 2)
        self.check_same("coverage", got, lines)
        self.assertIsNone(self.both("coverage", s)[1]["on"])
        code, got, lines = self.both("stale", s, "--max-age-days", "365", "--today", TODAY)
        self.assertEqual((code, got["today"], got["max_age_days"], [r["kind"] for r in got["rows"]]), (3, TODAY, 365, ["stale", "future"]))
        self.check_same("stale", got, lines)
        self.assertEqual((kv(lines[-1])["today"], kv(lines[-1])["max_age_days"]), (got["today"], got["max_age_days"]))
        code, got, lines = self.both("review", s, "--today", TODAY)
        self.assertEqual((code, got["today"], got["agency"], len(got["rows"])), (3, TODAY, None, 5))
        self.assertIn({"table": "policy", "agency": SA, "policy_row_id": pol(A, "a2", "2024-06-01")["policy_row_id"], "policy_id": "P",
                       "version_label": "a2", "status": "unreviewed", "added_at": "2027-02-01", "age_days": None}, got["rows"])
        self.check_same("review", got, lines)
        code, got, lines = self.both("review", s, "--agency", SB, "--today", TODAY)
        self.assertEqual((code, got["agency"], [r.get("agency") for r in got["rows"]]), (3, SB, [None, None, SB]))
        self.check_same("review", got, lines)
        code, got, lines = self.both("changes", s)
        self.assertEqual((code, [r["kind"] for r in got["rows"]]), (0, ["changed", "added", "no_facts", "no_facts"]))
        self.assertEqual((got["counts"]["pairs"], got["counts"]["changed_pairs"], got["counts"]["unknown_pairs"]), (2, 1, 1))
        self.check_same("changes", got, lines)
        code, got, lines = self.both("changes", s, "--agency", SG)
        self.assertEqual((code, got["agency"], got["rows"], got["counts"]["pairs"]), (0, SG, [], 0))
        for out in self.outputs:  # never a URL, hash, adder, duty, evidence, quote, fact value or store path
            for leak in (DUTY, URL, "https://", TSHA, ASHA, doc("a1"), doc("a2"), doc("b1"), ADDER, EVIDENCE, QUOTE, "30 days",
                         "90 days", "County", str(self.store)):
                self.assertNotIn(leak, out)

    def test_clean_store_rows_empty_counts_zero_exit_zero(self):
        self.build([law_row(r) for r in RULES], {A: ([pol(A, "a1", "2020-01-01")], None)})
        s, zero = str(self.store), {"gaps": ("gaps", "closed", "unestablished"), "coverage": ("missing", "not_in_effect", "disputed_only"),
                                    "stale": ("stale", "future"), "review": tuple(f"{k}_{x}" for k in ("law", "policy")
                                                                                  for x in ("unreviewed", "disputed"))}
        for args in (["gaps", s, "--on", ON], ["coverage", s, "--on", "2099-01-01"], ["stale", s, "--max-age-days", "365", "--today", TODAY],
                     ["review", s, "--today", TODAY]):
            code, got, lines = self.both(*args)
            self.assertEqual((code, got["rows"], len(lines)), (0, [], 1), args)
            self.assertEqual({k: got["counts"][k] for k in zero[args[0]]}, dict.fromkeys(zero[args[0]], 0), args)
        self.assertEqual(self.both("coverage", s)[1]["counts"]["covered"], len(RULES))

    def test_bad_policy_row_stderr_stdout_empty_exit_one(self):
        bad = pol(A, "a1", "2020-01-01")
        bad["doc_sha256"] = "not-hex"
        self.build([law_row(r) for r in RULES], {A: ([bad], None)})
        s = str(self.store)
        for args in (["governing", s, "--on", ON], ["gaps", s, "--on", ON], ["coverage", s], ["changes", s],
                     ["stale", s, "--max-age-days", "5"], ["review", s]):
            text, js = run(*args), run(*args, "--json")
            self.assertEqual(js, text, args)
            self.assertEqual(js[:2], (1, ""), args)
            self.assertIn(f"{SA}: row 2: doc_sha256 must be 64 lowercase hex", js[2], args)

    def test_json_misplaced_twice_or_on_other_modes_is_usage(self):
        self.findings_store()
        s = str(self.store)
        for args in (["governing", s, "--json", "--on", ON], ["gaps", s, "--on", "--json", ON], ["coverage", s, "--json", "--on", ON],
                     ["stale", s, "--json", "--max-age-days", "5"], ["review", s, "--json", "--agency", SA],
                     ["changes", s, "--json", "--agency", SA], ["governing", s, "--on", ON, "--json", "--json"],
                     ["coverage", s, "--json", "--json"], ["review", s, "--today", TODAY, "--json", "--json"],
                     ["changes", s, "--json", "--json"], ["review", s, "--agency", "--json"], ["governing", s, "--json"],
                     ["versions", s, "--agency", SA, "--json"], ["versions", s, "--agency", SA, "--on", ON, "--json"],
                     ["--check", s, "--json"], ["--json", "--check", s], ["compare", s, "--on", ON, "--json", "--json"]):
            code, out, err = run(*args)
            self.assertEqual((code, out), (2, ""), args)
            self.assertTrue(err.startswith("usage:"), args)
        usage = run("--help")[1]
        for form in ("governing STORE --on YYYY-MM-DD [--json]", "gaps STORE --on YYYY-MM-DD [--json]",
                     "coverage STORE [--on YYYY-MM-DD] [--json]", "stale STORE --max-age-days N [--today YYYY-MM-DD] [--json]",
                     "review STORE [--agency SLUG] [--today YYYY-MM-DD] [--json]", "changes STORE [--agency SLUG] [--json]"):
            self.assertIn(form, usage)

    def test_coverage_package_unreadable_json_stdout_empty(self):
        self.build([law_row(r) for r in RULES], {})
        real = law.load_package("us-ca")
        for effect in ([ValueError("boom")], [real, ValueError("boom")]):  # fails in load_all, or in the coverage branch
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(LP.law, "load_package", side_effect=effect), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = LP.main(["coverage", str(self.store), "--json"])
            self.assertEqual((code, out.getvalue(), err.getvalue()), (1, "", "row 1: law package unreadable\n"))


if __name__ == "__main__":
    unittest.main()
