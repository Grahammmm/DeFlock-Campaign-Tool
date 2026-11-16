"""flock_report: review.text_state_file is admitted strictly (identity shape, duplicate members, nonfinite tokens, nesting) (RF0452-10)."""
import json, os, subprocess, sys, unittest
from pathlib import Path
from unittest import mock

from tests.flock import test_report as base
from tests.flock import test_report_region as reg
from tests.flock.test_report import B64, HAPPY, OCR, RFIX, TS, FlockReportTest
from tests.flock.test_review import ROOT

sys.path.insert(0, str(ROOT / "tools"))
import flock_report  # noqa: E402

WHY = "config: review.text_state_file "
CANARY = "CANARYKEY-7731"
GOOD = "a" * 64
NESTED = WHY + "JSON nesting exceeds parser limit"


def rec(sha=GOOD, state="ocr_unreviewed", words="w"):
    return '{"sha256": %s, "state": "%s", "words": "%s"}' % (json.dumps(sha), state, words)


def doc(*records, extra=""):
    return '{"schema": "records-text-state-v1", %s"records": [%s]}' % (extra, ", ".join(records))


def run_tool(*args):
    done = subprocess.run([sys.executable, "-B", str(base.TOOL), *map(str, args)], capture_output=True, text=True, check=False, timeout=120)
    return done.returncode, done.stdout, done.stderr


class HelperTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp, self.n = Path(tmp.name), 0

    def read(self, text):
        self.n += 1
        path = self.tmp / ("r%d.json" % self.n)
        path.write_text(text)
        return flock_report.text_state(str(path))

    def refused(self, text, want=None):
        with self.assertRaises(flock_report.Fail) as ctx:
            self.read(text)
        self.assertTrue(str(ctx.exception).startswith(WHY), str(ctx.exception))
        if want:
            self.assertEqual(str(ctx.exception), want)
        self.assertNotIn(CANARY, str(ctx.exception))
        return str(ctx.exception)

    def test_identity_matrix(self):
        for bad in ("", "bad", "a" * 63, "a" * 65, "A" * 64, "g" * 64, " " + GOOD, GOOD + "\n", None, 7, True, [GOOD], {"a": GOOD}):
            with self.subTest(sha=bad):
                self.refused(doc(rec(bad)))
        self.refused(doc(rec(), rec()))
        self.refused(doc(rec(GOOD), rec("B" * 64)))  # unrelated malformed record refuses the whole receipt
        self.assertEqual(sorted(self.read(doc(rec(GOOD), rec("b" * 64)))), [GOOD, "b" * 64])
        self.assertEqual(self.read(doc()), {})

    def test_duplicate_members_any_depth(self):
        r = rec()
        cases = ['{"schema": "records-text-state-v1", "schema": "records-text-state-v1", "records": []}',
                 '{"schema": "records-text-state-v1", "records": [], "records": []}',
                 doc(r.replace('"state": "ocr_unreviewed"', '"state": "ocr_unreviewed", "state": "text_reviewed"')),
                 doc(r.replace('"state": "ocr_unreviewed"', '"state": "text_reviewed", "state": "ocr_unreviewed"')),
                 doc(r.replace('"words": "w"', '"words": "w", "words": "w"')),
                 doc(r[:-1] + ', "sha256": %s}' % json.dumps(GOOD)),
                 doc(r, extra='"meta": {"%s": 1, "%s": 1}, ' % (CANARY, CANARY))]
        for text in cases:
            with self.subTest(text=text[:60]):
                self.assertEqual(self.refused(text), WHY + "is not valid JSON")

    def test_nonfinite_tokens(self):
        for token in ("NaN", "Infinity", "-Infinity"):
            for text in (doc(rec(), extra='"extra": %s, ' % token), doc(rec().replace('"w"', '"w", "n": %s' % token))):
                with self.subTest(token=token):
                    self.assertEqual(self.refused(text), WHY + "is not valid JSON")

    def test_nesting_translated_at_the_seam(self):
        with mock.patch.object(flock_report, "ts_decode", side_effect=RecursionError(CANARY)):
            self.refused(doc(), NESTED)

    def test_real_deep_receipts(self):
        for text in ("[" * 100000 + "0" + "]" * 100000, '{"a":' * 100000 + "0" + "}" * 100000):
            self.refused(text, NESTED)

    def test_compatible_metadata_and_labels(self):
        got = self.read(doc(rec(GOOD, "text_reviewed"), rec("b" * 64, "ocr_flagged"), rec("c" * 64, "waiting", "Still waiting."),
                            extra='"run_id": "r", "counts": {"x": 1}, "future": [1, null, 2.5], '))
        self.assertEqual(got, {GOOD: "", "b" * 64: OCR, "c" * 64: "text state: Still waiting."})


class CliTest(unittest.TestCase):
    setUp, stage, outputs = FlockReportTest.setUp, FlockReportTest.stage, FlockReportTest.outputs

    def bad_files(self):
        deep = self.tmp / "deep.json"
        deep.write_text("[" * 100000 + "0" + "]" * 100000)
        out = {"short_hash": doc(rec("bad")), "upper_hash": doc(rec("B" * 64)), "empty_hash": doc(rec("")),
               "dup_state": doc(rec().replace('"state": "ocr_unreviewed"', '"state": "ocr_unreviewed", "state": "text_reviewed"')),
               "dup_meta": doc(rec(), extra='"%s": 1, "%s": 2, ' % (CANARY, CANARY)),
               "nonfinite": doc(rec(), extra='"%s": NaN, ' % CANARY)}
        paths = {}
        for name, text in out.items():
            paths[name] = self.tmp / (name + ".json")
            paths[name].write_text(text)
        paths["overdeep"] = deep
        return paths

    def snap(self, root):
        return sorted((str(p.relative_to(root)), p.lstat().st_mode, p.lstat().st_mtime_ns, p.read_bytes() if p.is_file() else b"")
                      for p in Path(root).rglob("*"))

    def no_leak(self, *streams):
        for s in streams:
            self.assertNotIn(CANARY, s)
            self.assertNotIn(str(self.tmp), s)
            self.assertNotIn("Traceback", s)

    def test_build_refuses_without_writing(self):
        for name, path in self.bad_files().items():
            with self.subTest(name=name):
                config, p = self.stage(review__text_state_file=str(path))
                code, out, err = run_tool("build", config)
                self.assertEqual(code, 1, out + err)
                self.assertTrue(out.startswith(WHY) and out.endswith(" errors=1\n"), out)
                self.no_leak(out, err)
                self.assertFalse((p["CO"] / "report").exists())

    def test_build_preserves_existing_draft(self):
        config, p = self.stage()
        self.assertEqual(base.build(config), (0, HAPPY))
        before, path = self.snap(p["CO"] / "report"), self.bad_files()["dup_state"]
        cfg = json.loads(config.read_text())
        cfg["review"]["text_state_file"] = str(path)
        config.write_text(json.dumps(cfg))
        want = path.read_bytes()
        code, out, err = run_tool("build", config)
        self.assertEqual(code, 1, out + err)
        self.assertEqual(self.snap(p["CO"] / "report"), before)  # bytes, modes, mtimes, no temps
        self.assertEqual(path.read_bytes(), want)
        self.no_leak(out, err)

    def test_check_text_and_json(self):
        for name in ("dup_state", "short_hash", "nonfinite", "overdeep"):
            path = self.bad_files()[name]
            config, p = self.stage(review__text_state_file=str(path))
            with self.subTest(name=name):
                code, out, err = run_tool("check", config)
                self.assertEqual(code, 1, out + err)
                self.assertIn(WHY, out)
                self.no_leak(out, err)
                code, out, err = run_tool("check", config, "--json")
                self.assertEqual(code, 1, out + err)
                data = json.loads(out)  # exactly one document
                self.assertEqual(data["state"], "invalid")
                self.assertTrue(any(e.startswith(WHY) for e in data["errors"]), data)
                self.assertEqual(err, "")
                self.no_leak(out, err)
                self.assertFalse((p["CO"] / "report").exists())

    def test_valid_receipts_unchanged(self):
        config, p = self.stage()
        self.assertEqual(base.build(config), (0, HAPPY))
        plain = {f.name: f.read_bytes() for f in (p["CO"] / "report").iterdir()}
        copy = self.tmp / "good.json"
        copy.write_bytes(TS.read_bytes())
        config2, p2 = self.stage(review__text_state_file=str(copy))
        self.assertEqual(base.build(config2), (0, HAPPY))
        text, _, _ = self.outputs(p2)
        self.assertEqual(text.count(OCR), 4)
        self.assertNotEqual({f.name: f.read_bytes() for f in (p2["CO"] / "report").iterdir()}, plain)


class RegionTest(unittest.TestCase):
    setUp, stage, agency_b, two = (getattr(reg.FlockReportRegionTest, n) for n in ("setUp", "stage", "agency_b", "two"))
    def test_invalid_agency_receipt_refuses_region(self):
        ag, matrix, out = self.two()
        bad = self.tmp / "bad-ts.json"
        bad.write_text(doc(rec("B" * 64)))
        cfg = json.loads(ag[reg.B][0].read_text())
        cfg["review"]["text_state_file"] = str(bad)
        ag[reg.B][0].write_text(json.dumps(cfg))
        args = ["--matrix", matrix, "--out", out] + [x for s in (reg.A, reg.B) for x in ("--agency", "%s=%s" % (s, ag[s][0]))]
        for extra in ((), ("--check",), ("--check", "--json")):
            with self.subTest(extra=extra):
                code, so, se = run_tool("region", *extra, *args)
                self.assertEqual(code, 1, so + se)
                self.assertIn("agency %s: %s" % (reg.B, WHY), so + se)
                self.assertNotIn(str(bad), so + se)
                self.assertTrue(CANARY not in so + se and "Traceback" not in so + se)
                self.assertFalse(out.exists())

