"""RF1741-5: commas inside a srcset candidate URL must not conceal unlinked assets. Synthetic data only; no network."""
import unittest

from campaign_tool.public_asset_links import unlinked_asset_paths

ASSETS = ["orphan.png", "real.png", "second.png"]


def page(tag):
    return "<html><body>" + tag + "</body></html>"


class SrcsetCommaTests(unittest.TestCase):
    def unlinked(self, srcset, tag="img", assets=ASSETS, closing=">"):
        html = page('<%s srcset="%s"%s' % (tag, srcset, closing))
        return unlinked_asset_paths({"index.html": html}, list(assets))

    def test_data_and_external_url_commas_never_acknowledge_a_payload_named_asset(self):
        cases = [
            "data:image/svg+xml,/orphan.png 1x, /real.png 2x",
            "DATA:image/svg+xml,a,b,/orphan.png 1x, /real.png 2x",
            "https://other.example.invalid/image.png?hint=,/orphan.png 1x, /real.png 2x",
            "https://other.example.invalid/image.png#,/orphan.png 1x, /real.png 2x",
        ]
        for tag in ("img", "source"):
            for closing in (">", " />"):
                for srcset in cases:
                    with self.subTest(tag=tag, closing=closing, srcset=srcset):
                        self.assertEqual(self.unlinked(srcset, tag, closing=closing), ["orphan.png", "second.png"])

    def test_adjacent_comma_is_inside_one_url_until_whitespace(self):
        self.assertEqual(self.unlinked("/real.png,/orphan.png 1x"), ASSETS)
        self.assertEqual(self.unlinked("/real.png 1x, /second.png 2x"), ["orphan.png"])
        self.assertEqual(self.unlinked("/real.png, /second.png"), ["orphan.png"])
        self.assertEqual(self.unlinked("/real.png 1x,/second.png 2x"), ["orphan.png"])

    def test_width_density_empty_and_tolerated_descriptors(self):
        self.assertEqual(self.unlinked("/real.png 480w,\n\t/second.png\t800w,,"), ["orphan.png"])
        for empty in ("", "   ", ",,,"):
            self.assertEqual(self.unlinked(empty), ASSETS)
        self.assertEqual(self.unlinked("/real.png 1x 2x junk"), ["orphan.png", "second.png"])
        self.assertEqual(self.unlinked("/real.png 1x, /real.png 2x"), ["orphan.png", "second.png"])
        # valueless attribute
        html = page("<img srcset>")
        self.assertEqual(unlinked_asset_paths({"index.html": html}, list(ASSETS)), ASSETS)

    def test_local_path_policy_stays_strict(self):
        self.assertEqual(self.unlinked("/Real.png 1x, //x.example.invalid/second.png 2x"), ASSETS)
        self.assertEqual(self.unlinked("/%72eal.png 1x, ../../second.png 2x"), ASSETS)
        files = {"a/b/page.html": page('<source srcset="../../real.png 1x, &#47;second.png 2x">')}
        self.assertEqual(unlinked_asset_paths(files, list(ASSETS)), ["orphan.png"])

    def test_other_reference_surfaces_still_union(self):
        html = page('<img srcset="data:x,/orphan.png 1x"><a href="/real.png">r</a><img src="/second.png">')
        self.assertEqual(unlinked_asset_paths({"index.html": html}, list(ASSETS)), ["orphan.png"])


if __name__ == "__main__":
    unittest.main()
