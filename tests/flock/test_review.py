import importlib.util, json, os, shutil, stat, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
TOOL, FIX = ROOT / "tools" / "flock_review.py", ROOT / "tests" / "fixtures" / "flock"
SLUG = "sample-city-police-department"
SCRIPTS = {"facts-intake": "inventory log_summary roster_summary ocr_log_counts facts_build",
           "rules-intake": "rules_build diff_versions check_rules",
           "compare": "training_vs_search compare_build check_compare f3_from_yaml summarize"}
MODELS = {"anthropic": "claude-opus-5-5", "openai": "gpt-5.5"}
WAITS = (("fact-drafts", "anthropic", "FO", "drafts/a.yaml"), ("rules-read", "anthropic", None, None),
         ("assess", "anthropic", "CO", "ASSESSMENTS.yaml"), ("f3-findings", "openai", "CO", "F3-FINDINGS.yaml"))
STUB = '''import csv, hashlib, json, sys
from pathlib import Path
OVERRIDE, FIX = @OVR@, Path(@FIX@)
ONLY, REQS = "no", ""  # report-stage stub data: an override may set the INVENTORY analysis_only value and REQUIREMENTS rows
me, a = Path(sys.argv[0]).stem, sys.argv[1:]
with open(Path(sys.argv[0]).parents[2] / "CALLS.log", "a") as log:
    log.write(me + "\\n")
exec(OVERRIDE)
cfg = json.loads(Path(a[0]).read_text()) if a[0].endswith(".json") else {}
def put(path, text):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text)
def copy(name, path):
    put(path, (FIX / name).read_text())
if me == "inventory":
    put(Path(a[1]) / "INVENTORY.csv", "request,doc_id,title,sha256,analysis_only\\nREQ-101,1,doc.pdf," + "b" * 64 + "," + ONLY + "\\n")
elif me == "log_summary":
    put(Path(a[1]) / "LOG-SUMMARY-by-user.csv", "user,count\\n")
elif me == "roster_summary":
    put(Path(a[1]) / ("ROSTER-SUMMARY-" + a[2] + ".json"), "{}\\n")
elif me == "facts_build":
    copy("compare-facts.csv", Path(a[1]) / "FACTS-v1-record.csv")
elif me == "rules_build":
    out = Path(cfg["rules_intake"]["out"])
    put(out / "REQUIREMENTS.csv", "source,version,section,req_id,page\\n" + REQS)
    put(out / "VERSION-DIFF.csv", "diff," + hashlib.sha256(Path(a[a.index("--f2-tool") + 1]).read_bytes()).hexdigest() + "\\n")
    copy("law-sample.csv", out / "F2" / "LAW.csv")
    copy("policy-sample.csv", out / "F2" / "agencies" / cfg["review"]["slug"] / "POLICY.csv")
elif me == "compare_build":
    sha = hashlib.sha256(Path(cfg["compare"]["assessments"]).read_bytes()).hexdigest()
    out = Path(cfg["compare"]["out"])
    out.mkdir(parents=True, exist_ok=True)
    header = "finding_id kind req_id source version section obligation topic requirement verdict basis fact_ids record_ids pages other_refs related_req_ids analysis_only next_step".split()
    with (out / "FINDINGS.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        for fid, verdict in (("R001", "met"), ("R002", "contradicted")):
            writer.writerow(dict(finding_id=fid, kind="requirement", req_id=fid, verdict=verdict,
                                 basis="assessments sha256 " + sha, analysis_only="no"))
    put(out / "BUILD-REPORT.txt", "findings=2 errors=0\\nrequirement: 2 {'contradicted': 1, 'met': 1}\\nanalysis_only rows: 0\\n")
elif me == "f3_from_yaml":
    copy("findings-sample.csv", Path(a[1]) / "FINDINGS.csv")
else:
    print(me + " ok")
'''


def tool(*args, full=False):  # G1's tests stop at counts; the verify stages 21-27 belong to test_review_verify
    if args[0] == "run" and "--until" not in args and not full:
        args += ("--until", "counts")
    done = subprocess.run([sys.executable, "-B", str(TOOL), *map(str, args)], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout


def write_stub(method, name, override=""):
    folder = next(d for d, names in SCRIPTS.items() if name in names.split())
    path = Path(method) / folder / "scripts" / (name + ".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(STUB.replace("@OVR@", repr(override)).replace("@FIX@", repr(str(FIX))))


def make_review(tmp, stubs=None, slug=None, **config_overrides):
    """Stub pipeline: stubs maps a script name to extra stub code (None = script absent); keys a__b__c set config a.b.c."""
    tmp, stubs = Path(tmp), {k.removesuffix(".py"): v for k, v in (stubs or {}).items()}
    out, method = tmp / "out", tmp / "method"
    for name in " ".join(SCRIPTS.values()).split():
        if stubs.get(name, "") is not None:
            write_stub(method, name, stubs.get(name, ""))
    text = (FIX / "review-sample.json").read_text().replace("@OUT@", str(out)).replace("@METHOD@", str(method))
    cfg = json.loads(text.replace("@PY@", sys.executable))
    if slug is not None:
        cfg = json.loads(json.dumps(cfg).replace(SLUG, slug).replace("Sample City Police Department", slug))
    for key, value in config_overrides.items():
        *parents, last = key.split("__")
        node = cfg
        for part in parents:
            node = node[part]
        node[last] = value
    for rel in ("records/1__doc.pdf", "law.spec.csv", "policy.spec.csv", "law.f2map.csv", "policy.txt"):
        (out / "inputs" / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / "inputs" / rel).write_text("x\n")
    config = tmp / "review.json"
    config.write_text(json.dumps(cfg))
    return config, {"out": out, "method": method, "FO": out / "facts", "RO": out / "rules", "CO": out / "compare",
                    "calls": method / "CALLS.log", "RUN": out / "compare" / "store" / "agencies" / cfg["review"]["slug"] / "runs" / "2026-10-30"}


def mark_all(config_path, provider_by_stage):
    return [tool("mark", config_path, stage, "--provider", p, "--model", MODELS.get(p, "model-1"))[0]
            for stage, p in provider_by_stage.items()]


def calls(paths):
    return paths["calls"].read_text().split() if paths["calls"].exists() else []


def tree(out):
    return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(Path(out).rglob("*")) if p.is_file()}


class FlockReviewTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def drive(self, config, paths, stop=None):
        for stage, provider, key, rel in WAITS:
            code, out = tool("run", config)
            self.assertEqual(code, 3, out)
            self.assertIn("stage %s WAITING (not marked)" % stage, out)
            if key:
                (paths[key] / rel).parent.mkdir(parents=True, exist_ok=True)
                (paths[key] / rel).write_text(stage + " input\n")
            self.assertEqual(mark_all(config, {stage: provider}), [0])
            if stage == stop:
                return None
        return tool("run", config)

    def test_happy_path_and_resume(self):
        config, p = make_review(self.tmp)
        code, out = tool("--check", config)
        self.assertEqual((code, out), (0, "stages=28 skip=2 errors=0\n"))
        code, out = self.drive(config, p)
        self.assertEqual(code, 0, out)
        stages = json.loads((p["out"] / "REVIEW-STATE.json").read_text())["stages"]
        self.assertEqual([stages[s]["rc"] for s in ("roster", "rules-diff")], [0, 0])
        self.assertTrue((p["out"] / ".review/logs/f3-check.log").read_text().rstrip().endswith("findings=7 errors=0"))
        meta = json.loads((p["RUN"] / "RUN.json").read_text())
        self.assertEqual((meta["author_provider"], meta["author_model"]), ("openai", "gpt-5.5"))
        before_calls, state = calls(p), (p["out"] / "REVIEW-STATE.json").read_bytes()
        code, out = tool("run", config)
        self.assertEqual((code, out.count(" cached\n"), out.count("rc=")), (0, 14, 0), out)
        self.assertEqual((calls(p), (p["out"] / "REVIEW-STATE.json").read_bytes()), (before_calls, state))
        listing = tree(p["out"])
        code, out = tool("status", config)
        self.assertEqual(code, 0)
        self.assertIn("16 f3-findings wait done openai/gpt-5.5\n", out)
        self.assertTrue(out.endswith("stages=28 done=18 stale=0 waiting=0 pending=8 failed=0 skip=2 next=verify-plan locked=no\n"))
        self.assertEqual(tree(p["out"]), listing)
        code, out = tool("run", config, full=True)
        self.assertEqual(code, 3, out)
        self.assertIn("stage verify WAITING (not marked)", out)

    def test_invalidation(self):
        config, p = make_review(self.tmp)
        self.assertEqual(self.drive(config, p)[0], 0)
        (p["CO"] / "ASSESSMENTS.yaml").write_text("edited\n")
        code, out = tool("run", config)
        self.assertEqual(code, 3)
        self.assertIn("stage assess WAITING (changed since mark)", out)
        seen = len(calls(p))
        self.assertEqual(mark_all(config, {"assess": "anthropic"}), [0])
        code, out = tool("run", config)
        self.assertEqual((code, calls(p)[seen:]), (0, ["compare_build", "check_compare", "summarize"]), out)
        facts = p["FO"] / "store" / "agencies" / SLUG / "FACTS.csv"
        original = facts.read_bytes()
        facts.write_bytes(original.replace(b"Vigilant", b"Vigilent"))
        seen = len(calls(p))
        code, out = tool("run", config)
        self.assertEqual(code, 0, out)
        self.assertEqual([line.split()[1] for line in out.splitlines() if "rc=" in line], ["facts-import"])
        self.assertEqual((facts.read_bytes(), calls(p)[seen:]), (original, []))
        moved = list(p["out"].glob(".review/superseded/facts-import-*/store/agencies/%s/FACTS.csv" % SLUG))
        self.assertEqual(len(moved), 1)
        self.assertIn(b"Vigilent", moved[0].read_bytes())

    def test_f2_only_change_invalidates_rules_and_downstream(self):
        engine = self.tmp / "engine"
        shutil.copytree(ROOT / "tools", engine / "tools")
        (engine / "campaign_tool").symlink_to(ROOT / "campaign_tool", target_is_directory=True)
        golden = engine / "tests/fixtures/flock/morrobay-golden.csv"
        golden.parent.mkdir(parents=True)
        shutil.copyfile(FIX / golden.name, golden)
        with patch(__name__ + ".TOOL", engine / "tools/flock_review.py"):
            config, p = make_review(self.tmp)
            self.assertEqual(self.drive(config, p)[0], 0)
            code, out = tool("run", config)
            self.assertEqual(code, 0, out)
            self.assertIn("stage rules-build cached", out)
            before = calls(p)
            state_path = p["out"] / "REVIEW-STATE.json"
            state = json.loads(state_path.read_text())["stages"]
            f2 = engine / "tools/flock_law_policy.py"
            f2.write_bytes(f2.read_bytes() + b"\n# F2-only revision\n")
            code, out = tool("status", config)
            self.assertEqual(code, 0, out)
            self.assertIn("9 rules-build run stale", out)
            self.assertIn("10 rules-diff run pending", out)
            code, out = tool("run", config)
            self.assertEqual(code, 3, out)
            self.assertEqual(calls(p)[len(before):], ["rules_build", "diff_versions", "check_rules"])
            self.assertIn("stage assess WAITING (upstream changed)", out)
            after = json.loads(state_path.read_text())["stages"]
            self.assertNotEqual(state["rules-build"]["fp"], after["rules-build"]["fp"])
            self.assertEqual(state["rules-read"]["mark"], after["rules-read"]["mark"])
            self.assertEqual(mark_all(config, {"assess": "anthropic"}), [0])
            code, out = tool("run", config)
            self.assertEqual(code, 3, out)
            self.assertIn("stage compare-build done rc=0", out)
            self.assertIn("stage f3-findings WAITING (upstream changed)", out)
            self.assertEqual(mark_all(config, {"f3-findings": "openai"}), [0])
            code, out = tool("run", config)
            self.assertEqual(code, 0, out)
            self.assertIn("stage f3-scaffold done rc=0", out)

    def test_changed_fact_output_rebuilds_compare_and_f3(self):
        config, p = make_review(self.tmp)
        self.assertEqual(self.drive(config, p)[0], 0)
        state_path = p["out"] / "REVIEW-STATE.json"
        before = json.loads(state_path.read_text())["stages"]
        wait_files = {name: (p["CO"] / name).read_bytes()
                      for name in ("ASSESSMENTS.yaml", "F3-FINDINGS.yaml")}
        write_stub(p["method"], "facts_build",
                   '(Path(a[1]) / "FACTS-v1-record.csv").write_text('
                   '(FIX / "compare-facts.csv").read_text().replace("Vigilant", "Vigilent")); sys.exit(0)')
        code, out = tool("run", config)
        self.assertEqual(code, 3, out)
        self.assertIn("stage assess WAITING (upstream changed)", out)
        after = json.loads(state_path.read_text())["stages"]
        self.assertNotEqual(before["facts-import"]["outputs"], after["facts-import"]["outputs"])
        self.assertEqual(mark_all(config, {"assess": "anthropic"}), [0])
        code, out = tool("run", config)
        self.assertEqual(code, 3, out)
        self.assertIn("stage compare-build done rc=0", out)
        self.assertIn("stage f3-findings WAITING (upstream changed)", out)
        self.assertEqual(mark_all(config, {"f3-findings": "openai"}), [0])
        code, out = tool("run", config)
        self.assertEqual(code, 0, out)
        self.assertIn("stage f3-scaffold done rc=0", out)
        self.assertEqual(wait_files, {name: (p["CO"] / name).read_bytes() for name in wait_files})

    def test_logs_change_rebuilds_facts_without_draft_remark(self):
        config, p = make_review(self.tmp)
        self.assertEqual(self.drive(config, p)[0], 0)
        state_path = p["out"] / "REVIEW-STATE.json"
        mark = json.loads(state_path.read_text())["stages"]["fact-drafts"]["mark"]
        write_stub(p["method"], "log_summary",
                   '(Path(a[1]) / "LOG-SUMMARY-by-user.csv").write_text("user,count\\nnew,1\\n"); sys.exit(0)')
        code, out = tool("run", config)
        self.assertEqual(code, 0, out)
        self.assertIn("stage facts-build done rc=0", out)
        self.assertEqual(json.loads(state_path.read_text())["stages"]["fact-drafts"]["mark"], mark)

    def test_fail_closed(self):
        cases = [({"compare__f3__slug": "other-city"}, "config: compare.f3.slug"),
                 ({"review__out": str(ROOT / "review-out")}, "config: review.out"),
                 ({"compare__facts": "/elsewhere/FACTS.csv"}, "config: compare.facts"),
                 ({"compare__f3__applicability": "x" * 201}, "config: compare.f3.applicability"),
                 ({"stubs": {"check_rules": None}}, "config: review.method_dir lacks check_rules.py"),
                 ({"review__region_config": str(FIX / "region-sample.json")}, "config: region_config has no in-scope")]
        for n, (overrides, expected) in enumerate(cases):
            with self.subTest(expected=expected):
                config, p = make_review(self.tmp / str(n), **overrides)
                listing = tree(p["out"])
                code, out = tool("run", config, "--seed-dir", FIX / "seed")
                self.assertEqual(code, 1)
                self.assertIn(expected, out)
                self.assertEqual(tree(p["out"]), listing)
        config, p = make_review(self.tmp / "stub", stubs={"check_compare": "sys.exit(1)"})
        self.drive(config, p, stop="assess")
        code, out = tool("run", config)
        self.assertEqual(code, 1)
        self.assertIn("stage compare-selftest failed rc=1", out)
        self.assertNotIn("counts", out)
        write_stub(p["method"], "check_compare")
        seen = len(calls(p))
        code, out = tool("run", config)
        self.assertEqual((code, calls(p)[seen:]), (3, ["check_compare"]), out)
        self.assertEqual(tool("mark", config, "compare-build", "--provider", "local", "--model", "m")[0], 2)
        self.assertEqual(tool("mark", config, "assess", "--provider", "bogus", "--model", "m")[0], 2)
        (p["out"] / ".review" / "LOCK").write_text("1\n")
        state = (p["out"] / "REVIEW-STATE.json").read_bytes()
        code, out = tool("run", config)
        self.assertEqual(code, 1)
        self.assertIn("locked: ", out)
        self.assertEqual((p["out"] / "REVIEW-STATE.json").read_bytes(), state)

    def test_put_writes_through_fresh_temp(self):  # BL-DF-1: a planted or stale <name>.tmp is never followed, reused or left
        spec = importlib.util.spec_from_file_location("_bl_df_1_review", TOOL)
        spec.loader.exec_module(review := importlib.util.module_from_spec(spec))
        folder, sentinel = self.tmp / "o", self.tmp / "sentinel"
        folder.mkdir()
        sentinel.write_bytes(b"keep\n")
        (folder / "a.txt.tmp").symlink_to(sentinel)
        review.put(folder / "a.txt", b"one\n")
        self.assertEqual((sentinel.read_bytes(), os.readlink(folder / "a.txt.tmp")), (b"keep\n", str(sentinel)))
        st = os.lstat(folder / "a.txt")
        self.assertEqual((stat.S_ISREG(st.st_mode), stat.S_IMODE(st.st_mode), (folder / "a.txt").read_bytes()), (True, 0o600, b"one\n"))
        (folder / "b.txt.tmp").write_bytes(b"stale\n")
        (folder / "b.txt.tmp").chmod(0o644)
        before = os.lstat(folder / "b.txt.tmp")
        review.put(folder / "b.txt", b"two\n")
        after, out = os.lstat(folder / "b.txt.tmp"), os.lstat(folder / "b.txt")
        self.assertEqual(((folder / "b.txt.tmp").read_bytes(), stat.S_IMODE(after.st_mode), after.st_ino), (b"stale\n", 0o644, before.st_ino))
        self.assertEqual((stat.S_IMODE(out.st_mode), (folder / "b.txt").read_bytes()), (0o600, b"two\n"))
        self.assertNotEqual(out.st_ino, before.st_ino)
        listing, ino = sorted(os.listdir(folder)), os.lstat(folder / "a.txt").st_ino
        with patch.object(review.os, "replace", side_effect=OSError("replace failed")), self.assertRaises(OSError):
            review.put(folder / "a.txt", b"three\n")
        self.assertEqual(((folder / "a.txt").read_bytes(), os.lstat(folder / "a.txt").st_ino), (b"one\n", ino))
        self.assertEqual(sorted(os.listdir(folder)), listing)

    def status(self, config):  # RF0905-3 helpers and tests below: status names why a stage is not done
        code, out = tool("status", config)
        self.assertEqual(code, 0, out)
        return out

    def test_status_reason_fingerprint_stable_and_legacy_state(self):
        config, p = make_review(self.tmp)
        self.assertEqual(self.drive(config, p)[0], 0)
        for line in self.status(config).splitlines()[:-1]:
            self.assertTrue(line.split()[3] not in ("done", "skip") or "(" not in line, line)
        spec = importlib.util.spec_from_file_location("_rf0905_3_review", TOOL)
        spec.loader.exec_module(review := importlib.util.module_from_spec(spec))
        c, dig, seen = review.context(config), {}, 0
        state = review.load_state(c)
        for i, (sid, kind, _) in enumerate(review.STAGES):
            word, _, fp = review.evaluate(c, state, dig, i)
            if kind == "run" and word == "done":
                body, rec, seen = review.fingerprint_parts(c, i, dig, state), state["stages"][sid], seen + 1
                self.assertEqual((rec["fp"], fp, rec["parts"]), (review.digest(body), review.digest(body), {k: review.digest(v) for k, v in body.items()}))
        self.assertEqual(seen, 14)
        state_path = p["out"] / "REVIEW-STATE.json"
        for rec in state["stages"].values():
            rec.pop("parts", None)
        state_path.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")
        legacy = state_path.read_bytes()
        code, out = tool("run", config)
        self.assertEqual((code, out.count(" cached\n"), out.count("rc="), state_path.read_bytes()), (0, 14, 0, legacy), out)
        facts = p["FO"] / "store" / "agencies" / SLUG / "FACTS.csv"
        facts.write_bytes(facts.read_bytes() + b"\n")
        listing = tree(p["out"])
        self.assertIn("\n7 facts-import run stale (inputs or outputs changed)\n", self.status(config))
        self.assertEqual(tree(p["out"]), listing)
        code, out = tool("run", config)
        self.assertEqual((code, [line.split()[1] for line in out.splitlines() if "rc=" in line]), (0, ["facts-import"]), out)
        self.assertIn("parts", json.loads(state_path.read_text())["stages"]["facts-import"])

    def test_status_reason_scripts_changed(self):  # the F2-only edit path
        engine = self.tmp / "engine"
        shutil.copytree(ROOT / "tools", engine / "tools")
        (engine / "campaign_tool").symlink_to(ROOT / "campaign_tool", target_is_directory=True)
        golden = engine / "tests/fixtures/flock/morrobay-golden.csv"
        golden.parent.mkdir(parents=True)
        shutil.copyfile(FIX / golden.name, golden)
        with patch(__name__ + ".TOOL", engine / "tools/flock_review.py"):
            config, p = make_review(self.tmp)
            self.assertEqual(self.drive(config, p)[0], 0)
            f2 = engine / "tools/flock_law_policy.py"
            f2.write_bytes(f2.read_bytes() + b"\n# F2-only revision\n")
            out = self.status(config)
        self.assertIn("\n9 rules-build run stale (scripts changed)\n10 rules-diff run pending (needs rules-build)\n", out)
        self.assertIn("\n11 rules-selftest run pending (needs rules-build)\n", out)

    def test_status_reason_command_upstream_inputs_config(self):
        config, p = make_review(self.tmp)
        self.assertEqual(self.drive(config, p)[0], 0)
        (p["FO"] / "drafts" / "x-computed.yaml").write_text("computed\n")  # not a marked draft, but a facts-build argument
        self.assertIn("\n5 fact-drafts wait done anthropic/claude-opus-5-5\n6 facts-build run stale (command changed)\n"
                      "7 facts-import run pending (needs facts-build)\n", self.status(config))
        write_stub(p["method"], "log_summary", '(Path(a[1]) / "LOG-SUMMARY-by-user.csv").write_text("user,count\\nnew,1\\n"); sys.exit(0)')
        self.assertEqual(tool("run", config, "--until", "logs")[0], 0)
        self.assertIn("\n6 facts-build run stale (upstream changed, command changed)\n", self.status(config))
        self.assertEqual(tool("run", config)[0], 0)
        cfg = json.loads(config.read_text())
        cfg["compare"]["f3"]["applicability"] = "Sample city police; edited"
        config.write_text(json.dumps(cfg))
        out = self.status(config)
        self.assertEqual([line.split()[3] for line in out.splitlines()[:11]], ["done"] * 3 + ["skip"] + ["done"] * 7, out)
        self.assertIn("\n13 assess wait waiting anthropic/claude-opus-5-5 (upstream changed)\n14 compare-build run pending (needs assess)\n", out)
        self.assertEqual(mark_all(config, {"assess": "anthropic"}), [0])
        out = self.status(config)
        self.assertIn("\n14 compare-build run stale (config changed)\n15 compare-selftest run pending (needs compare-build)\n", out)
        self.assertIn("\n16 f3-findings wait waiting openai/gpt-5.5 (upstream changed)\n", out)
        (p["out"] / "inputs" / "records" / "1__doc.pdf").write_text("y\n")
        self.assertTrue(self.status(config).startswith("1 inventory run stale (inputs changed)\n2 logs run pending (needs inventory)\n"))

    def test_status_reason_output_changed_missing_and_more(self):
        config, p = make_review(self.tmp)
        self.assertEqual(self.drive(config, p)[0], 0)
        rel = "store/agencies/%s/FACTS.csv" % SLUG
        facts = p["FO"] / rel
        original = facts.read_bytes()
        facts.write_bytes(original.replace(b"Vigilant", b"Vigilent"))
        listing = tree(p["out"])
        self.assertIn("\n7 facts-import run stale (output changed: %s)\n" % rel, self.status(config))
        self.assertEqual(tree(p["out"]), listing)
        facts.unlink()
        self.assertIn("\n7 facts-import run stale (output missing: %s)\n" % rel, self.status(config))
        for name in ("VERSION-DIFF.csv", "REQUIREMENTS.csv"):  # two rules-build outputs
            (p["RO"] / name).write_bytes(b"edited\n")
        self.assertIn("\n9 rules-build run stale (output changed: REQUIREMENTS.csv +1)\n", self.status(config))

    def test_status_reason_wait_and_failed(self):
        config, p = make_review(self.tmp, stubs={"check_compare": "sys.exit(1)"})
        self.assertIn("stage fact-drafts WAITING (not marked)", tool("run", config)[1])
        self.assertIn("\n5 fact-drafts wait waiting (not marked)\n6 facts-build run pending (needs fact-drafts)\n", self.status(config))
        self.drive(config, p, stop="assess")
        self.assertIn("stage compare-selftest failed rc=1", tool("run", config)[1])
        out = self.status(config)
        self.assertIn("\n15 compare-selftest run failed (rc=1)\n16 f3-findings wait waiting (not marked)\n", out)
        self.assertIn("\n20 counts run pending (never run)\n", out)
        (p["CO"] / "ASSESSMENTS.yaml").write_text("edited\n")
        self.assertIn("stage assess WAITING (changed since mark)", tool("run", config)[1])
        self.assertIn("\n13 assess wait waiting anthropic/claude-opus-5-5 (changed since mark)\n", self.status(config))


if __name__ == "__main__":
    unittest.main()
