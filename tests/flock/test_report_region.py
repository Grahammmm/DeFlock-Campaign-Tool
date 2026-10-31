"""flock_report region: a private cross-agency draft from COMPARE-MATRIX.csv, cited only from current agency drafts (NB-DF-10)."""
import csv, hashlib, json, os, re, shutil, subprocess, sys, unittest

from tests.flock import test_report as base
from tests.flock.test_review import SLUG

A, B = SLUG, "north-county-sheriff"
A_NAME, B_NAME = "Sample City Police Department", "North County Sheriff"
MATRIX = ("topic,claim_class,%s,%s\naudit,duty,medium,strong\naudit,claim,weak,-\nretention,duty,strong,rejected\n"
          "sharing,claim,weak,-\n" % (A, B))
ZERO = "rejected: 0, no rating: 0, not current: 0, no current source: 0"
HAPPY = "region topics=3 agencies=2 current=2 excluded=0 sources=5\n"
NAMES = ("REGION-REPORT-DRAFT.md", "SOURCES.json")


def region(*args):
    done = subprocess.run([sys.executable, "-B", str(base.TOOL), "region", *map(str, args)],
                          capture_output=True, text=True, check=False, timeout=120)
    return done.returncode, done.stdout, done.stderr


class FlockReportRegionTest(unittest.TestCase):
    setUp, stage = base.FlockReportTest.setUp, base.FlockReportTest.stage

    def agency_b(self, hidden=("99999999", "Sample hidden log.xlsx"), deny=None, **kwargs):
        """A second agency: same fictional fixtures under its own slug, display name and cited document."""
        config, p = self.stage(**kwargs)
        cfg = json.loads(config.read_text())
        cfg["review"]["slug"], cfg["facts_intake"]["fact_agency"] = B, B_NAME
        if deny:
            cfg["review"]["report_denylist"] = deny
        config.write_text(json.dumps(cfg))
        src = self.tmp / str(self.n) / "facts-b.csv"  # fact ids hash the agency: import B's own, then repoint its findings
        src.write_text((self.tmp / str(self.n) / "facts-v1.csv").read_text().replace(A_NAME, B_NAME))
        subprocess.run([sys.executable, "-B", str(base.ROOT / "tools" / "flock_facts.py"), "import", str(src), "--store",
                        str(p["FO"] / "store"), "--added-by", "tester", "--date", "2026-10-30"], capture_output=True, check=True)
        key = lambda r: (r["source_sha256"], r["page"], r["fact_type"], r["value"])  # noqa: E731
        ids = [{key(r): r["fact_id"] for r in csv.DictReader((p["FO"] / "store" / "agencies" / s / "FACTS.csv").read_text().splitlines())}
               for s in (A, B)]
        findings = p["CO"] / "FINDINGS.csv"
        text = findings.read_text()
        for k, old in ids[0].items():
            text = text.replace(old, ids[1][k])
        findings.write_text(text)
        plan = json.loads((p["CO"] / "VERIFY-PLAN.json").read_text())
        plan["findings_sha256"] = hashlib.sha256(findings.read_bytes()).hexdigest()
        (p["CO"] / "VERIFY-PLAN.json").write_text(json.dumps(plan))
        inventory = p["FO"] / "INVENTORY.csv"
        text = inventory.read_text().replace(",1001,Sample retention schedule.pdf,", ",2002,North records index.pdf,")
        inventory.write_text(text.replace(",99999999,Sample hidden log.xlsx,", ",%s,%s," % hidden))
        return config, p

    def two(self, matrix=MATRIX, b=None, **kwargs):  # -> agency configs, matrix dir, out dir; both reports built and current
        (ca, pa), (cb, pb) = self.stage(**kwargs), self.agency_b(**(b or {}))
        for config in (ca, cb):
            self.assertEqual(base.build(config)[0], 0, base.build(config))
        (self.tmp / "matrix").mkdir(exist_ok=True)
        (self.tmp / "matrix" / "COMPARE-MATRIX.csv").write_text(matrix)
        return {A: (ca, pa), B: (cb, pb)}, self.tmp / "matrix", self.tmp / "region"

    def run_region(self, ag, matrix, out, *extra, slugs=(A, B)):
        args = ["--matrix", matrix, "--out", out, *extra]
        for slug in slugs:
            args += ["--agency", "%s=%s" % (slug, ag[slug][0])]
        return region(*args)

    def outputs(self, out):
        return (out / NAMES[0]).read_text(), json.loads((out / NAMES[1]).read_text())

    def refused(self, out, result, code, *expected):
        self.assertEqual(result[0], code, result)
        for line in expected:
            self.assertIn(line, result[1] + result[2])
        self.assertFalse([n for n in NAMES if (out / n).exists()])
        self.assertFalse([f for f in (os.listdir(out) if out.is_dir() else ()) if f.endswith(".tmp")])

    def test_two_current_agencies_three_topics(self):
        ag, matrix, out = self.two()
        self.assertEqual(self.run_region(ag, matrix, out), (0, HAPPY, ""))
        text, sources = self.outputs(out)
        lines = text.splitlines()
        self.assertEqual(lines[0], base.LINE1)
        self.assertEqual([l for l in lines if l.startswith("- ")], [
            "- audit: strong %s [1, 2]; medium %s [3, 4]; weak none; %s" % (B_NAME, A_NAME, ZERO),
            "- retention: strong %s [3, 5]; medium none; weak none; %s" % (A_NAME, ZERO.replace("rejected: 0", "rejected: 1")),
            "- sharing: strong none; medium none; weak %s [3, 5]; %s" % (A_NAME, ZERO.replace("no rating: 0", "no rating: 1"))])
        self.assertEqual([(e["n"], e["agency"], e["kind"]) for e in sources],
                         [(1, B, "record"), (2, B, "rule"), (3, A, "record"), (4, A, "rule"), (5, A, "rule")])
        self.assertEqual(sources[0]["label"], "Request REQ-101, document 2002 (North records index), page 3, 4, 5")
        self.assertEqual(sources[2]["label"], "Request REQ-101, document 1001 (Sample retention schedule), page 3, 4, 5")
        self.assertEqual(sources[2]["pages"], ["3", "4", "5"])
        self.assertTrue(sources[3]["label"].startswith("Sample State Code") and sources[4]["label"].startswith("Sample City ALPR"))
        cited = {int(n) for g in re.findall(r"\[([0-9, ]+)\]", "\n".join(lines[:lines.index("## Sources")])) for n in g.split(", ")}
        self.assertEqual(cited, {e["n"] for e in sources})
        for e in sources:
            self.assertIn("[%d] %s: %s" % (e["n"], A_NAME if e["agency"] == A else B_NAME, e["label"]), lines)
        self.assertEqual({oct(os.stat(out / n).st_mode & 0o777) for n in NAMES}, {"0o600"})
        before = {n: (out / n).read_bytes() for n in NAMES}  # numbering is stable: a rerun gives the same bytes
        self.assertEqual(self.run_region(ag, matrix, out, "--replace"), (0, HAPPY, ""))
        self.assertEqual({n: (out / n).read_bytes() for n in NAMES}, before)
        kept = sorted(f for f in os.listdir(out) if ".superseded-" in f)
        self.assertEqual([f.split(".superseded-")[0] for f in kept], sorted(NAMES))
        self.assertEqual(sorted((out / f).read_bytes() for f in kept), sorted(before.values()))

    def test_stale_agency_excluded(self):
        ag, matrix, out = self.two()
        draft = ag[A][1]["CO"] / "report" / "REPORT-DRAFT.md"
        draft.write_text(draft.read_text() + "- **Hand-added line** [1 p.9]\n")
        self.assertEqual(self.run_region(ag, matrix, out), (0, HAPPY.replace("current=2 excluded=0 sources=5",
                                                                              "current=1 excluded=1 sources=2"), ""))
        text, sources = self.outputs(out)
        self.assertEqual({e["agency"] for e in sources}, {B})
        self.assertNotIn(A_NAME, text)
        self.assertNotIn("Sample retention schedule", text + json.dumps(sources))
        bullets = [l for l in text.splitlines() if l.startswith("- ")]
        self.assertEqual(len(bullets), 3)
        self.assertTrue(all("not current: 1" in l for l in bullets), bullets)
        self.assertIn("- retention: strong none; medium none; weak none; rejected: 1, no rating: 0, not current: 1", text)
        os.unlink(ag[B][1]["CO"] / "report" / "SOURCES.json")  # a missing draft is excluded the same way
        code, stdout, _ = self.run_region(ag, matrix, out, "--replace")
        self.assertEqual((code, stdout), (0, "region topics=3 agencies=2 current=0 excluded=2 sources=0\n"))
        text, sources = self.outputs(out)
        self.assertEqual(sources, [])
        self.assertNotIn(B_NAME, text)

    def test_rejected_and_uncited_counted_never_named(self):
        matrix = MATRIX.replace("sharing,claim,weak,-", "sharing,claim,rejected,rejected") + "vendor,duty,strong,rejected\n"
        ag, mdir, out = self.two(matrix=matrix)
        self.assertEqual(self.run_region(ag, mdir, out)[:2], (0, HAPPY.replace("topics=3", "topics=4")))
        text, _ = self.outputs(out)
        line = {l.split(":")[0]: l for l in text.splitlines() if l.startswith("- ")}
        self.assertEqual(line["- sharing"], "- sharing: strong none; medium none; weak none; rejected: 2, no rating: 0, "
                                            "not current: 0, no current source: 0")
        self.assertNotIn(B_NAME, line["- retention"])  # B is rejected on retention: counted only
        self.assertEqual(line["- vendor"], "- vendor: strong none; medium none; weak none; rejected: 1, no rating: 0, "
                                           "not current: 0, no current source: 1")  # A's vendor finding is not shown

    def test_hidden_values_never_appear(self):
        ag, matrix, out = self.two()
        self.assertEqual(self.run_region(ag, matrix, out)[0], 0)
        both = "\n".join((out / n).read_text() for n in NAMES).lower()
        for hidden in ("99999999", "sample hidden log", base.HIDDEN, "date and plate only", "training materials"):
            self.assertNotIn(hidden, both)
        cases = [("Sample retention schedule", {"hidden": ("77777777", "Sample retention schedule.pdf")}),
                 ("1001", {"hidden": ("1001", "Other hidden log.xlsx")}),
                 ("document 1001", {"deny": ["Document 1001"]})]  # agency B's hidden title, doc id or denylist found in agency A's text
        for planted, b in cases:
            with self.subTest(planted=planted):
                self.setUp()
                ag, matrix, out = self.two(b=b)
                code, stdout, stderr = self.run_region(ag, matrix, out)
                self.refused(out, (code, stdout, stderr), 1)
                self.assertTrue(stdout.splitlines() and all(re.fullmatch(r"line \d+: denylisted string( \(SOURCES.json\))?", l)
                                                            for l in stdout.splitlines()), stdout)
                self.assertNotIn(planted.lower(), stdout.lower())
                self.assertFalse(out.exists())

    def test_column_mapping(self):
        ag, matrix, out = self.two()
        self.refused(out, self.run_region(ag, matrix, out, slugs=(A,)), 2, "matrix column %s has no --agency" % B)
        extra = "\n".join(l.rsplit(",", 1)[0] for l in MATRIX.splitlines()) + "\n"
        (matrix / "COMPARE-MATRIX.csv").write_text(extra)
        self.refused(out, self.run_region(ag, matrix, out), 2, "--agency %s has no matrix column" % B)
        (matrix / "COMPARE-MATRIX.csv").write_text(MATRIX)
        self.refused(out, self.run_region(ag, matrix, out, slugs=(A, B, A)), 2, "--agency %s: give SLUG=CONFIG once" % A)
        swapped = region("--matrix", matrix, "--out", out, "--agency", "%s=%s" % (A, ag[B][0]), "--agency", "%s=%s" % (B, ag[A][0]))
        self.refused(out, swapped, 1, "agency %s: config: review.slug is not the matrix column" % A)
        (matrix / "COMPARE-MATRIX.csv").write_text(MATRIX.replace("strong,rejected", "strongest,rejected"))
        self.refused(out, self.run_region(ag, matrix, out), 1, "matrix: COMPARE-MATRIX.csv has no rows or a cell")
        for header in (MATRIX.replace(",%s\n" % B, ",%s\n" % A, 1), MATRIX.replace("topic,claim_class", "claim_class,topic", 1)):
            (matrix / "COMPARE-MATRIX.csv").write_text(header)  # a slug twice, or the columns out of order
            self.refused(out, self.run_region(ag, matrix, out), 1, "matrix: COMPARE-MATRIX.csv header is not topic")
        os.unlink(matrix / "COMPARE-MATRIX.csv")
        self.refused(out, self.run_region(ag, matrix, out), 1, "io: FileNotFoundError")
        self.assertFalse(out.exists())

    def test_out_refused(self):
        ag, matrix, out = self.two()
        inside = base.ROOT / "tests" / "fixtures" / "flock" / "report" / "region-out"
        self.refused(inside, self.run_region(ag, matrix, inside), 1, "out: is inside the repository\n")
        self.assertFalse(inside.exists())
        for store in (ag[A][1]["CO"] / "region", ag[B][1]["FO"] / "store" / "region"):
            self.refused(store, self.run_region(ag, matrix, store), 1, "out: is inside an agency store\n")
            self.assertFalse(store.exists())
        out.mkdir()
        (out / NAMES[1]).write_text("organizer copy\n")
        code, stdout, _ = self.run_region(ag, matrix, out)
        self.assertEqual((code, stdout), (1, "out: SOURCES.json exists; pass --replace to supersede it\n"))
        self.assertEqual((out / NAMES[1]).read_text(), "organizer copy\n")
        self.assertFalse((out / NAMES[0]).exists())
        os.unlink(out / NAMES[1])
        (out / NAMES[1]).symlink_to(ag[A][1]["CO"] / "FINDINGS.csv")
        code, stdout, _ = self.run_region(ag, matrix, out, "--replace")
        self.assertEqual((code, stdout), (1, "out: SOURCES.json is not a regular file\n"))
        self.assertTrue((out / NAMES[1]).is_symlink())

    def test_lint_hit_writes_nothing(self):
        planted = lambda text, _: text.replace(base.BASIS, base.BASIS + " Ask name@city.example.")  # noqa: E731
        (ca, pa), (cb, pb) = self.stage(), self.agency_b()
        for config in (ca, cb):
            self.assertEqual(base.build(config)[0], 0, base.build(config))
        shutil.copyfile(pa["CO"] / "FINDINGS.csv", self.tmp / "clean.csv")  # the cited SC-01 basis now carries an email
        (pa["CO"] / "FINDINGS.csv").write_text(planted((pa["CO"] / "FINDINGS.csv").read_text(), None))
        plan = json.loads((pa["CO"] / "VERIFY-PLAN.json").read_text())
        plan["findings_sha256"] = hashlib.sha256((pa["CO"] / "FINDINGS.csv").read_bytes()).hexdigest()
        (pa["CO"] / "VERIFY-PLAN.json").write_text(json.dumps(plan))
        (self.tmp / "matrix").mkdir()
        (self.tmp / "matrix" / "COMPARE-MATRIX.csv").write_text(MATRIX)
        out, ag = self.tmp / "region", {A: (ca, pa), B: (cb, pb)}
        code, stdout, stderr = self.run_region(ag, self.tmp / "matrix", out)
        self.refused(out, (code, stdout, stderr), 1, "agency %s: row SC-01: email-like string\n" % A)
        self.assertNotIn("name@city.example", stdout + stderr)
        self.assertFalse(out.exists())
        (self.tmp / "matrix" / "COMPARE-MATRIX.csv").write_text(MATRIX.replace("sharing,", "ask x@y.example,"))
        shutil.copyfile(self.tmp / "clean.csv", pa["CO"] / "FINDINGS.csv")
        plan["findings_sha256"] = hashlib.sha256((pa["CO"] / "FINDINGS.csv").read_bytes()).hexdigest()
        (pa["CO"] / "VERIFY-PLAN.json").write_text(json.dumps(plan))
        code, stdout, stderr = self.run_region(ag, self.tmp / "matrix", out)
        self.refused(out, (code, stdout, stderr), 1)
        self.assertEqual(stdout, "line 7: email-like string\n")
        self.assertFalse(out.exists())

    def test_cli(self):
        done = subprocess.run([sys.executable, "-B", str(base.TOOL), "--help"], capture_output=True, text=True, check=False)
        self.assertIn("  region --matrix DIR --agency SLUG=CONFIG ... --out DIR [--replace]", done.stdout)
        code, stdout, stderr = region("--matrix", self.tmp)
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("--out", stderr)


if __name__ == "__main__":
    unittest.main()
