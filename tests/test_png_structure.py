"""RF1410-12: PNG container integrity (chunk CRCs, chunk order) before scanning, stripping or public use.

Every PNG is built here from bytes. Container framing only: no pixel decoding is claimed or tested.
"""
import json
import unittest
import zlib

from campaign_tool import image_meta
from campaign_tool.content import ContentError
from campaign_tool.content_check import content_problems
from campaign_tool.image_meta import UNREADABLE, image_size, metadata_kinds, strip
from campaign_tool.site import build_site
from tests.test_image_meta import Campaign, GPS_VALUE, tiff

SIG = image_meta.PNG_SIGNATURE


def chunk(name, body=b""):
    return len(body).to_bytes(4, "big") + name + body + zlib.crc32(name + body).to_bytes(4, "big")


def bad_crc(name, body=b"", crc=None):
    good = zlib.crc32(name + body)
    crc = (good ^ 1) if crc is None else crc
    return len(body).to_bytes(4, "big") + name + body + crc.to_bytes(4, "big")


IHDR_BODY = (3).to_bytes(4, "big") + (2).to_bytes(4, "big") + bytes(5)
IHDR, IDAT, IEND = chunk(b"IHDR", IHDR_BODY), chunk(b"IDAT", b"\x78\x9c\x01\x02"), chunk(b"IEND")
TEXT, EXIF = chunk(b"tEXt", b"Comment\0home"), chunk(b"eXIf", tiff())


def png(*chunks):
    return SIG + b"".join(chunks)


GOOD = png(IHDR, IDAT, IEND)


class RefusalTests(unittest.TestCase):
    def assertRefused(self, data, name):
        self.assertEqual(metadata_kinds(data, ".png"), [UNREADABLE], name)
        with self.assertRaisesRegex(ValueError, "^unreadable image$"):
            strip(data, ".png")
        self.assertIsNone(image_size(data, ".png"), name)

    def test_bad_crc_in_every_chunk_kind(self):
        plte = (b"PLTE", bytes(3))
        for name, bad in (("IHDR", bad_crc(b"IHDR", IHDR_BODY)), ("IDAT", bad_crc(b"IDAT", b"\x78\x9c")),
                          ("tEXt", bad_crc(b"tEXt", b"k\0v")), ("eXIf", bad_crc(b"eXIf", tiff())),
                          ("PLTE", bad_crc(*plte)), ("IEND", bad_crc(b"IEND"))):
            parts = {"IHDR": [bad, IDAT, IEND], "IDAT": [IHDR, bad, IEND], "IEND": [IHDR, IDAT, bad]}
            self.assertRefused(png(*parts.get(name, [IHDR, bad, IDAT, IEND])), name)

    def test_high_bit_crc_is_compared_unsigned(self):
        body = b"\x00" * 5
        self.assertRefused(png(IHDR, bad_crc(b"tEXt", body, crc=zlib.crc32(b"tEXt" + body) ^ 0x80000000), IDAT, IEND),
                           "high bit flipped")
        # a genuine checksum with the high bit set is accepted
        for n in range(200):
            body = bytes([n])
            if zlib.crc32(b"zzZz" + body) >> 31:
                self.assertEqual(metadata_kinds(png(IHDR, chunk(b"zzZz", body), IDAT, IEND), ".png"), [])
                break
        else:
            self.fail("no high-bit CRC found")

    def test_bad_crc_in_removable_chunk_is_not_hidden_by_strip(self):
        self.assertRefused(png(IHDR, bad_crc(b"tEXt", b"Comment\0home"), IDAT, IEND), "text")

    def test_order_and_structure(self):
        plte = chunk(b"PLTE", bytes(6))
        cases = {
            "iend only": png(IEND), "no ihdr": png(IDAT, IEND), "idat before ihdr": png(IDAT, IHDR, IEND),
            "duplicate ihdr": png(IHDR, IHDR, IDAT, IEND), "ihdr late": png(TEXT, IHDR, IDAT, IEND),
            "short ihdr": png(chunk(b"IHDR", bytes(12)), IDAT, IEND), "no idat": png(IHDR, IEND),
            "non-contiguous idat": png(IHDR, IDAT, TEXT, IDAT, IEND), "nonempty iend": png(IHDR, IDAT, chunk(b"IEND", b"x")),
            "plte after idat": png(IHDR, IDAT, plte, IEND), "duplicate plte": png(IHDR, plte, plte, IDAT, IEND),
            "empty plte": png(IHDR, chunk(b"PLTE"), IDAT, IEND), "plte not triples": png(IHDR, chunk(b"PLTE", bytes(4)), IDAT, IEND),
            "plte 257 entries": png(IHDR, chunk(b"PLTE", bytes(771)), IDAT, IEND),
            "unknown critical": png(IHDR, chunk(b"ABCD"), IDAT, IEND), "digit in type": png(IHDR, chunk(b"tEX1"), IDAT, IEND),
            "reserved bit": png(IHDR, chunk(b"text", b"x"), IDAT, IEND), "no iend": png(IHDR, IDAT),
            "length past end": png(IHDR, IDAT) + (99).to_bytes(4, "big") + b"IEND\0\0\0\0",
            "length over 2^31": png(IHDR, IDAT, (2 ** 31).to_bytes(4, "big") + b"tEXt" + bytes(8), IEND),
        }
        for name, data in cases.items():
            self.assertRefused(data, name)

    def test_accepted_shapes(self):
        plte = chunk(b"PLTE", bytes(768))
        for name, data in {"split idat": png(IHDR, IDAT, IDAT, IEND), "plte": png(IHDR, plte, IDAT, IEND),
                           "unknown ancillary": png(IHDR, chunk(b"zzZz", b"opaque"), IDAT, IEND)}.items():
            self.assertEqual(metadata_kinds(data, ".png"), [], name)
            self.assertEqual(image_size(data, ".png"), (3, 2), name)

    def test_every_truncation_and_chunk_limit_refuse_cleanly(self):
        data = png(IHDR, TEXT, EXIF, IDAT, IEND)
        for cut in range(len(data)):
            self.assertEqual(metadata_kinds(data[:cut], ".png"), [UNREADABLE], cut)
            self.assertIsNone(image_size(data[:cut], ".png"), cut)
        filler = chunk(b"zzZz")
        self.assertEqual(metadata_kinds(png(IHDR, filler * 9997, IDAT, IEND), ".png"), [])
        self.assertEqual(metadata_kinds(png(IHDR, filler * 9998, IDAT, IEND), ".png"), [UNREADABLE])
        self.assertEqual(metadata_kinds(SIG + bytes(image_meta.MAX_BYTES), ".png"), [UNREADABLE])

    def test_trailing_data_after_valid_iend_only(self):
        self.assertEqual(metadata_kinds(GOOD + b"junk", ".png"), ["trailing data"])
        self.assertEqual(strip(GOOD + b"junk", ".png"), GOOD)
        self.assertRefused(png(IHDR, IDAT, bad_crc(b"IEND")) + b"junk", "bad iend then junk")


class PreservationTests(unittest.TestCase):
    def test_strip_keeps_accepted_chunks_exactly_and_is_idempotent(self):
        opaque = chunk(b"zzZz", b"\x00opaque\xff")
        dirty = png(IHDR, TEXT, opaque, EXIF, IDAT, IDAT, IEND) + b"trailer"
        clean = strip(dirty, ".png")
        self.assertEqual(clean, png(IHDR, opaque, IDAT, IDAT, IEND))
        self.assertEqual(strip(clean, ".png"), clean)
        self.assertEqual(metadata_kinds(clean, ".png"), [])
        self.assertEqual(image_size(dirty, ".png"), image_size(clean, ".png"))
        self.assertEqual(image_size(clean, ".png"), (3, 2))


class IntegrationTests(Campaign):
    def put(self, data):
        target = self.assets / "photos" / "council.png"
        target.write_bytes(data)
        return target

    def test_build_and_check_name_unreadable_and_keep_prior_public_tree(self):
        self.put(GOOD)
        build_site(self.root)
        before = self.tree()
        self.put(png(IHDR, bad_crc(b"IDAT", b"\x78\x9c"), IEND))
        with self.assertRaises(ContentError) as caught:
            build_site(self.root)
        self.assertIn("public-assets/photos/council.png", str(caught.exception))
        self.assertIn(UNREADABLE, str(caught.exception))
        problems = " ".join(map(str, content_problems(self.root)))
        self.assertIn("council.png", problems)
        self.assertIn(UNREADABLE, problems)
        result = self.cli("build", "--directory", str(self.root))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(UNREADABLE.encode(), result.stderr)
        self.assertNotIn(GPS_VALUE, result.stdout + result.stderr)
        after = self.tree()
        public = {k: v for k, v in after.items() if k.startswith("public/")}
        self.assertEqual(public, {k: v for k, v in before.items() if k.startswith("public/")})

    def test_strip_cli_refuses_without_creating_output(self):
        source = self.put(png(IHDR, bad_crc(b"tEXt", b"k\0v"), IDAT, IEND))
        original = source.read_bytes()
        result = self.cli("content", "strip-metadata", str(source))
        self.assertEqual(result.returncode, 1)
        self.assertIn(UNREADABLE.encode(), result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)
        self.assertFalse(source.with_name("council.clean.png").exists())
        self.assertEqual(source.read_bytes(), original)

    def test_strip_cli_accepts_valid_png(self):
        source = self.put(png(IHDR, TEXT, IDAT, IEND))
        result = self.cli("content", "strip-metadata", str(source))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["removed"], ["text"])
        self.assertEqual(source.with_name("council.clean.png").read_bytes(), GOOD)


if __name__ == "__main__":
    unittest.main()
