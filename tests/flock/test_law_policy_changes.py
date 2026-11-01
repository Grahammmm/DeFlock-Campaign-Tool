"""flock_law_policy changes STORE [--agency SLUG]: policy-kind fact types that differ between consecutive adopted versions (DF1537-03)."""
import csv
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_law_policy.py"
SPEC = importlib.util.spec_from_file_location("_flock_facts_t", ROOT / "tools" / "flock_facts.py")
FACTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FACTS)
CITY, SLUG = "Sample City Police Department", "sample-city-police-department"
PLANTED = "SENTINEL-QUOTE-77"


def run(*args):
    done = subprocess.run([sys.executable, "-B", str(TOOL), *args], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout, done.stderr


def doc(label):
    return hashlib.sha256(label.encode()).hexdigest()


def policy(label, start, end="", pid="P", agency=CITY, evidence="Synthetic minutes adopt policy"):
    key = (FACTS.slugify(agency), pid, label, doc(label))
    rid = "P-" + hashlib.sha256("\x1f".join(key).encode()).hexdigest()[:12]
    return [rid, agency, pid, label, doc(label), "2019-12-01", evidence, "f" * 64 if evidence != "unverified" else "",
            start if evidence != "unverified" else "", end, "tester", "2026-10-30", "unreviewed"]


def fact(label, fact_type, value, kind="policy", status="unreviewed", page="1", agency=CITY):
    r = dict(agency=agency, request="R-1", fact_type=fact_type, value=value, source_sha256=doc(label), page=page,
             verbatim_quote=f"{PLANTED} {value}", kind=kind, effective_from="", effective_to="", added_by="tester",
             added_at="2026-10-30", status=status)
    r["fact_id"] = FACTS.fact_id(r)
    return r


def line(pair, item, pid="P", slug=SLUG):
    return f"changes | {slug} | {pid} | {pair} | {item}"


def counts(a=1, p=1, n=1, c=0, u=0, d=0):
    return f"agencies={a} policies={p} pairs={n} changed_pairs={c} unknown_pairs={u} disputed_skipped={d} | ids only, review-required"


class ChangesTest(unittest.TestCase):
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

    def agency(self, rows, facts=None, slug=SLUG):
        self.write(slug, "POLICY.csv", rows, [
            "policy_row_id", "agency", "policy_id", "version_label", "doc_sha256", "footer_version_date",
            "adoption_evidence", "adoption_sha256", "governs_from", "governs_to", "added_by", "added_at", "status"])
        if facts is not None:
            self.write(slug, "FACTS.csv", [[f[k] for k in FACTS.V2] for f in facts], FACTS.V2)

    def snapshot(self):
        return sorted((str(p), p.read_bytes() if p.is_file() and not p.is_symlink() else b"", p.lstat().st_mtime_ns)
                      for p in self.store.rglob("*"))

    def changes(self, *extra):  # (exit, stdout lines, stderr); the store is never written
        before = self.snapshot()
        code, out, err = run("changes", str(self.store), *extra)
        self.assertEqual(self.snapshot(), before, "store changed")
        return code, out.splitlines(), err

    def test_added_removed_changed_and_whitespace_case_only(self):
        r1, r2 = fact("v1", "retention_days", "30 days"), fact("v2", "retention_days", "90 days")
        a1, s2 = fact("v1", "audit_interval", "monthly"), fact("v2", "sharing_partner", "County")
        same = (fact("v1", "camera_count", "Ten  Cameras"), fact("v2", "camera_count", " ten cameras", page="2"))
        self.agency([policy("v1", "2020-01-01", "2020-12-31"), policy("v2", "2021-01-01")], [r1, r2, a1, s2, *same])
        self.assertEqual(self.changes(), (0, [
            line("v1 -> v2", f"removed audit_interval {a1['fact_id']}"),
            line("v1 -> v2", f"changed retention_days {r1['fact_id']} -> {r2['fact_id']}"),
            line("v1 -> v2", f"added sharing_partner {s2['fact_id']}"), counts(c=1)], ""))

    def test_equal_versions_print_only_counts(self):
        self.agency([policy("v1", "2020-01-01", "2020-12-31"), policy("v2", "2021-01-01")],
                    [fact("v1", "retention_days", "30"), fact("v2", "retention_days", "30")])
        self.assertEqual(self.changes(), (0, [counts()], ""))

    def test_three_versions_in_governs_from_order_rows_shuffled(self):
        self.agency([policy("v3", "2022-01-01"), policy("v1", "2020-01-01", "2020-12-31"), policy("v2", "2021-01-01", "2021-12-31")],
                    [fact("v3", "t", "c"), fact("v1", "t", "a"), fact("v2", "t", "b")])
        code, out, _ = self.changes()
        self.assertEqual((code, [x.split(" | ")[3] for x in out[:-1]], out[-1]), (0, ["v1 -> v2", "v2 -> v3"], counts(n=2, c=2)))

    def test_version_without_policy_facts_is_unknown(self):
        self.agency([policy("v1", "2020-01-01", "2020-12-31"), policy("v2", "2021-01-01")], [fact("v1", "t", "a")])
        self.assertEqual(self.changes(), (0, [line("v1 -> v2", "no policy facts for v2"), counts(u=1)], ""))
        self.agency([policy("v1", "2020-01-01", "2020-12-31"), policy("v2", "2021-01-01")])  # no FACTS.csv at all
        (self.store / "agencies" / SLUG / "FACTS.csv").unlink()
        self.assertEqual(self.changes(), (0, [line("v1 -> v2", "no policy facts for v1"),
                                              line("v1 -> v2", "no policy facts for v2"), counts(u=1)], ""))

    def test_unverified_middle_skipped_and_neighbours_compared(self):
        self.agency([policy("v1", "2020-01-01", "2020-12-31"), policy("mid", "", evidence="unverified"), policy("v2", "2021-01-01")],
                    [fact("v1", "t", "a"), fact("mid", "t", "z"), fact("v2", "t", "a")])
        self.assertEqual(self.changes(), (0, [counts()], ""))
        self.agency([policy("q1", "", evidence="unverified"), policy("q2", "", evidence="unverified")], [])
        self.assertEqual(self.changes(), (0, [counts(n=0)], ""))

    def test_disputed_excluded_and_record_kind_ignored(self):
        gone = fact("v2", "t", "b", status="disputed")
        self.agency([policy("v1", "2020-01-01", "2020-12-31"), policy("v2", "2021-01-01")],
                    [fact("v1", "t", "a"), fact("v2", "t", "a", page="3"), gone, fact("v2", "u", "x", kind="record")])
        code, out, _ = self.changes()
        self.assertEqual((code, out), (0, [counts(d=1)]))
        self.assertNotIn(gone["fact_id"], "\n".join(out))

    def test_same_type_twice_multiset(self):
        old = [fact("v1", "partner", "A"), fact("v1", "partner", "B")]
        new = [fact("v2", "partner", "b"), fact("v2", "partner", "a")]
        self.agency([policy("v1", "2020-01-01", "2020-12-31"), policy("v2", "2021-01-01")], old + new)
        self.assertEqual(self.changes(), (0, [counts()], ""))
        new[1] = fact("v2", "partner", "c")
        self.agency([policy("v1", "2020-01-01", "2020-12-31"), policy("v2", "2021-01-01")], old + new)
        ids = lambda rows: ";".join(sorted(r["fact_id"] for r in rows))  # noqa: E731
        self.assertEqual(self.changes(), (0, [line("v1 -> v2", f"changed partner {ids(old)} -> {ids(new)}"), counts(c=1)], ""))
        self.agency([policy("v1", "2020-01-01", "2020-12-31"), policy("v2", "2021-01-01")], old + new[:1])  # a duplicate dropped
        self.assertEqual(self.changes()[1][0], line("v1 -> v2", f"changed partner {ids(old)} -> {new[0]['fact_id']}"))

    def test_agency_filter_and_unknown_agency(self):
        other, oslug = "Beta Sheriff", "beta-sheriff"
        self.agency([policy("v1", "2020-01-01", "2020-12-31"), policy("v2", "2021-01-01")], [fact("v1", "t", "a"), fact("v2", "t", "b")])
        self.agency([policy("w1", "2020-01-01", "2020-12-31", "Q", other), policy("w2", "2021-01-01", "", "Q", other)],
                    [fact("w1", "t", "a", agency=other)], slug=oslug)
        code, out, _ = self.changes()
        self.assertEqual((code, len(out), out[-1]), (0, 3, counts(a=2, p=2, n=2, c=1, u=1)))
        code, out, _ = self.changes("--agency", oslug)
        self.assertEqual((code, out), (0, [line("w1 -> w2", "no policy facts for w2", "Q", oslug), counts(u=1)]))
        self.assertEqual(self.changes("--agency", "nobody"), (0, [counts(a=0, p=0, n=0)], ""))
        self.write(oslug, "FACTS.csv", [["bad"]], FACTS.V2)  # out of scope under --agency: not read
        self.assertEqual(self.changes("--agency", SLUG)[0], 0)

    def test_symlinked_agencies_and_facts_refused(self):
        self.agency([policy("v1", "2020-01-01", "2020-12-31"), policy("v2", "2021-01-01")], [fact("v1", "t", "a")])
        real = self.store.parent / "real"
        (self.store / "agencies").rename(real)
        os.symlink(real, self.store / "agencies")
        code, out, err = self.changes()
        self.assertEqual((code, out), (1, []))
        self.assertIn("store: agencies is a link", err)
        (self.store / "agencies").unlink()
        real.rename(self.store / "agencies")
        target = self.store / "agencies" / SLUG / "FACTS.csv"
        target.rename(self.store.parent / "FACTS.csv")
        os.symlink(self.store.parent / "FACTS.csv", target)
        self.assertEqual(self.changes(), (1, [], f"{SLUG}: FACTS.csv is a link\n"))

    def test_malformed_facts_row_names_file_and_row(self):
        bad = fact("v2", "t", "b")
        bad["fact_id"] = "F-000000000000"
        self.agency([policy("v1", "2020-01-01", "2020-12-31"), policy("v2", "2021-01-01")], [fact("v1", "t", "a"), bad])
        self.assertEqual(self.changes(), (1, [], f"{SLUG}: FACTS.csv row 3: fact_id is invalid\n"))
        self.write(SLUG, "FACTS.csv", [], FACTS.V1)  # a v1 header is not accepted in a store folder
        code, out, err = self.changes()
        self.assertEqual((code, out), (1, []))
        self.assertIn(f"{SLUG}: FACTS.csv row 1:", err)
        (self.store / "agencies" / SLUG / "FACTS.csv").write_bytes(b"\xff\xfe\x00bad")
        self.assertEqual(self.changes(), (1, [], f"{SLUG}: FACTS.csv row 1: file unreadable (UnicodeDecodeError)\n"))

    def test_policy_load_error_prints_nothing_on_stdout(self):
        self.agency([policy("v1", "2020-01-01", "2021-06-30"), policy("v2", "2021-06-30")], [fact("v1", "t", "a")])
        code, out, err = self.changes()
        self.assertEqual((code, out), (1, []))
        self.assertIn(f"{SLUG}: row 3: governs_from overlaps row 2", err)

    def test_stdout_never_carries_values_or_quotes(self):
        self.agency([policy("v1", "2020-01-01", "2020-12-31"), policy("v2", "2021-01-01")],
                    [fact("v1", "retention", "VALUE-SENTINEL-1"), fact("v2", "retention", "VALUE-SENTINEL-2"),
                     fact("v2", "added_type", "VALUE-SENTINEL-3")])
        code, out, _ = self.changes()
        text = "\n".join(out)
        self.assertEqual((code, out[-1]), (0, counts(c=1)))
        for leak in ("VALUE-SENTINEL", PLANTED, doc("v1"), "f" * 64, "Synthetic minutes", str(self.store)):
            self.assertNotIn(leak, text)

    def test_usage_errors(self):
        self.agency([policy("v1", "2020-01-01")])
        for args in (["changes"], ["changes", str(self.store / "agencies" / SLUG / "POLICY.csv")],
                     ["changes", str(self.store / "missing")], ["changes", str(self.store), "--agency"],
                     ["changes", str(self.store), "--agency", "Sample City"], ["changes", str(self.store), "--agency", ""],
                     ["changes", str(self.store), "--on", SLUG], ["changes", str(self.store), "--agency", SLUG, "x"]):
            code, out, err = run(*args)
            self.assertEqual((code, out), (2, ""), args)
            self.assertTrue(err.startswith("usage:"), args)

    def test_help_lists_changes(self):
        code, out, _ = run("--help")
        self.assertEqual((code, "changes STORE [--agency SLUG]" in out), (0, True))


if __name__ == "__main__":
    unittest.main()
