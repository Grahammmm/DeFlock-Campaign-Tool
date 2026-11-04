"""RF0450-10: `brief STORE --agency SLUG --against FILE.md` re-derives the draft read-only: 0 current, 3 drifted, 1 refused."""
import os, subprocess, sys, unittest
from tests.flock import test_publish as base
from tests.flock import test_publish_brief as pb

A, B = pb.A, pb.B


def drifted(added, removed, changed, label="brief.md"):
    return "brief | against=%s state=drifted added=%d removed=%d changed=%d\n" % (label, added, removed, changed)


class PublishBriefAgainstTest(pb.PublishBriefTest):
    def against(self, path=None, agency=base.AGENCY, tool="flock_publish.py"):
        args = ([] if tool != "flock_publish.py" else ["brief"]) + [str(self.store), "--agency", agency,
                                                                     "--against", str(path or self.brief_md)]
        done = subprocess.run([sys.executable, "-B", str(base.TOOLS / tool), *args], capture_output=True, text=True, check=False)
        self.hygiene(done.stdout + done.stderr)
        self.assertEqual(done.stderr, "")
        return done.returncode, done.stdout

    def written(self, *ids):  # approve ids in run A and write the draft the comparisons start from
        self.approve(A, *ids)
        self.assertEqual(self.brief(), (0, pb.summary(2, len(ids), 0)))
        return self.brief_md.read_bytes(), self.brief_md.stat().st_mtime_ns

    def assert_untouched(self, before):
        self.assertEqual((self.brief_md.read_bytes(), self.brief_md.stat().st_mtime_ns), before)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["brief.md", "store"])

    def test_current_draft_exit_0_and_file_untouched(self):
        before = self.written("N1", "O1")
        self.assertEqual(self.against(), (0, "brief | against=brief.md state=current approved=2\n"))
        self.assert_untouched(before)
        self.assertEqual(self.against(tool="flock_publish_brief.py"), (0, "brief | against=brief.md state=current approved=2\n"))
        self.assert_untouched(before)

    def test_one_more_approval_is_added(self):
        before = self.written("N1")
        self.approve(A, "O1")
        self.assertEqual(self.against(), (3, "brief | drift | header | changed\nbrief | drift | O1 | added\n" + drifted(1, 0, 0)))
        self.assert_untouched(before)

    def test_needs_review_is_removed_and_edited_ref_is_changed(self):
        before = self.written("N1", "O1", "A1")
        self.entry(A, "O1")["review"]["status"] = "needs-review"  # what recheck does; approval fields kept
        self.save(A)
        self.approve(A, "N1", ref="G-2")
        out = "brief | drift | header | changed\nbrief | drift | N1 | changed\nbrief | drift | O1 | removed\n"
        self.assertEqual(self.against(), (3, out + drifted(0, 1, 1)))
        self.assert_untouched(before)

    def test_changed_without_header_change(self):
        self.written("N1", "O1")
        self.approve(A, "O1", on="2026-10-31")
        self.assertEqual(self.against(), (3, "brief | drift | O1 | changed\n" + drifted(0, 0, 1)))

    def test_hand_edited_first_or_last_line_is_header(self):
        data, _ = self.written("N1")
        for old, new in ((pb.FIRST, "DRAFT - sent"), (pb.LAST, "END.")):
            with self.subTest(line=new):
                self.brief_md.write_bytes(data.replace(old.encode(), new.encode()))
                self.assertEqual(self.against(), (3, "brief | drift | header | changed\n" + drifted(0, 0, 0)))
        self.brief_md.write_bytes(data.rstrip(b"\n"))  # the missing final newline is a byte difference too
        self.assertEqual(self.against(), (3, "brief | drift | header | changed\n" + drifted(0, 0, 0)))

    def test_hand_added_sections_keyed_by_safe_token_only(self):
        data, _ = self.written("N1")
        extra = b"## Contact the chief directly (x)\n\nsome text\n\n## not/a-token here\n\n## N1 (dup)\n\n"
        self.brief_md.write_bytes(data.replace(pb.LAST.encode(), extra + pb.LAST.encode()))
        code, out = self.against()
        self.assertEqual((code, out), (3, "brief | drift | ? | removed\nbrief | drift | Contact | removed\n"
                                          "brief | drift | N1 | changed\n" + drifted(0, 2, 1)))
        self.assertNotIn("chief", out)
        self.assertNotIn("not/a-token", out)

    def test_bad_against_paths_refused_with_fixed_reason(self):
        self.written("N1")
        link, fifo, folder = self.tmp / "link.md", self.tmp / "fifo.md", self.tmp / "folder.md"
        big, latin, gone = self.tmp / "big.md", self.tmp / "latin.md", self.tmp / "gone.md"
        link.symlink_to(self.brief_md)
        os.mkfifo(fifo)
        folder.mkdir()
        big.write_bytes(b"x" * (3 * 1024 * 1024))
        latin.write_bytes("DRAFT caf\u00e9\n".encode("latin-1"))
        dangling = self.tmp / "dangling.md"
        dangling.symlink_to(self.tmp / "nowhere.md")
        for path, why in ((link, "is a link"), (dangling, "is a link"), (fifo, "not a regular file"), (folder, "not a regular file"),
                          (big, "too large"), (latin, "not UTF-8"), (gone, "missing"), (self.brief_md / "x", "missing")):
            with self.subTest(why=why, path=path.name):
                self.assertEqual(self.against(path), (1, "brief refused: --against %s\n" % why))
        self.assertFalse(os.path.lexists(gone))

    def test_exactly_two_mib_is_read(self):
        self.written("N1")
        edge = self.tmp / "edge.md"
        edge.write_bytes(b"y" * (2 * 1024 * 1024))
        self.assertEqual(self.against(edge)[0], 3)
        edge.write_bytes(b"y" * (2 * 1024 * 1024 + 1))
        self.assertEqual(self.against(edge), (1, "brief refused: --against too large\n"))

    def test_store_refusals_match_brief(self):
        self.written("N1")
        self.assertEqual(self.against(agency="Bad Slug"), (1, "brief refused: --agency is not a slug\n"))
        self.assertEqual(self.against(agency="no-such-agency"), (1, "brief refused: agency has no runs folder\n"))
        self.assertEqual(base.run("flock_publish.py", "brief", str(base.ROOT), "--agency", base.AGENCY, "--against", str(self.brief_md)),
                         (1, "flock-publish: STORE is inside the repository\n"))
        os.rename(self.store / "agencies" / base.AGENCY, self.tmp / "moved")
        (self.store / "agencies" / base.AGENCY).symlink_to(self.tmp / "moved")
        self.assertEqual(self.against(), (1, "brief refused: agency folder is a link\n"))

    def test_nothing_approved_any_more(self):
        before = self.written("N1")
        self.entry(A, "N1")["review"] = dict(pb.PENDING)
        self.save(A)
        self.assertEqual(self.against(), (1, "brief refused: nothing approved\n"))
        self.assertEqual(self.against(tool="flock_publish_brief.py"), (1, "brief refused: nothing approved\n"))
        self.assert_untouched(before)

    def test_skipped_runs_named_as_in_brief(self):
        before = self.written("N1")
        self.entry(B, "T1")["text"] = "Changed."
        self.save(B)
        self.assertEqual(self.against(), (0, "brief | %s | skipped: check failed\n" % B
                                          + "brief | against=brief.md state=current approved=1\n"))
        self.assert_untouched(before)

    def test_contact_pattern_refused_as_in_brief(self):
        self.written("N1")
        self.approve(A, "N1", ref="call 805-555-1234")
        self.assertEqual(self.against(), (1, "brief refused: an approved entry carries an email or phone pattern\n"))

    def test_usage(self):
        self.written("N1")
        md, store = str(self.brief_md), str(self.store)
        for argv in (("brief", store, "--agency", base.AGENCY, "--out", str(self.tmp / "n.md"), "--against", md),
                     ("brief", store, "--agency", base.AGENCY, "--against", md, "--copy", "x"),
                     ("brief", store, "--agency", base.AGENCY, "--against", ""),
                     ("brief", store, "--against", md),
                     ("stage", self.rd, "--copy", "x", "--out", str(self.tmp / "s"), "--against", md),
                     ("export", self.rd, "--site-slug", "s", "--against", md),
                     ("status", store, "--against", md), ("--check", md, "--against", md)):
            with self.subTest(argv=argv):
                self.assertEqual(base.run("flock_publish.py", *argv), (2, ""))
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["brief.md", "store"])


for _name in [n for n in dir(pb.PublishBriefTest) if n.startswith("test_")]:
    if _name not in vars(PublishBriefAgainstTest):
        setattr(PublishBriefAgainstTest, _name, None)  # brief tests run once, in tests/flock/test_publish_brief.py

if __name__ == "__main__":
    unittest.main()
