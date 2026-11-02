"""DF1822-13: own-line ``![alt](/path)`` images from public-assets on the content site; everything else as main.

Every image is built here from bytes. ``images=None`` (newsletter, CLI) must render exactly as before.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zlib
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from campaign_tool.image_meta import PNG_SIGNATURE, image_size
from campaign_tool.markdown_lite import lint, render
from campaign_tool.newsletter.draft import build_draft
from campaign_tool.site import Site, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
DETAIL = "images render only as their own line: ![alt 1-200 chars](/path-in-public-assets)"


def chunk(kind, body):
    return len(body).to_bytes(4, "big") + kind + body + zlib.crc32(kind + body).to_bytes(4, "big")


def png(width, height):
    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"
    return PNG_SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", b"\x78\x9c\x01\x02") + chunk(b"IEND", b"")


def segment(marker, body):
    return bytes([0xFF, marker]) + (len(body) + 2).to_bytes(2, "big") + body


SCAN = segment(0xDA, b"\x01\x01\x00\x00\x3f\x00") + b"\x12\xff\x00\x34\xff\xd0\x56"


def jpeg(width=0, height=0, sof=True):
    # A DHT (0xC4) comes first: it sits in the SOF range and must not be read as the frame size.
    sof = segment(0xC0, b"\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x01\x01\x11\x00") if sof else b""
    return b"\xff\xd8" + segment(0xC4, b"\x00\x7f\x7f\x7f\x7f" + bytes(16)) + sof + SCAN + b"\xff\xd9"


def riff(kind, body):
    return kind + len(body).to_bytes(4, "little") + body + b"\0" * (len(body) & 1)


def webp(first):
    body = first + riff(b"VP8L", b"\x2f\x00\x00\x00\x00")
    return b"RIFF" + (len(body) + 4).to_bytes(4, "little") + b"WEBP" + body


def vp8x(width, height):
    return riff(b"VP8X", b"\x10" + bytes(3) + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little"))


def vp8l(width, height):
    return riff(b"VP8L", b"\x2f" + ((width - 1) | (height - 1) << 14).to_bytes(4, "little"))


def vp8(width, height):
    return riff(b"VP8 ", b"\x00\x00\x00\x9d\x01\x2a" + width.to_bytes(2, "little") + height.to_bytes(2, "little"))


IMAGES = {"/photos/c.png": (1200, 800), "/photos/d.jpg": (640, 480), "/x.png": (10, 20)}


class ImageSize(unittest.TestCase):
    def test_sizes_by_format(self):
        self.assertEqual(image_size(png(1200, 800), ".png"), (1200, 800))
        self.assertEqual(image_size(jpeg(640, 480), ".JPG"), (640, 480))
        self.assertEqual(image_size(jpeg(7, 9), ".jpeg"), (7, 9))
        self.assertEqual(image_size(webp(vp8x(300, 200)), ".webp"), (300, 200))
        self.assertEqual(image_size(webp(vp8l(33, 44)), ".webp"), (33, 44))
        self.assertEqual(image_size(webp(vp8(320, 240)), ".webp"), (320, 240))

    # 5. malformed, out of bounds or another format: None and no exception
    def test_malformed_is_none(self):
        cases = {
            "png without IHDR": (PNG_SIGNATURE + chunk(b"IDAT", b"\x78") + chunk(b"IEND", b""), ".png"),
            "png 0x0": (png(0, 0), ".png"), "png 30000x10": (png(30000, 10), ".png"),
            "png 10x20001": (png(10, 20001), ".png"), "not a png": (b"\x89PNG", ".png"),
            "jpeg without SOF": (jpeg(sof=False), ".jpg"), "jpeg 0 wide": (jpeg(0, 5), ".jpg"),
            "webp short VP8X": (webp(riff(b"VP8X", bytes(6))), ".webp"),
            "webp VP8 bad start code": (webp(riff(b"VP8 ", bytes(10))), ".webp"),
            "svg": (b"<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'/>", ".svg"),
            "ico": (bytes(22), ".ico"), "pdf": (b"%PDF-1.4", ".pdf"), "empty": (b"", ".png"),
        }
        for name, (data, suffix) in cases.items():
            self.assertIsNone(image_size(data, suffix), name)
        for data, suffix in ((png(5, 5), ".png"), (jpeg(5, 5), ".jpg"), (webp(vp8x(5, 5)), ".webp")):
            for end in range(len(data)):  # every truncation
                self.assertIsNone(image_size(data[:end], suffix), (suffix, end))
        broken = {"/bad.png": image_size(png(30000, 10), ".png"), "/no.jpg": image_size(jpeg(sof=False), ".jpg")}
        sizes = {k: v for k, v in broken.items() if v}
        for line in ("![Wide](/bad.png)", "![No](/no.jpg)"):
            self.assertEqual(render(line, sizes), render(line))
            self.assertEqual(lint(line, sizes), [(1, "image", DETAIL)])


class RenderImages(unittest.TestCase):
    # 1. valid line -> exact img with the file's size; alt and src escaped
    def test_own_line_renders_img(self):
        self.assertEqual(render("![Council chamber, 3 June](/photos/c.png)", IMAGES),
                         '<p><img src="/photos/c.png" alt="Council chamber, 3 June" width="1200" height="800" '
                         'loading="lazy"></p>')
        self.assertEqual(render('  ![ Say "hi" <b> & go ](/photos/d.jpg)  ', IMAGES),
                         '<p><img src="/photos/d.jpg" alt="Say &quot;hi&quot; &lt;b&gt; &amp; go" width="640" '
                         'height="480" loading="lazy"></p>')
        self.assertEqual(lint("![Council](/photos/c.png)", IMAGES), [])

    def test_flushes_paragraph_and_list_first(self):
        source = "Intro **line**\n![A](/x.png)\nAfter\n\n- one\n![B](/x.png)\n1. two\n![C](/x.png)"
        img = '<p><img src="/x.png" alt="{}" width="10" height="20" loading="lazy"></p>'
        self.assertEqual(render(source, IMAGES), "\n".join([
            "<p>Intro <strong>line</strong></p>", img.format("A"), "<p>After</p>", "<ul><li>one</li></ul>",
            img.format("B"), "<ol><li>two</li></ol>", img.format("C")]))

    # 2. images=None (CLI, newsletter) -> main's output
    def test_without_images_main_output(self):
        self.assertEqual(render("![Council](/photos/c.png)"), "<p>!Council</p>")
        self.assertEqual(render("![Council](/photos/c.png)", None), "<p>!Council</p>")
        self.assertEqual(render("![Council](/photos/c.png)", {}), "<p>!Council</p>")
        self.assertEqual(lint("![Council](/photos/missing.jpg)"), [])  # syntax only: the CLI cannot see assets

    def test_newsletter_never_renders_images(self):
        manifest = {"schema_version": 1, "campaign": {
            "name": "Cedar", "consent_footer": "![Council](/photos/c.png) Unsubscribe at any time."}}
        html = build_draft(manifest, now=datetime(2026, 11, 2, tzinfo=timezone.utc))["html"]
        self.assertIn("<p>!Council <a href=", html)
        self.assertNotIn("<img", html)

    # 3. bad alt, external, relative, missing or SVG path -> main's output and one image lint, no link-dropped
    def test_bad_lines_stay_text_and_lint_once(self):
        lines = ["![](/photos/c.png)", "![   ](/photos/c.png)", "![" + "a" * 201 + "](/photos/c.png)",
                 "![a](https://example.org/c.png)", "![a](../x.png)", "![a](/photos/missing.jpg)",
                 "![a](/logo.svg)", "![a](/photos/c.png?v=1)", "![a](/photos/c.png) trailing"]
        for line in lines:
            self.assertEqual(render(line, IMAGES), render(line), line)
            self.assertEqual(lint(line, IMAGES), [(1, "image", DETAIL)], line)
        for line in lines[:3] + lines[4:5]:  # bad syntax is named even without the asset map
            self.assertEqual(lint(line), [(1, "image", DETAIL)], line)
        self.assertEqual(render("![a](/photos/c.png)", {"/photos/c.png.svg": (1, 1)}), "<p>!a</p>")
        self.assertEqual(lint("![a](/photos/c.png)", {"/logo.svg": (1, 1)}), [(1, "image", DETAIL)])

    # 4. inline or in a list item -> main's output + image lint
    def test_inline_and_list_item_stay_text(self):
        for line in ("see ![a](/x.png) here", "- ![a](/x.png)", "1. ![a](/x.png)", "## ![a](/x.png)",
                     "**![a](/x.png)**", "![a](/x.png) ![b](/x.png)"):
            self.assertEqual(render(line, IMAGES), render(line), line)
            self.assertEqual(lint(line, IMAGES), [(1, "image", DETAIL)], line)
        self.assertEqual(lint("Bad [link](http://a.example) and ![a](/x.png)", IMAGES),
                         [(1, "image", DETAIL), (1, "link-dropped", "link target is not absolute https")])
        table = "| a | b |\n|---|---|\n| ![a](/x.png) | c |"
        self.assertEqual(render(table, IMAGES), render(table))
        self.assertEqual(lint(table, IMAGES), [(3, "image", DETAIL)])

    # 7. the CLI names a bad image line and passes a valid-syntax one
    def test_cli_lint(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad, good = Path(tmp) / "bad.md", Path(tmp) / "good.md"
            bad.write_text("Intro\n\nsee ![a](/x.png) here\n", encoding="utf-8")
            good.write_text("Intro\n\n![Council](/photos/c.png)\n", encoding="utf-8")
            run = [sys.executable, "-m", "campaign_tool.markdown_lite", "--lint"]
            out = subprocess.run(run + [str(bad)], cwd=REPO_ROOT, capture_output=True, text=True)
            self.assertEqual(out.returncode, 1, out.stderr)
            self.assertEqual(out.stdout, f"{bad}:3: image: {DETAIL}\nfiles=1 findings=1\n")
            out = subprocess.run(run + [str(good)], cwd=REPO_ROOT, capture_output=True, text=True)
            self.assertEqual((out.returncode, out.stdout), (0, "files=1 findings=0\n"), out.stderr)


class SiteImages(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-site-images-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        photos = self.root / "content" / "public-assets" / "photos"
        photos.mkdir(parents=True)
        (photos / "c.png").write_bytes(png(1200, 800))
        (photos / "wide.png").write_bytes(png(30000, 10))
        (photos / "logo.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
        self.finding = self.root / "content" / "findings" / "cedar-policy-posted.json"

    def tree(self):
        public = self.root / "public"
        return {p.relative_to(public).as_posix(): p.read_bytes() for p in public.rglob("*") if p.is_file()}

    def test_sizes_from_public_assets(self):
        from campaign_tool.cli import read_config
        from campaign_tool.content import load_content
        site = Site(self.root, read_config(self.root), load_content(self.root))
        self.assertEqual(site.image_sizes, {"/photos/c.png": (1200, 800)})

    # 6. no image lines -> public/ byte-identical to rendering without the asset map (main)
    def test_no_image_lines_byte_identical(self):
        build_site(self.root)
        built = self.tree()
        shutil.rmtree(self.root / "public")
        with mock.patch.object(Site, "md", lambda self, text: render(text)):
            build_site(self.root)
        self.assertEqual(sorted(built), sorted(self.tree()))
        for name, data in self.tree().items():
            self.assertTrue(built[name] == data, name)
        self.assertIn("findings/cedar-policy-posted.html", built)
        self.assertIn("data/findings.json", built)
        self.assertIn("feed.xml", built)

    def test_finding_page_renders_img_and_bad_lines_stay_text(self):
        finding = json.loads(self.finding.read_text(encoding="utf-8"))
        finding["body_md"] += "\n\n![Council chamber](/photos/c.png)\n\n![Wide](/photos/wide.png)\n\n![Logo](/photos/logo.svg)"
        self.finding.write_text(json.dumps(finding), encoding="utf-8")
        build_site(self.root)
        page = (self.root / "public" / "findings" / "cedar-policy-posted.html").read_text(encoding="utf-8")
        self.assertIn('<p><img src="/photos/c.png" alt="Council chamber" width="1200" height="800" loading="lazy"></p>',
                      page)
        self.assertEqual(page.count("<img"), 1)
        self.assertIn("<p>!Wide</p>", page)
        self.assertIn("<p>!Logo</p>", page)
        self.assertNotIn("Council chamber", (self.root / "public" / "feed.xml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
