"""flock_compare: RUN.json records FACTS/LAW/POLICY/law-package hashes; --check names an input changed since scaffold (RF1150-3)."""
import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest import mock

from campaign_tool import law
from tests.flock.test_compare import AGENCY, FIXTURES, TOOLS, run

NAMES = ["FACTS.csv", "LAW.csv", "POLICY.csv", "law-package"]
CLEAN = "findings=7 errors=0\n"
INVALID = "RUN.json: inputs is invalid\nfindings=7 errors=1\n"
HEX64 = re.compile(r"[0-9a-fA-F]{64}")


def changed(*names):
    return "".join("inputs: %s changed since scaffold (compare again in a new run folder)\n" % n for n in names)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def append(path, row):
    with open(path, "a", encoding="utf-8", newline="") as handle:
        csv.writer(handle, lineterminator="\n").writerow(row)


def module():
    spec = importlib.util.spec_from_file_location("_compare_inputs", TOOLS / "flock_compare.py")
    compare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(compare)
    return compare


class CompareInputsTest(unittest.TestCase):
    def setUp(self, law_policy=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp, self.store = Path(tmp.name), Path(tmp.name) / "store"
        self.assertEqual(self.facts_import(FIXTURES / "compare-facts.csv"), 0)
        self.agency = self.store / "agencies" / AGENCY
        self.paths = {"FACTS.csv": self.agency / "FACTS.csv", "LAW.csv": self.store / "LAW.csv",
                      "POLICY.csv": self.agency / "POLICY.csv"}
        if law_policy:
            self.copy_law_policy()
        self.run_dir = self.agency / "runs" / "2026-10-30"
        self.assertEqual(run("flock_compare.py", "scaffold", "--store", str(self.store), "--agency", AGENCY, "--date",
                             "2026-10-30", "--provider", "anthropic", "--model", "claude-opus-5-5",
                             "--applicability", "Sample city police; local ALPR policy applies"),
                         (0, "scaffolded run for %s on 2026-10-30\n" % AGENCY))
        shutil.copy(FIXTURES / "findings-sample.csv", self.run_dir / "FINDINGS.csv")
        self.outputs = []

    def copy_law_policy(self):
        shutil.copy(FIXTURES / "law-sample.csv", self.paths["LAW.csv"])
        shutil.copy(FIXTURES / "policy-sample.csv", self.paths["POLICY.csv"])

    def facts_import(self, path):
        return run("flock_facts.py", "import", str(path), "--store", str(self.store), "--added-by", "tester",
                   "--date", "2026-10-30")[0]

    def check(self, tool="flock_compare.py"):
        result = run(tool, "--check", str(self.run_dir))
        self.outputs.append(result[1])
        return result

    def meta(self):
        return json.loads((self.run_dir / "RUN.json").read_text(encoding="utf-8"))

    def write_meta(self, meta):
        (self.run_dir / "RUN.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    def tearDown(self):  # 9: no output line carries a hash or the store path
        for out in self.outputs:
            self.assertIsNone(HEX64.search(out))
            self.assertNotIn(str(self.tmp), out)

    def edit_facts(self):  # one valid fact row appended through flock-facts
        src = self.tmp / "more-facts.csv"
        with open(FIXTURES / "compare-facts.csv", encoding="utf-8", newline="") as handle:
            header = next(csv.reader(handle))
        with open(src, "w", encoding="utf-8", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows([header, [
                "Sample City Police Department", "REQ-909", "retention", "30 days", "c" * 64, "2",
                "Reads are kept for 30 days."]])
        self.assertEqual(self.facts_import(src), 0)

    def edit_law(self):  # one LAW.csv cell
        with open(self.paths["LAW.csv"], encoding="utf-8", newline="") as handle:
            table = list(csv.reader(handle))
        table[2][table[0].index("duty")] = "Sample duty two, edited"
        with open(self.paths["LAW.csv"], "w", encoding="utf-8", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows(table)

    def edit_policy(self):  # one valid POLICY.csv row added
        with open(self.paths["POLICY.csv"], encoding="utf-8", newline="") as handle:
            table = list(csv.reader(handle))
        row = dict(zip(table[0], table[1]), version_label="26-55 second copy")
        row["policy_row_id"] = module().law_policy.derived_id(row)
        append(self.paths["POLICY.csv"], [row[k] for k in table[0]])

    def test_recorded_and_clean(self):  # 1
        inputs = self.meta()["inputs"]
        self.assertEqual(list(inputs), NAMES)
        self.assertEqual(inputs, {**{n: sha(p) for n, p in self.paths.items()}, "law-package": sha(law.package_path("us-ca"))})
        self.assertEqual(self.check(), (0, CLEAN))

    def test_each_input_changes_and_restores(self):  # 2
        for name, edit in (("FACTS.csv", self.edit_facts), ("LAW.csv", self.edit_law), ("POLICY.csv", self.edit_policy)):
            with self.subTest(name):
                original = self.paths[name].read_bytes()
                edit()
                self.assertNotEqual(self.paths[name].read_bytes(), original)
                self.assertEqual(self.check(), (1, changed(name) + "findings=7 errors=1\n"))
                self.paths[name].write_bytes(original)
                self.assertEqual(self.check(), (0, CLEAN))

    def test_package_changed_in_process(self):  # 3, plus the fixed order when all four change
        copy = self.tmp / "package.json"
        copy.write_bytes(law.package_path("us-ca").read_bytes() + b"\n")
        compare = module()
        with mock.patch.object(compare.law, "package_path", lambda jurisdiction, base=None: copy):
            self.assertEqual(compare.check(self.run_dir), ([changed("law-package").rstrip("\n")], 7))
            self.edit_policy(), self.edit_law(), self.edit_facts()
            errs, count = compare.check(self.run_dir)
        self.assertEqual((errs, count), (changed(*NAMES).splitlines(), 7))
        self.outputs.append("\n".join(errs))

    def test_absent_at_scaffold_then_copied(self):  # 4
        CompareInputsTest.setUp(self, law_policy=False)
        self.assertEqual((self.meta()["inputs"]["LAW.csv"], self.meta()["inputs"]["POLICY.csv"]), (None, None))
        self.copy_law_policy()
        self.assertEqual(self.check(), (1, changed("LAW.csv", "POLICY.csv") + "findings=7 errors=2\n"))

    def test_removed_later(self):  # 5
        self.paths["POLICY.csv"].unlink()
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertEqual([x for x in out.splitlines() if x.startswith("inputs: ")], changed("POLICY.csv").splitlines())
        self.assertIn("store: POLICY.csv row 1: file unreadable (FileNotFoundError)", out.splitlines())

    def test_legacy_run_without_inputs(self):  # 6: byte-identical to the output before this change
        meta = self.meta()
        del meta["inputs"]
        self.write_meta(meta)
        self.assertEqual(self.check(), (0, CLEAN))
        self.edit_law()  # a legacy run has nothing to compare against
        self.assertEqual(self.check(), (0, CLEAN))

    def test_malformed_inputs(self):  # 7
        good = self.meta()["inputs"]
        cases = {"list": list(good.values()), "missing key": {k: good[k] for k in NAMES[:3]},
                 "extra key": {**good, "VERIFY.csv": None}, "short": {**good, "LAW.csv": "abc"},
                 "uppercase": {**good, "FACTS.csv": good["FACTS.csv"].upper()}, "number": {**good, "POLICY.csv": 0},
                 "trailing newline": {**good, "law-package": good["law-package"] + "\n"}, "null": None}
        for label, value in cases.items():
            with self.subTest(label):
                self.write_meta({**self.meta(), "inputs": value})
                self.assertEqual(self.check(), (1, INVALID))
        self.write_meta({**self.meta(), "inputs": good})
        self.assertEqual(self.check(), (0, CLEAN))

    def test_verify_check_refuses_changed_input(self):  # 8
        rd = str(self.run_dir)
        self.assertEqual(run("flock_verify.py", "start", rd, "--provider", "openai", "--model", "gpt-5-codex"),
                         (0, "started verify run\n"))
        shutil.copy(FIXTURES / "verify-sample.csv", self.run_dir / "VERIFY.csv")
        self.assertEqual(self.check("flock_verify.py"), (0, "verified=7 errors=0\n"))
        self.edit_policy()
        code, out = self.check("flock_verify.py")
        self.assertEqual(code, 1)
        self.assertIn("findings: " + changed("POLICY.csv").rstrip("\n"), out.splitlines())
