import csv, hashlib, importlib.util, io, json, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
_spec = importlib.util.spec_from_file_location("_publish_fixture_facts", TOOLS / "flock_facts.py")
flock_facts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(flock_facts)
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
        # Citation fixture now records an original and an explicit not-required proposal.
        source = self.store / "original.txt"
        source.write_bytes(b"synthetic original\n")
        facts = self.store / "agencies" / AGENCY / "FACTS.csv"
        source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        self.fact_ids = {}
        with facts.open(newline="") as handle:
            table = list(csv.DictReader(handle))
        for row in table:
            old_id = row['fact_id']
            row['source_sha256'] = source_sha
            row['fact_id'] = flock_facts.fact_id(row)
            row['source_path'] = str(source)
            self.fact_ids[old_id] = row['fact_id']
        with facts.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(table[0]))
            writer.writeheader()
            writer.writerows(table)
        redaction = facts.parent / "redaction" / source_sha
        redaction.mkdir(parents=True, mode=0o700)
        redaction.parent.chmod(0o700)
        proposal = redaction / "PROPOSAL.json"
        proposal.write_text(json.dumps({"schema_version": 1, "document_id": "synthetic", "source_sha256": source_sha,
            "redaction_required": False, "proposed": [], "not_detected_note": "synthetic fixture",
            "scan_receipt": "c" * 64, "created_at": "2026-10-30T00:00:00Z"}))
        proposal.chmod(0o600)
        self.run_dir = self.store / "agencies" / AGENCY / "runs" / "2026-10-30"
        self.assertEqual(run("flock_compare.py", "scaffold", "--store", str(self.store), "--agency", AGENCY, "--date", "2026-10-30",
                             "--provider", "anthropic", "--model", "claude-opus-5-5", "--applicability", "Sample city police")[0], 0)
        (self.run_dir / "FINDINGS.csv").write_text(self.fixture_text("findings-sample.csv"))
        self.rd, self.out = str(self.run_dir), self.run_dir / "PUBLISH.json"
        self.assertEqual(run("flock_verify.py", "start", self.rd, "--provider", "openai", "--model", "gpt-5-codex")[0], 0)
        (self.run_dir / "VERIFY.csv").write_text(self.fixture_text("verify-sample.csv"))
        self.assertEqual(run("flock_verify.py", "finish", self.rd), (0, "public_ok=4 questions=2 rejected=1\n"))
    def fixture_text(self, name):
        text = (FIXTURES / name).read_text()
        for old, new in self.fact_ids.items():
            text = text.replace(old, new)
        return text
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
        base, doc = list(csv.reader(io.StringIO(self.fixture_text("findings-sample.csv")))), {}
        o1 = {"claim": "The sample department admitted outside agencies have query access.",
              "quote": "Twelve outside agencies have query access.", "quote_fact_id": self.fact_ids["F-76dbe8a9b4ac"]}
        mutations = [("T1", {"claim": "The sample training materials were withheld; ask records@sample.org."}, "email")]
        # DF-4 ruling 1 (alert-only): quote-gated wording is a review-required note; the finding is still exported.
        table = [list(r) for r in base]
        row = next(r for r in table if r[0] == "O1")
        for column, value in o1.items():
            row[base[0].index(column)] = value
        with open(self.run_dir / "FINDINGS.csv", "w", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows(table)
        self.assertEqual(run("flock_verify.py", "--check", self.rd)[0], 0)
        self.assertEqual(self.export(), (0, "review-required: finding O1: quote-gated wording\nexported=4 refused=0\n"))
        self.assertIn("O1", [f["finding_id"] for f in json.loads(self.out.read_text())["findings"]])
        self.out.unlink()
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
    def call(self, repo, *argv):  # in process, with verify_tool.REPO (the REPO flock_publish uses) patched to a temp root
        import contextlib, importlib.util, io
        from unittest import mock
        spec = importlib.util.spec_from_file_location("_publish_rf5", TOOLS / "flock_publish.py")
        publish, out = importlib.util.module_from_spec(spec), io.StringIO()
        spec.loader.exec_module(publish)
        with mock.patch.object(publish.verify_tool, "REPO", Path(repo)), contextlib.redirect_stdout(out):
            return publish.main(list(argv)), out.getvalue()
    def test_rundir_inside_repository_refused_before_writing(self):
        import os
        refused, snap = (1, "flock-publish: RUNDIR is inside the repository\n"), lambda d: {p.name: p.read_bytes() for p in d.iterdir()}
        link, empty = self.tmp / "link", self.store / "empty"  # empty: no RUN.json, so 1 must beat family refusal 3
        link.symlink_to(self.run_dir); empty.mkdir()
        before = snap(self.run_dir)
        self.addCleanup(os.chdir, os.getcwd()); os.chdir(self.run_dir)
        for repo, rd in ((self.store, self.rd), (self.run_dir, self.rd), (self.store, str(link)), (self.store, "."), (self.store, str(empty))):
            self.assertEqual(self.call(repo, "export", rd, "--site-slug", "sample"), refused, (repo, rd))
        self.assertEqual((snap(self.run_dir), snap(empty)), (before, {}))
        self.assertEqual(run("flock_publish.py", "export", str(ROOT / "no-such-run"), "--site-slug", "sample"), refused)
    def test_lookalike_sibling_proceeds_and_check_is_not_guarded(self):
        repo = self.store.with_name("repo"); repo.mkdir()
        copy = self.store.rename(self.store.with_name("repo-copy"))
        rd = copy / self.run_dir.relative_to(self.store)
        facts = copy / "agencies" / AGENCY / "FACTS.csv"
        facts.write_text(facts.read_text().replace(str(self.store / "original.txt"), str(copy / "original.txt")))
        import hashlib
        meta = json.loads((rd / "RUN.json").read_text())
        meta["inputs"]["FACTS.csv"] = hashlib.sha256(facts.read_bytes()).hexdigest()
        (rd / "RUN.json").write_text(json.dumps(meta))
        self.assertEqual(self.call(repo, "export", str(rd), "--site-slug", "sample"), (0, "exported=4 refused=0\n"))
        self.assertEqual(self.call(copy, "--check", str(rd / "PUBLISH.json")), (0, "findings=4 approved=0 errors=0\n"))
if __name__ == "__main__":
    unittest.main()
