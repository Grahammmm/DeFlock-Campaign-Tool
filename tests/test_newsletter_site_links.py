"""newsletter --site PUBLIC: every campaign-site link in the draft reaches the built public/. Fictional data; nothing is sent."""
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.newsletter.__main__ import main  # noqa: E402
from campaign_tool.newsletter.site_links import external_links, site_links  # noqa: E402
from campaign_tool.site_check import resolve  # noqa: E402

BASE = "https://cedar-records.example.invalid"
FINDING = "/findings/cedar-policy-posted.html"
OLD_KEYS = ["subject", "counts", "excluded", "written", "sent", "source", "since_from"]


def manifest(base_url, paths, meetings=()):
    findings = [{"title": "Fictional finding " + str(i), "summary": "A fictional record.", "classification": "documented_fact",
                 "confidence": "verified", "path": path, "published_at": "2099-01-02T00:00:00Z"}
                for i, path in enumerate(paths)]
    return {"schema_version": 1, "campaign": {"name": "Cedar", "base_url": base_url, "county_name": "Cedar"},
            "since": "2099-01-01T00:00:00Z", "findings": findings, "meetings": list(meetings)}


class SiteLinksTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="fictional-newsletter-site-")
        cls.built = Path(cls._tmp.name) / "built"
        shutil.copytree(REPO_ROOT / "examples" / "fictional-campaign", cls.built)
        subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(cls.built)],
                       cwd=REPO_ROOT, check=True, capture_output=True, timeout=120)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="fictional-newsletter-run-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(self.built, self.root)
        self.public = self.root / "public"
        self.kit = self.root / "kit"

    def cli(self, *extra):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool.newsletter", "--directory", str(self.root), *extra],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)

    def write_manifest(self, data):
        path = self.tmp / "manifest.json"
        path.write_text(json.dumps(data))
        return str(path)

    def test_fictional_build_every_link_found_exit_0(self):
        result = self.cli("--from-content", "--site", str(self.public))
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["site_links"], {"public": str(self.public), "checked": 2, "external_skipped": 1, "missing": []})
        self.assertEqual(site_links(json.loads((self.kit / "newsletter-draft.json").read_text()), BASE), ["/", FINDING])
        self.assertFalse(out["sent"])
        self.assertNotIn("newsletter: link", result.stderr)

    def test_deleted_finding_page_is_named_exit_3_and_draft_still_written(self):
        (self.public / FINDING.lstrip("/")).unlink()
        result = self.cli("--from-content", "--site", str(self.public))
        self.assertEqual(result.returncode, 3, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["site_links"]["missing"], [{"link": FINDING, "reason": "no_page"}])
        self.assertIn(f"newsletter: link {FINDING} not in {self.public} (no_page)", result.stderr)
        for ext in ("html", "txt", "json"):
            self.assertTrue((self.kit / ("newsletter-draft." + ext)).is_file())
        self.assertFalse(out["sent"])

    def test_fragment_must_match_an_id_on_the_page(self):
        draft = {"html": f'<a href="{BASE}{FINDING}#corrections">a</a> <a href="{BASE}{FINDING}#nope">b</a>'
                         f' <a href="{BASE}#top">c</a> <a href="{BASE}#gone">d</a>', "text": ""}
        links = site_links(draft, BASE)
        self.assertEqual(links, ["/#gone", "/#top", FINDING + "#corrections", FINDING + "#nope"])
        self.assertEqual([resolve(self.public, link) for link in links], ["no_fragment", None, None, "no_fragment"])

    def test_no_base_url_checks_plain_text_paths(self):
        path = self.write_manifest(manifest(None, [FINDING, "/findings/gone.html"]))
        result = self.cli("--manifest", path, "--site", str(self.public))
        self.assertEqual(result.returncode, 3, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["site_links"]["checked"], 2)
        self.assertEqual(out["site_links"]["missing"], [{"link": "/findings/gone.html", "reason": "no_page"}])
        self.assertIn(f"newsletter: link /findings/gone.html not in {self.public} (no_page)", result.stderr)
        ok = self.cli("--manifest", self.write_manifest(manifest(None, [FINDING])), "--site", str(self.public))
        self.assertEqual((ok.returncode, json.loads(ok.stdout)["site_links"]["missing"]), (0, []), ok.stderr)

    def test_null_campaign_is_treated_as_no_base_url(self):
        data = manifest(None, [FINDING, "/findings/gone.html"])
        data["campaign"] = None
        result = self.cli("--manifest", self.write_manifest(data), "--site", str(self.public))
        self.assertEqual(result.returncode, 3, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["site_links"]["checked"], 2)
        self.assertEqual(out["site_links"]["missing"], [{"link": "/findings/gone.html", "reason": "no_page"}])

    def test_other_hosts_counted_never_resolved_or_fetched(self):
        meetings = [{"body": "Cedar Council", "starts_at": "2099-06-02T18:00:00-07:00", "agenda_item": "Item 1",
                     "agenda_url": "https://agenda.example.invalid/a/1"},
                    {"body": "Cedar Board", "starts_at": "2099-06-03T18:00:00-07:00", "agenda_item": "Item 2",
                     "agenda_url": BASE + ".evil.example.invalid/x"}]
        path = self.write_manifest(manifest(BASE, [FINDING], meetings))
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("socket.socket.connect", side_effect=AssertionError("network")) as connect, \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")) as opener, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--directory", str(self.root), "--manifest", path, "--site", str(self.public)])
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual((connect.call_count, opener.call_count), (0, 0))
        report = json.loads(stdout.getvalue())["site_links"]
        self.assertEqual((report["checked"], report["external_skipped"], report["missing"]), (2, 3, []))
        draft = json.loads((self.kit / "newsletter-draft.json").read_text())
        self.assertEqual(external_links(draft, BASE), sorted(["https://agenda.example.invalid/a/1",
                                                               BASE + ".evil.example.invalid/x", "{{ unsubscribe }}"]))

    def test_link_escaping_public_is_missing_and_outside_never_read(self):
        (self.root / "x").write_text("outside public/")
        (self.public / "findings" / "away.html").symlink_to(self.root / "x")
        links = site_links({"html": f'<a href="{BASE}/../x">a</a><a href="{BASE}/findings/../../x">b</a>'
                                    f'<a href="{BASE}/findings/away.html">c</a>', "text": ""}, BASE)
        self.assertEqual(links, ["/../x", "/findings/../../x", "/findings/away.html"])
        with mock.patch("pathlib.Path.read_text", side_effect=AssertionError("read")):
            self.assertEqual([resolve(self.public, link) for link in links], ["no_page"] * 3)

    def test_bad_site_folder_stops_before_any_draft(self):
        (self.tmp / "file").write_text("not a folder")
        (self.tmp / "link").symlink_to(self.public)
        for site in (self.tmp / "file", self.tmp / "missing", self.tmp / "link"):
            result = self.cli("--from-content", "--site", str(site))
            self.assertEqual(result.returncode, 1, site)
            self.assertIn(f"Stopped: --site {site}: must be an existing folder", result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse((self.kit / "newsletter-draft.json").exists(), "no draft written")

    def test_without_site_output_unchanged(self):
        result = self.cli("--from-content")
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(list(out), OLD_KEYS)
        self.assertEqual(result.stdout, json.dumps({key: out[key] for key in OLD_KEYS}, indent=2) + "\n")
        broken = self.cli("--manifest", self.write_manifest(manifest(None, ["/findings/gone.html"])))
        self.assertEqual((broken.returncode, "site_links" in json.loads(broken.stdout)), (0, False), broken.stderr)


if __name__ == "__main__":
    unittest.main()
