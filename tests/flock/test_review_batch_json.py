import contextlib, csv, io, json, os, re, unittest
from unittest import mock

from tests.flock import test_review_batch as base
from tests.flock.test_review import ROOT, SLUG, tool
from tests.flock.test_review_lock import load

batch = load("_rf1230_21_batch", ROOT / "tools" / "flock_review_batch.py")
SECOND, CAMPUS = base.SLUGS[1:]
SCHEMA, CANARY = "flock-review-batch-status-v1", "RF1230-21-CANARY-dir"
KEYS = sorted(set(base.HEADER) - {"config"})
SUMMARY = re.compile(r"agencies=(\d+) done=(\d+) waiting=(\d+) in_flight=(\d+) failed=(\d+) invalid=(\d+) held=(\d+) locked=(\d+) ready=(\d+)")
MOVED = re.compile(r"changes advanced=(\d+) regressed=(\d+) state=(\d+) new=(\d+) removed=(\d+) unchanged=(\d+)")
COUNTS = ("agencies", "done", "waiting", "in_flight", "failed", "invalid", "held", "locked", "ready")
KINDS = ("advanced", "regressed", "state", "new", "removed", "unchanged")
# Text stdout captured from origin/main 88edf506 on these fixtures (golden: --json must not change the text form).
GOLDEN_MIXED = ("second-city-police ready 2/28 next=inventory\nsample-city-police-department waiting 5/28 next=fact-drafts\n"
                "campus-police invalid / next=- why=config:review.run_date\n"
                "agencies=3 done=0 waiting=1 in_flight=0 failed=0 invalid=1 held=0 locked=0 ready=1\n")
GOLDEN_PREV = ("sample-city-police-department waiting 5/28 next=fact-drafts\ncampus-police held 2/28 next=inventory why=roster:not-in-scope\n"
               "change sample-city-police-department advanced ready->waiting 2->5\nchange campus-police new -->held -->2\n"
               "change second-city-police removed ready->- 2->-\nchanges advanced=1 regressed=0 state=0 new=1 removed=1 unchanged=0\n"
               "agencies=2 done=0 waiting=1 in_flight=0 failed=0 invalid=0 held=1 locked=0 ready=0\n")


class ReviewBatchJsonTest(unittest.TestCase):
    trees = base.FlockReviewBatchTest.trees

    def setUp(self):  # every config, output and list lives under a canary folder name that must never reach the JSON
        base.FlockReviewBatchTest.setUp(self)
        self.tmp = self.tmp / CANARY
        self.tmp.mkdir()

    def listing(self, *configs):
        self.list.write_text("".join("%s\n" % c for c in configs))

    def mixed(self):  # list order: ready, waiting, invalid (with a field why)
        base.FlockReviewBatchTest.world(self)
        self.assertEqual(tool("run", self.configs[0])[0], 3)  # SLUG reaches the fact-drafts wait
        cfg = json.loads(self.configs[2].read_text())
        cfg["review"]["run_date"] = "31/10/2026"
        self.configs[2].write_text(json.dumps(cfg))
        self.listing(self.configs[1], self.configs[0], self.configs[2])

    def moved(self):  # prev: SLUG, SECOND ready; now: SLUG advanced, SECOND removed, CAMPUS new
        base.FlockReviewBatchTest.world(self)
        self.listing(*self.configs[:2])
        self.assertEqual(self.run_(out=self.tmp / "prev", json_=False)[0], 0)
        self.assertEqual(tool("run", self.configs[0])[0], 3)
        self.listing(self.configs[0], self.configs[2])
        return self.tmp / "prev" / "BATCH-STATUS.csv"

    def run_(self, *words, out=None, prev=None, json_=True):  # -> (exit code, stdout); the JSON form never names a config path
        args = ["status", str(self.list), "--roster", str(base.ROSTER)] + (["--out", str(out)] if out else []) + (
            ["--prev", str(prev)] if prev else []) + (["--json"] if json_ else [])
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown), contextlib.redirect_stderr(io.StringIO()):
            code = batch.main(args + [str(w) for w in words])
        if json_:
            self.assertNotIn(CANARY, shown.getvalue())
        return code, shown.getvalue()

    def doc(self, *words, code=0, **kw):
        got, text = self.run_(*words, **kw)
        self.assertEqual(got, code, text)
        doc = json.loads(text)
        self.assertEqual(text, json.dumps(doc, sort_keys=True, indent=1, ensure_ascii=False) + "\n")  # one document, keys sorted
        self.assertEqual((doc["schema"], doc["review_required"]), (SCHEMA, True))
        return doc

    def test_rows_in_list_order_with_counts(self):
        self.mixed()
        code, text = self.run_(out=self.tmp / "text", json_=False)
        self.assertEqual(code, 0, text)
        doc = self.doc()
        self.assertEqual((doc["errors"], doc["changes"], doc["change_counts"]), ([], None, None))
        agencies = doc["agencies"]
        self.assertEqual([sorted(a) for a in agencies], [KEYS] * 3)
        self.assertEqual([(a["slug"], a["state"], a["why"]) for a in agencies],
                         [(SECOND, "ready", ""), (SLUG, "waiting", ""), (CAMPUS, "invalid", "config:review.run_date")])
        self.assertEqual([(a["stages_done"], a["stages_total"]) for a in agencies[2:]], [(None, None)])
        self.assertEqual([a["next"] for a in agencies[:2]], ["inventory", "fact-drafts"])
        self.assertTrue(all(isinstance(a[k], int) for a in agencies[:2] for k in ("stages_done", "stages_total")))
        with open(self.tmp / "text" / "BATCH-STATUS.csv", newline="") as handle:  # the same rows as the CSV, less `config`
            rows = list(csv.DictReader(handle))
        self.assertEqual([{k: str(v if v is not None else "") for k, v in a.items()} for a in agencies],
                         [{k: v for k, v in r.items() if k != "config"} for r in rows])
        totals = [int(n) for n in SUMMARY.search(text).groups()]
        self.assertEqual(doc["counts"], dict(zip(COUNTS, totals)))
        self.assertEqual(totals, [3, 0, 1, 0, 0, 1, 0, 0, 1])

    def test_prev_changes_match_the_text_lines(self):
        prev = self.moved()
        code, text = self.run_(prev=prev, json_=False)
        self.assertEqual(code, 0, text)
        doc = self.doc(prev=prev)
        self.assertEqual(doc["changes"], [
            {"key": SLUG, "kind": "advanced", "old_state": "ready", "new_state": "waiting", "old_done": 2, "new_done": 5},
            {"key": CAMPUS, "kind": "new", "old_state": None, "new_state": "held", "old_done": None, "new_done": 2},
            {"key": SECOND, "kind": "removed", "old_state": "ready", "new_state": None, "old_done": 2, "new_done": None}])
        self.assertEqual(len(doc["changes"]), len([x for x in text.splitlines() if x.startswith("change ")]))
        self.assertEqual(doc["change_counts"], dict(zip(KINDS, map(int, MOVED.search(text).groups()))))
        self.assertEqual(doc["change_counts"], {"advanced": 1, "regressed": 0, "state": 0, "new": 1, "removed": 1, "unchanged": 0})
        self.assertEqual(doc["counts"], dict(zip(COUNTS, map(int, SUMMARY.search(text).groups()))))

    def test_out_files_equal_text_mode(self):
        prev = self.moved()
        for extra in ({}, {"prev": prev}):
            with self.subTest(prev=bool(extra)):
                tag = "p" if extra else "n"
                self.assertEqual(self.run_(out=self.tmp / ("text-" + tag), json_=False, **extra)[0], 0)
                self.doc(out=self.tmp / ("json-" + tag), **extra)
                for name in ("BATCH-STATUS.csv", "BATCH-CHANGES.csv"):
                    text, made = self.tmp / ("text-" + tag) / name, self.tmp / ("json-" + tag) / name
                    self.assertEqual(made.exists(), text.exists())
                    self.assertEqual(made.read_bytes() if made.exists() else None, text.read_bytes() if text.exists() else None)
                    self.assertEqual(made.exists() and made.stat().st_mode & 0o777, text.exists() and 0o600)
                self.assertEqual((self.tmp / "text-p" / "BATCH-CHANGES.csv").exists(), bool(extra))

    def test_refusals_are_the_text_lines(self):
        prev = self.moved()
        bad = self.tmp / "bad-prev.csv"
        bad.write_text(prev.read_text().replace("run_date", "date", 1))
        real = self.tmp / "real"
        real.mkdir()
        os.symlink(real, self.tmp / "link")
        before = self.trees()
        for kw, line in (({"prev": bad, "out": self.tmp / "o"}, "prev: header differs"),
                         ({"out": self.tmp / "link"}, "out: --out must be a real folder")):
            with self.subTest(line=line):
                self.assertEqual(self.run_(json_=False, **kw), (1, line + "\n"))
                doc = self.doc(code=1, **kw)
                self.assertEqual(doc, {"schema": SCHEMA, "review_required": True, "errors": [line], "agencies": [], "counts": None})
                self.assertEqual((os.listdir(real), (self.tmp / "o").exists(), self.trees()), ([], False, before))
        with mock.patch.object(batch.fr, "put", side_effect=PermissionError(CANARY)):  # an OSError: its type name only
            code, text = self.run_(out=self.tmp / "o", json_=False)
            self.assertEqual((code, text.splitlines()[-1]), (1, "io: PermissionError"))
            self.assertEqual(self.doc(code=1, out=self.tmp / "o")["errors"], ["io: PermissionError"])

    def test_json_is_for_status_without_run(self):
        self.mixed()
        before = self.trees()
        for words in (["plan", str(self.list), "--json"], ["status", str(self.list), "--run", "--json", "--out", str(self.out)]):
            with self.subTest(words=words[:3]), contextlib.redirect_stdout(io.StringIO()) as shown, \
                    contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as stop:
                batch.main(words + [str(w) for w in self.gate])
            self.assertEqual((stop.exception.code, shown.getvalue()), (2, ""))
            self.assertIn("--json", err.getvalue())
            self.assertEqual((self.out.exists(), self.trees()), (False, before))
        self.assertEqual(self.doc()["agencies"][0]["state"], "ready")  # nothing ran

    def test_text_stdout_is_unchanged(self):
        self.mixed()
        self.assertEqual(self.run_(json_=False), (0, GOLDEN_MIXED))
        self.tmp = self.tmp / "second"
        self.tmp.mkdir()
        prev = self.moved()
        self.assertEqual(self.run_(prev=prev, json_=False), (0, GOLDEN_PREV))

    def test_no_config_path_in_the_json(self):
        base.FlockReviewBatchTest.world(self)
        unread = self.tmp / "unreadable.json"
        unread.write_text("{not json")
        self.listing(*self.configs, unread)
        self.assertEqual(self.run_(out=self.tmp / "prev", json_=False)[0], 0)
        self.listing(*self.configs)
        code, text = self.run_(prev=self.tmp / "prev" / "BATCH-STATUS.csv", json_=False)
        self.assertIn("change config:%s removed invalid->- -->-" % unread.resolve(), text)  # text names the path (unchanged)
        doc = self.doc(prev=self.tmp / "prev" / "BATCH-STATUS.csv")  # run_ asserts the canary is absent
        self.assertEqual(doc["changes"], [{"key": None, "kind": "removed", "old_state": "invalid", "new_state": None,
                                           "old_done": None, "new_done": None}])
        self.listing(*self.configs, unread)
        self.assertNotIn("config", [k for a in self.doc()["agencies"] for k in a])


if __name__ == "__main__":
    unittest.main()
