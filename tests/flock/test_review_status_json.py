"""RF1240-11: `flock_review.py status CONFIG --json` is one flock-review-status-v1 document; text and exit codes unchanged."""
import contextlib, importlib.util, io, json, os, socket, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

from tests.flock.test_review import MODELS, TOOL, make_review, mark_all, tool, tree

spec = importlib.util.spec_from_file_location("_rf1240_11_review", TOOL)
spec.loader.exec_module(review := importlib.util.module_from_spec(spec))
KEYS = ["counts", "errors", "locked", "next", "review_required", "schema", "stages", "total"]
# `status CONFIG` on a fresh make_review config, captured from origin/main 88edf506 before this change
GOLDEN_FRESH = """1 inventory run pending (never run)
2 logs run pending (needs inventory)
3 roster run pending (needs inventory)
4 ocr-logs run skip
5 fact-drafts wait pending (needs inventory)
6 facts-build run pending (needs logs)
7 facts-import run pending (needs facts-build)
8 rules-read wait waiting (not marked)
9 rules-build run pending (needs rules-read)
10 rules-diff run pending (needs rules-build)
11 rules-selftest run pending (needs rules-build)
12 joins run skip
13 assess wait pending (needs facts-import)
14 compare-build run pending (needs facts-import)
15 compare-selftest run pending (needs compare-build)
16 f3-findings wait pending (needs facts-import)
17 f3-scaffold run pending (needs facts-import)
18 f3-write run pending (needs f3-scaffold)
19 f3-check run pending (needs f3-write)
20 counts run pending (needs compare-build)
21 verify-plan run pending (needs compare-build)
22 verify-recount run pending (needs logs)
23 verify wait pending (needs verify-plan)
24 verify-check run pending (needs verify)
25 f4-start run pending (needs f3-check)
26 f4 wait pending (needs f4-start)
27 f4-finish run pending (needs f4)
28 report run pending (needs inventory)
stages=28 done=0 stale=0 waiting=1 pending=25 failed=0 skip=2 next=inventory locked=no
"""


def render(doc):  # the text form, rebuilt from the document
    lines = ["%d %s %s %s%s%s" % (s["n"], s["id"], s["kind"], s["state"], " %(provider)s/%(model)s" % s["mark"] if s["mark"] else "",
                                  " (%s)" % s["reason"] if s["reason"] else "") for s in doc["stages"]]
    counts = " ".join("%s=%d" % (k, doc["counts"][k]) for k in ("done", "stale", "waiting", "pending", "failed", "skip"))
    return "\n".join(lines + ["stages=%d %s next=%s locked=%s" % (doc["total"], counts, doc["next"] or "-", doc["locked"])]) + "\n"


def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class ReviewStatusJsonTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.config, self.p = make_review(self.tmp)

    def both(self, config=None):  # -> (text, doc, by_id); same exit code, nothing written, JSON stdout is one document
        config = config or self.config
        before = tree(self.p["out"])
        (tcode, text), (jcode, out) = tool("status", config), tool("status", config, "--json")
        self.assertEqual((tcode, jcode, tree(self.p["out"])), (0, 0, before), text + out)
        doc = json.loads(out)
        self.assertEqual((sorted(doc), doc["schema"], doc["review_required"], doc["total"], doc["errors"]),
                         (KEYS, "flock-review-status-v1", True, 28, []))
        self.assertEqual(out, json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
        self.assertEqual(render(doc), text)  # every state, mark, reason, count, next and lock equal to the text form
        return text, doc, {s["id"]: s for s in doc["stages"]}

    def test_fresh_config_matches_text_and_golden(self):
        text, doc, _ = self.both()
        self.assertEqual(text, GOLDEN_FRESH)
        self.assertEqual([(s["n"], s["id"], s["kind"]) for s in doc["stages"]], [(i + 1, sid, kind) for i, (sid, kind, _) in enumerate(review.STAGES)])
        self.assertEqual([s["state"] for s in doc["stages"]], [line.split()[3] for line in text.splitlines()[:-1]])
        summary = dict(w.split("=") for w in text.splitlines()[-1].split()[1:7])
        self.assertEqual(doc["counts"], {k: int(v) for k, v in summary.items()})
        self.assertEqual((doc["next"], doc["locked"]), ("inventory", "no"))
        self.assertFalse((self.p["out"] / "REVIEW-STATE.json").exists() or (self.p["out"] / ".review").exists())

    def test_wait_stage_reason_and_mark(self):
        self.assertEqual(tool("run", self.config)[0], 3)  # stops at fact-drafts
        _, doc, by = self.both()
        self.assertEqual((by["fact-drafts"]["state"], by["fact-drafts"]["reason"], by["fact-drafts"]["mark"]), ("waiting", "not marked", None))
        self.assertEqual((by["inventory"]["state"], by["inventory"]["reason"], doc["next"]), ("done", "", "fact-drafts"))
        draft = self.p["FO"] / "drafts" / "a.yaml"
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text("fact-drafts input\n")
        self.assertEqual(mark_all(self.config, {"fact-drafts": "anthropic"}), [0])
        self.assertEqual(tool("run", self.config)[0], 3)  # stops at rules-read
        text, doc, by = self.both()
        self.assertIn("\n5 fact-drafts wait done anthropic/claude-opus-5-5\n", text)
        self.assertEqual((by["fact-drafts"]["state"], by["fact-drafts"]["mark"]), ("done", {"provider": "anthropic", "model": MODELS["anthropic"]}))
        self.assertEqual((by["rules-read"]["state"], by["rules-read"]["reason"], doc["next"]), ("waiting", "not marked", "rules-read"))
        draft.write_text("edited after mark\n")  # a marked wait stage that is waiting again keeps its mark
        text, doc, by = self.both()
        self.assertEqual((by["fact-drafts"]["state"], by["fact-drafts"]["reason"], by["fact-drafts"]["mark"]["provider"]),
                         ("waiting", "changed since mark", "anthropic"))
        self.assertEqual(doc["next"], "fact-drafts")

    def test_stale_and_failed_reasons(self):
        self.assertEqual(tool("run", self.config, "--until", "inventory")[0], 0)
        (self.p["out"] / "inputs" / "records" / "1__doc.pdf").write_text("y\n")
        _, doc, by = self.both()
        self.assertEqual((by["inventory"]["state"], by["inventory"]["reason"], by["logs"]["reason"]), ("stale", "inputs changed", "needs inventory"))
        self.assertEqual((doc["counts"]["stale"], doc["next"]), (1, "inventory"))
        self.assertEqual(tool("run", self.config, "--until", "inventory")[0], 0)
        (self.p["FO"] / "INVENTORY.csv").write_text("edited\n")
        _, _, by = self.both()
        self.assertEqual((by["inventory"]["state"], by["inventory"]["reason"]), ("stale", "output changed: INVENTORY.csv"))
        config, self.p = make_review(self.tmp / "failed", stubs={"inventory": "sys.exit(1)"})
        self.assertEqual(tool("run", config)[0], 1)
        _, doc, by = self.both(config)
        self.assertEqual((by["inventory"]["state"], by["inventory"]["reason"], doc["counts"]["failed"], doc["next"]), ("failed", "rc=1", 1, "inventory"))

    def test_lock_states_read_only(self):
        lock = self.p["out"] / ".review" / "LOCK"
        lock.parent.mkdir(mode=0o700, parents=True)
        for text, word in (("%d %s\n" % (dead_pid(), socket.gethostname()), "stale"), ("%d %s\n" % (os.getpid(), socket.gethostname()), "yes")):
            with self.subTest(word=word):
                lock.write_text(text)
                _, doc, _ = self.both()  # both() asserts the out tree is unchanged: no aside, no state file
                self.assertEqual((doc["locked"], lock.read_text(), sorted(p.name for p in lock.parent.iterdir())), (word, text, ["LOCK"]))

    def refused(self, config, code):  # -> errors; the text lines equal errors (+ the check summary line), same exit code
        (tcode, text), (jcode, out) = tool("status", config), tool("status", config, "--json")
        doc = json.loads(out)
        self.assertEqual((tcode, jcode), (code, code), text + out)
        self.assertEqual({k: v for k, v in doc.items() if k != "errors"}, {"schema": "flock-review-status-v1", "review_required": True,
                         "stages": [], "counts": None, "next": None, "locked": None, "total": 28})
        self.assertEqual(text.splitlines()[:len(doc["errors"])], doc["errors"])
        return doc["errors"], text

    def test_refusals(self):
        config, p = make_review(self.tmp / "bad", review__schema=2, compare__f3__applicability="")
        errors, text = self.refused(config, 1)
        self.assertEqual((errors, text.splitlines()[-1]), (["config: review.schema is not 1", "config: compare.f3.applicability is not 1-200 chars"],
                                                           "stages=28 skip=2 errors=2"))
        self.assertFalse(p["out"].joinpath("REVIEW-STATE.json").exists())
        self.assertEqual(self.refused(self.tmp / "absent.json", 1),
                         (["config: CONFIG cannot be read (FileNotFoundError)"], "config: CONFIG cannot be read (FileNotFoundError)\n"))
        state = self.p["out"] / "REVIEW-STATE.json"
        state.write_text('{"schema": 1, "slug": "another-agency", "stages": {}}\n')
        why = "state: %s is unreadable or for another agency" % state
        self.assertEqual(self.refused(self.config, 1), ([why], why + "\n"))
        for argv, shown in (([], "io: PermissionError\n"), (["--json"], None)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), patch.object(review, "load_state", side_effect=PermissionError):
                code = review.main(["status", str(self.config)] + argv)
            self.assertEqual(code, 1)
            self.assertEqual(out.getvalue() if shown else json.loads(out.getvalue())["errors"], shown or ["io: PermissionError"])
        code, out = tool("status", self.config, "--json", "--until", "nope")
        self.assertEqual((code, json.loads(out)["errors"], json.loads(out)["stages"]), (2, ["usage: --until is not a stage id"], []))

    def test_json_with_run_mark_or_check_is_usage(self):
        for args in (("run", self.config, "--json"), ("mark", self.config, "fact-drafts", "--provider", "anthropic", "--model", "m", "--json"),
                     ("--check", self.config, "--json"), ("status", self.config, "--check", self.config, "--json"), ("--json",)):
            with self.subTest(args=args[0]):
                done = subprocess.run([sys.executable, "-B", str(TOOL), *map(str, args)], capture_output=True, text=True, check=False)
                self.assertEqual((done.returncode, done.stdout), (2, ""), done.stderr)
                self.assertIn("usage:", done.stderr)
                self.assertFalse((self.p["out"] / "REVIEW-STATE.json").exists() or (self.p["out"] / ".review").exists() or self.p["calls"].exists())


if __name__ == "__main__":
    unittest.main()
