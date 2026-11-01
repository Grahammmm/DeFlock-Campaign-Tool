"""DF1737-12: a verified agency's Flock transparency slug becomes a link only (never fetched)."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.content import ContentError
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
URL = "https://transparency.flocksafety.com/example-pd"
FIELD = ".flock_transparency_slug"


def refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted")


class FlockTransparencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-flock-transparency-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        cities = self.root / "content" / "map" / "cities.json"
        mapped = json.loads(cities.read_text())
        mapped["example"] = {"name": "Example", "bounds": [[0.5, 0.5], [1.5, 1.5]]}
        cities.write_text(json.dumps(mapped))

    def write_agency(self, **fields):
        path = self.root / "content" / "agencies.json"
        agencies = json.loads((EXAMPLE / "content" / "agencies.json").read_text())
        agency = {"agency_id": "ca-example-police", "slug": "example", "name": "Example PD (fictional)",
                  "kind": "police", "jurisdiction_name": "Example"}
        agency.update(fields)
        path.write_text(json.dumps(agencies + [agency]))

    def build(self):
        with mock.patch("urllib.request.urlopen", side_effect=refuse_network) as urlopen:
            written = build_site(self.root)
        self.assertEqual(urlopen.call_count, 0)
        return written

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def assert_no_link(self):
        for name in ("agencies.html", "index.html", "site-data.js"):
            self.assertNotIn("transparency.flocksafety.com", self.read(name), name)
        self.assertNotIn("flockTransparencyUrl", self.read("site-data.js"))
        self.assertNotIn(">Flock transparency portal</a>", self.read("agencies.html"))

    def test_verified_slug_renders_one_link_and_card_payload(self):
        self.write_agency(verified=True, flock_transparency_slug="example-pd")
        self.build()
        agencies = self.read("agencies.html")
        link = f'<a href="{URL}" rel="noopener noreferrer">Flock transparency portal</a>'
        self.assertEqual(agencies.count(URL), 1)
        self.assertEqual(agencies.count(link), 1)
        article = agencies.split('<article id="example">', 1)[1].split("</article>", 1)[0]
        self.assertIn(link, article)
        data = self.read("site-data.js")
        cities = json.loads(data.split("var CITIES=", 1)[1].split(";\n", 1)[0])
        self.assertEqual(cities["example"]["flockTransparencyUrl"], URL)
        self.assertEqual(sum("flockTransparencyUrl" in city for city in cities.values()), 1)
        self.assertNotIn(URL, self.read("index.html"))
        self.assertIn('id="city-flock-link"', self.read("index.html"))

    def test_unverified_valid_slug_is_dropped(self):
        for verified in (False, "true", 1, None):
            with self.subTest(verified=verified):
                self.write_agency(verified=verified, flock_transparency_slug="example-pd")
                self.build()
                self.assert_no_link()

    def test_null_or_absent_slug_has_no_link(self):
        for fields in ({"verified": True, "flock_transparency_slug": None}, {"verified": True}):
            with self.subTest(fields=fields):
                self.write_agency(**fields)
                self.build()
                self.assert_no_link()

    def test_malformed_slug_stops_build_and_keeps_old_public(self):
        self.write_agency(verified=True, flock_transparency_slug="example-pd")
        self.build()
        before = {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}
        bad = ["Example-PD", "example--pd", "-pd", "pd-", "pd/../x", "pd?x=1", "a" * 81, 7, True, ["example-pd"],
               "", "example pd", "example-pd\n", "a-b-c-d-e-f-g-h-i", "ex\u0430mple"]
        for verified in (True, False):
            for slug in bad:
                with self.subTest(slug=slug, verified=verified):
                    self.write_agency(verified=verified, flock_transparency_slug=slug)
                    with self.assertRaises(ContentError) as caught:
                        self.build()
                    message = str(caught.exception)
                    self.assertIn(FIELD, message)
                    self.assertIn("lowercase letters, digits and single hyphens only", message)
                    if isinstance(slug, str) and len(slug) > 3:
                        self.assertNotIn(slug.strip(), message)
                    after = {p.relative_to(self.public).as_posix(): p.read_bytes()
                             for p in self.public.rglob("*") if p.is_file()}
                    self.assertEqual(after, before)

    def test_longest_valid_slug_is_accepted(self):
        slug = "-".join(["a" * 9] * 8)
        self.assertEqual(len(slug), 79)
        self.write_agency(verified=True, flock_transparency_slug=slug)
        self.build()
        self.assertIn(f"https://transparency.flocksafety.com/{slug}", self.read("agencies.html"))

    def test_cli_build_check_leak_scan_passes(self):
        self.write_agency(verified=True, flock_transparency_slug="example-pd")
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(self.root), "--check"],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Public tree check passed", result.stdout)
        self.assertIn(URL, self.read("agencies.html"))


if __name__ == "__main__":
    unittest.main()
