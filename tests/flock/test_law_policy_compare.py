"""flock_law_policy compare STORE --on D [--types T,...]: agencies' governing policy facts grouped into value classes (RF2310-7)."""
import csv
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_law_policy.py"
SPEC = importlib.util.spec_from_file_location("_flock_facts_cmp", ROOT / "tools" / "flock_facts.py")
FACTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FACTS)
A, B, C = "Alpha Police Department", "Beta Sheriff", "Gamma City Police"
SA, SB, SC = "alpha-police-department", "beta-sheriff", "gamma-city-police"
PLANTED, ON = "SENTINEL-QUOTE-91", "2025-06-01"
BANNED = re.compile(r"\b(late|overdue)\b|violat|unlawful|illegal|non-?complian|breach|\bcomplian", re.I)
POLICY_HEADER = ["policy_row_id", "agency", "policy_id", "version_label", "doc_sha256", "footer_version_date",
                 "adoption_evidence", "adoption_sha256", "governs_from", "governs_to", "added_by", "added_at", "status"]


def run(*args):
    done = subprocess.run([sys.executable, "-B", str(TOOL), *args], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout, done.stderr


def doc(label):
    return hashlib.sha256(label.encode()).hexdigest()


def policy(agency, label, start, end="", evidence="Synthetic minutes adopt policy"):
    key = (FACTS.slugify(agency), "P", label, doc(label))
    rid = "P-" + hashlib.sha256("\x1f".join(key).encode()).hexdigest()[:12]
    return [rid, agency, "P", label, doc(label), "2019-12-01", evidence, "f" * 64 if evidence != "unverified" else "",
            start if evidence != "unverified" else "", end, "tester", "2026-10-30", "unreviewed"]


def fact(agency, label, fact_type, value, status="unreviewed", page="1"):
    r = dict(agency=agency, request="R-1", fact_type=fact_type, value=value, source_sha256=doc(label), page=page,
             verbatim_quote=f"{PLANTED} {value}", kind="policy", effective_from="", effective_to="", added_by="tester",
             added_at="2026-10-30", status=status)
    r["fact_id"] = FACTS.fact_id(r)
    return r


def footer(a=3, g=3, u=0, t=1, x=0, y=0, d=0, r=0):
    return (f"agencies={a} governed={g} ungoverned={u} types={t} differ={x} agree={y} disputed_skipped={d} unreadable_skipped={r}"
            " | ids only, research view, not a legal conclusion")


class CompareTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "store"
        self.store.mkdir()

    def write(self, slug, name, rows, header):
        path = self.store / "agencies" / slug / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows([header, *rows])
        return path

    def agency(self, name, rows, facts=()):
        slug = FACTS.slugify(name)
        self.write(slug, "POLICY.csv", rows, POLICY_HEADER)
        self.write(slug, "FACTS.csv", [[f[k] for k in FACTS.V2] for f in facts], FACTS.V2)

    def one(self, name, label, fact_type, value):  # an agency governed on ON by one version carrying one fact
        f = fact(name, label, fact_type, value)
        self.agency(name, [policy(name, label, "2025-01-01")], [f])
        return f["fact_id"]

    def snapshot(self):
        return sorted((str(p), p.read_bytes() if p.is_file() and not p.is_symlink() else b"", p.lstat().st_mtime_ns)
                      for p in self.store.rglob("*"))

    def compare(self, *extra, on=ON):  # (exit, stdout lines, stderr); the store is never written
        before = self.snapshot()
        code, out, err = run("compare", str(self.store), "--on", on, *extra)
        self.assertEqual(self.snapshot(), before, "store changed")
        for text in (out, err):
            self.assertIsNone(BANNED.search(text), text)
        return code, out.splitlines(), err

    def test_same_value_written_differently_is_one_class_agree(self):
        ids = [self.one(A, "a1", "retention", "30 days"), self.one(B, "b1", "retention", "30  Days"),
               self.one(C, "c1", "retention", " 30 DAYS ")]
        self.assertEqual(self.compare(), (0, [
            f"compare | retention | class 1: {SA} ({ids[0]}), {SB} ({ids[1]}), {SC} ({ids[2]}) | agree", footer(y=1)], ""))

    def test_two_values_differ_and_class_numbering_follows_first_slug(self):
        ia, ib, ic = self.one(A, "a1", "retention", "30 days"), self.one(B, "b1", "retention", "90 days"), \
            self.one(C, "c1", "retention", "30 days")
        self.assertEqual(self.compare(), (3, [
            f"compare | retention | class 1: {SA} ({ia}), {SC} ({ic}) | class 2: {SB} ({ib}) | differ", footer(x=1)], ""))
        self.store = self.store.parent / "renamed"  # the 90-day agency now sorts first: it becomes class 1
        self.store.mkdir()
        i0, i1 = self.one("Aardvark Sheriff", "z1", "retention", "90 days"), self.one(B, "b1", "retention", "30 days")
        self.assertEqual(self.compare(), (3, [
            f"compare | retention | class 1: aardvark-sheriff ({i0}) | class 2: {SB} ({i1}) | differ", footer(a=2, g=2, x=1)], ""))

    def test_single_documenting_agency_is_neither_agree_nor_differ(self):
        ia = self.one(A, "a1", "retention", "30 days")
        self.agency(B, [policy(B, "b1", "2025-01-01")])
        self.assertEqual(self.compare(), (0, [f"compare | retention | class 1: {SA} ({ia}) | undocumented: {SB}",
                                              footer(a=2, g=2)], ""))
        ic = self.one(C, "c1", "retention", "30 days")  # one class of two, but beta still undocumented: no agree
        self.assertEqual(self.compare()[1][0], f"compare | retention | class 1: {SA} ({ia}), {SC} ({ic}) | undocumented: {SB}")

    def test_unverified_only_and_closed_window_are_ungoverned_and_undocumented(self):
        hidden = [fact(A, "a1", "retention", "30 days"), fact(B, "b1", "retention", "30 days")]
        self.agency(A, [policy(A, "a1", "", evidence="unverified")], hidden[:1])
        self.agency(B, [policy(B, "b1", "2020-01-01", "2025-05-31")], hidden[1:])
        ic = self.one(C, "c1", "retention", "30 days")
        code, out, _ = self.compare()
        self.assertEqual((code, out), (0, [f"compare | {SA} | no governing adopted policy on {ON}",
                                           f"compare | {SB} | no governing adopted policy on {ON}",
                                           f"compare | retention | class 1: {SC} ({ic}) | undocumented: {SA}, {SB}",
                                           footer(g=1, u=2)]))
        for f in hidden:
            self.assertNotIn(f["fact_id"], "\n".join(out))
        code, out, _ = self.compare(on="2025-05-31")  # governs_to is inclusive
        self.assertEqual((code, out[-1]), (0, footer(g=2, u=1, y=0)))
        self.assertIn(f"class 1: {SB} ({hidden[1]['fact_id']}), {SC} ({ic})", out[1])

    def test_disputed_counted_not_used_and_non_governing_version_ignored(self):
        old, gone = fact(A, "a1", "retention", "90 days"), fact(A, "a2", "retention", "90 days", status="disputed")
        keep = fact(A, "a2", "retention", "30 days", page="2")
        self.agency(A, [policy(A, "a1", "2020-01-01", "2024-12-31"), policy(A, "a2", "2025-01-01")], [old, gone, keep])
        ib = self.one(B, "b1", "retention", "30 days")
        code, out, _ = self.compare()
        self.assertEqual((code, out), (0, [f"compare | retention | class 1: {SA} ({keep['fact_id']}), {SB} ({ib}) | agree",
                                           footer(a=2, g=2, y=1, d=1)]))
        self.assertNotIn(old["fact_id"], "\n".join(out))
        self.assertNotIn(gone["fact_id"], "\n".join(out))

    def test_two_facts_of_one_type_key_on_sorted_tuple_and_show_both_ids(self):
        fa = [fact(A, "a1", "partner", "County"), fact(A, "a1", "partner", "State", page="2")]
        fb = [fact(B, "b1", "partner", "state"), fact(B, "b1", "partner", "county", page="2")]
        self.agency(A, [policy(A, "a1", "2025-01-01")], fa)
        self.agency(B, [policy(B, "b1", "2025-01-01")], fb)
        ic = self.one(C, "c1", "partner", "county")
        ids = lambda rows: ";".join(sorted(r["fact_id"] for r in rows))  # noqa: E731
        self.assertEqual(self.compare(), (3, [
            f"compare | partner | class 1: {SA} ({ids(fa)}), {SB} ({ids(fb)}) | class 2: {SC} ({ic}) | differ", footer(x=1)], ""))

    def test_values_and_quotes_never_printed(self):
        self.one(A, "a1", "retention", "VALUE-SENTINEL-1")
        self.one(B, "b1", "retention", "VALUE-SENTINEL-2")
        code, out, err = self.compare()
        self.assertEqual((code, out[-1]), (3, footer(a=2, g=2, x=1)))
        bad = fact(C, "c1", "retention", "VALUE-SENTINEL-3")
        bad["fact_id"] = "F-000000000000"
        self.agency(C, [policy(C, "c1", "2025-01-01")], [bad])
        code2, out2, err2 = self.compare()
        self.assertEqual((code2, out2), (1, []))
        for text in ("\n".join(out), err, err2):
            for leak in ("VALUE-SENTINEL", PLANTED, doc("a1"), "f" * 64, "Synthetic minutes", str(self.store)):
                self.assertNotIn(leak, text)

    def test_types_filter_and_unknown_type(self):
        ir = self.one(A, "a1", "retention", "30 days")
        self.agency(B, [policy(B, "b1", "2025-01-01")], [fact(B, "b1", "sharing", "County")])
        self.agency(C, [policy(C, "c1", "2025-01-01", evidence="unverified")])
        want = [f"compare | {SC} | no governing adopted policy on {ON}",
                f"compare | retention | class 1: {SA} ({ir}) | undocumented: {SB}, {SC}",
                f"compare | unknown_type | undocumented: {SA}, {SB}, {SC}", footer(g=2, u=1, t=2)]
        self.assertEqual(self.compare("--types", "retention,unknown_type"), (0, want, ""))
        self.assertEqual(self.compare("--types", " unknown_type , retention,retention")[1], want)
        self.assertEqual(self.compare()[1][-1], footer(g=2, u=1, t=2))  # no filter: retention and sharing

    def test_usage_errors(self):
        self.one(A, "a1", "retention", "30 days")
        s, policy_csv = str(self.store), str(self.store / "agencies" / SA / "POLICY.csv")
        for args in ([], [s], [s, "--on"], [s, "--on", "2026-13-01"], [s, "--on", "2026-1-01"], [policy_csv, "--on", ON],
                     [str(self.store / "missing"), "--on", ON], [s, "--on", ON, "--types", ""],
                     [s, "--on", ON, "--types", "a,,b"], [s, "--on", ON, "--types", ",".join(f"t{i}" for i in range(21))],
                     [s, "--on", ON, "--types", "x" * 61], [s, "--on", ON, "--on", ON], [s, "--on", ON, "--agency", SA],
                     [s, "--types", "retention"], [s, "--on", ON, "--types"], [s, "--on", ON, "--types", "a", "x"]):
            code, out, err = run("compare", *args)
            self.assertEqual((code, out), (2, ""), args)
            self.assertTrue(err.startswith("usage:"), args)
        ok = [s, "--on", ON, "--types", ",".join(["x" * 60] + [f"t{i}" for i in range(19)])]
        self.assertEqual(run("compare", *ok)[0], 0)
        self.assertEqual(run("compare", s, "--types", "retention", "--on", ON)[0], 0)  # either option order

    def test_policy_link_and_bad_facts_go_to_stderr_only(self):
        self.one(A, "a1", "retention", "30 days")
        self.one(B, "b1", "retention", "30 days")
        target = self.store / "agencies" / SB / "FACTS.csv"
        target.write_text("not,a,facts,header\n", encoding="utf-8")
        code, out, err = self.compare()
        self.assertEqual((code, out), (1, []))
        self.assertTrue(err.startswith(f"{SB}: FACTS.csv row 1:"), err)
        self.one(B, "b1", "retention", "30 days")
        link = self.store / "agencies" / SB / "POLICY.csv"
        link.rename(self.store.parent / "POLICY.csv")
        os.symlink(self.store.parent / "POLICY.csv", link)
        self.assertEqual(self.compare(), (1, [], f"{SB}: POLICY.csv is a link\n"))

    def test_help_lists_compare(self):
        code, out, _ = run("--help")
        self.assertEqual((code, "compare STORE --on YYYY-MM-DD [--types T1,T2]" in out), (0, True))


if __name__ == "__main__":
    unittest.main()
