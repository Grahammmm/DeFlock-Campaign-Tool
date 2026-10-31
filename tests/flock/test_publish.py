import csv, json, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
FIXTURES = ROOT / "tests" / "fixtures" / "flock"
AGENCY = "sample-city-police-department"
def run(tool, *args):
    done = subprocess.run([sys.executable, "-B", str(TOOLS / tool), *args], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout
class FlockPublishTest(unittest.TestCase):
    def setUp(self):  # the temp store is built exactly as tests/flock/test_verify.py builds it, then F4 finish
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.tmp, self.store = Path(tmp.name), Path(tmp.name) / "store"
        self.assertEqual(run("flock_facts.py", "import", str(FIXTURES / "compare-facts.csv"), "--store", str(self.store),
                             "--added-by", "tester", "--date", "2026-10-30")[0], 0)
        shutil.copy(FIXTURES / "law-sample.csv", self.store / "LAW.csv")
        shutil.copy(FIXTURES / "policy-sample.csv", self.store / "agencies" / AGENCY / "POLICY.csv")
        self.run_dir = self.store / "agencies" / AGENCY / "runs" / "2026-10-30"
        self.assertEqual(run("flock_compare.py", "scaffold", "--store", str(self.store), "--agency", AGENCY, "--date", "2026-10-30",
                             "--provider", "anthropic", "--model", "claude-opus-5-5", "--applicability", "Sample city police")[0], 0)
        shutil.copy(FIXTURES / "findings-sample.csv", self.run_dir / "FINDINGS.csv")
        self.rd, self.out = str(self.run_dir), self.run_dir / "PUBLISH.json"
        self.assertEqual(run("flock_verify.py", "start", self.rd, "--provider", "openai", "--model", "gpt-5-codex")[0], 0)
        shutil.copy(FIXTURES / "verify-sample.csv", self.run_dir / "VERIFY.csv")
        self.assertEqual(run("flock_verify.py", "finish", self.rd), (0, "public_ok=4 questions=2 rejected=1\n"))
    def export(self):
        return run("flock_publish.py", "export", self.rd, "--site-slug", "sample")
    def check(self, doc):
        (self.tmp / "x.json").write_text(json.dumps(doc))
        return run("flock_publish.py", "--check", str(self.tmp / "x.json"))
    def test_happy_path(self):
        self.assertEqual(self.export(), (0, "exported=4 refused=0\n"))
        raw = self.out.read_text(); doc = json.loads(raw)
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o600)
        self.assertEqual([f["finding_id"] for f in doc["findings"]], ["N1", "O1", "A1", "T1"])
        for f in doc["findings"]:
            kinds = [c["kind"] for c in f["cites"]]
            self.assertTrue({"law", "policy"} & set(kinds) and "record" in kinds, f["finding_id"])
            self.assertEqual(f["review"]["status"], "pending")
        self.assertEqual(doc["findings"][0]["cites"][-1]["label"], "Request REQ-101, page 2")
        for key in ('"quote"', '"next_step"', '"verbatim_quote"'):
            self.assertNotIn(key, raw)
        self.assertEqual(run("flock_publish.py", "--check", str(self.out)), (0, "findings=4 approved=0 errors=0\n"))
    def test_fail_closed(self):
        signed = (self.run_dir / "VERIFY.json").read_text()
        (self.run_dir / "VERIFY.json").write_text(json.dumps({"schema": 1, "verifier_provider": "anthropic"}))
        self.assertEqual(self.export(), (3, "refused: verifier family equals author family\n"))
        self.assertFalse(self.out.exists())
        (self.run_dir / "VERIFY.json").write_text(signed)
        with open(FIXTURES / "findings-sample.csv", newline="") as handle:
            base, doc = list(csv.reader(handle)), {}
        mutations = [("O1", {"claim": "The sample department admitted outside agencies have query access.",
                             "quote": "Twelve outside agencies have query access.", "quote_fact_id": "F-76dbe8a9b4ac"}, "gated"),
                     ("T1", {"claim": "The sample training materials were withheld; ask records@sample.org."}, "email")]
        for fid, fields, category in mutations:
            with self.subTest(fid=fid):
                table = [list(r) for r in base]
                row = next(r for r in table if r[0] == fid)
                for column, value in fields.items():
                    row[base[0].index(column)] = value
                with open(self.run_dir / "FINDINGS.csv", "w", newline="") as handle:
                    csv.writer(handle, lineterminator="\n").writerows(table)
                self.assertEqual(run("flock_verify.py", "--check", self.rd)[0], 0)
                self.assertEqual(self.export(), (0, "finding %s: refused %s\nexported=3 refused=1\n" % (fid, category)))
                self.assertNotIn(fid, [f["finding_id"] for f in json.loads(self.out.read_text())["findings"]])
                self.assertEqual(self.export(), (1, "export refused: PUBLISH.json already exists\n"))
                doc = json.loads(self.out.read_text())
                self.out.unlink()
        doc["findings"][0]["review"] = {"status": "approved", "approved_by": "G", "approved_on": "2026-10-30", "approval_ref": "G-1"}
        self.assertEqual(self.check(doc), (0, "findings=3 approved=1 errors=0\n"))
        cases = [("review.approval_ref", ""), ("rating", "medium"), ("review.quote", "private source text"),
                 ("cites.-1.verbatim_quote", "private source text"), ("cites.0.review", "MISSING"), ("cites.0.url", "https://["),
                 ("cites.0.review", {}), ("cites.0.kind", []), ("cites.0", None), ("cites", {}),
                 ("review.approved_by", "MISSING"), ("review.approved_on", "2026-02-30"), ("review.status", []),
                 ("finding_id", None), ("finding_id", ""), ("topic", {}), ("topic", "Bad topic"), ("claim_class", {}),
                 ("claim_class", "unknown"), ("review", {"status": "pending"}), ("review.approval_ref", {}),
                 ("@agency", None), ("@agency", "Bad slug"), ("@run_date", "2026-02-30"), ("@run_date", []),
                 ("@author_family", {}), ("@verifier_family", "unknown"), ("@schema_version", True)]
        for path, value in cases:
            bad = json.loads(json.dumps(doc))
            target, parts = (bad, path[1:].split(".")) if path.startswith("@") else (bad["findings"][0], path.split("."))
            for key in parts[:-1]:
                target = target[int(key)] if isinstance(target, list) else target[key]
            if value == "MISSING": target.pop(parts[-1])
            else: target[int(parts[-1]) if isinstance(target, list) else parts[-1]] = value
            code, output = self.check(bad)
            self.assertEqual(code, 1, path)
            self.assertRegex(output, r"findings=\d+ approved=\d+ errors=[1-9]\d*\n$", path)
            self.assertNotIn("private source text", output)
    def test_strong_only(self):
        public = (self.run_dir / "PUBLIC-OK.csv").read_text()
        for fid in ("N2", "R1"):
            with self.subTest(fid=fid):
                (self.run_dir / "PUBLIC-OK.csv").write_text(public + fid + ",x,record-gap,strong,F-x,,\n")
                code, out = self.export()
                self.assertEqual(code, 1)
                self.assertIn("PUBLIC-OK.csv %s: not a confirmed strong finding" % fid, out)
                self.assertFalse(self.out.exists())
if __name__ == "__main__":
    unittest.main()
