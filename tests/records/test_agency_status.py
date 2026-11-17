import hashlib
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from campaign_tool.records import cli
from campaign_tool.records.agency_run import status

NOW = 1_800_000_000
ALL = ("EVIDENCE-INDEX.md", "evidence-index.tsv", "events.tsv", "inventory.tsv",
       "pages/DOC_p001.txt", "FINDINGS.md", "drafts/d1.md", "NEXT-RESPONSE.md")


def run(path, *extra):
    out = io.StringIO()
    with redirect_stdout(out):
        code = status.main([str(path), "--now", str(NOW), *extra])
    return code, out.getvalue().splitlines()


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.dir = self.root / "sheriff"
        self.dir.mkdir()

    def make(self, *names, age=3600 * 5):
        for name in names:
            p = self.dir / name
            p.parent.mkdir(exist_ok=True)
            p.write_text("x")
            os.utime(p, (NOW - age, NOW - age))

    def states(self, lines):
        return {l[:20].strip(): l[21:29].strip() for l in lines[1:-1]}


class StatusTests(Base):
    def test_sheriff_shaped_folder_reports_every_stage(self):
        self.make(*ALL)
        code, lines = run(self.dir)
        self.assertEqual(code, 0)
        self.assertEqual(lines[0], "agency run: sheriff")
        self.assertEqual(len(lines), 8)
        for line in lines[1:7]:
            self.assertIn(" present  5h", line)
        self.assertEqual(lines[-1], "next: none, all stages present")

    def test_empty_folder_next_is_evidence_index(self):
        code, lines = run(self.dir)
        self.assertEqual(code, 0)
        self.assertEqual(set(self.states(lines).values()), {"missing"})
        self.assertEqual(lines[-1], "next: evidence index")

    def test_pages_without_events_names_events(self):
        self.make("EVIDENCE-INDEX.md", "evidence-index.tsv", "inventory.tsv", "pages/DOC_p001.txt")
        _, lines = run(self.dir)
        self.assertEqual(self.states(lines)["events"], "missing")
        self.assertEqual(lines[-1], "next: events")

    def test_pages_without_evidence_index_names_evidence_index(self):
        self.make("inventory.tsv", "pages/DOC_p001.txt")
        _, lines = run(self.dir)
        self.assertEqual(lines[-1], "next: evidence index")

    def test_partial_stage_is_not_present(self):
        self.make("EVIDENCE-INDEX.md", "inventory.tsv")
        _, lines = run(self.dir)
        st = self.states(lines)
        self.assertEqual(st["evidence index"], "partial")
        self.assertEqual(st["inventory and pages"], "partial")
        self.assertEqual(lines[-1], "next: evidence index")

    def test_empty_drafts_dir_and_empty_pages_dir_are_missing(self):
        (self.dir / "drafts").mkdir()
        (self.dir / "pages").mkdir()
        (self.dir / "pages" / "notes.txt").write_text("x")
        _, lines = run(self.dir)
        st = self.states(lines)
        self.assertEqual(st["drafts"], "missing")
        self.assertEqual(st["inventory and pages"], "missing")

    def test_symlinked_stage_file_is_missing(self):
        real = self.root / "real.tsv"
        real.write_text("x")
        (self.dir / "events.tsv").symlink_to(real)
        _, lines = run(self.dir)
        self.assertEqual(self.states(lines)["events"], "missing")

    def test_lines_under_100_characters(self):
        long = self.root / ("a" * 200)
        long.mkdir()
        _, lines = run(long)
        self.assertTrue(all(len(l) < 100 for l in lines))
        self.assertTrue(lines[0].endswith("..."))

    def test_age_text_boundaries(self):
        got = [status.age_text(s) for s in (59, 60, 59 * 60, 3600, 47 * 3600, 48 * 3600, -5)]
        self.assertEqual(got, ["now", "1m", "59m", "1h", "47h", "2d", "now"])

    def test_read_only_and_refusals(self):
        self.make(*ALL)

        def snap():
            return sorted((str(p.relative_to(self.dir)), p.stat().st_mtime,
                           hashlib.sha256(p.read_bytes()).hexdigest())
                          for p in self.dir.rglob("*") if p.is_file())
        before = snap()
        run(self.dir)
        self.assertEqual(before, snap())
        (self.root / "file").write_text("x")
        (self.root / "link").symlink_to(self.dir)
        for bad in (self.root / "nope", self.root / "file", self.root / "link"):
            code, lines = run(bad)
            self.assertEqual((code, lines), (2, ["refused agency_status_dir"]))

    def test_cli_registered(self):
        self.assertEqual(cli.COMMANDS["agency-status"], ("agency_run.status", "main"))


if __name__ == "__main__":
    unittest.main()
