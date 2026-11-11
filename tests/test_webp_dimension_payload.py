"""RF0600-4: WebP dimension headers come from the declared chunk payload, never from alignment padding."""
import tempfile
import unittest
from pathlib import Path

from campaign_tool.content import ContentError, load_brand
from campaign_tool.image_meta import image_size, metadata_kinds, strip


def vp8x_payload(width=300, height=200, length=10):
    return (b"\x10" + bytes(3) + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little"))[:length]


def vp8_payload(width=320, height=240, length=10):
    return (bytes(3) + b"\x9d\x01\x2a" + width.to_bytes(2, "little") + height.to_bytes(2, "little"))[:length]


def webp(fourcc, payload, pad=b"\0", tail=b""):
    chunk = fourcc + len(payload).to_bytes(4, "little") + payload + (pad if len(payload) & 1 else b"")
    body = b"WEBP" + chunk + tail
    return b"RIFF" + len(body).to_bytes(4, "little") + body


class WebpDimensionPayload(unittest.TestCase):
    def test_nine_byte_vp8x_and_vp8_do_not_borrow_alignment(self):
        for fourcc, make in ((b"VP8X", vp8x_payload), (b"VP8 ", vp8_payload)):
            for pad in (b"\0", b"\x01", b"\xf0"):
                with self.subTest(fourcc=fourcc, pad=pad):
                    short = make(length=9)
                    self.assertIsNone(image_size(webp(fourcc, short, pad), ".webp"))
            self.assertIsNotNone(image_size(webp(fourcc, make()), ".webp"))

    def test_boundaries_and_pad_independence(self):
        self.assertEqual(image_size(webp(b"VP8X", vp8x_payload() + b"\0"), ".webp"), (300, 200))
        self.assertEqual(image_size(webp(b"VP8 ", vp8_payload() + b"\0"), ".webp"), (320, 240))
        for pad in range(256):
            odd = bytes([pad])
            self.assertEqual(image_size(webp(b"VP8L", b"\x2f\x00\x00\x00\x00", odd), ".webp"), (1, 1))
            self.assertEqual(image_size(webp(b"VP8X", vp8x_payload(length=10) + b"\0" * 1, odd), ".webp"), (300, 200))
            self.assertIsNone(image_size(webp(b"VP8X", vp8x_payload(length=9), odd), ".webp"))
            self.assertIsNone(image_size(webp(b"VP8 ", vp8_payload(length=9), odd), ".webp"))
        for length in range(9):
            self.assertIsNone(image_size(webp(b"VP8X", vp8x_payload(length=length)), ".webp"))

    def test_following_chunks_and_tail_cannot_complete_header(self):
        tail = b"VP8L" + (5).to_bytes(4, "little") + b"\x2f\x00\x00\x00\x00" + b"\0"
        self.assertIsNone(image_size(webp(b"VP8X", vp8x_payload(length=9), b"\0", tail), ".webp"))
        self.assertIsNone(image_size(webp(b"VP8 ", vp8_payload(length=9), b"\0", tail), ".webp"))

    def test_real_brand_loader_refuses_short_payload_without_mutation(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-webp-") as root:
            assets = Path(root)
            (assets / "ok.webp").write_bytes(webp(b"VP8X", vp8x_payload()))
            self.assertEqual(load_brand({"logo": "ok.webp", "logo_alt": "Logo"}, assets)["logo_size"], (300, 200))
            for name, data in (("x.webp", webp(b"VP8X", vp8x_payload(length=9))),
                               ("v.webp", webp(b"VP8 ", vp8_payload(length=9)))):
                (assets / name).write_bytes(data)
                with self.assertRaises(ContentError) as caught:
                    load_brand({"logo": name, "logo_alt": "Logo"}, assets)
                self.assertEqual(str(caught.exception),
                                 "site.json.brand.logo: unreadable image (no readable width and height)")
                self.assertEqual((assets / name).read_bytes(), data)

    def test_metadata_and_strip_ranges_stay_byte_identical(self):
        exif = b"XMP " + (4).to_bytes(4, "little") + b"abcd"
        data = webp(b"VP8 ", vp8_payload() + b"\0", tail=exif)
        self.assertTrue(metadata_kinds(data, ".webp"))
        self.assertEqual(metadata_kinds(strip(data, ".webp"), ".webp"), [])
        self.assertEqual(strip(strip(data, ".webp"), ".webp"), strip(data, ".webp"))
        self.assertEqual(image_size(strip(data, ".webp"), ".webp"), (320, 240))


if __name__ == "__main__":
    unittest.main()
