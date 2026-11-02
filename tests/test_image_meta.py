"""DF1822-12: public-assets images carrying metadata are refused by build and content-check; strip-metadata cleans.

Every image is built here from bytes (no Pillow, no fixtures). The GPS value bytes must never be printed.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

from campaign_tool import image_meta
from campaign_tool.content import ContentError
from campaign_tool.content_check import content_problems
from campaign_tool.image_meta import UNREADABLE, metadata_kinds, strip
from campaign_tool.site import ASSET_MAX_BYTES, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
GPS_VALUE = b"35.28275N120.65962W"


def segment(marker, body):
    return bytes([0xFF, marker]) + (len(body) + 2).to_bytes(2, "big") + body


def tiff(gps=True):
    """Big-endian TIFF: IFD0 with one entry (GPS pointer 0x8825 or Make 0x010F), then the 'value' bytes."""
    tag = 0x8825 if gps else 0x010F
    entry = tag.to_bytes(2, "big") + (4).to_bytes(2, "big") + (1).to_bytes(4, "big") + (26).to_bytes(4, "big")
    return b"MM\0*" + (8).to_bytes(4, "big") + (1).to_bytes(2, "big") + entry + (0).to_bytes(4, "big") + GPS_VALUE


JFIF = segment(0xE0, b"JFIF\0\x01\x01\0\0\x01\0\x01\0\0")
ICC = segment(0xE2, b"ICC_PROFILE\0\x01\x01icc-bytes")
JPEG_META = {"exif": segment(0xE1, b"Exif\0\0" + tiff()), "xmp": segment(0xE1, b"http://ns.adobe.com/xap/1.0/\0<x/>"),
             "iptc": segment(0xED, b"Photoshop 3.0\0iptc"), "com": segment(0xFE, b"taken at home")}
SCAN = segment(0xDA, b"\x01\x01\x00\x00\x3f\x00") + b"\x12\xff\x00\x34\xff\xd0\x56\xff\xff"


def jpeg(*metadata):
    return b"\xff\xd8" + JFIF + b"".join(metadata) + ICC + segment(0xDB, b"\x00" + bytes(64)) + SCAN + b"\xff\xd9"


def chunk(kind, body):
    return len(body).to_bytes(4, "big") + kind + body + zlib.crc32(kind + body).to_bytes(4, "big")


IHDR, IDAT, IEND = chunk(b"IHDR", bytes(13)), chunk(b"IDAT", b"\x78\x9c\x01\x02"), chunk(b"IEND", b"")


def png(*metadata, tail=b""):
    return image_meta.PNG_SIGNATURE + IHDR + b"".join(metadata) + IDAT + IEND + tail


def riff(kind, body):
    return kind + len(body).to_bytes(4, "little") + body + b"\0" * (len(body) & 1)


def webp(*metadata, flags=0x10):
    body = riff(b"VP8X", bytes([flags]) + bytes(9)) + riff(b"VP8L", b"\x2f\x00\x00\x00\x00") + b"".join(metadata)
    return b"RIFF" + (len(body) + 4).to_bytes(4, "little") + b"WEBP" + body


def png_chunks_valid(data):
    pos, names = 8, []
    while pos < len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        kind, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + length]
        if zlib.crc32(kind + body).to_bytes(4, "big") != data[pos + 8 + length:pos + 12 + length]:
            return None
        names.append(kind)
        pos += 12 + length
    return names


class Campaign(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-image-meta-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.assets = self.root / "content" / "public-assets"
        self.photo = self.assets / "photos" / "council.jpg"
        self.photo.parent.mkdir(parents=True)

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                              capture_output=True, timeout=120)

    def tree(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file()}


class MetadataKindsTests(unittest.TestCase):
    def test_kinds_by_format(self):
        self.assertEqual(metadata_kinds(jpeg(JPEG_META["exif"]), ".jpg"), ["EXIF GPS"])
        no_gps = segment(0xE1, b"Exif\0\0" + tiff(gps=False))
        self.assertEqual(metadata_kinds(jpeg(no_gps, *list(JPEG_META.values())[1:]), ".JPEG"),
                         ["EXIF", "IPTC", "XMP", "comment"])
        text, exif = chunk(b"tEXt", b"Comment\0home"), chunk(b"eXIf", tiff())
        self.assertEqual(metadata_kinds(png(text, exif), ".png"), ["EXIF GPS", "text"])
        xmp, ztxt = chunk(b"iTXt", b"XML:com.adobe.xmp\0\0\0\0\0<x/>"), chunk(b"zTXt", b"Author\0\0x")
        self.assertEqual(metadata_kinds(png(xmp, ztxt), ".png"), ["XMP", "text"])
        self.assertEqual(metadata_kinds(png(tail=b"after IEND"), ".png"), ["trailing data"])
        both = webp(riff(b"EXIF", b"Exif\0\0" + tiff(gps=False)), riff(b"XMP ", b"<x/>"), flags=0x1C)
        self.assertEqual(metadata_kinds(both, ".webp"), ["EXIF", "XMP"])
        for clean, suffix in ((jpeg(), ".jpg"), (png(), ".png"), (webp(), ".webp"), (b"<svg/>", ".svg")):
            self.assertEqual(metadata_kinds(clean, suffix), [], suffix)

    # 5. truncated or malformed structures are "unreadable image"; no exception escapes
    def test_unreadable_structures(self):
        filler = chunk(b"zzZz", b"")
        cases = {
            "jpeg without EOI": (jpeg()[:-2], ".jpg"), "jpeg segment past end": (b"\xff\xd8\xff\xe0\x00\x40JFIF", ".jpg"),
            "png chunk length past end": (png()[:-12] + (99).to_bytes(4, "big") + b"IEND\0\0\0\0", ".png"),
            "png 10 001 chunks": (image_meta.PNG_SIGNATURE + IHDR + filler * 9999 + IEND, ".png"),
            "png without IEND": (png()[:-12], ".png"), "not a png": (b"\x89PNG", ".png"),
            "webp riff size past end": (webp()[:-1], ".webp"), "jpeg exif bad tiff": (jpeg(segment(0xE1, b"Exif\0\0XX")), ".jpg"),
            "too large": (b"\xff\xd8" + bytes(ASSET_MAX_BYTES), ".jpg"),
        }
        for name, (data, suffix) in cases.items():
            self.assertEqual(metadata_kinds(data, suffix), [UNREADABLE], name)
            with self.assertRaisesRegex(ValueError, "^unreadable image$"):
                strip(data, suffix)
        self.assertEqual(metadata_kinds(image_meta.PNG_SIGNATURE + IHDR + filler * 9998 + IEND, ".png"), [])
        for data, suffix in ((jpeg(*JPEG_META.values()), ".jpg"), (png(chunk(b"eXIf", tiff())), ".png"),
                             (webp(riff(b"EXIF", tiff())), ".webp")):
            for cut in range(len(data)):  # every truncation answers with a list, never an exception
                self.assertIsInstance(metadata_kinds(data[:cut], suffix), list)

    # 4. strip keeps every non-metadata byte, PNG CRCs, WebP RIFF size and flags; idempotent
    def test_strip_keeps_image_bytes(self):
        dirty = jpeg(*JPEG_META.values()) + b"trailer"
        self.assertEqual(strip(dirty, ".jpg"), jpeg())
        dirty_png = png(chunk(b"tEXt", b"k\0v"), chunk(b"eXIf", tiff()), chunk(b"iTXt", b"XML:com.adobe.xmp\0\0\0\0\0x"),
                        tail=b"junk")
        cleaned_png = strip(dirty_png, ".png")
        self.assertEqual(cleaned_png, png())
        self.assertEqual(png_chunks_valid(cleaned_png), [b"IHDR", b"IDAT", b"IEND"])
        dirty_webp = webp(riff(b"EXIF", tiff()), riff(b"XMP ", b"<x/>"), flags=0x1C)
        cleaned_webp = strip(dirty_webp, ".webp")
        self.assertEqual(cleaned_webp, webp(flags=0x10))
        self.assertEqual(int.from_bytes(cleaned_webp[4:8], "little"), len(cleaned_webp) - 8)
        self.assertEqual(cleaned_webp[20] & 0x0C, 0)
        for data, suffix in ((dirty, ".jpg"), (dirty_png, ".png"), (dirty_webp, ".webp")):
            once = strip(data, suffix)
            self.assertEqual(strip(once, suffix), once)
            self.assertEqual(metadata_kinds(once, suffix), [])
            self.assertNotIn(GPS_VALUE, once)
        with self.assertRaises(ValueError):
            strip(b"<svg/>", ".svg")

    def test_limit_matches_build(self):
        self.assertEqual(image_meta.MAX_BYTES, ASSET_MAX_BYTES)


class BuildGateTests(Campaign):
    # 1. JPEG with EXIF GPS: build refused naming EXIF GPS, the value never shown; public/ not written
    def test_gps_jpeg_refused_value_never_shown(self):
        self.photo.write_bytes(jpeg(JPEG_META["exif"]))
        with self.assertRaises(ContentError) as caught:
            build_site(self.root)
        self.assertEqual(str(caught.exception),
                         "public-assets/photos/council.jpg: image metadata (EXIF GPS); run content strip-metadata")
        self.assertFalse((self.root / "public").exists())
        result = self.cli("build", "--directory", str(self.root))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"image metadata (EXIF GPS)", result.stderr)
        self.assertNotIn(GPS_VALUE, result.stdout + result.stderr)
        self.assertFalse((self.root / "public").exists())

    # 2. PNG tEXt + eXIf and WebP EXIF + XMP with VP8X flags: refused naming both kinds
    def test_png_and_webp_refused_with_both_kinds(self):
        (self.assets / "a.png").write_bytes(png(chunk(b"tEXt", b"Author\0me"), chunk(b"eXIf", tiff(gps=False))))
        with self.assertRaisesRegex(ContentError, r"^public-assets/a\.png: image metadata \(EXIF, text\); "):
            build_site(self.root)
        (self.assets / "a.png").unlink()
        (self.assets / "b.webp").write_bytes(webp(riff(b"EXIF", tiff()), riff(b"XMP ", b"<x/>"), flags=0x1C))
        with self.assertRaisesRegex(ContentError, r"^public-assets/b\.webp: image metadata \(EXIF GPS, XMP\); "):
            build_site(self.root)
        (self.assets / "b.webp").write_bytes(webp()[:-1])
        with self.assertRaisesRegex(ContentError, r"image metadata \(unreadable image\)"):
            build_site(self.root)
        self.assertFalse((self.root / "public").exists())

    # 3. clean JPEG/PNG/WebP (and an out-of-scope SVG) are copied byte-identical
    def test_clean_images_copied_byte_identical(self):
        files = {"photos/council.jpg": jpeg(), "logo.png": png(), "hero.webp": webp(), "icon.svg": b"<svg>exif</svg>"}
        for name, data in files.items():
            (self.assets / name).write_bytes(data)
        build_site(self.root)
        for name, data in files.items():
            self.assertEqual((self.root / "public" / name).read_bytes(), data, name)


class ContentCheckTests(Campaign):
    # 7. content-check lists the image problem beside the other problems, exits 3 and writes nothing
    def test_listed_with_other_problems_exit_3(self):
        self.photo.write_bytes(jpeg(JPEG_META["exif"]))
        (self.assets / "ok.png").write_bytes(png())
        (self.root / "content" / "site.json").write_text("{", encoding="utf-8")
        problems = content_problems(self.root)
        self.assertIn("public-assets/photos/council.jpg: image metadata (EXIF GPS); run content strip-metadata",
                      problems)
        self.assertTrue(any(line.startswith("site.json") for line in problems), problems)
        self.assertFalse(any("ok.png" in line for line in problems))
        before = self.tree()
        result = self.cli("content-check", "--directory", str(self.root))
        self.assertEqual(result.returncode, 3)
        self.assertIn(b"public-assets/photos/council.jpg: image metadata (EXIF GPS)", result.stdout)
        self.assertNotIn(GPS_VALUE, result.stdout + result.stderr)
        self.assertEqual(self.tree(), before)

    def test_clean_campaign_has_no_problem(self):
        self.photo.write_bytes(jpeg())
        self.assertEqual(content_problems(self.root), [])


class StripMetadataCliTests(Campaign):
    def test_writes_clean_copy_beside_original(self):
        dirty = jpeg(JPEG_META["exif"], JPEG_META["com"])
        self.photo.write_bytes(dirty)
        result = self.cli("content", "strip-metadata", str(self.photo))
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.photo.with_name("council.clean.jpg")
        self.assertEqual(json.loads(result.stdout), {"removed": ["EXIF GPS", "comment"], "written": str(target)})
        self.assertNotIn(GPS_VALUE, result.stdout)
        self.assertEqual(target.read_bytes(), jpeg())
        self.assertEqual(self.photo.read_bytes(), dirty)  # the organizer replaces the original by hand
        self.photo.write_bytes(target.read_bytes())
        target.unlink()
        build_site(self.root)
        self.assertEqual((self.root / "public" / "photos" / "council.jpg").read_bytes(), jpeg())

    # 6. symlink source, existing target, dangling-symlink target, --out inside public/, unreadable: nothing written
    def test_refusals_write_nothing(self):
        self.photo.write_bytes(jpeg(JPEG_META["exif"]))
        link = self.photo.with_name("link.jpg")
        link.symlink_to(self.photo)
        existing = self.photo.with_name("council.clean.jpg")
        existing.write_bytes(b"organizer file")
        dangling = self.root / "dangling.jpg"
        dangling.symlink_to(self.root / "nowhere.jpg")
        (self.root / "public").mkdir()
        broken = self.photo.with_name("broken.jpg")
        broken.write_bytes(jpeg()[:-2])
        cases = [((str(link), "--out", str(self.root / "l.jpg")), "is a symlink"),
                 ((str(self.photo),), "already exists"),
                 ((str(self.photo), "--out", str(dangling)), "already exists"),
                 ((str(self.photo), "--out", str(self.root / "public" / "x.jpg")), "inside a public/ folder"),
                 ((str(self.photo), "--out", "public/x.jpg"), "inside a public/ folder"),
                 ((str(broken), "--out", str(self.root / "b.jpg")), "unreadable image")]
        before = self.tree()
        for args, reason in cases:
            result = self.cli("content", "strip-metadata", *args)
            self.assertEqual(result.returncode, 1, args)
            self.assertIn(reason.encode(), result.stderr, args)
            self.assertNotIn(GPS_VALUE, result.stdout + result.stderr)
        self.assertEqual(self.tree(), before)
        self.assertEqual(existing.read_bytes(), b"organizer file")
        self.assertFalse((self.root / "nowhere.jpg").exists() or (REPO_ROOT / "public" / "x.jpg").exists())


if __name__ == "__main__":
    unittest.main()
