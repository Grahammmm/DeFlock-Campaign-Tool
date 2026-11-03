"""DF1737-03: ``build --budget KB`` per-page first-party weight; over-budget pages named, exit 3.

Synthetic content only; no network (``urllib.request.urlopen`` is patched to raise).
"""
import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import cli
from campaign_tool.site_check import page_weights

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
NO_NETWORK = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network access attempted"))
WITHIN = "Page weight within {} KB (first-party, uncompressed)."


def run_main(*argv):
    """Run ``campaign ARGV``; returns (exit code, stdout, stderr). A normal return is exit 0."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with NO_NETWORK, mock.patch.object(sys, "argv", ["campaign", *argv]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as stop:
            code = stop.code
    return code, out.getvalue(), err.getvalue()


class HandWrittenTreeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-budget-")
        self.addCleanup(temporary.cleanup)
        self.public = Path(temporary.name) / "public"
        (self.public / "data").mkdir(parents=True)
        for name, size in (("style.css", 100), ("app.js", 300), ("data/map.geojson", 50), ("logo.png", 40),
                           ("pic.webp", 20), ("font.woff2", 10), ("mod.js", 5), ("favicon.ico", 7)):
            (self.public / name).write_bytes(b"x" * size)

    def write(self, name, body):
        path = self.public / name
        path.write_text(f"<!doctype html><html><head><title>t</title></head><body>{body}</body></html>")
        return path.stat().st_size

    def weights(self):
        with NO_NETWORK:
            return {row["page"]: row for row in page_weights(self.public)}

    def test_every_asset_kind_counted_once_per_page_and_on_each_page(self):
        html = self.write("index.html", '<link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="style.css?v=2">'
                          '<script src="/app.js"></script><script src="./app.js#x"></script><img src="/logo.png">'
                          '<picture><source src="pic.webp"></picture><link rel="preload" href="/font.woff2">'
                          '<link rel="modulepreload" href="/mod.js"><link rel="icon" href="/favicon.ico">'
                          '<a href="/data/map.geojson">raw</a>')
        other = self.write("about.html", '<link rel="Preload Stylesheet" href="/style.css"><img src="data/map.geojson">')
        rows = self.weights()
        self.assertEqual(sorted(rows), ["about.html", "index.html"])
        self.assertEqual(rows["index.html"]["assets"], [("app.js", 300), ("font.woff2", 10), ("logo.png", 40),
                                                        ("mod.js", 5), ("pic.webp", 20), ("style.css", 100)])
        self.assertEqual(rows["index.html"]["html_bytes"], html)
        self.assertEqual(rows["index.html"]["total"], html + 475)
        self.assertEqual(rows["about.html"]["assets"], [("data/map.geojson", 50), ("style.css", 100)])
        self.assertEqual(rows["about.html"]["total"], other + 150)

    def test_external_references_never_counted_or_fetched(self):
        html = self.write("index.html", '<script src="https://cdn.example.invalid/app.js"></script>'
                          '<script src="//cdn.example.invalid/x.js"></script><img src="HTTPS://tiles.example.invalid/1.png">'
                          '<img src="data:image/png;base64,AAAA"><link rel="stylesheet" href="//fonts.example.invalid/a.css">')
        self.assertEqual(self.weights()["index.html"], {"page": "index.html", "html_bytes": html, "assets": [],
                                                        "total": html})

    def test_missing_escaping_and_symlinked_targets_skipped_without_crash(self):
        (self.public / "linked.js").symlink_to(self.public / "app.js")
        html = self.write("index.html", '<script src="/missing.js"></script><img src="../../outside.png">'
                          '<script src="/linked.js"></script><img src=""><script></script><img src="/style.css">')
        self.assertEqual(self.weights()["index.html"]["assets"], [("style.css", 100)])
        self.assertEqual(self.weights()["index.html"]["total"], html + 100)

    def test_exactly_at_budget_is_within_and_one_byte_over_is_not(self):
        html = self.write("index.html", '<link rel="stylesheet" href="/style.css">')
        (self.public / "style.css").write_bytes(b"x" * (1024 - html))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.weight_budget(self.public, 1), 0)
        self.assertIn("  index.html 1024 bytes\n", out.getvalue())
        self.assertIn(WITHIN.format(1), out.getvalue())
        (self.public / "style.css").write_bytes(b"x" * (1025 - html))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.weight_budget(self.public, 1), 3)
        self.assertIn(f"over budget: index.html 1025 bytes > 1 KB; largest: style.css {1025 - html}\n", out.getvalue())
        self.assertNotIn("within", out.getvalue())

    def test_top_three_largest_by_size_and_page_without_assets(self):
        (self.public / "app.js").write_bytes(b"x" * 2000)
        self.write("index.html", '<script src="/mod.js"></script><img src="/logo.png"><script src="/app.js"></script>'
                   '<link rel="stylesheet" href="/style.css">')
        self.write("bare.html", "")
        self.write("big.html", "y" * 2000)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.weight_budget(self.public, 1), 3)
        text = out.getvalue()
        self.assertIn("; largest: app.js 2000, style.css 100, logo.png 40\n", text)
        self.assertNotIn("mod.js", text)
        self.assertNotIn("over budget: bare.html", text)  # its html alone is far below 1 KB
        self.assertRegex(text, r"over budget: big\.html \d+ bytes > 1 KB; largest: \(no assets\)\n")


class BuiltSiteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-budget-build-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"

    def pages(self):
        return sorted(p.relative_to(self.public).as_posix() for p in self.public.rglob("*.html"))

    def test_large_budget_lists_every_page_and_exits_zero(self):
        code, out, err = run_main("build", "--directory", str(self.root), "--budget", "100000")
        self.assertEqual(code, 0, err)
        report = out.split("Page weight (first-party", 1)[1].splitlines()[1:]
        listed = [line.split()[0] for line in report if line.startswith("  ")]
        self.assertEqual(listed, self.pages())
        self.assertGreater(len(listed), 3)
        self.assertIn(WITHIN.format(100000), out)
        self.assertNotIn("over budget", out)
        index = next(row for row in page_weights(self.public) if row["page"] == "index.html")
        self.assertIn("style.css", dict(index["assets"]))
        self.assertIn(f"  index.html {index['total']} bytes\n", out)

    def test_budget_one_names_every_page_with_top_three_matching_stat(self):
        code, out, _ = run_main("build", "--directory", str(self.root), "--budget", "1")
        self.assertEqual(code, 3)
        self.assertNotIn("within", out)
        over = {line.split()[2]: line for line in out.splitlines() if line.startswith("over budget: ")}
        self.assertEqual(sorted(over), self.pages())
        for page, line in over.items():
            total = int(line.split()[3])
            self.assertIn(f" bytes > 1 KB; largest: ", line)
            named = line.split("; largest: ", 1)[1].split(", ")
            self.assertLessEqual(len(named), 3)
            sizes = [int(item.rsplit(" ", 1)[1]) for item in named]
            self.assertEqual(sizes, sorted(sizes, reverse=True))
            for item in named:
                path, size = item.rsplit(" ", 1)
                self.assertEqual(int(size), (self.public / path).stat().st_size, line)
            self.assertGreater(total, 1024)
        self.assertIn("style.css", over["index.html"])

    def test_check_failure_keeps_exit_one_and_skips_budget(self):
        real = cli.check_public_tree

        def add_missing(public):
            path = public / "about.html"
            path.write_text(path.read_text(encoding="utf-8").replace("</body>", '<script src="/gone.js"></script></body>'))
            return real(public)

        with mock.patch.object(cli, "check_public_tree", add_missing):
            code, out, err = run_main("build", "--directory", str(self.root), "--check", "--budget", "1")
        self.assertEqual(code, 1)
        self.assertIn("public/about.html: broken link /gone.js", err)
        self.assertNotIn("over budget", out)
        self.assertNotIn("Page weight", out)
        self.assertIn("about.html", [row["page"] for row in page_weights(self.public)])  # missing asset: no crash

    def test_check_pass_then_budget_evaluated(self):
        code, out, err = run_main("build", "--directory", str(self.root), "--check", "--budget", "100000")
        self.assertEqual(code, 0, err)
        self.assertLess(out.index("Public tree check passed"), out.index(WITHIN.format(100000)))

    def test_invalid_budget_is_argparse_error_and_nothing_built(self):
        for value in ("0", "-5", "abc", "1.5", ""):
            code, out, err = run_main("build", "--directory", str(self.root), "--budget", value)
            self.assertEqual(code, 2, value)
            self.assertIn("--budget", err)
            self.assertFalse(self.public.exists(), value)
        code, _, err = run_main("build", "--directory", str(self.root), "--preview", "--budget", "5")
        self.assertEqual(code, 2)
        self.assertIn("cannot be combined with --preview", err)
        self.assertFalse(self.public.exists())

    def test_without_budget_no_weight_report(self):
        code, out, err = run_main("build", "--directory", str(self.root))
        self.assertEqual(code, 0, err)
        self.assertNotIn("Page weight", out)

    def test_starter_weight_is_index_plus_stylesheet(self):
        starter = self.root.parent / "starter"
        code, _, err = run_main("init", "--directory", str(starter), "--name", "Synthetic Campaign",
                                "--county", "Synthetic County", "--state", "ca")
        self.assertEqual(code, 0, err)
        code, out, err = run_main("build", "--directory", str(starter), "--budget", "500")
        self.assertEqual(code, 0, err)
        public = starter / "public"
        expected = (public / "index.html").stat().st_size + (public / "style.css").stat().st_size
        self.assertEqual([row["page"] for row in page_weights(public)], ["index.html"])
        self.assertIn(f"  index.html {expected} bytes\n", out)
        self.assertIn(WITHIN.format(500), out)


if __name__ == "__main__":
    unittest.main()
