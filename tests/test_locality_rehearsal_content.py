"""Content-site leg of the offline two-locality rehearsal: published findings, every public file scanned."""
import io
import json
import socket
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from campaign_tool import cli, site
from tools import rehearse_localities
from tools.rehearse_localities import rehearse
from tools.synthetic_content import write_synthetic_content

STARTER_KEYS = {"county", "state", "synthetic_only", "objects", "delivery_receipts", "requests_drafted",
                "law_package_status", "jurisdiction_verified", "production_ready",
                "safe_to_send_automatically", "public_files"}
TOP_KEYS = {"schema", "synthetic_only", "live_pilot_completed", "external_sends", "localities"}
PAGES = ("findings/synthetic-finding-1.html", "findings/synthetic-finding-2.html", "sources.html", "feed.xml")
REAL_DOCTOR = cli.doctor
REAL_BUILD_SITE = site.build_site


def offline_rehearse():
    with patch.object(socket, "create_connection", side_effect=AssertionError("offline only")):
        return rehearse()


def leaking_content(text_for):
    """write_synthetic_content that adds ``text_for(root, written)`` to finding 1's summary."""
    def write(root, label):
        written = write_synthetic_content(root, label)
        path = Path(root) / "content" / "findings" / "synthetic-1.json"
        finding = json.loads(path.read_text(encoding="utf-8"))
        finding["summary"] += " " + text_for(Path(root), written)
        path.write_text(json.dumps(finding), encoding="utf-8")
        return written
    return write


def build_without(name):
    def build(root, today=None):
        written = REAL_BUILD_SITE(root, today)
        if (Path(root) / "content").is_dir():
            target = Path(root) / "public" / name
            target.unlink()
            written = [path for path in written if path != target]
        return written
    return build


def doctor_with(field):
    def doctor(root):
        output = io.StringIO()
        with redirect_stdout(output):
            REAL_DOCTOR(root)
        report = json.loads(output.getvalue())
        if (Path(root) / "content").is_dir():
            report[field] = True
        print(json.dumps(report))
    return doctor


class ContentRehearsalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = offline_rehearse()

    def assertFailsWith(self, reason):
        with self.assertRaises(RuntimeError) as caught:
            offline_rehearse()
        self.assertEqual(str(caught.exception), reason)

    def test_offline_run_builds_every_content_page_for_both_localities(self):
        self.assertIs(self.report["content_build"], True)
        self.assertEqual([r["county"] for r in self.report["localities"]], ["San Luis Obispo", "Alameda"])
        for locality in self.report["localities"]:
            self.assertEqual(locality["content_findings"], 2)
            for page in PAGES + ("index.html", "data/findings.json", "site-data.js", "agency-cards.js"):
                self.assertRegex(locality["content_public_files"].get(page, ""), "^[0-9a-f]{64}$", page)
            # Synthetic sites set no base_url, so RF2030-2 writes no sitemap.xml.
            self.assertNotIn("sitemap.xml", locality["content_public_files"])

    def test_source_bytes_private_paths_and_temp_root_in_a_finding_are_refused(self):
        leaks = {
            "source bytes": lambda root, written: written["source_bytes"][0].decode("utf-8"),
            "objects path": lambda root, written: "see private/objects/abc",
            "temp root": lambda root, written: str(root.parent) + "/notes",
        }
        for name, text_for in leaks.items():
            with self.subTest(name), \
                    patch.object(rehearse_localities, "write_synthetic_content", leaking_content(text_for)):
                self.assertFailsWith("content_private_bytes_in_public")

    def test_missing_feed_or_finding_page_is_refused(self):
        for name in ("feed.xml", "sources.html", "findings/synthetic-finding-2.html"):
            with self.subTest(name), patch.object(site, "build_site", build_without(name)):
                self.assertFailsWith("content_build_missing_page")

    def test_content_sites_distinct_and_identical_index_refused(self):
        first, second = self.report["localities"]
        self.assertNotEqual(first["content_public_files"]["index.html"],
                            second["content_public_files"]["index.html"])
        real = rehearse_localities._content_build

        def same_index(*args):
            result = real(*args)
            result["content_public_files"]["index.html"] = "same"
            return result
        with patch.object(rehearse_localities, "_content_build", same_index):
            self.assertFailsWith("locality_output_not_distinct")

    def test_readiness_holds_rechecked_after_content_build(self):
        for locality in self.report["localities"]:
            self.assertIs(locality["production_ready"], False)
            self.assertIs(locality["safe_to_send_automatically"], False)
        for field in ("production_ready", "safe_to_send_automatically"):
            with self.subTest(field), patch.object(cli, "doctor", doctor_with(field)):
                self.assertFailsWith("readiness_hold_lost")

    def test_temp_roots_removed_after_success_and_failed_require(self):
        with tempfile.TemporaryDirectory() as temp_root:
            def leftovers():
                return [p for p in Path(temp_root).iterdir() if p.name.startswith("synthetic-locality-rehearsal-")]
            with patch.object(tempfile, "tempdir", temp_root):
                offline_rehearse()
                self.assertEqual(leftovers(), [])
                with patch.object(site, "build_site", build_without("feed.xml")):
                    self.assertFailsWith("content_build_missing_page")
                self.assertEqual(leftovers(), [])

    def test_starter_fields_of_the_report_unchanged(self):
        self.assertEqual(set(self.report), TOP_KEYS | {"content_build"})
        self.assertEqual(self.report["schema"], "offline-locality-rehearsal-v1")
        for locality in self.report["localities"]:
            self.assertEqual(set(locality), STARTER_KEYS | {"content_public_files", "content_findings"})
            self.assertEqual((locality["objects"], locality["delivery_receipts"]), (1, 2))
            starter = locality["public_files"]
            self.assertIn("index.html", starter)
            self.assertFalse([name for name in starter if name.startswith("findings") or name == "feed.xml"])
            self.assertNotEqual(starter["index.html"], locality["content_public_files"]["index.html"])


if __name__ == "__main__":
    unittest.main()
