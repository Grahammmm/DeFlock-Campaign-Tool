"""DF1207-13: site.json.share_image -> og:image (+ size and alt) and twitter:card summary_large_image.

Every image is built here from bytes; nothing is fetched. Without the key the build is as main builds it.
"""
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from campaign_tool.content import ContentError
from campaign_tool.content_check import content_problems
from campaign_tool.site import build_site
from tests.test_site_brand import chunk, png, vp8x, webp
from tests.test_site_share_meta import share_tags, values

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "fictional-campaign"
BASE = "https://cedar-records.example.invalid"
ALT = "Cedar County board room with a camera policy slide"
IMAGE_TAG = re.compile(r'^  <meta (?:property|name)="(?:og|twitter):image[^\n]*\n', re.M)


def jpeg(width, height):
    sof = b"\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x01\x01\x11\x00"
    return b"\xff\xd8\xff\xc0" + (len(sof) + 2).to_bytes(2, "big") + sof + b"\xff\xd9"


class ShareImage(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-share-image-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.assets = self.root / "content" / "public-assets"
        self.assets.mkdir()
        (self.assets / "share.png").write_bytes(png(1200, 630))
        self.site_path = self.root / "content" / "site.json"
        self.site = json.loads(self.site_path.read_text(encoding="utf-8"))
        self.site.pop("base_url", None)

    def build(self, share=None, base=None):
        document = dict(self.site)
        if base:
            document["base_url"] = base
        if share is not None:
            document["share_image"] = share
        self.site_path.write_text(json.dumps(document), encoding="utf-8")
        if (self.root / "public").exists():
            shutil.rmtree(self.root / "public")
        build_site(self.root)
        public = self.root / "public"
        return {p.relative_to(public).as_posix(): p.read_bytes() for p in public.rglob("*") if p.is_file()}

    def refused(self, share, field):
        document = dict(self.site, share_image=share)
        self.site_path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(ContentError) as caught:
            build_site(self.root)
        message = str(caught.exception)
        self.assertTrue(message.startswith(f"site.json.share_image.{field}: "), message)
        self.assertIn(message, content_problems(self.root))
        return message

    def pages(self, built):
        return [n for n in built if n.endswith(".html") and n != "404.html"]

    # 1. with base_url: absolute og:image, size, alt, large card, twitter:image
    def test_png_with_base_url(self):
        built = self.build({"path": "share.png", "alt": ALT}, BASE)
        self.assertGreater(len(self.pages(built)), 5)
        for name in self.pages(built):
            tags = share_tags(built[name].decode("utf-8"))
            self.assertEqual(values(tags, "og:image"), [BASE + "/share.png"], name)
            self.assertEqual(values(tags, "og:image:width"), ["1200"], name)
            self.assertEqual(values(tags, "og:image:height"), ["630"], name)
            self.assertEqual(values(tags, "og:image:alt"), [ALT], name)
            self.assertEqual(values(tags, "twitter:card"), ["summary_large_image"], name)
            self.assertEqual(values(tags, "twitter:image"), [BASE + "/share.png"], name)
            self.assertEqual(values(tags, "twitter:image:alt"), [ALT], name)
        self.assertEqual(built["share.png"], png(1200, 630))

    # 2. without base_url: root-relative og:image, no twitter:image, card still large
    def test_png_without_base_url_and_jpeg(self):
        (self.assets / "art").mkdir()
        (self.assets / "art" / "wide.jpeg").write_bytes(jpeg(2400, 1260))
        built = self.build({"path": "art/wide.jpeg", "alt": ALT})
        for name in self.pages(built):
            tags = share_tags(built[name].decode("utf-8"))
            self.assertEqual(values(tags, "og:image"), ["/art/wide.jpeg"], name)
            self.assertEqual(values(tags, "og:image:width"), ["2400"], name)
            self.assertEqual(values(tags, "og:image:height"), ["1260"], name)
            self.assertEqual(values(tags, "twitter:card"), ["summary_large_image"], name)
            self.assertEqual(values(tags, "twitter:image"), [], name)

    # 3. wrong size, ratio, type, missing file, traversal -> share_image.path
    def test_path_refusals(self):
        files = {"small.png": png(1199, 630), "big.png": png(2401, 1261), "square.png": png(1200, 1200),
                 "short.png": png(1200, 629), "tall.jpg": jpeg(1200, 700), "fake.webp": webp(vp8x(1200, 630)),
                 "art.svg": b'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630"></svg>'}
        for name, data in files.items():
            (self.assets / name).write_bytes(data)
        for name in (*files, "missing.png", "../site.json", "a/../share.png", "/share.png"):
            with self.subTest(name):
                self.refused({"path": name, "alt": ALT}, "path")
        self.refused({"alt": ALT}, "path")
        self.refused({"path": 5, "alt": ALT}, "path")

    # 4. alt required, one line, 1-300
    def test_alt_refusals(self):
        for alt in (None, "", "   ", "two\nlines", "two\rlines", "x" * 301, 7):
            with self.subTest(alt=alt):
                self.refused({"path": "share.png", **({} if alt is None else {"alt": alt})}, "alt")
        self.build({"path": "share.png", "alt": "x" * 300})  # the limit itself is accepted

    # 5. image metadata -> the existing metadata refusal, naming the field
    def test_metadata_refused(self):
        (self.assets / "exif.png").write_bytes(png(1200, 630, chunk(b"eXIf", b"II*\0\x08\0\0\0\0\0")))
        self.assertIn("image metadata (EXIF)", self.refused({"path": "exif.png", "alt": ALT}, "path"))

    # 6. alt with a quote and < is escaped in every tag
    def test_alt_escaped(self):
        alt = 'A "quoted" <b>sign</b> & more'
        built = self.build({"path": "share.png", "alt": alt}, BASE)
        for name in self.pages(built):
            html = built[name].decode("utf-8")
            self.assertNotIn("<b>sign", html, name)
            self.assertEqual(html.count("A &quot;quoted&quot; &lt;b&gt;sign&lt;/b&gt; &amp; more"), 2, name)
            self.assertEqual(values(share_tags(html), "og:image:alt"), [alt], name)

    # 7. the 404 page carries no image or share tags
    def test_404_has_no_image_tags(self):
        built = self.build({"path": "share.png", "alt": ALT}, BASE)
        html = built["404.html"].decode("utf-8")
        self.assertEqual(share_tags(html), [])
        self.assertNotIn("share.png", html)

    # 8. without the key every file is the same as the image build minus the image lines (card back to summary)
    def test_no_share_image_changes_nothing_else(self):
        plain = self.build(None, BASE)
        for name in self.pages(plain):
            tags = share_tags(plain[name].decode("utf-8"))
            self.assertEqual(values(tags, "twitter:card"), ["summary"], name)
            self.assertFalse(any("image" in key for _, key, _ in tags), name)
        with_image = self.build({"path": "share.png", "alt": ALT}, BASE)
        self.assertEqual(set(with_image), set(plain))
        for name, data in plain.items():
            if name.endswith(".html") and name != "404.html":
                rebuilt = IMAGE_TAG.sub("", with_image[name].decode("utf-8")).replace(
                    '"twitter:card" content="summary_large_image"', '"twitter:card" content="summary"')
                self.assertTrue(rebuilt == data.decode("utf-8"), name)
            elif name in with_image and name != "sitemap.xml":
                self.assertTrue(with_image[name] == data, name)
        self.assertEqual(self.build(None, BASE), plain)  # deterministic


if __name__ == "__main__":
    unittest.main()
