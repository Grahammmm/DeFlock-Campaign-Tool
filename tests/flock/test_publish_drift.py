"""RF1130-2: `--check FILE --run RUNDIR` re-derives the export from RUNDIR; any entry that drifted from it is refused."""
import json, shutil, unittest
from tests.flock import test_publish as base

APPROVED = {"status": "approved", "approved_by": "G", "approved_on": "2026-10-30", "approval_ref": "G-1"}
SECRET = "private source text"


class PublishDriftTest(base.FlockPublishTest):  # setUp builds store/agencies/<AGENCY>/runs/2026-10-30 (F4 finished)
    def setUp(self):
        super().setUp()
        self.assertEqual(self.export(), (0, "exported=4 refused=0\n"))
        self.doc = json.loads(self.out.read_text())

    def drift(self, doc=None, rd=None):  # doc None: the exported file itself
        path = self.out
        if doc is not None:
            path = self.tmp / "x.json"
            path.write_text(json.dumps(doc))
        return base.run("flock_publish.py", "--check", str(path), "--run", rd or self.rd)

    def edited(self, fid, **fields):
        doc = json.loads(json.dumps(self.doc))
        entry = next(f for f in doc["findings"] if f["finding_id"] == fid)
        entry.update(fields)
        return doc

    def snap(self):
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.run_dir.iterdir()}

    def test_fresh_export_and_human_approval_pass(self):
        self.assertEqual(self.drift(), (0, "findings=4 approved=0 errors=0 drift=0\n"))
        self.assertEqual(self.drift(self.edited("N1", review=APPROVED)), (0, "findings=4 approved=1 errors=0 drift=0\n"))
        self.assertEqual(base.run("flock_publish.py", "--check", str(self.out)), (0, "findings=4 approved=0 errors=0\n"))

    def test_edited_text_refused_without_echo(self):
        old = self.doc["findings"][0]["text"]
        for new in (old.replace("search log", "search record"), old + " ", old.replace("sample", "Sample", 1)):
            for review in (self.doc["findings"][0]["review"], APPROVED):
                with self.subTest(new=new, review=review["status"]):
                    self.assertEqual(self.drift(self.edited("N1", text=new, review=review)),
                                     (1, "finding N1: text differs from the run\nfindings=4 approved=%d errors=1 drift=1\n"
                                      % (review is APPROVED)))

    def test_edited_cites_topic_and_class_refused(self):
        cites = self.doc["findings"][0]["cites"]
        relabel = json.loads(json.dumps(cites)); relabel[-1]["label"] = "Request REQ-999, page 2"
        rehost = json.loads(json.dumps(cites))
        rehost[0]["url"] = rehost[0]["url"].replace("leginfo.legislature.ca.gov", "oag.ca.gov")
        self.assertNotEqual(rehost, cites)
        for name, new in (("label", relabel), ("host", rehost), ("order", cites[::-1])):
            with self.subTest(name):
                self.assertEqual(self.drift(self.edited("N1", cites=new)),
                                 (1, "finding N1: cites differs from the run\nfindings=4 approved=0 errors=1 drift=1\n"))
        code, out = self.drift(self.edited("N1", topic="other-topic", claim_class="duty-concern"))
        self.assertEqual((code, out), (1, "finding N1: topic differs from the run\nfinding N1: claim_class differs from the run\n"
                                          "findings=4 approved=0 errors=2 drift=2\n"))
        self.assertNotIn("https://", out)

    def test_finding_no_longer_eligible(self):
        verify = (self.run_dir / "VERIFY.csv").read_text()
        row = next(line for line in verify.splitlines() if line.startswith("T1,"))
        flipped = "T1,downgrade,F-a71d059d7a51,6,medium,Existing training material withholding notices,Page 6 is unclear."
        (self.run_dir / "VERIFY.csv").write_text(verify.replace(row, flipped))
        for name in ("PUBLIC-OK.csv", "QUESTIONS.md"):
            (self.run_dir / name).unlink()
        self.assertEqual(base.run("flock_verify.py", "finish", self.rd)[0], 0)  # re-signed as the test store does
        self.assertEqual(self.drift(), (1, "finding T1: not in the run's eligible findings\n"
                                           "findings=4 approved=0 errors=1 drift=1\n"))

    def test_file_from_another_run(self):
        other = self.run_dir.with_name("2026-10-31")
        shutil.copytree(self.run_dir, other)
        (other / "PUBLISH.json").unlink()
        self.assertEqual(self.drift(rd=str(other)), (1, "file: run_date differs from the run\n"
                                                         "findings=4 approved=0 errors=1 drift=1\n"))
        doc = dict(self.doc, agency="other-agency", author_family="xai")
        self.assertEqual(self.drift(doc), (1, "file: agency differs from the run\nfile: author_family differs from the run\n"
                                              "findings=4 approved=0 errors=2 drift=2\n"))

    def test_entry_missing_from_file_is_a_note_until_one_is_approved(self):
        older = dict(self.doc, findings=self.doc["findings"][:3])
        self.assertEqual(self.drift(older), (0, "note: run has 1 eligible findings not in the file\n"
                                                "findings=3 approved=0 errors=0 drift=0\n"))
        older["findings"][0] = dict(older["findings"][0], review=APPROVED)
        self.assertEqual(self.drift(older), (1, "run: finding T1 eligible but missing from the file\n"
                                                "findings=3 approved=1 errors=1 drift=1\n"))

    def test_duplicate_and_unknown_entries_refused(self):
        doc = json.loads(json.dumps(self.doc))
        doc["findings"].append(dict(doc["findings"][0], review=APPROVED))
        doc["findings"].append(dict(doc["findings"][1], finding_id="N2"))
        self.assertEqual(self.drift(doc), (1, "finding N1: appears more than once in the file\n"
                                              "finding N2: not in the run's eligible findings\n"
                                              "findings=6 approved=1 errors=2 drift=2\n"))

    def test_schema_errors_skip_the_rederive(self):
        code, out = self.drift(self.edited("N1", text=SECRET + " ask records@sample.org"))
        self.assertEqual((code, out), (1, "finding N1: text refused email\nfindings=4 approved=0 errors=1 drift=0\n"))
        self.assertEqual(self.drift({"findings": []}), (1, "file: schema or header is invalid\nfindings=0 approved=0 errors=1 drift=0\n"))

    def test_derive_refusal_named(self):
        (self.run_dir / "VERIFY.json").write_text(json.dumps({"schema": 1, "verifier_provider": "anthropic"}))
        self.assertEqual(self.drift(), (1, "refused: verifier family equals author family\nrun: cannot re-derive (exit 3)\n"
                                           "findings=4 approved=0 errors=1 drift=1\n"))
        shallow = self.tmp / "loose"
        shutil.copytree(self.run_dir, shallow)
        self.assertEqual(self.drift(rd=str(shallow)), (1, "export refused: RUNDIR must be <store>/agencies/<slug>/runs/<date>\n"
                                                           "run: cannot re-derive (exit 1)\nfindings=4 approved=0 errors=1 drift=1\n"))

    def test_usage_errors(self):
        for argv in (("--run", self.rd), ("--check", str(self.out), "--run", self.rd, "--site-slug", "sample"),
                     ("export", self.rd, "--site-slug", "sample", "--run", self.rd), ("--check", str(self.out), "--run", "")):
            with self.subTest(argv=argv):
                self.assertEqual(base.run("flock_publish.py", *argv), (2, ""))

    def test_rundir_inside_repository_refused_before_reading(self):
        refused = (1, "flock-publish: RUNDIR is inside the repository\n")
        for repo, path in ((self.store, self.out), (self.run_dir, self.out), (self.store, self.tmp / "missing.json")):
            with self.subTest(repo=repo.name, path=path.name):
                self.assertEqual(self.call(repo, "--check", str(path), "--run", self.rd), refused)
        self.assertEqual(self.call(self.store, "--check", str(self.out), "--run", str(self.store / "nope")), refused)

    def test_check_run_never_writes(self):
        before = self.snap()
        drifted = self.edited("N1", text="Changed.")
        for doc in (None, drifted):
            self.drift(doc)
        self.assertEqual(self.snap(), before)


for _name in [n for n in dir(base.FlockPublishTest) if n.startswith("test_")]:
    setattr(PublishDriftTest, _name, None)  # base tests run once, in tests/flock/test_publish.py

if __name__ == "__main__":
    unittest.main()
