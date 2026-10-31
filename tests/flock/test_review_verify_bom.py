"""RF1105-14: a BOM-saved FINDINGS.csv / VERIFY.csv reads like the BOM-less copy; a PLAN.json that is not a verify plan says so."""
import contextlib, hashlib, importlib.util, io, json, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / rel)
    spec.loader.exec_module(module := importlib.util.module_from_spec(spec))
    return module


VERIFY = load("_rf1105_14_verify", "flock_review_verify.py")
BOM = b"\xef\xbb\xbf"
FINDINGS = ("finding_id,kind,verdict,basis\nF1,requirement,met,b\nF2,requirement,not met,b\nF3,requirement,contradicted,b\n"
            "F4,requirement,cannot tell,b\nF5,requirement,met,b\nF6,requirement,n/a,b\n").encode()
HEAD = "row_id,result,reason,page_cite\n"
NOT_PLAN = "plan: PLAN.json is not a verify plan"
TALLY = "planned=%d rows=%d agree=%d disagree=0 needs_source=0 missing=0 extra=0 errors=0"


class ReviewVerifyBomTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def run_main(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            code = VERIFY.main([str(a) for a in argv])
        return code, printed.getvalue()

    def write(self, name, data):
        path = self.tmp / name
        path.write_bytes(data)
        return path

    def make_plan(self, data, name):
        findings = self.write(name + ".csv", data)
        out = self.tmp / (name + ".json")
        code, printed = self.run_main("plan", findings, "--seed", 11, "--size", 2, "--out", out)
        self.assertEqual(code, 0, printed)
        return findings, out, json.loads(out.read_text(encoding="utf-8")), printed

    def verify_rows(self, spec):
        return "".join("%s,agree,r,p.1\n" % i for i in spec["priority_ids"] + spec["sample_ids"])

    def test_bom_findings_plan_matches_bomless_ids_with_own_hash(self):
        _, _, plain, _ = self.make_plan(FINDINGS, "plain")
        bom_path, _, bom, printed = self.make_plan(BOM + FINDINGS, "bom")
        self.assertEqual(bom["priority_ids"], ["F2", "F3"])
        self.assertEqual((bom["priority_ids"], bom["sample_ids"], bom["pool_size"]),
                         (plain["priority_ids"], plain["sample_ids"], plain["pool_size"]))
        self.assertNotIn("", bom["sample_ids"])
        self.assertEqual(bom["findings_sha256"], hashlib.sha256(bom_path.read_bytes()).hexdigest())
        self.assertNotEqual(bom["findings_sha256"], plain["findings_sha256"])
        self.assertTrue(printed.endswith("findings_sha256=%s\n" % bom["findings_sha256"]))

    def test_bom_findings_rows_are_still_validated(self):
        bad = self.write("bad.csv", BOM + FINDINGS + b",requirement,met,b\nF1,requirement,maybe,b\n")
        out = self.tmp / "o.json"
        code, printed = self.run_main("plan", bad, "--seed", 11, "--size", 2, "--out", out)
        self.assertEqual((code, printed.splitlines()),
                         (1, ["row 8: finding_id is empty", "row 9: verdict is unknown", "row 9: finding_id is duplicated", "plan: errors=3"]))
        self.assertFalse(out.exists())

    def test_bom_verify_passes_where_bomless_passes(self):
        findings, plan, spec, _ = self.make_plan(BOM + FINDINGS, "bom")
        want = TALLY % (4, 4, 4)
        for name, prefix in (("plain.csv", b""), ("bom-verify.csv", BOM)):
            with self.subTest(verify=name):
                verify = self.write(name, prefix + (HEAD + self.verify_rows(spec)).encode())
                self.assertEqual(self.run_main("check", plan, findings, verify), (0, want + "\n"))

    def test_bom_verify_with_wrong_header_still_refused(self):
        findings, plan, spec, _ = self.make_plan(FINDINGS, "plain")
        verify = self.write("v.csv", BOM + ("row_id,result,reason\n" + self.verify_rows(spec)).encode())
        code, printed = self.run_main("check", plan, findings, verify)
        self.assertEqual(code, 1)
        self.assertEqual(printed.splitlines()[0], "row 1: header is not row_id,result,reason,page_cite")

    def test_broken_plan_is_named(self):
        findings, plan, spec, _ = self.make_plan(FINDINGS, "plain")
        verify = self.write("v.csv", (HEAD + self.verify_rows(spec)).encode())
        drop = lambda key: {k: v for k, v in spec.items() if k != key}  # noqa: E731
        cases = {"list": b"[]", "not json": b"{", "not utf-8": b'{"schema": 1, "x": "\xff"}',
                 "no sample_ids": json.dumps(drop("sample_ids")).encode(),
                 "number in priority_ids": json.dumps({**spec, "priority_ids": [spec["priority_ids"][0], 7]}).encode(),
                 "priority_ids a string": json.dumps({**spec, "priority_ids": "F2"}).encode(),
                 "schema 2": json.dumps({**spec, "schema": 2}).encode(),
                 "short hash": json.dumps({**spec, "findings_sha256": spec["findings_sha256"][:63]}).encode(),
                 "upper-case hash": json.dumps({**spec, "findings_sha256": spec["findings_sha256"].upper()}).encode(),
                 "no hash": json.dumps(drop("findings_sha256")).encode()}
        for label, data in cases.items():
            with self.subTest(plan=label):
                broken = self.write("broken.json", data)
                self.assertEqual(self.run_main("check", broken, findings, verify), (1, NOT_PLAN + "\n"))

    def test_missing_plan_stays_io_error(self):
        findings, _, spec, _ = self.make_plan(FINDINGS, "plain")
        verify = self.write("v.csv", (HEAD + self.verify_rows(spec)).encode())
        self.assertEqual(self.run_main("check", self.tmp / "absent.json", findings, verify), (1, "io: FileNotFoundError\n"))

    def test_no_bom_output_unchanged(self):
        findings, plan, spec, printed = self.make_plan(FINDINGS, "plain")
        digest = hashlib.sha256(FINDINGS).hexdigest()
        self.assertEqual(printed, "priority=2 pool=3 sample=2 findings_sha256=%s\n" % digest)
        verify = self.write("v.csv", (HEAD + self.verify_rows(spec)).encode())
        self.assertEqual(self.run_main("check", plan, findings, verify), (0, TALLY % (4, 4, 4) + "\n"))
        partial = self.write("p.csv", (HEAD + "F2,agree,r,p.1\n").encode())
        code, out = self.run_main("check", plan, findings, partial)
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines()[-1], "planned=4 rows=1 agree=1 disagree=0 needs_source=0 missing=3 extra=0 errors=3")


if __name__ == "__main__":
    unittest.main()
