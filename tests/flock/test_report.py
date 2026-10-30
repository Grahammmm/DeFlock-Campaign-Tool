import csv, hashlib, json, re, shutil, stat, subprocess, sys, tempfile, unittest
from pathlib import Path

from tests.flock.test_review import FIX, ROOT, SLUG, make_review

TOOL, RFIX, HIDDEN = ROOT / "tools" / "flock_report.py", FIX / "report", "c" * 64
LINE1 = "Research draft - not legal advice. Built from independently checked findings; review required before any use."
HAPPY = "included=3 verified_agree=3 unchecked=3 withheld_analysis_only=1 withheld_verify=1 sources=3 errors=0\n"
BASIS = "Plate reads are kept for 365 days."


def build(config, *extra):
    done = subprocess.run([sys.executable, "-B", str(TOOL), "build", str(config), *map(str, extra)],
                          capture_output=True, text=True, check=False)
    return done.returncode, done.stdout


class FlockReportTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp, self.n = Path(tmp.name), 0

    def stage(self, edit=None, drop=None, plan_sha=None, verify=True, **overrides):
        self.n += 1
        config, p = make_review(self.tmp / str(self.n), **overrides)
        for key, names in (("FO", ["INVENTORY.csv"]), ("RO", ["REQUIREMENTS.csv"]), ("CO", ["FINDINGS.csv", "VERIFY.csv"])):
            p[key].mkdir(parents=True, exist_ok=True)
            for name in names:
                shutil.copyfile(RFIX / name, p[key] / name)
        src = self.tmp / str(self.n) / "facts-v1.csv"
        src.write_text((FIX / "compare-facts.csv").read_text()
                       + "Sample City Police Department,REQ-101,search_reasons,none,%s,1,No reason field is shown.\n" % HIDDEN)
        subprocess.run([sys.executable, "-B", str(ROOT / "tools" / "flock_facts.py"), "import", str(src), "--store", str(p["FO"] / "store"),
                        "--added-by", "tester", "--date", "2026-10-30"], capture_output=True, check=True)
        with open(p["FO"] / "store" / "agencies" / SLUG / "FACTS.csv", newline="") as handle:
            hidden_fact = next(r["fact_id"] for r in csv.DictReader(handle) if r["source_sha256"] == HIDDEN)
        findings, verify_csv = p["CO"] / "FINDINGS.csv", p["CO"] / "VERIFY.csv"
        if edit:
            findings.write_text(edit(findings.read_text(), hidden_fact))
        if drop:
            verify_csv.write_text("".join(l for l in verify_csv.read_text().splitlines(True) if not l.startswith(drop + ",")))
        if not verify:
            verify_csv.unlink()
        plan = {"schema": 1, "findings_sha256": plan_sha or hashlib.sha256(findings.read_bytes()).hexdigest(),
                "priority_ids": ["SC-01", "SC-02", "SC-03"], "sample_ids": ["SC-06", "SC-04"]}
        (p["CO"] / "VERIFY-PLAN.json").write_text(json.dumps(plan))
        return config, p

    def outputs(self, p):
        report = p["CO"] / "report"
        return (report / "REPORT-DRAFT.md").read_text(), json.loads((report / "SOURCES.json").read_text()), report

    def test_happy_path(self):
        config, p = self.stage()
        self.assertEqual(build(config), (0, HAPPY))
        text, sources, report = self.outputs(p)
        lines = text.splitlines()
        self.assertEqual(lines[0], LINE1)
        bullets = [line for line in lines if line.startswith("- ")]
        self.assertEqual(len(bullets), 3)
        cited = set()
        for line in bullets:
            group = re.search(r"\[([^\[\]]+)\]$", line)
            self.assertIsNotNone(group, line)
            cited |= {int(item.split()[0]) for item in group.group(1).split(", ")}
        self.assertEqual(cited, {e["n"] for e in sources})
        self.assertEqual([e["n"] for e in sources], [1, 2, 3])
        record = next(e for e in sources if e["kind"] == "record")
        self.assertEqual(record["label"], "Request REQ-101, document 1001 (Sample retention schedule), page 3, 4, 5")
        self.assertIn("[%d] %s" % (record["n"], record["label"]), lines)
        self.assertIn("- **Plate reads are deleted after 30 days** - the records show the opposite. %s [1 p.5, 2 section 4.2 page 2]"
                      % BASIS, lines)
        self.assertEqual({stat.S_IMODE(f.stat().st_mode) for f in report.iterdir()}, {0o600})
        before = {f.name: f.read_bytes() for f in report.iterdir()}
        self.assertEqual(build(config), (0, HAPPY))
        self.assertEqual({f.name: f.read_bytes() for f in report.iterdir()}, before)

    def test_blocking(self):
        basis = lambda extra: {"edit": lambda text, _: text.replace(BASIS, BASIS + " " + extra)}  # noqa: E731
        cases = [("99999999", "row SC-01: denylisted string", basis("See 99999999.")),
                 ("Sample hidden log", "row SC-01: denylisted string", basis("See Sample hidden log.")),
                 ("LOG-SUMMARY-by-user", "row SC-01: denylisted string",
                  dict(basis("See LOG-SUMMARY-by-user."), review__report_denylist=["LOG-SUMMARY-by-user"])),
                 ("violated", "row SC-01: quote-gated wording", basis("The agency violated it.")),
                 ("name@city.example", "row SC-01: email-like string", basis("Ask name@city.example.")),
                 (HIDDEN, "row SC-01: cite resolves to an analysis-only record",
                  {"edit": lambda text, fact: text.replace("F-53baa6f20643", fact)}),
                 (None, "row SC-02: priority row has no verify result", {"drop": "SC-02"}),
                 (None, "verify: VERIFY-PLAN findings_sha256 does not match FINDINGS.csv",
                  {"plan_sha": hashlib.sha256(b"older findings").hexdigest()}),
                 (None, "verify: missing (run the G2 verify first)", {"verify": False})]
        for offending, expected, kwargs in cases:
            with self.subTest(expected=expected, offending=offending):
                config, p = self.stage(**kwargs)
                code, out = build(config)
                self.assertEqual(code, 1, out)
                self.assertIn(expected + "\n", out)
                self.assertFalse((p["CO"] / "report").exists())
                for secret in (offending or HIDDEN, "99999999", "sample hidden log", HIDDEN):
                    self.assertNotIn(secret.lower(), out.lower())
        inside = ROOT / "tests" / "fixtures" / "flock" / "report" / "out"
        config, p = self.stage()
        self.assertEqual(build(config, "--out", inside), (1, "out: is inside the repository\nincluded=0 verified_agree=0 unchecked=0 "
                                                          "withheld_analysis_only=0 withheld_verify=0 sources=0 errors=1\n"))
        self.assertFalse(inside.exists())

    def test_preexisting_temp_names(self):
        config, p = self.stage()
        report, findings = p["CO"] / "report", p["CO"] / "FINDINGS.csv"
        report.mkdir()
        before = findings.read_bytes()
        (report / ".REPORT-DRAFT.md.tmp").symlink_to(findings)
        (report / ".SOURCES.json.tmp").write_text("stale")
        (report / ".SOURCES.json.tmp").chmod(0o644)
        self.assertEqual(build(config), (0, HAPPY))
        self.assertEqual(findings.read_bytes(), before)
        self.assertEqual((report / ".SOURCES.json.tmp").read_text(), "stale")
        self.assertEqual({stat.S_IMODE((report / n).lstat().st_mode) for n in ("REPORT-DRAFT.md", "SOURCES.json")}, {0o600})

    def test_withholding(self):
        config, p = self.stage()
        self.assertEqual(build(config)[0], 0)
        text, sources, _ = self.outputs(p)
        both = text + json.dumps(sources)
        for withheld in ("Users complete annual system training", "Every search records a stated reason", "Training materials are withheld",
                         "date and plate only"):
            self.assertNotIn(withheld, both)
        self.assertIn("| kind | met | not met | contradicted | cannot tell | n/a | total |", text)
        self.assertIn("| requirement | 1 | 1 | 2 | 1 | 1 | 6 |", text)
        self.assertIn("| setting | 0 | 0 | 0 | 1 | 0 | 1 |", text)


if __name__ == "__main__":
    unittest.main()
