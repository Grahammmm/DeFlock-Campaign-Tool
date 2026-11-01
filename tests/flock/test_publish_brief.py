"""DF1537-12: `brief STORE --agency SLUG --out FILE.md` writes one private draft of an agency's approved PUBLISH.json entries."""
import json, os, shutil, subprocess, sys, unittest
from pathlib import Path
from unittest import mock
from tests.flock import test_publish as base

A, B = "2026-10-30", "2026-10-29"  # run A (newest) is the base fixture run; run B is its copy one day older
PENDING = {"status": "pending", "approved_by": "", "approved_on": "", "approval_ref": ""}
FIRST = "DRAFT - not sent - G approval required"
LAST = "END OF BRIEF DRAFT. Do not send, post or share without G's approval. Contact details are intentionally omitted."
EMAIL = r"[^\s@]+@[^\s@]+\.[a-z]{2,}"
PHONE = r"(?<!\d)(?:\(\d{3}\)\s?|\d{3}[ .-])\d{3}[ .-]\d{4}(?!\d)"


def summary(runs, approved, skipped, written="brief.md"):
    return "brief | agency=%s runs=%d approved=%d skipped_runs=%d written=%s\n" % (base.AGENCY, runs, approved, skipped, written)


class PublishBriefTest(base.FlockPublishTest):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.export(), (0, "exported=4 refused=0\n"))
        self.runs = self.run_dir.parent
        shutil.copytree(self.run_dir, self.runs / B)
        self.docs = {d: json.loads((self.runs / d / "PUBLISH.json").read_text()) for d in (A, B)}
        self.docs[B]["run_date"] = B
        self.save(B)
        shutil.copy(base.FIXTURES / "contact-sample.csv", self.runs.parent / "CONTACT.csv")
        self.brief_md = self.tmp / "brief.md"

    def save(self, day):
        (self.runs / day / "PUBLISH.json").write_text(json.dumps(self.docs[day]))

    def approve(self, day, *ids, ref="G-1", on="2026-10-30"):
        for f in self.docs[day]["findings"]:
            if f["finding_id"] in ids:
                f["review"] = {"status": "approved", "approved_by": "G", "approved_on": on, "approval_ref": ref}
        self.save(day)

    def entry(self, day, fid):
        return next(f for f in self.docs[day]["findings"] if f["finding_id"] == fid)

    def hygiene(self, output):  # edge path 9: no entry text or cite label ever reaches stdout/stderr
        for doc in self.docs.values():
            for f in doc["findings"]:
                for secret in [f["text"]] + [c["label"] for c in f["cites"]]:
                    self.assertNotIn(secret, output)

    def brief(self, *argv, out=None, agency=base.AGENCY, tool="flock_publish.py"):
        args = ([] if tool != "flock_publish.py" else ["brief"]) + [str(self.store), "--agency", agency,
                                                                     "--out", str(out or self.brief_md), *argv]
        done = subprocess.run([sys.executable, "-B", str(base.TOOLS / tool), *args], capture_output=True, text=True, check=False)
        self.hygiene(done.stdout + done.stderr)
        return done.returncode, done.stdout

    def refused(self, result, line, out=None):
        self.assertEqual(result, (1, "brief refused: %s\n" % line))
        self.assertFalse(os.path.lexists(out or self.brief_md))

    def block(self, day, fid, also=""):  # the fixed template for one finding, built from the file's own fields
        f = self.entry(day, fid)
        cites = ["- %s (%s)" % (c["label"], c["url"]) if c["kind"] == "law" else "- " + c["label"] for c in f["cites"]]
        return ["## %s (%s, %s, run %s)%s" % (fid, f["topic"], f["claim_class"], day, also), "", "> " + f["text"], "",
                "Sources:"] + cites + ["", "Approval: G-1 (2026-10-30)", ""]

    def test_two_runs_three_approved_one_pending(self):
        self.approve(A, "N1", "O1")
        self.approve(B, "A1")
        self.assertEqual(self.brief(), (0, summary(2, 3, 0)))
        self.assertEqual(self.brief_md.stat().st_mode & 0o777, 0o600)
        text = self.brief_md.read_text(encoding="utf-8")
        head = [FIRST, "# %s — findings brief (3 approved findings)" % base.AGENCY, "Research draft - not legal advice", ""]
        self.assertEqual(text, "\n".join(head + self.block(A, "N1") + self.block(A, "O1") + self.block(B, "A1") + [LAST]) + "\n")
        self.assertNotIn(self.entry(A, "T1")["text"], text)  # pending in both runs
        self.assertNotIn("(also run", text)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["brief.md", "store"])  # no temp file left

    def test_same_text_in_two_runs_listed_once_with_older_date(self):
        self.approve(A, "N1", "O1")
        self.approve(B, "A1", "N1")
        self.assertEqual(self.brief(), (0, summary(2, 3, 0)))
        text = self.brief_md.read_text(encoding="utf-8")
        self.assertEqual(text.count(self.entry(A, "N1")["text"]), 1)
        self.assertIn("\n".join(self.block(A, "N1", " (also run %s)" % B)), text)
        self.assertEqual(text.count("\n## "), 3)

    def test_drifted_or_unverifiable_run_skipped_and_named(self):
        self.approve(A, "N1", "O1")
        self.approve(B, "A1")
        self.entry(B, "T1")["text"] = "Changed."
        self.save(B)
        (self.runs / "2026-10-28").mkdir()  # a run with no PUBLISH.json is not a publish run: not counted
        self.assertEqual(self.brief(), (0, "brief | %s | skipped: check failed\n" % B + summary(1, 2, 1)))
        self.assertNotIn(self.entry(B, "A1")["text"], self.brief_md.read_text(encoding="utf-8"))
        self.brief_md.unlink()
        self.entry(B, "T1")["text"] = self.entry(A, "T1")["text"]
        self.save(B)
        (self.runs / B / "VERIFY.json").write_text(json.dumps({"schema": 1, "verifier_provider": "anthropic"}))  # same family
        self.assertEqual(self.brief(), (0, "brief | %s | skipped: check failed\n" % B + summary(1, 2, 1)))

    def test_nothing_approved_writes_nothing(self):
        result = self.brief()
        self.refused(result, "nothing approved")
        self.approve(B, "A1")
        (self.runs / B / "PUBLISH.json").write_text("{not json")  # the only approval sits in a broken run
        self.assertEqual(self.brief(), (1, "brief | %s | skipped: check failed\nbrief refused: nothing approved\n" % B))
        self.assertFalse(self.brief_md.exists())

    def test_out_paths_refused(self):
        self.approve(A, "N1")
        taken, dangling = self.tmp / "taken.md", self.tmp / "dangling.md"
        taken.write_text("old")
        dangling.symlink_to(self.tmp / "nowhere.md")
        for out, line in ((taken, "--out already exists"), (dangling, "--out already exists"),
                          (self.tmp / "no-dir" / "b.md", "--out folder does not exist"),
                          (base.ROOT / "df1537-12-brief.md", "--out is inside the repository"),
                          (self.store / "agencies" / "b.md", "--out is inside STORE/agencies"),
                          (self.runs / A / "b.md", "--out is inside STORE/agencies")):
            with self.subTest(line=line, out=out.name):
                code, text = self.brief(out=out)
                self.assertEqual((code, text), (1, "brief refused: %s\n" % line))
        self.assertEqual((taken.read_text(), os.path.lexists(self.tmp / "nowhere.md"), (base.ROOT / "df1537-12-brief.md").exists(),
                          (self.store / "agencies" / "b.md").exists(), (self.runs / A / "b.md").exists()), ("old", False, False, False, False))
        self.assertEqual(base.run("flock_publish.py", "brief", str(base.ROOT), "--agency", base.AGENCY, "--out", str(self.brief_md)),
                         (1, "flock-publish: STORE is inside the repository\n"))
        self.refused(self.brief(agency="Bad Slug"), "--agency is not a slug")
        self.refused(self.brief(agency="no-such-agency"), "agency has no runs folder")
        os.rename(self.store / "agencies" / base.AGENCY, self.tmp / "moved")
        (self.store / "agencies" / base.AGENCY).symlink_to(self.tmp / "moved")
        self.refused(self.brief(), "agency folder is a link")

    def test_usage(self):
        for argv in (("brief", str(self.store), "--agency", base.AGENCY), ("brief", str(self.store), "--out", str(self.brief_md)),
                     ("brief", "--agency", base.AGENCY, "--out", str(self.brief_md)),
                     ("brief", str(self.store), "--agency", base.AGENCY, "--out", str(self.brief_md), "--copy", "x"),
                     ("brief", str(self.store), "--agency", base.AGENCY, "--out", str(self.brief_md), "--site-slug", "s"),
                     ("status", str(self.store), "--agency", base.AGENCY), ("stage", self.rd, "--copy", "x", "--out", "y", "--agency", "a")):
            with self.subTest(argv=argv):
                self.assertEqual(base.run("flock_publish.py", *argv), (2, ""))
        self.assertFalse(self.brief_md.exists())
        done = subprocess.run([sys.executable, "-B", str(base.TOOLS / "flock_publish_brief.py"), "--help"], capture_output=True, text=True)
        self.assertEqual((done.returncode, "{export,status,stage,brief,recheck}" in done.stdout), (0, True))

    def test_linked_run_or_file_skipped_never_read(self):
        self.approve(A, "N1")
        self.approve(B, "T1")  # B's approval must never be used once B is a link
        os.rename(self.runs / B, self.tmp / "b-real")
        (self.runs / B).symlink_to(self.tmp / "b-real")
        (self.runs / "2026-10-31").mkdir()
        (self.runs / "2026-10-31" / "PUBLISH.json").symlink_to(self.runs / A / "PUBLISH.json")
        read, real = [], Path.read_text

        def spy(path, *args, **kwargs):
            read.append(Path(path))
            return real(path, *args, **kwargs)
        with mock.patch.object(Path, "read_text", spy):
            code, out = self.call(self.tmp / "repo", "brief", str(self.store), "--agency", base.AGENCY, "--out", str(self.brief_md))
        self.hygiene(out)
        self.assertEqual((code, out), (0, "brief | 2026-10-31 | skipped: link\nbrief | %s | skipped: link\n" % B + summary(1, 1, 2)))
        self.assertIn(self.runs / A / "PUBLISH.json", read)  # the spy sees the reads it guards
        self.assertEqual([p for p in read if str(p).startswith((str(self.runs / B), str(self.runs / "2026-10-31")))], [])
        self.assertNotIn(self.entry(B, "T1")["text"], self.brief_md.read_text(encoding="utf-8"))

    def test_contact_details_never_in_the_brief(self):
        self.approve(A, "N1", "O1", "A1", "T1")
        self.assertEqual(self.brief()[0], 0)
        text = self.brief_md.read_text(encoding="utf-8")
        self.assertIn("records@city.example", (self.runs.parent / "CONTACT.csv").read_text())
        self.assertNotRegex(text, EMAIL)
        self.assertNotRegex(text, PHONE)
        self.brief_md.unlink()
        for ref in ("ask records@city.example", "call 805-555-1234"):
            with self.subTest(ref=ref):
                self.approve(A, "N1", ref=ref)
                self.refused(self.brief(), "an approved entry carries an email or phone pattern")

    def test_injected_link_failure_leaves_nothing(self):
        self.approve(A, "N1")
        with mock.patch("os.link", side_effect=OSError("disk full")):
            result = self.call(self.tmp / "repo", "brief", str(self.store), "--agency", base.AGENCY, "--out", str(self.brief_md))
        self.assertEqual(result, (1, "io: OSError\n"))
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["store"])

    def test_direct_entry_point_matches_verb(self):
        self.approve(A, "N1")
        self.assertEqual(self.brief(tool="flock_publish_brief.py"), (0, summary(2, 1, 0)))


for _name in [n for n in dir(base.FlockPublishTest) if n.startswith("test_")]:
    setattr(PublishBriefTest, _name, None)  # base tests run once, in tests/flock/test_publish.py

if __name__ == "__main__":
    unittest.main()
