"""DF2037-17: site.json brand (logo, accent/ink colours with a WCAG contrast check, favicon).

Every image is built here from bytes. Without ``brand`` every built file stays as main builds it.
"""
import json
import os
import shutil
import tempfile
import unittest
import zlib
from pathlib import Path

from campaign_tool.content import NO_SVG, TEXT_BACKGROUNDS, ContentError, contrast, load_brand
from campaign_tool.content_check import content_problems
from campaign_tool.image_meta import PNG_SIGNATURE
from campaign_tool.site import EXTRA_CSS, build_site

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "fictional-campaign"
SECRET = "private-name-77"  # a file-name value that must never appear in a refusal
LOGO_RULE = ".brand-logo{max-height:48px;width:auto;height:auto}\n"


def chunk(kind, body):
    return len(body).to_bytes(4, "big") + kind + body + zlib.crc32(kind + body).to_bytes(4, "big")


def png(width, height, extra=b""):
    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"
    return PNG_SIGNATURE + chunk(b"IHDR", ihdr) + extra + chunk(b"IDAT", b"\x78\x9c\x01\x02") + chunk(b"IEND", b"")


def riff(kind, body):
    return kind + len(body).to_bytes(4, "little") + body + b"\0" * (len(body) & 1)


def webp(body):
    return b"RIFF" + (len(body) + 4).to_bytes(4, "little") + b"WEBP" + body


def vp8x(width, height):
    return riff(b"VP8X", b"\x10" + bytes(3) + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little"))


TEXT_PNG = png(120, 40, chunk(b"tEXt", b"Comment\0" + SECRET.encode()))
EXIF_PNG = png(120, 40, chunk(b"eXIf", b"II*\0\x08\0\0\0\0\0"))
TRUNCATED_PNG = PNG_SIGNATURE + chunk(b"IDAT", b"\x78") + chunk(b"IEND", b"")
BAD_WEBP = webp(b"VP8X" + (1000).to_bytes(4, "little") + bytes(10))


class BrandSite(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-site-brand-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.assets = self.root / "content" / "public-assets"
        self.assets.mkdir()
        (self.assets / "logo.png").write_bytes(png(120, 40))
        (self.assets / "favicon.ico").write_bytes(bytes(22))
        self.site_path = self.root / "content" / "site.json"
        self.site = json.loads(self.site_path.read_text(encoding="utf-8"))

    def write_brand(self, brand):
        document = dict(self.site, brand=brand) if brand is not None else self.site
        self.site_path.write_text(json.dumps(document), encoding="utf-8")

    def tree(self):
        public = self.root / "public"
        return {p.relative_to(public).as_posix(): p.read_bytes() for p in public.rglob("*") if p.is_file()}

    def build(self, brand):
        self.write_brand(brand)
        if (self.root / "public").exists():
            shutil.rmtree(self.root / "public")
        build_site(self.root)
        return self.tree()

    def refused(self, brand, field):
        """Build refuses naming ``field``, writes nothing, and content-check lists the same line."""
        self.write_brand(brand)
        with self.assertRaises(ContentError) as caught:
            build_site(self.root)
        message = str(caught.exception)
        self.assertTrue(message.startswith(f"site.json.brand{field}: "), message)
        self.assertNotIn(SECRET, message)
        self.assertFalse((self.root / "public").exists())
        self.assertIn(message, content_problems(self.root))
        return message

    # 1. no brand -> every built file as main (no brand markup, CSS exactly the shell CSS + EXTRA_CSS)
    def test_no_brand_byte_identical(self):
        built = self.build(None)
        self.assertEqual(self.build({}), built)  # an empty brand adds nothing either
        for name, data in built.items():
            self.assertNotIn(b"brand-logo", data, name)
            self.assertNotIn(b'rel="icon"', data, name)
        self.assertTrue(built["style.css"].decode("utf-8").endswith(EXTRA_CSS))
        self.assertNotIn(b"--coral:#", built["style.css"].split(EXTRA_CSS.encode())[-1])
        self.assertIn(b'<a class="brand" href="/">Cedar County Records Project</a>', built["index.html"])
        self.assertEqual(content_problems(self.root), [])

    # 2. PNG logo 120x40 + alt -> img with the file's size on every page; favicon link on every page
    def test_logo_and_favicon_on_every_page(self):
        built = self.build({"logo": "logo.png", "logo_alt": 'Cedar "Watch" & co', "favicon": "favicon.ico"})
        img = ('<a class="brand" href="/"><img class="brand-logo" src="/logo.png" alt="Cedar &quot;Watch&quot; '
               '&amp; co" width="120" height="40">Cedar County Records Project</a>')
        pages = [name for name in built if name.endswith(".html")]
        self.assertGreater(len(pages), 5)
        for name in pages:
            self.assertIn(img.encode(), built[name], name)
            self.assertIn(b'<link rel="icon" href="/favicon.ico">\n', built[name], name)
        self.assertEqual(built["logo.png"], png(120, 40))
        self.assertTrue(built["style.css"].decode().endswith(EXTRA_CSS + LOGO_RULE))
        webp_logo = self.assets / "art" / "mark.webp"
        webp_logo.parent.mkdir()
        webp_logo.write_bytes(webp(vp8x(300, 100)))
        built = self.build({"logo": "art/mark.webp", "logo_alt": "Mark"})
        self.assertIn(b'src="/art/mark.webp" alt="Mark" width="300" height="100">', built["about.html"])
        self.assertNotIn(b'rel="icon"', built["about.html"])

    # 3. missing, symlinked, outside, wrong suffix, image metadata -> refused by name, value never printed
    def test_logo_file_refusals(self):
        (self.root / "content" / f"{SECRET}.png").write_bytes(png(120, 40))
        os.symlink(self.assets / "logo.png", self.assets / f"{SECRET}.png")
        (self.assets / "dir").mkdir()
        os.symlink(self.assets / "dir", self.assets / "linked")
        (self.assets / "dir" / "logo.png").write_bytes(png(120, 40))
        (self.assets / f"{SECRET}-text.png").write_bytes(TEXT_PNG)
        (self.assets / f"{SECRET}-exif.png").write_bytes(EXIF_PNG)
        (self.assets / f"{SECRET}.jpg").write_bytes(b"\xff\xd8\xff\xd9")
        cases = {f"missing-{SECRET}.png": "file not found", f"{SECRET}.png": "symlink",
                 "linked/logo.png": "symlink", f"../{SECRET}.png": "plain file name", f"/{SECRET}.png": "plain file name",
                 f"dir//{SECRET}.png": "plain file name", f"{SECRET}.jpg": "use a .png or .webp file",
                 f"{SECRET}.gif": "use a .png or .webp file", f"{SECRET}-text.png": "image metadata (text)",
                 f"{SECRET}-exif.png": "image metadata (EXIF)", 7: "plain file name"}
        for value, reason in cases.items():
            with self.subTest(value=value):
                message = self.refused({"logo": value, "logo_alt": "Logo"}, ".logo")
                self.assertIn(reason, message)

    # 4. logo_alt required with logo, 1-120 plain characters; alt without logo refused
    def test_logo_alt(self):
        for brand in ({"logo": "logo.png"}, {"logo": "logo.png", "logo_alt": "a" * 121},
                      {"logo": "logo.png", "logo_alt": "   "}, {"logo": "logo.png", "logo_alt": "<b>Logo</b>"},
                      {"logo": "logo.png", "logo_alt": 5}):
            with self.subTest(brand=brand):
                self.assertIn("required with logo, 1-120 characters", self.refused(brand, ".logo_alt"))
        self.assertIn("set only together with logo", self.refused({"logo_alt": "Logo"}, ".logo_alt"))
        built = self.build({"logo": "logo.png", "logo_alt": "a" * 120})
        self.assertIn(f'alt="{"a" * 120}" width="120"'.encode(), built["index.html"])

    # 5. bad hex refused; upper case stored lowercase; unknown key and non-object refused
    def test_hex_colours(self):
        for value in ("#fff", "b83d2c", "#GGGGGG", "#b83d2c ", 0xB83D2C, None):
            for key in ("accent", "ink"):
                with self.subTest(key=key, value=value):
                    self.assertEqual(self.refused({key: value}, f".{key}"), f"site.json.brand.{key}: use a #rrggbb hex colour")
        self.assertEqual(load_brand({"accent": "#B83D2C", "ink": "#092B2D"}), {"accent": "#b83d2c", "ink": "#092b2d"})
        self.assertEqual(self.refused({"logo_url": "x"}, ""), "site.json.brand: unknown field(s) ['logo_url']")
        self.assertEqual(self.refused(["#b83d2c"], ""), "site.json.brand: expected an object")

    # 6. contrast below 4.5:1 refused with pair and ratio; the stylesheet defaults pass
    def test_contrast(self):
        self.assertEqual(round(contrast("#000000", "#ffffff"), 2), 21.0)  # WCAG reference values
        self.assertEqual(round(contrast("#777777", "#ffffff"), 2), 4.48)
        ratio = contrast("#ff9999", TEXT_BACKGROUNDS["cream"])
        self.assertLess(ratio, 4.5)
        self.assertEqual(self.refused({"accent": "#ff9999"}, ".accent"),
                         f"site.json.brand.accent: contrast {int(ratio * 100) / 100:.2f}:1 against cream (#fff8ec); "
                         "needs 4.5:1")
        on_cream, on_paper = contrast("#6e6e6e", "#fff8ec"), contrast("#6e6e6e", "#f3eee4")
        self.assertGreaterEqual(on_cream, 4.5)  # passes cream, fails paper: both pairs are checked
        self.assertLess(on_paper, 4.5)
        self.assertEqual(self.refused({"ink": "#6E6E6E"}, ".ink"),
                         f"site.json.brand.ink: contrast {int(on_paper * 100) / 100:.2f}:1 against paper (#f3eee4); "
                         "needs 4.5:1")
        self.assertIn("against cream", self.refused({"ink": "#ffffff"}, ".ink"))
        for colour, pairs in (("#092b2d", ("cream", "paper")), ("#b83d2c", ("cream",))):
            for name in pairs:
                print(f"\n{colour} on {name}: {contrast(colour, TEXT_BACKGROUNDS[name]):.2f}:1")
                self.assertGreaterEqual(contrast(colour, TEXT_BACKGROUNDS[name]), 4.5)
        css = self.build({"accent": "#b83d2c", "ink": "#092B2D"})["style.css"].decode()
        self.assertTrue(css.endswith(EXTRA_CSS + ":root{--coral:#b83d2c;--ink:#092b2d}\n"))

    # 7. truncated PNG, bad WebP chunk, SVG with or without size, over 2000 px -> refused
    def test_unreadable_svg_and_oversize(self):
        (self.assets / "edge.png").write_bytes(png(2000, 1))  # built first: the bad files below stop any build
        self.assertIn(b'width="2000" height="1"', self.build({"logo": "edge.png", "logo_alt": "Edge"})["index.html"])
        shutil.rmtree(self.root / "public")
        (self.assets / "trunc.png").write_bytes(TRUNCATED_PNG)
        (self.assets / "bad.webp").write_bytes(BAD_WEBP)
        (self.assets / "wide.png").write_bytes(png(4000, 40))
        (self.assets / "tall.webp").write_bytes(webp(vp8x(10, 2001)))
        (self.assets / "sized.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'/>")
        (self.assets / "bare.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
        for name in ("trunc.png", "bad.webp"):
            self.assertIn("unreadable image", self.refused({"logo": name, "logo_alt": "Logo"}, ".logo"))
        self.assertEqual(self.refused({"logo": "wide.png", "logo_alt": "Logo"}, ".logo"),
                         "site.json.brand.logo: 4000x40 px; each side must be 1-2000 px")
        self.assertIn("10x2001 px", self.refused({"logo": "tall.webp", "logo_alt": "Logo"}, ".logo"))
        for name in ("sized.svg", "bare.svg"):
            self.assertEqual(self.refused({"logo": name, "logo_alt": "Logo"}, ".logo"), "site.json.brand.logo: " + NO_SVG)
            self.assertEqual(self.refused({"favicon": name}, ".favicon"), "site.json.brand.favicon: " + NO_SVG)

    # 3/7 for the favicon: missing, wrong suffix, metadata, unreadable
    def test_favicon_refusals(self):
        self.assertIn(b'<link rel="icon" href="/logo.png">', self.build({"favicon": "logo.png"})["404.html"])
        shutil.rmtree(self.root / "public")
        (self.assets / f"{SECRET}-text.png").write_bytes(TEXT_PNG)
        (self.assets / "trunc.png").write_bytes(TRUNCATED_PNG)
        for value, reason in ((f"{SECRET}.ico", "file not found"), (f"{SECRET}.gif", "use a .ico or .png file"),
                              ("logo.webp", "use a .ico or .png file"), (f"{SECRET}-text.png", "image metadata (text)"),
                              ("trunc.png", "unreadable image")):
            with self.subTest(value=value):
                self.assertIn(reason, self.refused({"favicon": value}, ".favicon"))

    # 8. only accent -> the override carries --coral only (and only ink -> --ink only)
    def test_only_set_keys_override(self):
        css = self.build({"accent": "#B83D2C"})["style.css"].decode()
        self.assertTrue(css.endswith(EXTRA_CSS + ":root{--coral:#b83d2c}\n"))
        css = self.build({"ink": "#000000"})["style.css"].decode()
        self.assertTrue(css.endswith(EXTRA_CSS + ":root{--ink:#000000}\n"))

    # 9. content-check lists the brand refusal beside other problems; meeting packs check all but the files
    def test_content_check_parity(self):
        (self.root / "content" / "findings" / "broken.json").write_text("{}", encoding="utf-8")
        message = self.refused_check({"accent": "#ff9999"})
        self.assertTrue(message.startswith("site.json.brand.accent: contrast "))
        self.assertEqual(load_brand({"logo": "absent.png", "logo_alt": "Logo"}),
                         {"logo": "absent.png", "logo_alt": "Logo", "logo_size": None})
        with self.assertRaises(ContentError):
            load_brand({"logo": "absent.svg", "logo_alt": "Logo"})

    def refused_check(self, brand):
        self.write_brand(brand)
        with self.assertRaises(ContentError) as caught:
            build_site(self.root)
        problems = content_problems(self.root)
        self.assertIn(str(caught.exception), problems)
        self.assertTrue(any(line.startswith("findings/broken.json") for line in problems), problems)
        return str(caught.exception)


if __name__ == "__main__":
    unittest.main()
