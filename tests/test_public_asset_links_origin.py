"""RF0540-3: HTTPS authority identity (port presence, userinfo presence) in unlinked-asset diagnostics. Synthetic; no network."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from campaign_tool.content_check import content_problems
from campaign_tool.public_asset_links import local_path, unlinked_asset_paths

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
HOST = "https://cedar.example.invalid"
LINE = "public-assets/authority.pdf: published but not linked from any page"


class AuthorityMatrixTests(unittest.TestCase):
    def test_effective_port_uses_presence_not_truthiness(self):
        cases = [
            (HOST, HOST, True), (HOST + ":443", HOST, True), (HOST, HOST + ":443", True),
            (HOST + ":0", HOST, False), (HOST, HOST + ":0", False),
            (HOST + ":0", HOST + ":443", False), (HOST + ":443", HOST + ":0", False),
            (HOST + ":0", HOST + ":0", True), (HOST + ":8443", HOST + ":8443", True),
            (HOST + ":8443", HOST, False), (HOST, HOST + ":8443", False),
            (HOST + ":", HOST, True), (HOST.upper().replace("HTTPS", "https"), HOST, True),
        ]
        for url, base, local in cases:
            got = local_path(url + "/a.pdf", "index.html", base)
            self.assertEqual(got, "a.pdf" if local else None, (url, base))
        for bad in (HOST + ":x", HOST + ":99999", HOST + ":1:2"):
            self.assertIsNone(local_path(bad + "/a.pdf", "index.html", HOST))
            self.assertIsNone(local_path(HOST + "/a.pdf", "index.html", bad))

    def test_empty_userinfo_never_establishes_local_authority(self):
        for url in ("https://@cedar.example.invalid/a.pdf", "https://:@cedar.example.invalid/a.pdf",
                    "https://u@cedar.example.invalid/a.pdf", "https://:p@cedar.example.invalid/a.pdf"):
            self.assertIsNone(local_path(url, "index.html", HOST), url)
        for base in ("https://@cedar.example.invalid", "https://:@cedar.example.invalid", "https://u@cedar.example.invalid"):
            self.assertIsNone(local_path(HOST + "/a.pdf", "index.html", base), base)
        self.assertEqual(local_path(HOST + "/a.pdf", "index.html", HOST), "a.pdf")
        self.assertEqual(local_path("/a.pdf", "index.html"), "a.pdf")  # relative: no base needed

    def test_all_reference_surfaces_share_authority_identity(self):
        bad = f"{HOST}:0/a.pdf"
        good = f"{HOST}:443/a.pdf"
        empty = "https://@cedar.example.invalid/a.pdf"
        html = lambda *tags: {"index.html": "<html><body>" + "".join(tags) + "</body></html>"}
        for url, cleared in ((bad, False), (empty, False), (good, True)):
            surfaces = {
                "anchor": dict(files=html(f'<a href="{url}">x</a>')),
                "img": dict(files=html(f'<img src="{url}">')),
                "srcset": dict(files=html(f'<img srcset="{url} 1x">')),
                "meta": dict(files=html(f'<meta property="og:image" content="{url}">')),
                "public_urls": dict(files={}, public_urls=[url]),
                "redirect": dict(files={}, redirect_targets=[url]),
            }
            for name, kw in surfaces.items():
                got = unlinked_asset_paths(kw.pop("files"), ["a.pdf"], base_url=HOST, **kw)
                self.assertEqual(got, [] if cleared else ["a.pdf"], (name, url))
        self.assertEqual(unlinked_asset_paths({}, ["a.pdf"], base_url=HOST, declared_assets=["a.pdf"]), [])


class SourceUrlIntegrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="synthetic-origin-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        assets = self.root / "content" / "public-assets"
        assets.mkdir(parents=True, exist_ok=True)
        (assets / "authority.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        site = json.loads((self.root / "content" / "site.json").read_text(encoding="utf-8"))
        self.base = site["base_url"]
        self.sources = self.root / "content" / "sources.json"

    def point_source(self, url):
        data = json.loads(self.sources.read_text(encoding="utf-8"))
        rows = data if isinstance(data, list) else data.get("sources", data)
        rows[0]["public_url"] = url
        self.sources.write_text(json.dumps(data), encoding="utf-8")

    def test_source_port_zero_reports_unlinked_through_real_checker(self):
        host = self.base.rstrip("/")
        scheme_host = host.split("/", 3)[:3]
        origin = "/".join(scheme_host)
        self.point_source(origin + ":0" + host[len(origin):] + "/authority.pdf")
        self.assertEqual(content_problems(self.root), [LINE])
        self.point_source(self.base.rstrip("/") + "/authority.pdf")
        self.assertEqual(content_problems(self.root), [])


if __name__ == "__main__":
    unittest.main()
