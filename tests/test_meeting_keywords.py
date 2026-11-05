"""BF-df-meeting-keywords: one ALPR keyword list (data/meeting-keywords.json) for Python and the Workers."""
import json, os, re, shutil, subprocess, sys, tempfile, unittest  # noqa: E401
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign_tool.meetings import KEYWORDS, KEYWORDS_FILE, MeetingsError, load_keywords  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "meeting-keyword-parity.json"
GENERATED = Path("workers") / "shared" / "src" / "generated" / "meeting-keywords.ts"
SYNC = Path("workers") / "scripts" / "sync-data.mjs"
# Synthetic contact data planted in the fixture; it must never reach a generated file or any output.
PLANTED = ("clerk.synthetic@example.invalid", "resident@example.invalid", "555-0142", "555-0199", "7SYN123", "8SYN999")
NODE = shutil.which("node")


def cases():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


class ParityTest(unittest.TestCase):
    def test_fixture_covers_the_spec(self):
        texts = [c["text"].lower() for c in cases()]
        self.assertGreaterEqual(len(texts), 24)
        for needle in ("license plate", "license-plate", "licenseplate", "alprs", "flocking", "alprx", "surveillance"):
            self.assertTrue(any(needle in t for t in texts), needle)
        self.assertIn(False, [c["match"] for c in cases()])

    def test_python_verdicts_match_fixture(self):
        for index, case in enumerate(cases()):
            with self.subTest(case=index):  # index only: fixture text is never echoed
                self.assertIs(bool(KEYWORDS.search(case["text"])), case["match"])

    def test_pattern_is_the_json_joined_and_matches_generated_source(self):
        spec = json.loads(KEYWORDS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(KEYWORDS.pattern, "|".join(spec["alternatives"]))
        self.assertTrue(KEYWORDS.flags & re.IGNORECASE)
        line = [l for l in (ROOT / GENERATED).read_text(encoding="utf-8").splitlines() if "MEETING_KEYWORDS_SOURCE" in l]
        self.assertEqual(json.loads(line[0].split(" = ", 1)[1].rstrip(";")), KEYWORDS.pattern)


class LoadErrorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "meeting-keywords.json"

    def assert_refused(self, content):
        if content is not None:
            self.path.write_text(content, encoding="utf-8")
        with self.assertRaises(MeetingsError) as ctx:
            load_keywords(self.path)
        self.assertIn(str(self.path), str(ctx.exception))

    def test_missing_malformed_and_invalid_files_raise_naming_the_file(self):
        good = {"schema_version": 1, "flags": "i", "alternatives": ["flock"]}
        self.path.write_text(json.dumps(good), encoding="utf-8")
        self.assertTrue(load_keywords(self.path).search("FLOCK"))
        self.path.unlink()
        self.assert_refused(None)
        self.assert_refused("{not json")
        self.assert_refused("[]")
        for change in ({"schema_version": 2}, {"schema_version": True}, {"flags": ""}, {"alternatives": []},
                       {"alternatives": "flock"}, {"alternatives": [""]}, {"alternatives": [1]},
                       {"alternatives": ["(unclosed"]}):
            with self.subTest(change=sorted(change)):
                self.assert_refused(json.dumps({**good, **change}))

    def test_import_without_the_file_raises_instead_of_falling_back(self):
        root = Path(self.tmp.name).resolve() / "repo"
        shutil.copytree(ROOT / "campaign_tool", root / "campaign_tool", ignore=shutil.ignore_patterns("__pycache__"))
        packaged = root / "campaign_tool" / "_resources" / "data" / "meeting-keywords.json"
        packaged.unlink()
        result = subprocess.run([sys.executable, "-B", "-c", "import campaign_tool.meetings"], cwd=root,
                                capture_output=True, text=True, timeout=120, env={**os.environ, "PYTHONPATH": str(root)})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MeetingsError", result.stderr)
        self.assertIn(str(packaged), result.stderr)

    def test_installed_package_reads_the_packaged_mirror_of_the_canonical_list(self):
        # The wheel ships campaign_tool/ only; the list must resolve inside the package, byte-equal to data/.
        self.assertEqual(KEYWORDS_FILE, ROOT / "campaign_tool" / "_resources" / "data" / "meeting-keywords.json")
        self.assertEqual(KEYWORDS_FILE.read_bytes(), (ROOT / "data" / "meeting-keywords.json").read_bytes())


@unittest.skipUnless(NODE, "node is required for workers/scripts/sync-data.mjs")
class GeneratorTest(unittest.TestCase):
    SOURCES = ("data", "jurisdictions", "workers/schema/d1.sql", "campaign_tool/assets/styles.css.tmpl", str(SYNC),
               "workers/wizard/src/generated", "workers/shared/src/generated")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in self.SOURCES:
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (shutil.copytree if (ROOT / name).is_dir() else shutil.copy2)(ROOT / name, self.root / name)
        self.spec = json.loads((self.root / "data" / "meeting-keywords.json").read_text(encoding="utf-8"))

    def sync(self, *args, root=None):
        result = subprocess.run([NODE, str((root or self.root) / SYNC), *args], capture_output=True, text=True, timeout=120)
        for planted in PLANTED:
            self.assertNotIn(planted, result.stdout + result.stderr)
        return result

    def write_spec(self, alternatives):
        (self.root / "data" / "meeting-keywords.json").write_text(
            json.dumps({**self.spec, "alternatives": alternatives}, indent=2) + "\n", encoding="utf-8")

    def snapshot(self):
        return {p: p.read_bytes() for p in sorted((self.root / "workers").rglob("*")) if p.is_file()}

    def test_check_is_clean_at_head_and_generated_file_is_byte_identical(self):
        self.assertEqual(self.sync("--check", root=ROOT).returncode, 0)
        (self.root / GENERATED).unlink()
        self.assertEqual(self.sync().returncode, 0)
        self.assertEqual((self.root / GENERATED).read_bytes(), (ROOT / GENERATED).read_bytes())

    def test_check_fails_on_drift_naming_the_target_then_regeneration_clears_it(self):
        self.write_spec(self.spec["alternatives"][:-1] + ["surveilance"])
        stale = self.sync("--check")
        self.assertEqual(stale.returncode, 1)
        self.assertEqual([l for l in stale.stderr.splitlines() if l.startswith("stale: ")],
                         ["stale: " + str(self.root / GENERATED)])
        self.assertEqual(self.sync().returncode, 0)
        self.assertEqual(self.sync("--check").returncode, 0)
        self.assertIn('|surveilance";', (self.root / GENERATED).read_text(encoding="utf-8"))

    def test_dialect_lint_refuses_by_name_and_writes_nothing(self):
        (self.root / GENERATED).unlink()  # a write would recreate it
        before = self.snapshot()
        for alternative, construct in (("(?i)flock", "(?i)"), ("(?P<vendor>flock)", "(?P<vendor>"),
                                       ("(?<vendor>flock)", "(?<vendor>"), ("\\p{L}lock", "\\p{L}"),
                                       ("fl\\1ock", "\\1"), ("\\Aflock", "\\A"), ("flock.*", ".")):
            with self.subTest(construct=construct):
                self.write_spec(["flock", alternative])
                result = self.sync()
                self.assertEqual(result.returncode, 1)
                self.assertIn("unsupported construct " + json.dumps(construct), result.stderr)
                self.assertIn(json.dumps(alternative), result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(self.snapshot(), before)


class OneCopyTest(unittest.TestCase):
    def test_keyword_literals_live_only_in_the_json_and_generated_file_has_banner(self):
        listed = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=ROOT,
                                capture_output=True, check=True).stdout.decode().split("\0")
        needles = ("license" + "[ -]?plate", "|" + "surveillance", "surveillance" + "|", "\\b" + "ALPRs?")
        mirror = "campaign_tool/_resources/data/meeting-keywords.json"  # byte-equal packaged copy, checked below
        holders = [n for n in listed if Path(n).suffix in {".py", ".ts", ".mjs", ".js", ".json", ".md", ".sql"}
                   and not n.startswith(str(GENERATED.parent) + "/") and n != mirror and (ROOT / n).is_file()
                   and any(s in (ROOT / n).read_text(encoding="utf-8", errors="replace") for s in needles)]
        self.assertEqual(holders, ["data/meeting-keywords.json"])
        self.assertEqual((ROOT / mirror).read_bytes(), (ROOT / holders[0]).read_bytes())
        self.assertTrue((ROOT / GENERATED).read_text(encoding="utf-8").startswith(
            "// GENERATED by workers/scripts/sync-data.mjs from data/meeting-keywords.json. Do not edit.\n"))

    def test_planted_contact_data_reaches_no_generated_file(self):
        for path in [*(ROOT / "workers").glob("*/src/generated/*")]:
            text = path.read_text(encoding="utf-8", errors="replace")
            self.assertFalse([p for p in PLANTED if p in text], path.name)


if __name__ == "__main__":
    unittest.main()
