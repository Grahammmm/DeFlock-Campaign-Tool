"""RF0210-5: ``build --budget`` counts img/source ``srcset`` candidates, deduplicated by normalized path.

Synthetic content only; no network (``urllib.request.urlopen`` and socket connections are blocked).
"""
import contextlib
import io
import json
import shutil
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import cli, site as site_module
from campaign_tool.site_check import check_links, page_weights
from tests.test_site_budget import EXAMPLE, WITHIN, run_main

DOC = "<!doctype html><html lang=\"en\"><head><title>t</title></head><body>{}</body></html>"


def tree_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(Path(root).rglob("*")) if p.is_file()}


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-srcset-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.public = self.tmp / "public"
        self.public.mkdir()
        for patcher in (mock.patch("urllib.request.urlopen", side_effect=AssertionError("network access attempted")),
                        mock.patch.object(socket.socket, "connect", side_effect=AssertionError("socket connection attempted"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def asset(self, name, size):
        path = self.public / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)

    def page(self, name, body):
        path = self.public / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DOC.format(body), encoding="utf-8")
        return path.stat().st_size

    def row(self, page="index.html"):
        return next(row for row in page_weights(self.public) if row["page"] == page)

    def budget(self, kb, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.weight_budget(self.public, kb, **kwargs)
        return code, out.getvalue()


class CandidateTests(Base):
    def test_srcset_only_img_and_picture_source_are_counted(self):
        for name, size in (("phone.webp", 10), ("desk.webp", 20), ("x1.png", 30), ("x2.png", 40), ("a.avif", 50),
                           ("b.webp", 60), ("fallback.jpg", 70), ("sole.png", 5)):
            self.asset(name, size)
        html = self.page("index.html",
                         '<img alt="m" srcset="/phone.webp 480w, /desk.webp 960w">'
                         '<img alt="m" srcset="x1.png 1x, x2.png 2x"/>'
                         '<picture><source media="(max-width: 600px)" type="image/avif" srcset="/a.avif">'
                         '<source type="image/webp" srcset="/b.webp 1x"/><img alt="m" src="fallback.jpg"></picture>'
                         '<picture><source srcset="/sole.png" /></picture>')
        row = self.row()
        self.assertEqual(row["assets"], [("a.avif", 50), ("b.webp", 60), ("desk.webp", 20), ("fallback.jpg", 70),
                                         ("phone.webp", 10), ("sole.png", 5), ("x1.png", 30), ("x2.png", 40)])
        self.assertEqual((row["html_bytes"], row["total"]), (html, html + 285))

    def test_script_srcset_is_not_an_asset_and_src_plus_srcset_union(self):
        for name in ("a.png", "b.png", "s.js", "ignored.png"):
            self.asset(name, 10)
        self.page("index.html", '<img alt="m" src="a.png" srcset="a.png 1x, b.png 2x"><script src="s.js" srcset="ignored.png"></script>'
                  '<a href="ignored.png">link</a>')
        self.assertEqual([p for p, _ in self.row()["assets"]], ["a.png", "b.png", "s.js"])

    def test_candidates_normalize_and_deduplicate_per_page(self):
        self.asset("img/a b.png", 100)
        self.asset("img/c.png", 200)
        self.asset("hero.webp", 400)
        self.asset("style.css", 8)
        self.page("img/inner.html", '<img alt="m" srcset="a%20b.png 1x, ./a%20b.png?v=2 2x, /img/c.png#frag 3x">')
        html = self.page("index.html",
                         '<div><section><picture>'
                         '<source srcset="/img/a%20b.png 1x, img/../img/a%20b.png?x=1 2x">'
                         '<img alt="m" src="/img/c.png" srcset="img/c.png#f 1x, &#x2F;hero.webp 2x"></picture></section></div>'
                         '<link rel="preload" href="/hero.webp"><link rel="stylesheet" href="style.css">')
        row = self.row()
        self.assertEqual(row["assets"], [("hero.webp", 400), ("img/a b.png", 100), ("img/c.png", 200), ("style.css", 8)])
        self.assertEqual(row["total"], html + 708)
        inner = self.row("img/inner.html")  # shared assets count again on another page; relative to that page
        self.assertEqual(inner["assets"], [("img/a b.png", 100), ("img/c.png", 200)])
        self.assertEqual([r["page"] for r in page_weights(self.public)], ["img/inner.html", "index.html"])
        self.assertEqual(page_weights(self.public), page_weights(self.public))

    def test_data_urls_with_commas_do_not_invent_local_assets(self):
        self.asset("local.png", 11)
        self.asset("after.png", 13)
        self.asset("1x", 99)
        self.asset("b64", 99)
        self.asset("AAAA", 99)
        html = self.page("index.html",
                         '<img alt="m" srcset="data:image/png;base64,AAAA,BBBB 1x, HTTPS://cdn.example.invalid/x.png 2x, '
                         'Http://cdn.example.invalid/y.png 3x, //cdn.example.invalid/z.png 4x, local.png 5x, '
                         'data:image/svg+xml,%3Csvg%3E 6x, after.png 7x">')
        row = self.row()
        self.assertEqual(row["assets"], [("after.png", 13), ("local.png", 11)])
        self.assertEqual(row["total"], html + 24)

    def test_empty_and_tolerated_srcset_do_not_crash(self):
        self.asset("a.png", 10)
        self.asset("b.png", 20)
        self.asset("480w", 99)
        self.asset("big", 99)
        html = self.page("index.html",
                         '<img alt="m" srcset=""><img alt="m" srcset="   "><img alt="m"><source><source srcset>'
                         '<img alt="m" srcset="a.png, ,, b.png 480w,">'
                         '<img alt="m" srcset="a.png big 2x"><img alt="m" srcset="b.png 12x7q">')
        self.assertEqual(self.row()["assets"], [("a.png", 10), ("b.png", 20)])
        self.assertEqual(self.row()["total"], html + 30)
        self.assertEqual(check_links(self.public), [])  # link checking agrees: the same candidates, nothing phantom

    def test_unsafe_candidates_are_skipped_and_links_still_diagnose(self):
        secret = self.tmp / "outside.png"
        secret.write_bytes(b"s" * 5000)
        self.asset("ok.png", 7)
        self.asset("real/target.png", 9)
        (self.public / "linked.png").symlink_to(self.public / "ok.png")
        (self.public / "dirlink").symlink_to(self.public / "real")
        before = tree_bytes(self.public)
        html = self.page("index.html",
                         '<img alt="m" srcset="missing.png 1x, ../outside.png 2x, /../outside.png 3x, '
                         '%2e%2e/outside.png 4x, ok.png%00.png 5x, linked.png 6x, dirlink/target.png 7x, ok.png 8x">')
        row = self.row()
        self.assertEqual(row["assets"], [("ok.png", 7)])
        self.assertEqual(row["total"], html + 7)
        broken = [href for page, href in check_links(self.public) if page == "index.html"]
        for href in ("missing.png", "../outside.png", "linked.png", "dirlink/target.png"):
            self.assertIn(href, broken)
        self.assertEqual(secret.read_bytes(), b"s" * 5000)
        after = tree_bytes(self.public)
        before["index.html"] = after["index.html"]
        self.assertEqual(before, after)


class BudgetTests(Base):
    def test_srcset_budget_boundary_and_largest_assets(self):
        self.asset("small.webp", 1)
        self.asset("big.webp", 1)
        html = self.page("index.html", '<img alt="m" srcset="small.webp 1x, big.webp 2x">')
        self.asset("big.webp", 1024 - html - 1)
        code, text = self.budget(1)
        self.assertEqual(code, 0, text)
        self.assertIn("  index.html 1024 bytes\n", text)
        self.assertIn(WITHIN.format(1), text)
        self.asset("big.webp", 1025 - html - 1)
        code, text = self.budget(1)
        self.assertEqual(code, 3)
        self.assertIn(f"over budget: index.html 1025 bytes > 1 KB; largest: big.webp {1025 - html - 1}, small.webp 1\n", text)
        self.assertNotIn("within", text)

    def test_original_srcset_only_page_no_longer_passes_one_kb(self):
        self.asset("phone.webp", 2048)
        self.asset("desktop.webp", 4096)
        html = self.page("index.html", '<img alt="Synthetic map" srcset="/phone.webp 480w, /desktop.webp 960w">')
        self.assertLess(html, 1024)
        self.assertEqual(check_links(self.public), [])
        self.assertEqual(self.row()["total"], html + 6144)
        code, text = self.budget(1)
        self.assertEqual(code, 3)
        self.assertIn(f"over budget: index.html {html + 6144} bytes > 1 KB; largest: desktop.webp 4096, phone.webp 2048\n", text)

    def test_read_only_measurement_leaves_public_tree_unchanged(self):
        self.asset("a.png", 50)
        self.page("index.html", '<img alt="m" srcset="a.png 1x">')
        before = tree_bytes(self.public)
        self.budget(1)
        self.assertEqual(tree_bytes(self.public), before)


class BuiltSiteTests(Base):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"

    def injected(self, gone_exists):
        """Real build_site, then responsive markup and synthetic assets appended in the test; site.py is untouched."""
        real = site_module.build_site

        def build(root, *args, **kwargs):
            written = real(root, *args, **kwargs)
            page = Path(root) / "public" / "index.html"
            page.write_text(page.read_text(encoding="utf-8").replace(
                "</body>", '<img alt="Synthetic map" srcset="/cand-small.webp 480w, /cand-large.webp 960w, '
                           '/cand-gone.webp 1x"></body>'), encoding="utf-8")
            self.asset("cand-small.webp", 2048)
            self.asset("cand-large.webp", 4096)
            if gone_exists:
                self.asset("cand-gone.webp", 1)
            return written
        return mock.patch.object(site_module, "build_site", build)

    def plain_budget_kb(self):
        code, _, err = run_main("build", "--directory", str(self.root), "--check", "--budget", "100000")
        self.assertEqual(code, 0, err)
        kb = self.row()["total"] // 1024 + 1  # the plain page fits, with under 1 KB of margin
        self.assertEqual(run_main("build", "--directory", str(self.root), "--check", "--budget", str(kb))[0], 0)
        return kb

    def test_check_passes_then_candidate_overage_exits_three(self):
        kb = self.plain_budget_kb()
        plain_assets = dict(self.row()["assets"])
        with self.injected(gone_exists=True):
            code, out, err = run_main("build", "--directory", str(self.root), "--check", "--budget", str(kb))
        self.assertEqual(code, 3, err)
        row = self.row()
        self.assertEqual(dict(row["assets"]), {**plain_assets, "cand-small.webp": 2048, "cand-large.webp": 4096,
                                               "cand-gone.webp": 1})
        self.assertLess(out.index("Public tree check passed"), out.index("over budget: index.html"))
        self.assertIn(f"over budget: index.html {row['total']} bytes > {kb} KB; largest: ", out)
        self.assertIn("cand-large.webp 4096", out)
        self.assertIn(f"  index.html {row['total']} bytes\n", out)

    def test_missing_candidate_fails_check_first_without_weight_table(self):
        kb = self.plain_budget_kb()
        with self.injected(gone_exists=False):
            code, out, err = run_main("build", "--directory", str(self.root), "--check", "--budget", str(kb))
        self.assertEqual(code, 1)  # existing exit; the other candidates are large but the check wins
        self.assertIn("public/index.html: broken link /cand-gone.webp", err)
        self.assertNotIn("Page weight", out)
        self.assertNotIn("over budget", out)

    def test_budget_without_check_counts_candidates(self):
        with self.injected(gone_exists=False):
            code, out, _ = run_main("build", "--directory", str(self.root), "--budget", "1")
        self.assertEqual(code, 3)
        self.assertRegex(out, r"over budget: index\.html \d+ bytes > 1 KB; largest: [^\n]*cand-large\.webp 4096")


class RecordTests(Base):
    def setUp(self):
        super().setUp()
        self.private = self.tmp / "private"
        self.private.mkdir()
        self.asset("phone.webp", 2048)
        self.asset("desktop.webp", 4096)
        self.html = self.page("index.html", '<img alt="m" srcset="/phone.webp 480w, /desktop.webp 960w">')

    def test_weight_record_uses_corrected_assets_and_is_deterministic(self):
        first, second = self.private / "a.json", self.private / "b.json"
        self.assertEqual(self.budget(100, weights_out=first)[0], 0)
        self.assertEqual(self.budget(100, weights_out=second)[0], 0)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        record = json.loads(first.read_text(encoding="utf-8"))
        self.assertEqual(record["schema"], "page-weights-v1")
        self.assertEqual(sorted(record), ["budget_kb", "pages", "schema"])
        self.assertEqual(record["pages"], [{"page": "index.html", "html_bytes": self.html, "total": self.html + 6144,
                                            "assets": [["desktop.webp", 4096], ["phone.webp", 2048]]}])

    def test_legacy_baseline_reports_increase_without_rewrite_or_exit_change(self):
        baseline = self.private / "old.json"
        baseline.write_text(json.dumps({"schema": "page-weights-v1", "budget_kb": 100, "pages": [
            {"page": "index.html", "html_bytes": self.html, "total": self.html, "assets": []}]}), encoding="utf-8")
        before = baseline.read_bytes()
        code, text = self.budget(100, weights_baseline=baseline)
        self.assertEqual(code, 0)  # growth against a baseline is informational; only the budget decides the exit
        self.assertIn(f"  index.html {self.html + 6144} bytes (+6144 since baseline)\n", text)
        self.assertIn(WITHIN.format(100), text)
        self.assertEqual(baseline.read_bytes(), before)
        code, text = self.budget(1, weights_baseline=baseline)
        self.assertEqual(code, 3)
        self.assertIn("(+6144 since baseline)", text)
        self.assertEqual(baseline.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
