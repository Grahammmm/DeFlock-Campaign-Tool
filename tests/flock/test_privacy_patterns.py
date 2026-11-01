"""NB-DF-01: the report, page and publish lints share tools/flock_privacy.py, so every lint catches the same phones, plates and
emails, and none of them echoes the matched text."""
import importlib.util, json, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS, PAGE_FIX = ROOT / "tools", ROOT / "tests" / "fixtures" / "flock" / "page"


def load(name):
    spec = importlib.util.spec_from_file_location("_nb_df_01_" + name, TOOLS / (name + ".py"))
    spec.loader.exec_module(module := importlib.util.module_from_spec(spec))
    return module


privacy, report, page, publish = (load(n) for n in ("flock_privacy", "flock_report", "flock_page", "flock_publish"))
# What each old copy missed: report and publish needed '-' before the last four digits (missed 805.555.1234, 805 555 1234,
# +1 805 555 1234) and a separator after ')' (missed (805)555-1234, as did page); all three flagged 12805-555-12345; page and report had no
# plate check; publish knew only the 7ABC123 plate shape.
PHONES = ("805.555.1234", "805 555 1234", "805-555-1234", "(805) 555-1234", "(805)555-1234", "+1 805 555 1234", "1-805-555-1234")
NOT_PHONES = ("2024-10-31", "$1,234.56", "ID 8055551234", "12805-555-12345")
PLATES = ("7ABC123", "7ABC 123", "7ABC-123", "4A12345")
NOT_PLATES = ("ABC1234", "7abc123", "17ABC1234")
EMAILS, NOT_EMAILS = ("a.b+c@city.ca.gov",), ("user@localhost", "@handle")


def run(tool, *args, cwd=None):
    done = subprocess.run([sys.executable, "-B", str(TOOLS / tool), *map(str, args)], capture_output=True, text=True,
                          check=False, cwd=cwd)
    return done.returncode, done.stdout + done.stderr


class PrivacyPatternsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.story_path = cls.tmp / "sample-story.json"
        code, out = run("flock_page.py", "build", "--cards", PAGE_FIX / "cards.json", "--sources", PAGE_FIX / "sources.json",
                        "--location", PAGE_FIX / "location.json", "--thread", PAGE_FIX / "thread.json", "--base-sources", 8,
                        "--out", cls.story_path)
        assert code == 0, out
        cls.story = json.loads(cls.story_path.read_text(encoding="utf-8"))
        cls.thread, cls.location = (json.loads((PAGE_FIX / n).read_text(encoding="utf-8")) for n in ("thread.json", "location.json"))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def report_lint(self, text):  # the report lint on one draft line (no denylist, no sources)
        errs = []
        report.lint({"deny": []}, "Basis. " + text, "", {}, 0, [], {}, [], errs)
        return errs

    def page_lint(self, text):
        story = json.loads(json.dumps(self.story))
        story["dek"] += " See " + text
        return page.lint(story, self.thread, self.location, 8)[0]

    def publish_lint(self, text):
        finding = {"finding_id": "N1", "topic": "retention", "claim_class": next(iter(publish.compare.CLASSES)), "rating": "strong",
                   "text": "Reads are kept. " + text, "cites": [], "review": {"status": "pending"}}
        return publish.finding_errors(finding)

    def lints(self, kind):  # (lint name, function, the line each lint prints for a hit of this kind)
        return (("report", self.report_lint, "line 1: %s-like string" % kind), ("page", self.page_lint, "dek 0: R9 %s-like string" % kind),
                ("publish", self.publish_lint, "text refused " + kind))

    def check(self, kind, caught, missed):
        for name, lint, line in self.lints(kind):
            for text in caught:
                with self.subTest(lint=name, kind=kind, text=text):
                    errs = lint(text)
                    self.assertIn(line, errs)
                    self.assertNotIn(text, "\n".join(errs))
            for text in missed:
                with self.subTest(lint=name, kind=kind, missed=text):
                    self.assertNotIn(line, lint(text))

    def test_phones_any_separator_caught_by_every_lint_without_echo(self):
        self.check("phone", PHONES, NOT_PHONES)

    def test_plates_caught_by_every_lint_without_echo(self):
        self.check("plate", PLATES, NOT_PLATES)

    def test_emails_caught_by_every_lint_without_echo(self):
        self.check("email", EMAILS, NOT_EMAILS)

    def test_module_categories_order(self):
        self.assertEqual(privacy.categories("7ABC123 805.555.1234 a.b+c@city.ca.gov"), ["email", "phone", "plate"])
        self.assertEqual(privacy.categories("Plate reads are kept for 365 days."), [])

    def test_publish_categories_keep_order_and_url_hex(self):
        self.assertEqual([name for name, _ in publish.PRIVACY], ["email", "phone", "url", "plate", "hex"])
        self.assertEqual([rx.pattern for name, rx in publish.PRIVACY if name in ("email", "phone", "plate")],
                         [privacy.EMAIL.pattern, privacy.PHONE.pattern, privacy.PLATE.pattern])
        hexed = "ab" * 16
        self.assertEqual(publish.text_rules("See https://x", False), ["url"])
        self.assertEqual(publish.text_rules("See www.example", False), ["url"])
        self.assertEqual(publish.text_rules("Digest " + hexed, False), ["hex"])
        self.assertEqual(publish.text_rules("Digest " + hexed[:-1], False), [])
        self.assertEqual(publish.text_rules("a.b+c@city.ca.gov 805.555.1234 https://x 7ABC123 " + hexed, False),
                         ["email", "phone", "url", "plate", "hex"])

    def test_tools_load_the_module_when_cwd_is_not_the_repo(self):
        with tempfile.TemporaryDirectory() as cwd:
            story = json.loads(json.dumps(self.story))
            story["dek"] += " Plate 7ABC 123, call 805 555 1234."
            (path := Path(cwd) / "story.json").write_text(json.dumps(story), encoding="utf-8")
            code, out = run("flock_page.py", "lint", "--story", path, "--thread", PAGE_FIX / "thread.json", "--location",
                            PAGE_FIX / "location.json", "--base-sources", 8, cwd=cwd)
            self.assertEqual(code, 1, out)
            self.assertIn("dek 0: R9 phone-like string\ndek 0: R9 plate-like string\n", out)
            self.assertNotIn("7ABC 123", out)
            self.assertNotIn("805 555 1234", out)
            for tool in ("flock_report.py", "flock_publish.py", "flock_privacy.py"):
                with self.subTest(tool=tool):
                    code, out = run(tool, "--help", cwd=cwd) if tool != "flock_privacy.py" else run(tool, cwd=cwd)
                    self.assertEqual((code, "Traceback" in out), (0, False), out)


if __name__ == "__main__":
    unittest.main()
