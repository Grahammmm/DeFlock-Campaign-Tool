"""DF0237-08: unlinked public assets are named by build, content-check and build --check. Synthetic data only; no network."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from campaign_tool.content import ContentError, load_site
from campaign_tool.content_check import content_problems
from campaign_tool.public_asset_links import unlinked_asset_paths, validate_unlinked_assets_ok
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
BASE = "https://cedar.example.invalid"
LINE = "public-assets/{}: published but not linked from any page"


def page(*tags):
    return "<html><head></head><body>" + "".join(tags) + "</body></html>"


class CollectorTests(unittest.TestCase):
    def unlinked(self, files, assets, **kw):
        return unlinked_asset_paths(files, assets, **kw)

    def test_reference_forms_and_origins(self):
        assets = ["a.pdf", "docs/b.pdf", "c.png", "d.png", "e.png", "f.png", "g.png", "h.png", "docs/x/y.png", "Case.png",
                  "p/q.png", "r.png", "s.png", "t.png", "u.png", "v.png"]
        files = {
            "index.html": page('<a href="/a.pdf?x=1#frag">a</a>', '<img src="c.png">', '<img srcset="d.png 1x, /e.png 2x">',
                               '<meta property="og:image" content="/f.png">', '<meta name="twitter:image" content="/g.png">',
                               '<a href="/Case.PNG">x</a>', '<a href="/Docs/b.pdf">x</a>'),
            "findings/one.html": page('<a href="../docs/b.pdf">b</a>', '<img src="&#47;h.png">', '<a href="../docs/x/y.png">y</a>'),
            "deep/er/page.html": page(f'<a href="{BASE}/p/q.png">q</a>', f'<a href="{BASE}:443/r.png">r</a>'),
        }
        self.assertEqual(self.unlinked(files, assets, base_url=BASE), ["Case.png", "s.png", "t.png", "u.png", "v.png"])
        # no base_url: absolute URLs never establish membership
        self.assertIn("p/q.png", self.unlinked(files, assets))
        self.assertIn("r.png", self.unlinked(files, assets))

    def test_rejected_urls_never_match(self):
        assets = ["a.png", "b.png", "docs/c.png", "d.png"]
        hostile = ['//cedar.example.invalid/a.png', 'http://cedar.example.invalid/a.png', 'https://cedar.example.invalid.evil.test/a.png',
                   'https://user:pw@cedar.example.invalid/a.png',  # pragma: allowlist secret
                   'https://cedar.example.invalid:8443/a.png', '/%61.png',
                   '/docs%2Fc.png', '..\\a.png', '../../a.png', 'data:image/png;base64,AAAA', 'javascript:a.png', 'mailto:a.png',
                   'https://other.example.invalid/b.png', '/docs/../d.png/', '//a.png']
        files = {"index.html": page(*(f'<a href="{url}">x</a>' for url in hostile))}
        self.assertEqual(self.unlinked(files, assets, base_url=BASE + "/site"), sorted(assets))
        # a sibling prefix is not the base path; the real prefix is
        files = {"index.html": page(f'<a href="{BASE}/sitemap/a.png">x</a>', f'<a href="{BASE}/site/b.png">y</a>')}
        self.assertEqual(self.unlinked(files, assets, base_url=BASE + "/site"), ["a.png", "d.png", "docs/c.png"])

    def test_each_public_surface_and_literals(self):
        assets = ["logo.png", "icon.ico", "share.png", "src.pdf", "redir.pdf", "text.pdf", "feed.pdf", "zip.pdf", "csv.pdf"]
        files = {"index.html": page("<p>text.pdf and /text.pdf</p>", '<script type="application/json">"/feed.pdf"</script>'),
                 "feed.xml": '<a href="/feed.pdf">f</a>', "data/sources.zip": b"\x00\x01/zip.pdf\xff", "data/manifest.csv": "csv.pdf\r\n"}
        found = self.unlinked(files, assets, base_url=BASE, public_urls=[f"{BASE}/src.pdf#p=2", "http://other.invalid/redir.pdf"],
                              declared_assets=["logo.png", "icon.ico", "share.png"], redirect_targets=["/redir.pdf", "//x.invalid/y"])
        self.assertEqual(found, ["csv.pdf", "feed.pdf", "text.pdf", "zip.pdf"])
        self.assertEqual(self.unlinked({}, ["b.pdf", "a.pdf", "a.pdf"]), ["a.pdf", "b.pdf"])


class ValidateTests(unittest.TestCase):
    def test_allowlist_shape_existence_and_safety(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-unlinked-") as tmp:
            assets = Path(tmp)
            (assets / "docs").mkdir()
            (assets / "docs" / "h.pdf").write_bytes(b"x")
            (assets / "a.pdf").write_bytes(b"x")
            (assets / "public-assets").mkdir()  # a real nested directory must not make the forbidden prefix acceptable
            (assets / "public-assets" / "a.pdf").write_bytes(b"x")
            (assets / "link").symlink_to(assets / "docs")
            (assets / "dangling.pdf").symlink_to(assets / "nope")
            self.assertEqual(validate_unlinked_assets_ok(["docs/h.pdf"], assets), ["docs/h.pdf"])
            self.assertEqual(validate_unlinked_assets_ok(["a.pdf"]), ["a.pdf"])  # syntax only without assets_dir
            names = [f"f{n}.pdf" for n in range(50)]
            self.assertEqual(validate_unlinked_assets_ok(names), names)
            bad = [[], names + ["g.pdf"], None, True, "a.pdf", 7, ["a.pdf", "a.pdf"], ["/a.pdf"], ["public-assets/a.pdf"], ["a.pdf?x"],
                   ["a.pdf#x"], ["../a.pdf"], ["./a.pdf"], ["docs\\h.pdf"], ["a%2e.pdf"], [""], ["docs//h.pdf"], [3], [" a.pdf"],
                   ["missing.pdf"], ["docs"], ["link/h.pdf"], ["dangling.pdf"]]
            for value in bad:
                with self.assertRaises(ContentError, msg=repr(value)) as raised:
                    validate_unlinked_assets_ok(value, assets)
                self.assertTrue(str(raised.exception).startswith("site.json.unlinked_assets_ok"), value)
            with self.assertRaises(ContentError) as raised:
                validate_unlinked_assets_ok(["a.pdf", "docs/h.pdf", "secret-name.pdf"], assets)
            self.assertIn("unlinked_assets_ok[2]:", str(raised.exception))
            self.assertNotIn("secret-name", str(raised.exception))
            document = json.loads((EXAMPLE / "content" / "site.json").read_text(encoding="utf-8"))
            document["unlinked_assets_ok"] = ["a.pdf"]
            self.assertEqual(load_site(document, assets)["unlinked_assets_ok"], ["a.pdf"])
            document["unlinked_assets_ok"] = None
            with self.assertRaises(ContentError):
                load_site(document, assets)


class CampaignTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-unlinked-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.assets = self.root / "content" / "public-assets"
        (self.assets / "docs").mkdir(parents=True)
        self.site = self.root / "content" / "site.json"
        self.document = json.loads(self.site.read_text(encoding="utf-8"))

    def write_site(self, **changes):
        self.document.update(changes)
        self.site.write_text(json.dumps(self.document), encoding="utf-8")

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args, "--directory", str(self.root)],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=300)

    def tree(self):
        return {p.relative_to(self.root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in sorted(self.root.rglob("*")) if p.is_file()}

    def two_assets(self):
        (self.assets / "docs" / "zeta.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        (self.assets / "alpha.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")

    def test_build_note_check_and_content_check_agree(self):
        self.two_assets()
        lines = [LINE.format("alpha.pdf"), LINE.format("docs/zeta.pdf")]
        self.assertEqual(content_problems(self.root), lines)
        result = self.cli("content-check")
        self.assertEqual((result.returncode, result.stdout.splitlines()[:1]), (3, ["content/: 2 problem(s)"]))
        self.assertEqual(result.stdout.splitlines()[-2:], lines)
        records = json.loads(self.cli("content-check", "--json").stdout)
        self.assertEqual([(r["file"], r["message"]) for r in records["problems"]],
                         [("public-assets/alpha.pdf", SUFFIX_TEXT), ("public-assets/docs/zeta.pdf", SUFFIX_TEXT)])
        built = self.cli("build")  # ordinary build: success, every asset copied, one sorted note per file
        self.assertEqual(built.returncode, 0, built.stderr)
        self.assertEqual([l for l in built.stderr.splitlines() if "not linked" in l], ["note: " + l for l in lines])
        for name in ("alpha.pdf", "docs/zeta.pdf"):
            self.assertEqual((self.root / "public" / name).read_bytes(), (self.assets / name).read_bytes())
        before = self.tree()
        checked = self.cli("build", "--check")
        self.assertEqual(checked.returncode, 1, checked.stdout)
        self.assertEqual([l.removeprefix("Stopped: ") for l in checked.stderr.splitlines() if "not linked" in l], lines)
        self.assertNotIn("passed", checked.stdout)
        self.assertNotIn("written to", checked.stdout)
        self.assertEqual(self.tree(), before)  # the failed check wrote nothing, public/ untouched

    def test_check_first_build_writes_nothing(self):
        self.two_assets()
        checked = self.cli("build", "--check")
        self.assertEqual(checked.returncode, 1)
        self.assertFalse((self.root / "public").exists())
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".public")], [])

    def test_link_or_acknowledgement_clears_and_stale_acknowledgement_refuses(self):
        self.two_assets()
        mission = self.document["mission_md"]
        self.write_site(mission_md=mission + f"\n\nRead the [handout]({self.document['base_url']}/docs/zeta.pdf).")
        self.assertEqual(content_problems(self.root), [LINE.format("alpha.pdf")])
        self.write_site(unlinked_assets_ok=["alpha.pdf", "docs/zeta.pdf"])  # a linked entry stays valid
        self.assertEqual(content_problems(self.root), [])
        built = self.cli("build", "--check")
        self.assertEqual(built.returncode, 0, built.stderr)
        self.assertEqual(self.cli("build").stderr.count("not linked"), 0)
        (self.assets / "alpha.pdf").unlink()  # asset removed, acknowledgement kept
        problems = content_problems(self.root)
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("site.json: site.json.unlinked_assets_ok[0]") or "unlinked_assets_ok[0]" in problems[0], problems)

    def test_source_public_url_counts_as_a_link(self):
        self.two_assets()
        self.write_site(unlinked_assets_ok=["alpha.pdf"])
        sources = json.loads((self.root / "content" / "sources.json").read_text(encoding="utf-8"))
        rows = sources if isinstance(sources, list) else sources.get("sources", sources)
        self.assertTrue(rows)
        rows[0]["public_url"] = f"{self.document['base_url']}/docs/zeta.pdf"
        (self.root / "content" / "sources.json").write_text(json.dumps(sources), encoding="utf-8")
        self.assertEqual(content_problems(self.root), [])

    def test_preview_unchanged_and_nothing_written(self):
        self.two_assets()
        before = self.tree()
        result = self.cli("build", "--preview")
        self.assertNotIn("not linked", result.stdout + result.stderr)
        self.assertEqual(self.tree(), before)

    def test_draft_only_reference_is_unlinked(self):
        self.two_assets()
        kit = self.root / "kit"
        kit.mkdir(exist_ok=True)
        (kit / "draft.md").write_text("[handout](/docs/zeta.pdf) alpha.pdf", encoding="utf-8")
        self.assertEqual(content_problems(self.root), [LINE.format("alpha.pdf"), LINE.format("docs/zeta.pdf")])


SUFFIX_TEXT = "published but not linked from any page"

if __name__ == "__main__":
    unittest.main()
