"""RF0450-9: flock_review_verify.py agreement --json-out NEWFILE - one private flock-review-agreement-v1 document, 0600, new file."""
import json, os, re, stat, unittest
from pathlib import Path
from unittest import mock

import tests.flock.test_review_verify_agreement as base  # module import, so its tests are not collected twice

KEYS = {"schema", "review_required", "plan_findings_sha256", "max_disagree", "groups", "verdicts", "disagree_ids",
        "priority_rows", "sample_rows", "pool", "sample_rate", "escalate"}
LINE = re.compile(r"^agreement \| (group|verdict) (.+) \| agree=(\d+) disagree=(\d+) needs_source=(\d+)$")


class AgreementJsonTest(unittest.TestCase):
    setUp, tearDown, main, make, write, agree = (base.AgreementTest.setUp, base.AgreementTest.tearDown, base.AgreementTest.main,
                                                 base.AgreementTest.make, base.AgreementTest.write, base.AgreementTest.agree)

    def out_path(self, name="A.json"):
        return self.tmp / "out" / name

    def json_run(self, pct=20, *extra, target=None):
        target = target or self.out_path()
        target.parent.mkdir(exist_ok=True)
        return self.agree(pct, *extra, "--json-out", target)

    def doc(self, target=None):
        return json.loads((target or self.out_path()).read_text(encoding="utf-8"))

    def test_clean_agreement_writes_0600_document_matching_the_printed_counts(self):
        pri, smp = self.make()
        self.write({})
        plain = self.agree()
        code, out = self.json_run()
        self.assertEqual((code, out), (0, plain[1] + "agreement | json=A.json\n"))
        target = self.out_path()
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        raw = target.read_text(encoding="utf-8")
        doc = json.loads(raw)
        self.assertEqual(raw, json.dumps(doc, sort_keys=True, indent=1, ensure_ascii=False) + "\n")
        self.assertEqual(set(doc), KEYS)
        plan = json.loads(self.plan.read_text(encoding="utf-8"))
        self.assertEqual({k: doc[k] for k in KEYS - {"groups", "verdicts"}},
                         {"schema": "flock-review-agreement-v1", "review_required": True, "max_disagree": 20, "escalate": "none",
                          "plan_findings_sha256": plan["findings_sha256"], "disagree_ids": [], "priority_rows": len(pri),
                          "sample_rows": len(smp), "pool": 12, "sample_rate": "83.3"})
        printed = {}
        for m in filter(None, map(LINE.match, out.splitlines())):
            printed.setdefault(m[1] + "s", {})[m[2]] = {"agree": int(m[3]), "disagree": int(m[4]), "needs_source": int(m[5])}
        self.assertEqual(list(printed["verdicts"]), ["met", "not met", "contradicted", "cannot tell"])
        self.assertEqual({"groups": doc["groups"], "verdicts": doc["verdicts"]}, printed)
        self.assertEqual(doc["groups"]["priority"], {"agree": 3, "disagree": 0, "needs_source": 0})

    def test_relative_path_is_made_absolute(self):
        self.make()
        self.write({})
        (self.tmp / "out").mkdir()
        cwd = os.getcwd()
        os.chdir(self.tmp / "out")
        self.addCleanup(os.chdir, cwd)
        code, out = self.agree(20, "--json-out", "R.json")
        self.assertEqual(code, 0, out)
        self.assertTrue(out.endswith("agreement | json=R.json\n"))
        self.assertEqual(self.doc(self.out_path("R.json"))["escalate"], "none")

    def test_escalations_priority_sample_and_both(self):
        _, smp = self.make()
        cases = (("priority", {"P2": "disagree"}, ["P2"]), ("sample", {i: "disagree" for i in smp[:3]}, []),
                 ("both", {"P1": "disagree", "P3": "needs-source", smp[0]: "needs-source", smp[1]: "disagree", smp[2]: "disagree"},
                  ["P1", "P3"]))
        for escalate, results, ids in cases:
            with self.subTest(escalate):
                self.write(results)
                target = self.out_path(escalate + ".json")
                code, out = self.json_run(target=target)
                self.assertEqual(code, 3, out)
                self.assertIn("escalate=%s |" % escalate, out)
                doc = self.doc(target)
                self.assertEqual((doc["escalate"], doc["disagree_ids"]), (escalate, ids))
        self.assertEqual(self.doc(self.out_path("both.json"))["groups"]["priority"], {"agree": 1, "disagree": 1, "needs_source": 1})

    def test_existing_target_is_refused_and_untouched(self):
        self.make()
        self.write({})
        (self.tmp / "out").mkdir()
        regular, dangling = self.out_path("old.json"), self.out_path("link.json")
        regular.write_bytes(b"old bytes\n")
        dangling.symlink_to(self.tmp / "out" / "nowhere.json")
        for target in (regular, dangling):
            with self.subTest(target.name):
                self.assertEqual(self.json_run(target=target), (1, "agreement: --json-out refused: exists\n"))
        self.assertEqual(regular.read_bytes(), b"old bytes\n")
        self.assertTrue(dangling.is_symlink())
        self.assertFalse(os.path.lexists(self.tmp / "out" / "nowhere.json"))

    def test_inside_repository_missing_folder_or_input_is_refused(self):
        self.make()
        self.write({})
        md = self.tmp / "V.md"
        md.write_text("notes\n")
        (self.tmp / "repo-link").symlink_to(base.ROOT / "tests")
        cases = ((base.ROOT / "rf0450-9-agreement.json", "inside repository"),
                 (self.tmp / "repo-link" / "rf0450-9-agreement.json", "inside repository"),  # resolved through a link
                 (self.tmp / "missing" / "A.json", "no folder"), (self.verify, "is an input"), (self.plan, "is an input"),
                 (self.findings, "is an input"), (md, "is an input"))
        before = [p.read_bytes() for p in (self.plan, self.findings, self.verify, md)]
        for target, reason in cases:
            with self.subTest(reason=reason, target=target.name):
                code, out = self.agree(20, "--md", md, "--json-out", target)
                self.assertEqual((code, out), (1, "agreement: --json-out refused: %s\n" % reason))
        self.assertFalse(os.path.lexists(base.ROOT / "rf0450-9-agreement.json"))
        self.assertFalse(os.path.lexists(base.ROOT / "tests" / "rf0450-9-agreement.json"))
        self.assertFalse((self.tmp / "missing").exists())
        self.assertEqual([p.read_bytes() for p in (self.plan, self.findings, self.verify, md)], before)

    def test_check_failure_prints_todays_lines_and_writes_nothing(self):
        _, smp = self.make()
        self.write({}, drop=(smp[3],))
        plain = self.agree()
        code, out = self.json_run()
        self.assertEqual((code, out), plain)
        self.assertEqual(code, 1)
        self.assertIn("plan: missing %s\n" % smp[3], out)
        self.assertTrue(out.endswith("agreement: check failed; run check first\n"))
        self.assertFalse(os.path.lexists(self.out_path()))

    def test_invalid_pool_size_or_write_failure_leaves_no_file(self):
        self.make()
        self.write({})
        body = json.loads(self.plan.read_text(encoding="utf-8"))
        self.plan.write_text(json.dumps({**body, "pool_size": 9}), encoding="utf-8")
        self.assertEqual(self.json_run(), (1, "agreement: plan pool_size is invalid\n"))
        self.assertFalse(os.path.lexists(self.out_path()))
        self.plan.write_text(json.dumps(body), encoding="utf-8")
        with mock.patch.object(base.VERIFY.os, "write", side_effect=OSError("disk full")):
            self.assertEqual(self.json_run(), (1, "io: OSError\n"))
        self.assertEqual(os.listdir(self.tmp / "out"), [])

    def test_check_does_not_accept_json_out(self):
        self.make()
        self.write({})
        code, out = self.main("check", self.plan, self.findings, self.verify, "--json-out", self.tmp / "C.json")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("unrecognized arguments: --json-out", self.seen[-1])
        self.assertFalse(os.path.lexists(self.tmp / "C.json"))

    def test_not_posted_id_and_private_text_never_reach_the_document(self):
        self.make(rows=base.ROWS + [("NP9", "n/a")])
        self.write({"P1": "disagree"})
        code, out = self.json_run(20, "--not-posted", "NP9")
        self.assertEqual(code, 3, out)
        raw = self.out_path().read_bytes()
        for text in (b"NP9", base.REASON.encode(), base.CITE.encode(), b"basis text", b"requirement"):
            self.assertNotIn(text, raw)
        self.assertEqual(self.doc()["disagree_ids"], ["P1"])


if __name__ == "__main__":
    unittest.main()
