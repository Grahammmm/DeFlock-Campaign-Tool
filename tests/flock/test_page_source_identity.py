"""RF1110-8: flock_page refuses duplicate/invalid source numbers and extra-source keys before numbering story citations."""
import copy, importlib.util, json, os, shutil, tempfile, unittest
from pathlib import Path

from tests.flock.test_page import FIX, ROOT, TOOL, run

SPEC = importlib.util.spec_from_file_location("flock_page_identity", TOOL)
assert SPEC and SPEC.loader
PAGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PAGE)
CANARY, KEEP = "ZQXIDENTITYCANARY", object()
COUNTS = dict.fromkeys(("cards", "admissions", "sources", "records_messages", "dropped_outreach"), 0)
BAD_NUM, DUP_NUM = "sources %d: R7 invalid source number", "sources %d: R7 duplicate source number"
BAD_KEY, DUP_KEY = "cards.extra_sources %d: R7 invalid source key", "cards.extra_sources %d: R7 duplicate source key"
BAD_LIST = "cards.extra_sources 0: R7 invalid source list"


class SourceIdentityTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.fix = self.tmp / "in"
        shutil.copytree(FIX, self.fix)
        self.data = {k: json.loads((self.fix / (k + ".json")).read_text(encoding="utf-8")) for k in ("cards", "sources", "location", "thread")}
        self.out = self.tmp / "out"
        self.out.mkdir()
        self.story = self.out / "sample-story.json"

    def save(self):
        for k, v in self.data.items():
            (self.fix / (k + ".json")).write_text(json.dumps(v, ensure_ascii=False), encoding="utf-8")

    def flags(self):
        return ["--cards", self.fix / "cards.json", "--sources", self.fix / "sources.json", "--location", self.fix / "location.json",
                "--thread", self.fix / "thread.json", "--base-sources", 8]

    def assemble(self, sources=None, extra=KEEP):
        cards = copy.deepcopy(self.data["cards"])
        if extra is not KEEP:
            cards["extra_sources"] = extra
        return PAGE.assemble(cards, copy.deepcopy(self.data["sources"] if sources is None else sources), self.data["location"], 8)

    def refusal(self, sources=None, extra=KEEP):
        with self.assertRaises(PAGE.Fail) as ctx:
            self.assemble(sources, extra)
        return str(ctx.exception)

    def source(self, n, **more):
        return dict(n=n, kind="rule", label="Synthetic rule %s" % CANARY, request=None, document_id=None, sha256=None, pages=[], **more)

    def snapshot(self):
        snap = {}
        for folder, dirs, files in os.walk(self.tmp):
            for name in dirs + files:
                p = Path(folder) / name
                st = p.lstat()
                snap[str(p)] = (st.st_mode, st.st_ino, st.st_mtime_ns, p.read_bytes() if p.is_file() else None)
        return snap

    def test_duplicate_numeric_definitions_refuse_before_render(self):
        base = self.data["sources"]
        for label, extra, at in (("conflicting, referenced", self.source(1), 0), ("identical, referenced", copy.deepcopy(base[0]), 0),
                                 ("conflicting, unreferenced", self.source(3), 2), ("identical, unreferenced", copy.deepcopy(base[2]), 2)):
            with self.subTest(label):  # the later definition in input order is the one reported, in either order
                self.assertEqual(self.refusal(base + [extra]), DUP_NUM % 3)
                self.assertEqual(self.refusal([extra] + base), DUP_NUM % (at + 1))
        self.assertEqual(self.refusal([self.source(1), self.source(1)] + base[1:]), DUP_NUM % 1)
        shared = [dict(s, label="Same label") for s in base]  # distinct numbers may share a label
        self.assertEqual(self.assemble(shared)[1], [])

    def test_number_identity_is_positive_exact_integer(self):
        one = self.source(1)
        for label, row in (("true after int 1", True), ("false", False), ("float", 1.0), ("string", "1"), ("zero", 0), ("negative", -3),
                           ("null", None), ("list", [1]), ("dict", {"n": 1})):
            with self.subTest(label):
                self.assertEqual(self.refusal([one, dict(self.source(2), n=row)]), BAD_NUM % 1)
                self.assertEqual(self.refusal([dict(self.source(2), n=row)] + self.data["sources"]), BAD_NUM % 0)
        missing = self.source(2)
        del missing["n"]
        self.assertEqual(self.refusal([one, missing]), BAD_NUM % 1)
        for row in ("text", 7, None, [1]):  # a non-object row
            self.assertEqual(self.refusal([one, row]), BAD_NUM % 1)
        story, errs = self.assemble([self.source(30), self.source(7), self.source(1), self.source(2)])  # sparse, out of order: allowed
        self.assertEqual(errs, [])

    def test_extra_keys_are_unique_literal_nonblank_strings(self):
        text = self.data["cards"]["extra_sources"][0]["text"]
        a = dict(key="meeting", text=text)
        for label, extra in (("identical", [a, dict(a)]), ("conflicting", [a, dict(a, text="other")])):
            with self.subTest(label):
                self.assertEqual(self.refusal(extra=extra), DUP_KEY % 1)
        self.assertEqual(self.refusal(extra=[a, dict(key="unused", text="x"), dict(key="unused", text="y")]), DUP_KEY % 2)
        self.assertEqual(self.refusal(extra="meeting"), BAD_LIST)
        self.assertEqual(self.refusal(extra={"key": "meeting", "text": "x"}), BAD_LIST)
        self.assertEqual(self.refusal(extra=0), BAD_LIST)
        for label, row in (("string row", "meeting"), ("null row", None), ("number key", dict(key=5, text="x")), ("bool key", dict(key=True, text="x")),
                           ("null key", dict(key=None, text="x")), ("blank", dict(key="", text="x")), ("spaces", dict(key="  \t", text="x")),
                           ("missing key", dict(text="x")), ("list key", dict(key=["a"], text="x"))):
            with self.subTest(label):
                self.assertEqual(self.refusal(extra=[a, row]), BAD_KEY % 1)
        for ok in (None, []):  # omitted/null/empty are the empty optional table when nothing refers to an x: key
            self.data["cards"]["findings"][0]["sources"] = [1]
            story, errs = self.assemble(extra=ok)
            self.assertEqual(errs, [])
        del self.data["cards"]["extra_sources"]
        self.assertEqual(self.assemble()[1], [])
        self.data = {k: json.loads((FIX / (k + ".json")).read_text(encoding="utf-8")) for k in self.data}
        story, errs = self.assemble(extra=[a, dict(key="Meeting", text="Upper case meeting."), dict(key=" meeting", text="Padded meeting.")])
        self.assertEqual(errs, [])
        self.data["cards"]["findings"][0]["sources"] = [1, "x:Meeting"]
        self.assertIn("Upper case meeting.", " ".join(self.assemble(extra=[a, dict(key="Meeting", text="Upper case meeting.")])[0]["extraSources"]))
        self.data["cards"]["findings"][0]["sources"] = [1, "x: meeting"]
        self.assertIn("Padded meeting.", " ".join(self.assemble(extra=[a, dict(key=" meeting", text="Padded meeting.")])[0]["extraSources"]))
        self.data["cards"]["findings"][0]["sources"] = [1, "x:meeting "]  # no stripping of a reference either
        self.assertEqual(self.assemble()[1].count("findings 0: R7 unknown source reference"), 1)

    def test_both_identity_tables_fail_deterministically_without_echo(self):
        dup_extra = [dict(key=CANARY, text="a"), dict(key=CANARY, text="b")]
        both = copy.deepcopy(self.data["sources"]) + [dict(self.source(1), label=CANARY)]
        self.assertEqual(self.refusal(both, dup_extra), DUP_NUM % 3)
        self.assertEqual(self.refusal(extra=dup_extra), DUP_KEY % 1)
        self.data["sources"], self.data["cards"]["extra_sources"] = both, dup_extra
        self.save()
        for command in ("build", "check"):
            args = [command, *self.flags()] + (["--out", self.story] if command == "build" else ["--story", self.story])
            code, out = run(*args)
            self.assertEqual(code, 1)
            self.assertEqual(out.splitlines().count(DUP_NUM % 3), 1)
            for secret in (CANARY, str(self.tmp)):
                self.assertNotIn(secret, out)
        self.data["sources"] = self.data["sources"][:3]  # source table valid: the later table's error is surfaced
        self.save()
        code, out = run("build", *self.flags(), "--out", self.story)
        self.assertEqual((code, out.splitlines()[0]), (1, DUP_KEY % 1))
        self.assertNotIn(CANARY, out)

    def test_build_invalid_identities_preserves_outputs(self):
        corrupt = {"duplicate number": ("sources", lambda d: d["sources"].append(self.source(1))),
                   "boolean number": ("sources", lambda d: d["sources"].append(self.source(True))),
                   "duplicate key": ("cards", lambda d: d["cards"]["extra_sources"].append(dict(key="meeting", text="again")))}
        self.assertEqual(run("build", *self.flags(), "--out", self.story)[0], 0)
        self.story.chmod(0o640)
        (self.out / "sample-story.json.superseded-20260101T000000000000Z").write_text("old", encoding="utf-8")
        good = copy.deepcopy(self.data)
        for label, (_, mutate) in corrupt.items():
            with self.subTest(label):
                self.data = copy.deepcopy(good)
                mutate(self.data)
                self.save()
                before, ino = self.snapshot(), self.story.stat().st_ino
                for extra in ((), ("--replace",)):
                    code, out = run("build", *self.flags(), "--out", self.story, *extra)
                    self.assertEqual(code, 1, out)
                    self.assertTrue(out.endswith("errors=1\n"), out)
                    self.assertEqual(self.snapshot(), before)
                self.assertEqual((self.story.stat().st_ino, self.story.stat().st_mode & 0o777), (ino, 0o640))
                self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["sample-story.json", "sample-story.json.superseded-20260101T000000000000Z"])
        fresh = self.tmp / "fresh-story.json"  # a fresh target stays absent
        self.assertEqual(run("build", *self.flags(), "--out", fresh)[0], 1)
        self.assertFalse(fresh.exists())
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["sample-story.json", "sample-story.json.superseded-20260101T000000000000Z"])

    def test_check_invalid_identity_text_json_parity_read_only(self):
        self.assertEqual(run("build", *self.flags(), "--out", self.story)[0], 0)
        (self.out / "sample-story.json.superseded-20260101T000000000000Z").write_text("old", encoding="utf-8")
        zero = " ".join("%s=0" % k for k in COUNTS)
        cases = {"duplicate number": (lambda d: d["sources"].append(self.source(2)), DUP_NUM % 3),
                 "boolean number": (lambda d: d["sources"].insert(1, self.source(True)), BAD_NUM % 1),
                 "duplicate key": (lambda d: d["cards"]["extra_sources"].append(dict(key="meeting", text="again")), DUP_KEY % 1),
                 "bad list": (lambda d: d["cards"].update(extra_sources="meeting"), BAD_LIST)}
        good = copy.deepcopy(self.data)
        for label, (mutate, message) in cases.items():
            self.data = copy.deepcopy(good)
            mutate(self.data)
            self.save()
            for target in (self.story, self.out / "absent-story.json"):
                with self.subTest(label, target=target.name):
                    before = self.snapshot()
                    code, text = run("check", *self.flags(), "--story", target)
                    jcode, raw = run("check", *self.flags(), "--story", target, "--json")
                    self.assertEqual(self.snapshot(), before)
                    doc = json.loads(raw)
                    self.assertEqual((code, jcode), (1, 1))
                    sup = 1 if target == self.story else 0  # existing superseded count reporting is kept
                    self.assertEqual(text.splitlines(), [message, "%s errors=1 superseded=%d state=invalid" % (zero, sup)])
                    self.assertEqual((doc["state"], doc["errors"], doc["differing_keys"], doc["counts"], doc["superseded"]),
                                     ("invalid", [message], [], COUNTS, sup))

    def test_valid_tables_preserve_bytes_numbering_and_references(self):
        story, errs = self.assemble()
        self.assertEqual(errs, [])
        self.assertEqual(story["extraSources"][:2], ["[9] Request R-1, City records held by DeFlock SLO, not posted here.",
                                                      "[10] Request R-1, Camera contract.pdf, page 2; inventory document 10000001."])
        self.assertEqual([f["cite"] for f in story["findings"]], ["[10, 11]", "[9]", "[9, 10]"])
        self.assertEqual(run("build", *self.flags(), "--out", self.story)[0], 0)
        self.assertEqual(self.story.read_text(encoding="utf-8"), PAGE.dump(story))
        # unused valid definitions, sparse ids and unused extra keys change nothing; repeats still collapse
        more = self.data["sources"] + [self.source(40), self.source(12)]
        extra = self.data["cards"]["extra_sources"] + [dict(key="unused", text="Unused.")]
        self.assertEqual(self.assemble(more, extra), (story, errs))
        # a numeric 1 and the x: namespace never collide
        self.data["cards"]["extra_sources"][0]["key"] = "1"
        self.data["cards"]["findings"][0]["sources"] = [1, "x:1"]
        self.assertEqual(self.assemble()[0]["findings"][0]["cite"], "[10, 11]")
        self.data["cards"]["findings"][0]["sources"] = [1, "x:nope"]
        self.assertIn("findings 0: R7 unknown source reference", self.assemble()[1])
        self.data["cards"]["findings"][0]["sources"] = [1, 99]
        self.assertIn("findings 0: R7 unknown source reference", self.assemble()[1])
        errs = self.assemble([])[1]  # an empty source table is valid; every numeric reference is then unknown (no crash)
        self.assertEqual((len(errs), {e.split(": ", 1)[1] for e in errs}), (8, {"R7 unknown source reference"}))


if __name__ == "__main__":
    unittest.main()
