"""RF1050-10: flock_page lint --json prints one flock-page-lint-v1 document from the same errors/counts; text and exit unchanged."""
import contextlib, importlib.util, io, json, unittest
from unittest import mock

import tests.flock.test_page as base  # module import only: the base TestCase is not collected again here

FIX, TOOL, run = base.FIX, base.TOOL, base.run
DOC_KEYS = ["counts", "errors", "problems", "review_required", "schema"]
ZERO = dict.fromkeys(("cards", "admissions", "sources", "records_messages", "dropped_outreach"), 0)


def text_of(doc):  # the text form a document stands for: one "<area> <index>: <rule> <why>" line per problem, then the counts line
    lines = [("%(area)s %(index)d: %(rule)s %(why)s" % p).rstrip() for p in doc["problems"]]
    return lines + [" ".join("%s=%d" % (k, doc["counts"][k]) for k in ZERO) + " errors=%d" % doc["errors"]]


def lint_lines(out):  # text lint lines without the DF-4 advisory notes, which --json sends to stderr instead
    return [line for line in out.splitlines() if not line.startswith("review-required:")]


class FlockPageLintJsonTest(unittest.TestCase):
    setUp, build, lint, mutations = base.FlockPageTest.setUp, base.FlockPageTest.build, base.FlockPageTest.lint, \
        base.FlockPageTest.mutations

    def both(self, story=None, thread=None, *extra):  # -> (text code, text out, json code, document, raw json out)
        code, out = self.lint(story, thread, *extra)
        jcode, raw = self.lint(story, thread, *extra, "--json")
        doc = json.loads(raw)
        self.assertEqual(sorted(doc), DOC_KEYS)
        self.assertEqual((doc["schema"], doc["review_required"]), ("flock-page-lint-v1", True))
        self.assertEqual(raw, json.dumps(doc, sort_keys=True, indent=1, ensure_ascii=False) + "\n")
        self.assertEqual(jcode, code)
        self.assertEqual(doc["errors"], len(doc["problems"]))
        return code, out, jcode, doc, raw

    def test_clean_story_has_no_problems_and_text_counts(self):
        code, out, _jcode, doc, _raw = self.both()
        self.assertEqual((code, out.splitlines()), (0, [base.HAPPY]))
        self.assertEqual((doc["errors"], doc["problems"]), (0, []))
        self.assertEqual(doc["counts"], dict(cards=3, admissions=2, sources=3, records_messages=2, dropped_outreach=1))
        self.assertEqual(text_of(doc), out.splitlines())

    def test_r1_r2_tone_errors_one_problem_each_in_lint_order(self):
        story = json.loads(json.dumps(self.story))
        story["findings"][0]["text"] += " Our outreach list grew. The City violated it."
        story["programStart"] = "2025-02-01"
        code, out, _jcode, doc, _raw = self.both(story)
        self.assertEqual(code, 1, out)
        self.assertEqual(text_of(doc), lint_lines(out))  # same lines, same order, same counts
        got = [(p["area"], p["index"], p["rule"]) for p in doc["problems"]]
        for want in (("findings", 0, "R1"), ("programEvents", 0, "R2")):
            self.assertIn(want, got)
        # DF-4 ruling 1: quote-gated wording ("violated") is an advisory text note, never a JSON problem
        self.assertNotIn(("findings", 0, "tone"), got)
        self.assertIn("review-required: findings 0: quote-gated wording", out.splitlines())
        self.assertLess(got.index(("findings", 0, "R1")), got.index(("programEvents", 0, "R2")))
        self.assertTrue(all(isinstance(p["index"], int) for p in doc["problems"]))
        for _want, story, thread, extra in self.mutations():  # every single-mutation error round-trips to its text line
            with self.subTest(want=_want):
                code, out, _jcode, doc, _raw = self.both(story, thread, *extra)
                self.assertEqual((code, text_of(doc)), (1, lint_lines(out)))
                self.assertNotIn("input", [p["area"] for p in doc["problems"]])

    def test_schema_key_error_parses_as_story_schema(self):
        story = json.loads(json.dumps(self.story))
        story["dek"] = story.pop("dek")  # same keys, wrong order
        code, _out, _jcode, doc, _raw = self.both(story)
        self.assertEqual(code, 1)
        self.assertEqual(doc["problems"][0], dict(area="story", index=0, rule="schema", why="keys missing or out of order"))

    def test_unreadable_story_and_missing_thread_are_input_problems(self):
        bad = self.tmp / "story.json"
        bad.write_text("not json {", encoding="utf-8")
        args = ("lint", "--story", bad, "--thread", self.tmp / "absent.json", "--location", FIX / "location.json", "--base-sources", 8)
        code, out = run(*args)
        jcode, raw = run(*args, "--json")
        doc = json.loads(raw)
        self.assertEqual((code, jcode), (1, 1))
        self.assertEqual(out.splitlines(), ["story 0: not JSON", "thread 0: missing",
                                         "cards=0 admissions=0 sources=0 records_messages=0 dropped_outreach=0 errors=2"])
        self.assertEqual(doc["counts"], ZERO)
        self.assertEqual(doc["problems"], [dict(area="input", index=0, rule="input", why="story 0: not JSON"),
                                           dict(area="input", index=0, rule="input", why="thread 0: missing")])
        self.assertNotIn(str(self.tmp), raw)

    def test_io_error_is_an_input_problem(self):
        spec = importlib.util.spec_from_file_location("flock_page_rf1050_10", TOOL)
        page = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(page)
        self.lint()  # writes story.json and thread.json
        argv = ["lint", "--story", str(self.tmp / "story.json"), "--thread", str(self.tmp / "thread.json"),
                "--location", str(FIX / "location.json"), "--base-sources", "8", "--json"]
        with mock.patch.object(page, "_inputs", side_effect=OSError), contextlib.redirect_stdout(io.StringIO()) as buf:
            code = page.main(argv)
        doc = json.loads(buf.getvalue())
        self.assertEqual((code, doc["counts"], doc["errors"]), (1, ZERO, 1))
        self.assertEqual(doc["problems"], [dict(area="input", index=0, rule="input", why="io 0: OSError")])
        self.assertEqual(page.problem("findings 2: R7"), dict(area="findings", index=2, rule="R7", why=""))
        self.assertEqual(page.problem("--story refused"), dict(area="input", index=0, rule="input", why="--story refused"))

    def test_json_only_on_lint(self):
        common = ("--location", FIX / "location.json", "--thread", FIX / "thread.json", "--base-sources", 8, "--json")
        for verb, extra in (("build", ("--cards", FIX / "cards.json", "--sources", FIX / "sources.json", "--out", self.tmp / "x.json")),
                            ("check", ("--cards", FIX / "cards.json", "--sources", FIX / "sources.json", "--story", self.out))):
            with self.subTest(verb=verb):
                done = base.subprocess.run([base.sys.executable, "-B", str(TOOL), verb, *map(str, extra + common)],
                                           capture_output=True, text=True, check=False)
                self.assertEqual(done.returncode, 2)
                self.assertIn("unrecognized arguments: --json", done.stderr)
                self.assertEqual(done.stdout, "")
        self.assertFalse((self.tmp / "x.json").exists())

    def test_json_never_carries_card_text_not_posted_names_or_paths(self):
        canary = "ZQXCANARYCARDTEXT"
        story = json.loads(json.dumps(self.story))
        story["findings"][0]["text"] += " %s outreach grew." % canary
        story["findings"][1]["label"] = canary + " label"
        thread = json.loads(json.dumps(self.thread))
        thread["messages"][1]["body"] += " See 10000003 and the Zqx Secret Ledger."
        _code, out, jcode, doc, raw = self.both(story, thread, "--not-posted", "10000003", "--not-posted-title", "Zqx Secret Ledger")
        self.assertEqual(jcode, 1)
        self.assertIn(dict(area="thread.messages", index=1, rule="R8", why="not-posted record named"), doc["problems"])
        for value in (canary, "10000003", "Zqx Secret Ledger", str(self.tmp), str(FIX)):
            self.assertNotIn(value, raw)
            self.assertNotIn(value, out)


if __name__ == "__main__":
    unittest.main()
