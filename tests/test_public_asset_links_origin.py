"""RF0540-3: HTTPS authority identity (port presence, userinfo presence) in unlinked-asset diagnostics. Synthetic; no network."""
import datetime
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.content_check import content_problems
from campaign_tool import public_asset_links
from campaign_tool.content import ContentError
from campaign_tool.public_asset_links import local_path, unlinked_asset_paths
from campaign_tool.site import build_site, preview_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
HOST = "https://cedar.example.invalid"
DAY = datetime.date(2026, 11, 11)
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

    def zero_port(self):
        host = self.base.rstrip("/")
        origin = "/".join(host.split("/", 3)[:3])
        self.point_source(origin + ":0" + host[len(origin):] + "/authority.pdf")

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args, "--directory", str(self.root)],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=300)

    def tree(self, base=None):
        base = base or self.root
        return {p.relative_to(base).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in sorted(base.rglob("*")) if p.is_file()}

    def test_content_check_text_and_json_name_the_asset(self):
        self.zero_port()
        result = self.cli("content-check")
        self.assertEqual((result.returncode, result.stdout.splitlines()[:1]), (3, ["content/: 1 problem(s)"]))
        self.assertEqual(result.stdout.splitlines()[-1], LINE)
        records = json.loads(self.cli("content-check", "--json").stdout)
        self.assertEqual([(r["file"], r["message"]) for r in records["problems"]],
                         [("public-assets/authority.pdf", "published but not linked from any page")])

    def test_strict_builder_and_cli_refuse_before_any_output(self):
        self.zero_port()
        with self.assertRaises(ContentError) as raised:
            build_site(self.root, DAY, require_linked_assets=True)
        self.assertEqual(str(raised.exception), LINE)
        checked = self.cli("build", "--check")
        self.assertEqual(checked.returncode, 1, checked.stdout)
        self.assertIn(LINE, checked.stderr)
        self.assertNotIn("passed", checked.stdout)
        self.assertFalse((self.root / "public").exists())
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".public")], [])
        # an existing public/ tree and the inputs keep their bytes and mtimes through a refusal
        build_site(self.root, DAY)
        before = self.tree()
        with self.assertRaises(ContentError):
            build_site(self.root, DAY, require_linked_assets=True)
        self.assertEqual(self.cli("build", "--check").returncode, 1)
        self.assertEqual(self.tree(), before)

    def test_ordinary_build_is_frozen_and_only_the_diagnostic_differs(self):
        self.zero_port()
        old, new = [], []
        build_site(self.root, DAY, notes=new)
        repaired = {k: v[0] for k, v in self.tree(self.root / "public").items()}
        shutil.rmtree(self.root / "public")
        with mock.patch.object(public_asset_links, "unlinked_asset_paths", return_value=[]):  # old diagnostic outcome
            build_site(self.root, DAY, notes=old)
        previous = {k: v[0] for k, v in self.tree(self.root / "public").items()}
        self.assertEqual([n for n in new if "not linked" in n], [LINE])
        self.assertEqual([n for n in old if "not linked" in n], [])
        self.assertEqual(sorted(repaired), sorted(previous))
        self.assertTrue(repaired == previous, "emitted bytes differ")
        self.assertEqual(repaired["authority.pdf"], b"%PDF-1.4\n%%EOF\n")

    def test_acknowledgement_and_preview_preserve_contracts(self):
        self.zero_port()
        build_site(self.root, DAY)
        before = self.tree()
        preview_site(self.root, DAY)
        self.assertEqual(self.tree(), before)  # preview writes nothing
        self.assertNotIn("not linked", self.cli("build", "--preview").stdout)
        self.assertEqual(self.tree(), before)
        site_path = self.root / "content" / "site.json"
        site = json.loads(site_path.read_text(encoding="utf-8"))
        site["unlinked_assets_ok"] = ["authority.pdf"]
        site_path.write_text(json.dumps(site), encoding="utf-8")
        self.assertEqual(content_problems(self.root), [])
        self.assertEqual(self.cli("build", "--check").returncode, 0)
        site["unlinked_assets_ok"] = ["missing.pdf"]  # stale acknowledgement still refuses
        site_path.write_text(json.dumps(site), encoding="utf-8")
        self.assertTrue(content_problems(self.root))


if __name__ == "__main__":
    unittest.main()
