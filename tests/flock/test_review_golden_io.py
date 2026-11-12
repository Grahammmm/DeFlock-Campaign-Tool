"""RF1058-5: flock_review_golden names an unreadable stage file (CLI and batch gate) and a missing --write folder."""
import builtins, contextlib, csv, importlib.util, io, json, tempfile, unittest
from pathlib import Path
from unittest import mock

from tests.flock.test_review import ROOT
from tests.flock.test_review_golden import SAMPLE, golden_review


def load(name):
    spec = importlib.util.spec_from_file_location("_io_" + name, ROOT / "tools" / (name + ".py"))
    return spec.loader.exec_module(module := importlib.util.module_from_spec(spec)) or module


GOLD, BATCH = load("flock_review_golden"), load("flock_review_batch")
GATE = "golden gate: Morro Bay golden must pass first"


def run(tool, *args):
    shown = io.StringIO()
    with contextlib.redirect_stdout(shown), contextlib.redirect_stderr(shown):
        code = tool.main([str(a) for a in args])
    return code, shown.getvalue()


class FlockReviewGoldenIoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.tmp = Path(tmp.name)
        cls.config = golden_review(cls.tmp / "run")
        cls.c = GOLD.fr.context(str(cls.config))
        cls.keys = len(json.loads(SAMPLE.read_text()))

    def damage(self, path, extra):  # as if the stage itself wrote the bad bytes: the stage cache keeps the hash it recorded
        before, recorded = path.read_bytes(), GOLD.fr.fhash(path)
        self.addCleanup(path.write_bytes, before)
        path.write_bytes(before + extra)
        for fr in (GOLD.fr, BATCH.gold.fr):
            real = fr.fhash
            patcher = mock.patch.object(fr, "fhash", lambda p, real=real: recorded if str(p) == str(path) else real(p))
            patcher.start()
            self.addCleanup(patcher.stop)

    def assertNamed(self, got, line):
        self.assertEqual(got, (1, line + "\n"))
        self.assertNotIn("Traceback", got[1])
        self.assertNotIn(str(self.tmp), got[1])

    def test_happy_path_unchanged(self):
        self.assertEqual(run(GOLD, self.config, "--expect", SAMPLE), (0, "golden ok keys=%d\n" % self.keys))

    def test_non_utf8_facts_named(self):
        self.damage(Path(self.c["facts"]), b"\xff\xfe")
        self.assertNamed(run(GOLD, self.config, "--expect", SAMPLE), "golden: facts unreadable (UnicodeDecodeError)")

    def test_malformed_requirements_named(self):  # one field over the limit; facts and findings stay under it
        widest = max(len(v or "") for p in (self.c["facts"], self.c["CO"] / "FINDINGS.csv")
                     for row in GOLD.rows(p, "x") for v in row.values() if isinstance(v, str))
        self.damage(self.c["RO"] / "REQUIREMENTS.csv", b"R9," + b"x" * (widest + 16) + b"\n")
        old = csv.field_size_limit(widest + 1)
        try:
            got = run(GOLD, self.config, "--expect", SAMPLE)
        finally:
            csv.field_size_limit(old)
        self.assertNamed(got, "golden: requirements unreadable (Error)")

    def test_non_utf8_f3_findings_md_named(self):
        self.damage(self.c["RUN"] / "FINDINGS.md", b"\xff\xfe")
        self.assertNamed(run(GOLD, self.config, "--expect", SAMPLE), "golden: f3-check unreadable (UnicodeDecodeError)")

    def test_batch_gate_names_unreadable_facts(self):
        self.damage(Path(self.c["facts"]), b"\xff\xfe")
        listing = self.tmp / "list.txt"
        listing.write_text(str(self.config) + "\n")
        got = run(BATCH, "plan", listing, "--golden-config", self.config, "--golden-expect", SAMPLE)
        self.assertNamed(got, "golden: facts unreadable (UnicodeDecodeError)\n" + GATE)

    def test_write_into_missing_folder_named(self):
        folder = self.tmp / "nope"
        self.assertNamed(run(GOLD, self.config, "--write", folder / "counts.json"), "golden: --write folder does not exist")
        self.assertFalse(folder.exists())

    def test_write_onto_existing_file_unchanged(self):
        folder = self.tmp / "exists"
        folder.mkdir(exist_ok=True)
        target, stale = folder / "counts.json", folder / ".counts.json.old1234.tmp"
        target.write_text("kept\n"), stale.write_text("stale\n")
        self.assertEqual(run(GOLD, self.config, "--write", target), (1, "golden: --write file already exists\n"))
        self.assertEqual((target.read_text(), stale.read_text()), ("kept\n", "stale\n"))
        self.assertEqual(sorted(p.name for p in folder.iterdir()), [stale.name, target.name])

    def test_permission_error_stays_io(self):
        blocked, real = str(self.c["RO"] / "REQUIREMENTS.csv"), builtins.open

        def fake(file, *args, **kwargs):
            if str(file) == blocked:
                raise PermissionError(13, "denied")
            return real(file, *args, **kwargs)

        with mock.patch("builtins.open", fake):
            got = run(GOLD, self.config, "--expect", SAMPLE)
        self.assertNamed(got, "io: PermissionError")


if __name__ == "__main__":
    unittest.main()
