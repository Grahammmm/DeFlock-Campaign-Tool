"""Offline tests for ``campaign_tool.law support-matrix`` and the committed docs/LAW-SUPPORT.md.

Synthetic packages under temporary directories only. The committed packages and
docs are read, never written. Run from the repository root:
python3 -B -m unittest tests.test_law_support_matrix -v
"""

import copy
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import law  # noqa: E402

DOC = REPO_ROOT / "docs" / "LAW-SUPPORT.md"
COMMAND = "python3 -m campaign_tool.law support-matrix --md > docs/LAW-SUPPORT.md"


def make_package(slug, rules, scopes, **changes):
    """Synthetic package for ``slug``; ``rules`` rows are (suffix, citation, from, to, review)."""
    package = copy.deepcopy(law.load_package("us-ca"))
    state = slug[3:]
    template = package["rules"][0]
    package["jurisdiction"] = slug
    package["rules"] = []
    for suffix, citation, start, end, review in rules:
        rule = copy.deepcopy(template)
        rule.update(rule_id=f"{state}-{suffix}", citation=citation, effective_from=start,
                    effective_to=end, review=review)
        package["rules"].append(rule)
    scope_template = package["request_scopes"][0]
    package["request_scopes"] = []
    for scope_id, suffixes in scopes:
        scope = copy.deepcopy(scope_template)
        scope.update(scope_id=scope_id, rule_ids=[f"{state}-{s}" for s in suffixes])
        package["request_scopes"].append(scope)
    package.update(changes)
    return package


RULES = (("b-open", "Synthetic Code 2", "2020-01-01", None, "verified"),
         ("a-historic", "Synthetic Code 1", "2010-01-01", "2015-12-31", "likely"),
         ("c-future", "Synthetic Code 3", "2999-01-01", None, "needs_attorney_review"),
         ("d-unused", "Synthetic Code 4", "2020-01-01", None, "likely"))
ONE_SCOPE = (("zeta", ("b-open",)),)
SCOPES = (("zeta", ("b-open", "a-historic")), ("alpha", ("b-open", "c-future")), ("empty", ()))


class Base:
    def __init__(self, testcase):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-law-matrix-")
        testcase.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)

    def write(self, slug, package):
        target = self.path / slug
        target.mkdir(exist_ok=True)
        (target / "package.json").write_text(json.dumps(package, indent=2), encoding="utf-8")
        return target / "package.json"


def run_cli(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool.law", "support-matrix", *args],
                          cwd=REPO_ROOT, capture_output=True)


class SupportMatrixTests(unittest.TestCase):
    def test_every_shipped_package_and_committed_doc_match(self):
        fresh = law.render_support_matrix(law.support_matrix()).encode("utf-8")
        self.assertEqual(DOC.read_bytes(), fresh)
        self.assertTrue(fresh.endswith(b"\n") and not fresh.endswith(b"\n\n") and b"\r" not in fresh)
        self.assertEqual(law.support_matrix(REPO_ROOT / "jurisdictions"), law.support_matrix())
        done = run_cli("--md")
        self.assertEqual((done.returncode, done.stderr), (0, b""))
        self.assertEqual(done.stdout, fresh)
        slugs = [p["jurisdiction"] for p in law.support_matrix()["packages"]]
        self.assertEqual(slugs, sorted(d.name for d in law.JURISDICTIONS.iterdir() if (d / "package.json").is_file()))
        self.assertIn(COMMAND, fresh.decode("utf-8"))
        docs = (REPO_ROOT / "docs" / "LAW-PACKAGES.md").read_text(encoding="utf-8")
        self.assertIn("(LAW-SUPPORT.md)", docs)
        self.assertIn(COMMAND, docs)

    def test_second_package_scope_mapping_and_all_windows(self):
        base = Base(self)
        base.write("us-wa", make_package("us-wa", RULES, SCOPES, status="reviewed",
                                         reviewed_by=["Synthetic Reviewer"], reviewed_at="2026-01-02"))
        base.write("us-ny", make_package("us-ny", RULES[:1], ONE_SCOPE))
        matrix = law.support_matrix(base.path)
        self.assertEqual(matrix["schema_version"], 1)
        self.assertEqual([p["jurisdiction"] for p in matrix["packages"]], ["us-ny", "us-wa"])
        wa = matrix["packages"][1]
        self.assertEqual((wa["status"], wa["reviewed_by"], wa["reviewed_at"]),
                         ("reviewed", ["Synthetic Reviewer"], "2026-01-02"))
        rows = {r["rule_id"]: r for r in wa["rules"]}
        self.assertEqual(list(rows), sorted(rows))
        self.assertEqual(rows["wa-b-open"]["request_scopes"], ["alpha", "zeta"])
        self.assertEqual(rows["wa-a-historic"]["request_scopes"], ["zeta"])
        self.assertEqual(rows["wa-c-future"]["request_scopes"], ["alpha"])
        self.assertEqual(rows["wa-d-unused"]["request_scopes"], [])
        self.assertEqual(rows["wa-a-historic"]["effective_to"], "2015-12-31")
        text = law.render_support_matrix(matrix)
        self.assertLess(text.index("## us-ny"), text.index("## us-wa"))
        self.assertIn("| wa-b-open | Synthetic Code 2 | 2020-01-01 | open | verified | alpha, zeta |", text)
        self.assertIn("| wa-a-historic | Synthetic Code 1 | 2010-01-01 | 2015-12-31 | likely | zeta |", text)
        self.assertIn("| wa-c-future | Synthetic Code 3 | 2999-01-01 | open | needs\\_attorney\\_review | alpha |", text)
        self.assertIn("| wa-d-unused | Synthetic Code 4 | 2020-01-01 | open | likely | — |", text)
        self.assertIn("- Package status: `reviewed`", text)
        self.assertIn("- Reviewed by (as recorded): Synthetic Reviewer", text)
        self.assertIn("- Reviewed at (as recorded): 2026-01-02", text)
        self.assertIn("- Package status: `draft`", text)
        self.assertIn("- Reviewed by (as recorded): none recorded", text)
        self.assertIn("- Determination period as encoded: 10 calendar days", text)
        self.assertIn("- One extension as encoded: up to 14 days", text)
        self.assertIn(law.HOLIDAY_NOTE, text)
        self.assertIn("not a production deadline", text)
        self.assertIn("send date is not proof of receipt", text)
        self.assertEqual(json.loads(run_cli("--base", str(base.path)).stdout), matrix)

    def test_matrix_drift_detects_each_data_domain(self):
        base = Base(self)
        original = make_package("us-wa", RULES, SCOPES)
        path = base.write("us-wa", original)
        before = law.render_support_matrix(law.support_matrix(base.path))
        expected = base.path / "expected.md"
        expected.write_text(before, encoding="utf-8")

        def change_rule(key, value):
            def mutate(package):
                package["rules"][0][key] = value
            return mutate

        def change_scope(package):
            package["request_scopes"][2]["rule_ids"] = [package["rules"][3]["rule_id"]]

        def change_records_law(key, value):
            def mutate(package):
                package["records_law"][key] = value
            return mutate

        def promote(package):
            package.update(status="reviewed", reviewed_by=["Synthetic Reviewer"], reviewed_at="2026-01-02")

        mutations = {
            "citation": change_rule("citation", "Synthetic Code 99"),
            "effective_from": change_rule("effective_from", "2021-02-03"),
            "effective_to": change_rule("effective_to", "2030-01-01"),
            "review": change_rule("review", "likely"),
            "scope": change_scope,
            "determination_days": change_records_law("determination_days", 30),
            "extension_days": change_records_law("determination_extension_days", 7),
            "day_type": change_records_law("day_type", "business"),
            "records_citation": change_records_law("citation", "Synthetic Records Act § 1"),
            "source": change_records_law("sources", [{"title": "Other", "url": "https://example.test/x",
                                                       "accessed": "2026-01-01"}]),
            "status": promote,
        }
        for name, mutate in mutations.items():
            with self.subTest(name):
                package = copy.deepcopy(original)
                mutate(package)
                path.write_text(json.dumps(package, indent=2), encoding="utf-8")
                after = law.render_support_matrix(law.support_matrix(base.path))
                self.assertNotEqual(after, before)
                self.assertNotEqual(after.encode("utf-8"), expected.read_bytes())
        path.write_text(json.dumps(original, indent=2), encoding="utf-8")
        self.assertEqual(law.render_support_matrix(law.support_matrix(base.path)), before)
        # A hand-edited expected file is a mismatch; the test never regenerates it.
        expected.write_text(before.replace("verified", "VERIFIED", 1), encoding="utf-8")
        self.assertNotEqual(law.render_support_matrix(law.support_matrix(base.path)).encode("utf-8"),
                            expected.read_bytes())

    def test_missing_empty_and_invalid_base(self):
        base = Base(self)
        done = run_cli("--base", str(base.path))
        self.assertEqual((done.returncode, json.loads(done.stdout)), (0, {"schema_version": 1, "packages": []}))
        done = run_cli("--md", "--base", str(base.path))
        self.assertEqual(done.returncode, 0)
        self.assertIn(b"No shipped jurisdiction packages.", done.stdout)
        (base.path / "README.md").write_text("not a package", encoding="utf-8")
        (base.path / "us-xx").mkdir()
        (base.path / "us-xx" / "notes.txt").write_text("no package here", encoding="utf-8")
        self.assertEqual(law.support_matrix(base.path)["packages"], [])

        missing = base.path / "does-not-exist"
        done = run_cli("--md", "--base", str(missing))
        self.assertEqual((done.returncode, done.stdout), (1, b""))
        self.assertIn(b"Stopped:", done.stderr)
        self.assertIn(str(missing).encode(), done.stderr)
        done = run_cli("--base", str(base.path / "README.md"))
        self.assertEqual((done.returncode, done.stdout), (1, b""))
        self.assertIn(str(base.path / "README.md").encode(), done.stderr)

        base.write("us-aa", make_package("us-aa", RULES[:1], ONE_SCOPE))
        for slug, content in (("us-bb", "{not json"), ("us-cc", json.dumps({"schema_version": 1})),
                              ("Bad", json.dumps(make_package("us-aa", RULES[:1], ONE_SCOPE)))):
            with self.subTest(slug):
                target = base.path / slug
                target.mkdir()
                (target / "package.json").write_text(content, encoding="utf-8")
                done = run_cli("--md", "--base", str(base.path))
                self.assertEqual((done.returncode, done.stdout), (1, b""))  # us-aa is valid, yet nothing prints
                self.assertTrue(done.stderr.startswith(b"Stopped:"))
                self.assertIn(str(target / "package.json").encode(), done.stderr)
                (target / "package.json").unlink()
                target.rmdir()
        mismatch = make_package("us-dd", RULES[:1], ONE_SCOPE)
        mismatch["jurisdiction"] = "us-ee"
        base.write("us-dd", mismatch)
        done = run_cli("--base", str(base.path))
        self.assertEqual((done.returncode, done.stdout), (1, b""))
        self.assertIn(str(base.path / "us-dd" / "package.json").encode(), done.stderr)
        self.assertEqual(run_cli("--md", "us-ca").returncode, 2)
        self.assertEqual(run_cli("--bogus").returncode, 2)

    def test_markdown_escaping_is_text_only(self):
        payload = 'a|b\nc `x` & <script>alert("1")</script> [l](http://e.test) https://e.test/ x@y.test www.e.test \\ *z*'
        base = Base(self)
        package = make_package("us-wa", [("b-open", payload, "2020-01-01", None, "verified")],
                               [("s", ("b-open",))], reviewed_by=[payload])
        package["records_law"]["name"] = payload
        package["records_law"]["sources"] = [
            {"title": payload, "url": "https://e.test/a b(c)<d>`e`|f\\g&amp;h?x=1&y=2", "accessed": "2026-01-01"},
            {"title": "Second", "url": "https://e.test/second", "accessed": "2026-01-02"}]
        base.write("us-wa", package)
        text = law.render_support_matrix(law.support_matrix(base.path))
        row = next(line for line in text.splitlines() if line.startswith("| wa-b-open"))
        self.assertEqual(len(re.findall(r"(?<!\\)\|", row)), 7)
        for line in (row, *[ln for ln in text.splitlines() if "e.test" in ln and "Source" not in ln]):
            for trigger in ("<script", "</", "://", "@y", "www.e"):
                self.assertNotIn(trigger, line)
            self.assertIsNone(re.search(r"(?<!\\)[\[\]]", line))  # no unescaped bracket: no link syntax
            self.assertNotIn("<", line)
            self.assertNotIn(">", line)
            self.assertNotIn("`x`", line)
        self.assertIn("&lt;script&gt;alert(&quot;1&quot;)&lt;/script&gt;", row)
        self.assertIn("a\\|b c \\`x\\` &amp;", row)
        self.assertNotIn("\n", row)
        links = [line for line in text.splitlines() if "[Source " in line]
        self.assertEqual(len(links), 2)
        self.assertTrue(links[0].startswith("  - [Source 1](https://e.test/a%20b%28c%29%3Cd%3E%60e%60%7Cf%5Cg&amp;amp;h?x=1&amp;y=2): "))
        self.assertTrue(links[1].startswith("  - [Source 2](https://e.test/second): Second, accessed 2026-01-02"))
        self.assertEqual(text, law.render_support_matrix(law.support_matrix(base.path)))
        self.assertEqual(text.count("\n- Reviewed at (as recorded): none recorded\n"), 1)
        for bad in ("http://e.test/x", "javascript:alert(1)", "https://", "https://e.test/a\nb", "ftp://e.test"):
            with self.subTest(bad):
                matrix = law.support_matrix(base.path)
                matrix["packages"][0]["records_law"]["sources"][0]["url"] = bad
                with self.assertRaises(ValueError):
                    law.render_support_matrix(matrix)
        self.assertEqual(law._md_text(None), "—")
        self.assertEqual(law._md_text(" \n\t "), "—")

    def test_legacy_commands_and_read_only_inputs(self):
        package = law.load_package("us-ca")
        files = [*law.JURISDICTIONS.glob("*/package.json"), *(REPO_ROOT / "jurisdictions").glob("*/package.json"),
                 DOC, REPO_ROOT / "docs" / "LAW-PACKAGES.md"]
        stamps = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}
        reads = []
        real_open = Path.open

        def recording_open(self, *args, **kwargs):
            reads.append(self.resolve())
            return real_open(self, *args, **kwargs)

        with tempfile.TemporaryDirectory(prefix="synthetic-law-legacy-") as scratch:
            overlay = Path(scratch) / "local-policy-overlay.json"
            overlay.write_text("{}", encoding="utf-8")
            cwd = os.getcwd()
            os.chdir(scratch)
            self.addCleanup(os.chdir, cwd)
            with mock.patch.object(socket, "socket", side_effect=AssertionError("network used")), \
                    mock.patch.object(Path, "open", recording_open):
                matrix = law.support_matrix()
                law.render_support_matrix(matrix)
        self.assertTrue(reads)
        allowed = {law.SCHEMA_PATH.resolve(), *(p.resolve() for p in law.JURISDICTIONS.glob("*/package.json"))}
        self.assertEqual(set(reads) - allowed, set())
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}, stamps)

        done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.law", "show", "us-ca"],
                              cwd=REPO_ROOT, capture_output=True, text=True)
        self.assertEqual(done.stdout, json.dumps(law.summary(package), indent=2, ensure_ascii=False) + "\n")
        done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.law", "rules", "us-ca", "--on",
                               "2024-06-01", "--review", "likely"], cwd=REPO_ROOT, capture_output=True, text=True)
        wanted = [law._rule_view(r) for r in law.rules_in_force(package, "2024-06-01") if r["review"] == "likely"]
        self.assertEqual(json.loads(done.stdout)["rules"], wanted)
        done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.law", "deadline", "us-ca", "--sent",
                               "2026-01-05", "--extension"], cwd=REPO_ROOT, capture_output=True, text=True)
        self.assertEqual(json.loads(done.stdout)["reminder"], law.deadline(package, "2026-01-05", True).isoformat())
        with tempfile.TemporaryDirectory(prefix="synthetic-law-sheet-") as scratch:
            out = Path(scratch) / "sheet.csv"
            done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.law", "review-sheet", "us-ca", "--out",
                                   str(out), "--as-of", "2026-10-10"], cwd=REPO_ROOT, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            filled = Path(scratch) / "filled.csv"
            filled.write_bytes(out.read_bytes())
            done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.law", "apply-sheet", "us-ca", "--sheet",
                                   str(filled), "--out", str(Path(scratch) / "proposed.json")], cwd=REPO_ROOT,
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(json.loads(done.stdout)["changed"], [])


if __name__ == "__main__":
    unittest.main()
