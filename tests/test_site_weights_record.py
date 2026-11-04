"""RF1939-3: ``build --budget KB --weights-out FILE / --weights-baseline FILE``.

A private ``page-weights-v1`` record of the per-page first-party weights, and per-page growth against a saved one.
Synthetic content only; no network (``urllib.request.urlopen`` is patched to raise).
"""
import contextlib
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from campaign_tool import cli
from campaign_tool.site_check import page_weights
from tests.test_site_budget import EXAMPLE, WITHIN, run_main


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-weights-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.private = self.tmp / "private"
        self.private.mkdir()

    def build(self, *extra, budget="100000"):
        return run_main("build", "--directory", str(self.root), "--budget", budget, *extra)


class RecordTests(Base):
    def test_options_need_budget_and_nothing_is_built(self):
        for flag in ("--weights-out", "--weights-baseline"):
            code, _, err = run_main("build", "--directory", str(self.root), flag, str(self.private / "w.json"))
            self.assertEqual(code, 2, flag)
            self.assertIn("need --budget", err)
            self.assertFalse(self.public.exists(), flag)
            self.assertEqual(list(self.private.iterdir()), [])

    def test_record_is_private_exact_and_never_overwritten(self):
        out = self.private / "w.json"
        code, text, err = self.build("--weights-out", str(out))
        self.assertEqual(code, 0, err)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertIn(WITHIN.format(100000), text)
        record = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(sorted(record), ["budget_kb", "pages", "schema"])
        self.assertEqual((record["schema"], record["budget_kb"]), ("page-weights-v1", 100000))
        rows = page_weights(self.public)
        self.assertEqual([p["page"] for p in record["pages"]], [r["page"] for r in rows])
        for item, row in zip(record["pages"], rows):
            self.assertEqual(item, {"page": row["page"], "html_bytes": row["html_bytes"], "total": row["total"],
                                    "assets": [list(a) for a in row["assets"]]})
        before = out.read_bytes()
        code, _, err = self.build("--weights-out", str(out))
        self.assertEqual(code, 1)
        self.assertIn("exists", err)
        self.assertEqual(out.read_bytes(), before)

    def test_refused_targets_leave_no_file(self):
        self.build()  # writes public/
        (self.private / "link.json").symlink_to(self.private / "missing.json")
        targets = [self.public / "w.json", self.public / "sub" / "w.json", self.root / "content" / "w.json",
                   self.private / "nodir" / "w.json", self.private / "link.json"]
        for target in targets:
            code, _, err = self.build("--weights-out", str(target))
            self.assertEqual(code, 1, target)
            self.assertIn("--weights-out", err)
            self.assertFalse(target.is_file(), target)
        self.assertFalse((self.public / "w.json").exists())
        self.assertFalse((self.root / "content" / "w.json").exists())
        self.assertFalse((self.private / "missing.json").exists())

    def test_equal_builds_give_byte_identical_records(self):
        first, second = self.private / "a.json", self.private / "b.json"
        self.assertEqual(self.build("--weights-out", str(first))[0], 0)
        self.assertEqual(self.build("--weights-out", str(second))[0], 0)
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_over_budget_still_writes_the_record_and_exits_three(self):
        out = self.private / "w.json"
        code, text, _ = self.build("--weights-out", str(out), budget="1")
        self.assertEqual(code, 3)
        self.assertIn("over budget: index.html", text)
        self.assertEqual(json.loads(out.read_text())["budget_kb"], 1)

    def test_baseline_round_trip_reports_no_change(self):
        first, second = self.private / "a.json", self.private / "b.json"
        self.build("--weights-out", str(first))
        code, text, err = self.build("--weights-baseline", str(first), "--weights-out", str(second))
        self.assertEqual(code, 0, err)
        lines = [line for line in text.split("Page weight (first-party", 1)[1].splitlines()[1:] if line.startswith("  ")]
        self.assertGreater(len(lines), 3)
        self.assertTrue(all(line.endswith(" bytes (±0 since baseline)") for line in lines), lines)
        self.assertEqual(first.read_bytes(), second.read_bytes())


class BaselineTests(Base):
    def setUp(self):
        super().setUp()
        self.public.mkdir()
        self.page("same.html", "aa")
        self.page("grew.html", "bb")
        self.page("shrank.html", "cc")
        self.page("fresh.html", "dd")
        self.sizes = {row["page"]: row["total"] for row in page_weights(self.public)}

    def page(self, name, body):
        (self.public / name).write_text(f"<!doctype html><html><body>{body}</body></html>")

    def baseline(self, **totals):
        path = self.private / "base.json"
        pages = [{"page": name, "html_bytes": 0, "total": total, "assets": []} for name, total in sorted(totals.items())]
        path.write_text(json.dumps({"schema": "page-weights-v1", "budget_kb": 1, "pages": pages}))
        return path

    def budget(self, budget, base, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.weight_budget(self.public, budget, weights_baseline=base, **kwargs)
        return code, out.getvalue()

    def test_four_line_kinds_exact(self):
        s = self.sizes
        base = self.baseline(**{"same.html": s["same.html"], "grew.html": s["grew.html"] - 1,
                                "shrank.html": s["shrank.html"] + 7, "gone.html": 99, "also-gone.html": 5})
        self.public.joinpath("fresh.html").rename(self.public / "fresh2.html")
        s = {row["page"]: row["total"] for row in page_weights(self.public)}
        code, text = self.budget(100, base)
        self.assertEqual(code, 0)
        self.assertEqual(text.splitlines(), [
            "Page weight (first-party: html + referenced site-local css/js/images, uncompressed):",
            f"  fresh2.html {s['fresh2.html']} bytes (new page)",
            f"  grew.html {s['grew.html']} bytes (+1 since baseline)",
            f"  same.html {s['same.html']} bytes (±0 since baseline)",
            f"  shrank.html {s['shrank.html']} bytes (-7 since baseline)",
            "removed since baseline: also-gone.html",
            "removed since baseline: gone.html",
            WITHIN.format(100)])

    def test_baseline_never_changes_the_exit_code(self):
        s = self.sizes
        base = self.baseline(**{name: total - 5 for name, total in s.items()})
        self.page("big.html", "y" * 3000)
        code, text = self.budget(1, base)
        self.assertEqual(code, 3)
        self.assertIn("(+5 since baseline)", text)
        self.assertIn("over budget: big.html", text)
        self.assertIn("  big.html", text)
        code, text = self.budget(100, base)  # growth while under budget
        self.assertEqual(code, 0)
        self.assertNotIn("over budget", text)

    def test_without_baseline_lines_are_the_plain_table(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.weight_budget(self.public, 100), 0)
        self.assertIn(f"  same.html {self.sizes['same.html']} bytes\n", out.getvalue())
        self.assertNotIn("baseline", out.getvalue())

    def test_unusable_baselines_are_named_refusals_before_the_table(self):
        good = self.baseline(**{"same.html": 1})
        link = self.private / "link.json"
        link.symlink_to(good)
        big = self.private / "big.json"
        big.write_text(json.dumps({"schema": "page-weights-v1", "pages": [], "pad": "x" * (2 * 1024 * 1024)}))
        fifo = self.private / "fifo.json"
        os.mkfifo(fifo)
        bad = {"notjson.json": "not json {", "wrongschema.json": json.dumps({"schema": "page-weights-v2", "pages": []}),
               "list.json": "[]", "nopages.json": json.dumps({"schema": "page-weights-v1"}),
               "badtotal.json": json.dumps({"schema": "page-weights-v1", "pages": [{"page": "a.html", "total": "9"}]}),
               "booltotal.json": json.dumps({"schema": "page-weights-v1", "pages": [{"page": "a.html", "total": True}]}),
               "dup.json": json.dumps({"schema": "page-weights-v1", "pages": [{"page": "a", "total": 1}, {"page": "a", "total": 2}]}),
               "ctrl.json": json.dumps({"schema": "page-weights-v1", "pages": [{"page": "a\x1b[2J", "total": 1}]})}
        for name, body in bad.items():
            (self.private / name).write_text(body)
        names = [link.name, big.name, fifo.name, "absent.json", *bad]
        for name in names:
            out = self.tmp / "out.json"
            code, text, err = self.run_cli(self.private / name, out)
            self.assertEqual(code, 1, name)
            self.assertIn(f"--weights-baseline {self.private / name}", err)
            self.assertNotIn("Page weight", text, name)
            self.assertFalse(out.exists(), name)
        code, text, err = self.run_cli(good, None)
        self.assertEqual(code, 0, err)
        self.assertIn("(new page)", text)

    def run_cli(self, baseline, out):
        # Rebuild into a fresh campaign so the CLI path is exercised end to end.
        shutil.rmtree(self.public, ignore_errors=True)
        extra = ["--weights-out", str(out)] if out else []
        return run_main("build", "--directory", str(self.root), "--budget", "100000",
                        "--weights-baseline", str(baseline), *extra)


if __name__ == "__main__":
    unittest.main()
