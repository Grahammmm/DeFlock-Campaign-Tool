"""flock_report check: the draft on disk equals a fresh build, or the stale/missing/linked file is named (RF1140-1)."""
import hashlib, json, os, subprocess, sys, unittest

from tests.flock import test_report as base

CURRENT = base.HAPPY[:-1] + " state=current\n"
DIFFERS, MISSING = "check: %s differs from a fresh build (rerun build)\n", "check: %s missing (run build)\n"
LINK = "check: %s is a link (refused)\n"


def check(config, *extra):
    done = subprocess.run([sys.executable, "-B", str(base.TOOL), "check", str(config), *map(str, extra)],
                          capture_output=True, text=True, check=False, timeout=60)
    assert done.stderr == "", done.stderr
    return done.returncode, done.stdout


class FlockReportCheckTest(unittest.TestCase):
    setUp, stage = base.FlockReportTest.setUp, base.FlockReportTest.stage

    def built(self, **kwargs):
        config, p = self.stage(**kwargs)
        self.assertEqual(base.build(config), (0, base.HAPPY))
        return config, p, p["CO"] / "report"

    def snap(self):  # every path under the temp tree: kind, size, mtime; links are never followed
        tree = {}
        for top, dirs, files in os.walk(self.tmp):
            for name in dirs + files:
                st = os.lstat(os.path.join(top, name))
                tree[os.path.relpath(os.path.join(top, name), self.tmp)] = (st.st_mode, st.st_size, st.st_mtime_ns)
        return tree

    def checked(self, config, *extra):  # check writes nothing anywhere in the tree and leaves no temp file
        before = self.snap()
        result = check(config, *extra)
        self.assertEqual(self.snap(), before)
        self.assertFalse([k for k in before if k.endswith(".tmp")])
        return result

    def state(self, out, state, *lines):
        self.assertEqual(out.splitlines()[-1].rsplit(" ", 1)[1], "state=" + state, out)
        self.assertTrue(out.splitlines()[-1].endswith(" errors=%d state=%s" % (len(out.splitlines()) - 1, state)), out)
        for line in lines:
            self.assertIn(line, out)

    def test_golden_and_out_dir(self):
        config, p, _ = self.built()
        self.assertEqual(self.checked(config), (0, CURRENT))
        elsewhere = self.tmp / "elsewhere"
        self.assertEqual(base.build(config, "--out", elsewhere), (0, base.HAPPY))
        self.assertEqual(self.checked(config, "--out", elsewhere), (0, CURRENT))
        code, out = self.checked(config, "--out", self.tmp / "never-built")
        self.assertEqual(code, 1)
        self.state(out, "missing", MISSING % "REPORT-DRAFT.md", MISSING % "SOURCES.json")
        self.assertFalse((self.tmp / "never-built").exists())

    def test_inputs_changed_after_build(self):
        config, p, _ = self.built()
        findings = p["CO"] / "FINDINGS.csv"
        findings.write_text(findings.read_text().replace(base.BASIS, "Plate reads are kept for two years."))
        plan = json.loads((p["CO"] / "VERIFY-PLAN.json").read_text())
        plan["findings_sha256"] = hashlib.sha256(findings.read_bytes()).hexdigest()
        (p["CO"] / "VERIFY-PLAN.json").write_text(json.dumps(plan))
        code, out = self.checked(config)
        self.assertEqual(code, 1)
        self.state(out, "stale", DIFFERS % "REPORT-DRAFT.md")
        self.assertNotIn("SOURCES.json", out)
        self.assertNotIn("two years", out)

    def test_hand_edit_names_only_that_file(self):
        config, p, report = self.built()
        draft, sources = report / "REPORT-DRAFT.md", report / "SOURCES.json"
        with open(draft, "a", encoding="utf-8") as handle:
            handle.write("- **Hand-added line** [1 p.9]\n")
        code, out = self.checked(config)
        self.assertEqual(code, 1)
        self.state(out, "stale", DIFFERS % "REPORT-DRAFT.md")
        self.assertNotIn("SOURCES.json", out)
        self.assertNotIn("Hand-added", out)
        config, p, report = self.built()  # bytes, not text: CRLF endings in SOURCES.json alone are stale
        (report / "SOURCES.json").write_bytes((report / "SOURCES.json").read_bytes().replace(b"\n", b"\r\n"))
        code, out = self.checked(config)
        self.assertEqual(code, 1)
        self.state(out, "stale", DIFFERS % "SOURCES.json")
        self.assertNotIn("REPORT-DRAFT.md", out)

    def test_missing(self):
        config, p = self.stage()
        code, out = self.checked(config)  # before any build: both named, the report folder is not created
        self.assertEqual(code, 1)
        self.state(out, "missing", MISSING % "REPORT-DRAFT.md", MISSING % "SOURCES.json")
        self.assertFalse((p["CO"] / "report").exists())
        config, p, report = self.built()
        os.unlink(report / "SOURCES.json")
        code, out = self.checked(config)
        self.assertEqual(code, 1)
        self.state(out, "missing", MISSING % "SOURCES.json")
        self.assertNotIn("REPORT-DRAFT.md", out)

    def test_link_and_special_file_refused(self):
        config, p, report = self.built()
        draft, copy = report / "REPORT-DRAFT.md", self.tmp / "copy.md"
        copy.write_bytes(draft.read_bytes())
        os.utime(copy, ns=(1_000_000_000, 1_000_000_000))
        os.unlink(draft)
        draft.symlink_to(copy)
        code, out = self.checked(config)
        self.assertEqual(code, 1)
        self.state(out, "invalid", LINK % "REPORT-DRAFT.md")
        self.assertEqual(copy.stat().st_mtime_ns, 1_000_000_000)
        os.unlink(report / "SOURCES.json")  # a link is reported before a missing file, and a dangling link is still a link
        os.unlink(copy)
        code, out = self.checked(config)
        self.state(out, "invalid", LINK % "REPORT-DRAFT.md")
        self.assertNotIn("SOURCES.json", out)
        config, p, report = self.built()
        os.unlink(report / "SOURCES.json")
        os.mkfifo(report / "SOURCES.json")  # never waits on a fifo
        code, out = self.checked(config)
        self.assertEqual(code, 1)
        self.state(out, "invalid", "check: SOURCES.json is not a regular file (refused)\n")

    def test_inputs_no_longer_build(self):
        config, p, _ = self.built()
        plan = json.loads((p["CO"] / "VERIFY-PLAN.json").read_text())
        plan["findings_sha256"] = hashlib.sha256(b"older findings").hexdigest()
        (p["CO"] / "VERIFY-PLAN.json").write_text(json.dumps(plan))
        code, out = self.checked(config)
        self.assertEqual(code, 1)
        self.state(out, "invalid", "verify: VERIFY-PLAN findings_sha256 does not match FINDINGS.csv\n")
        self.assertNotIn("check:", out)

    def test_withheld_record_after_build(self):
        config, p, _ = self.built()
        inventory = p["FO"] / "INVENTORY.csv"
        lines = inventory.read_text().splitlines(True)
        row = next(i for i, line in enumerate(lines) if ",1001," in line)
        lines[row] = lines[row].rstrip("\n")[:-len("no")] + "yes\n"
        inventory.write_text("".join(lines))
        code, out = self.checked(config)
        self.assertEqual(code, 1)
        self.state(out, "invalid")
        self.assertNotIn("check:", out)
        for secret in (base.HIDDEN, "1001", "sample retention schedule", "b" * 64):
            self.assertNotIn(secret, out.lower())

    def test_out_inside_repository(self):
        inside = base.ROOT / "tests" / "fixtures" / "flock" / "report" / "out"
        config, p = self.stage()
        self.assertEqual(self.checked(config, "--out", inside), (1, "out: is inside the repository\nincluded=0 verified_agree=0 "
                         "unchecked=0 withheld_analysis_only=0 withheld_verify=0 sources=0 errors=1 state=invalid\n"))
        self.assertFalse(inside.exists())

    def test_cli(self):
        done = subprocess.run([sys.executable, "-B", str(base.TOOL), "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0)
        self.assertIn("{build,check}", done.stdout)
        self.assertIn("  check CONFIG [--out DIR]", done.stdout)
        done = subprocess.run([sys.executable, "-B", str(base.TOOL), "check"], capture_output=True, text=True, check=False)
        self.assertEqual((done.returncode, done.stdout), (2, ""))
        self.assertIn("config", done.stderr)


if __name__ == "__main__":
    unittest.main()
