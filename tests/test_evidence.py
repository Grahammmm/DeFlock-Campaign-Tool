"""evidence --check: published findings' cited records are registered and stored (RF2010-1).

Synthetic bytes in a temporary campaign only; hashes are computed at run time.
Run from the repository root: python3 -m unittest tests.test_evidence -v
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign" / "content"
PLANTED_SOURCE = "planted-source-id-zq81x"
RECORDS = [b"synthetic evidence record %d\n" % index for index in range(6)]
HEX64 = re.compile(r"[0-9a-f]{64}")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def short(data):
    return sha(data)[:12]


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-evidence-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "campaign"
        self.content = self.root / "content"
        self.objects = self.root / "private" / "objects"
        self.ledger = self.root / "private" / "ledger.sqlite"
        self.cli("init", "--name", "Synthetic Campaign", "--county", "Synthetic", "--state", "CA")
        (self.content / "findings").mkdir(parents=True)
        shutil.copyfile(EXAMPLE / "site.json", self.content / "site.json")

    def cli(self, command, *arguments, code=0):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "campaign_tool", command, "--directory", str(self.root),
             *map(str, arguments)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def ingest(self, *records):
        for data in records:
            path = self.base / ("planted-original-name-%s.txt" % short(data))
            path.write_bytes(data)
            self.cli("ingest", "--file", path, "--source-id", PLANTED_SOURCE)

    def register(self, *records):
        entries = [{"sha256": sha(data), "title": "Synthetic record", "agency_id": "ca-synthetic",
                    "received_at": "2026-01-20T16:30:00+00:00", "public_url": None,
                    "fragment": "synthetic-records"} for data in records]
        (self.content / "sources.json").write_text(json.dumps(entries), encoding="utf-8")

    def finding(self, slug, *records):
        document = json.loads((EXAMPLE / "findings" / "cedar-policy-posted.json").read_text())
        document.pop("agency_id", None)
        document.update({"id": "fnd_" + slug.replace("-", "_"), "slug": slug,
                         "sources": [{"sha256": sha(data), "locator": "page:1"} for data in records]})
        (self.content / "findings" / (slug + ".json")).write_text(json.dumps(document), encoding="utf-8")

    def evidence(self, code=0, check=True):
        result = self.cli("evidence", *(["--check"] if check else []), code=code)
        return json.loads(result.stdout), result

    def problems(self, code=3):
        return self.evidence(code)[0]["problems"]

    def clean(self):
        self.ingest(*RECORDS[:3])
        self.register(*RECORDS[:3])
        self.finding("alpha", RECORDS[0], RECORDS[1])
        self.finding("bravo", RECORDS[1], RECORDS[2])

    def test_clean_campaign_exit_zero_with_and_without_check(self):
        self.clean()
        for check in (False, True):
            report, _ = self.evidence(0, check=check)
            self.assertEqual(report, {"findings": 2, "citations": 4, "problems": [], "ledger": "present"})

    def test_unregistered_and_never_ingested_list_both_codes(self):
        self.ingest(RECORDS[0], RECORDS[3])
        self.register(RECORDS[0])
        self.finding("alpha", RECORDS[0], RECORDS[3], RECORDS[4])
        self.assertEqual(self.problems(), sorted([
            {"finding": "alpha", "sha256": short(RECORDS[3]), "code": "unregistered"},
            {"finding": "alpha", "sha256": short(RECORDS[4]), "code": "unregistered"},
            {"finding": "alpha", "sha256": short(RECORDS[4]), "code": "not_ingested"},
        ], key=lambda item: (item["sha256"], item["code"])))

    def test_registered_never_ingested_is_not_ingested_and_check_exits_three(self):
        self.ingest(RECORDS[0])
        self.register(RECORDS[0], RECORDS[1])
        self.finding("alpha", RECORDS[0], RECORDS[1])
        expected = [{"finding": "alpha", "sha256": short(RECORDS[1]), "code": "not_ingested"}]
        self.assertEqual(self.problems(3), expected)
        self.assertEqual(self.evidence(0, check=False)[0]["problems"], expected)

    def test_object_deleted_or_symlinked_is_object_missing(self):
        self.clean()
        (self.objects / sha(RECORDS[0])).unlink()
        outside = self.base / "outside-copy"
        outside.write_bytes(RECORDS[2])
        target = self.objects / sha(RECORDS[2])
        target.unlink()
        target.symlink_to(outside)
        self.assertEqual(self.problems(), [
            {"finding": "alpha", "sha256": short(RECORDS[0]), "code": "object_missing"},
            {"finding": "bravo", "sha256": short(RECORDS[2]), "code": "object_missing"},
        ])

    def test_no_ledger_every_citation_not_ingested_and_nothing_created(self):
        shutil.rmtree(self.root / "private")
        self.register(RECORDS[0])
        self.finding("alpha", RECORDS[0])
        report, _ = self.evidence(3)
        self.assertEqual(report, {"findings": 1, "citations": 1, "ledger": "absent", "problems": [
            {"finding": "alpha", "sha256": short(RECORDS[0]), "code": "not_ingested"}]})
        self.assertFalse((self.root / "private").exists())

    def test_no_content_reports_zero_and_exits_zero(self):
        shutil.rmtree(self.content)
        report, _ = self.evidence(0)
        self.assertEqual(report, {"findings": 0, "citations": 0, "problems": [], "content": "absent"})

    def test_malformed_finding_stops_with_exit_one(self):
        self.clean()
        bad = json.loads((self.content / "findings" / "alpha.json").read_text())
        del bad["title"]
        (self.content / "findings" / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
        result = self.cli("evidence", "--check", code=1)
        self.assertTrue(result.stderr.startswith("Stopped: findings/bad.json: missing field"), result.stderr)
        self.assertEqual(result.stdout, "")
        (self.content / "findings" / "bad.json").write_text("{not json", encoding="utf-8")
        self.assertTrue(self.cli("evidence", code=1).stderr.startswith("Stopped: bad.json: not valid"))

    def test_same_hash_cited_by_two_findings_one_row_each(self):
        self.ingest(RECORDS[0])
        self.register(RECORDS[0], RECORDS[1])
        self.finding("bravo", RECORDS[0], RECORDS[1], RECORDS[1])
        self.finding("alpha", RECORDS[1], RECORDS[0])
        report, _ = self.evidence(3)
        self.assertEqual(report["citations"], 4)
        self.assertEqual(report["problems"], [
            {"finding": "alpha", "sha256": short(RECORDS[1]), "code": "not_ingested"},
            {"finding": "bravo", "sha256": short(RECORDS[1]), "code": "not_ingested"},
        ])

    def test_output_never_names_source_id_original_name_or_full_hash(self):
        self.ingest(RECORDS[0], RECORDS[3])
        self.register(RECORDS[0], RECORDS[1])
        self.finding("alpha", RECORDS[0], RECORDS[1], RECORDS[3])
        (self.objects / sha(RECORDS[0])).unlink()
        report, result = self.evidence(3)
        self.assertEqual({item["code"] for item in report["problems"]},
                         {"unregistered", "not_ingested", "object_missing"})
        output = result.stdout + result.stderr
        self.assertNotIn(PLANTED_SOURCE, output)
        self.assertNotIn("planted-original-name", output)
        self.assertIsNone(HEX64.search(output))

    def test_ledger_opened_read_only(self):
        self.clean()
        (self.objects / sha(RECORDS[1])).unlink()
        before = (self.ledger.read_bytes(), os.stat(self.ledger).st_mtime_ns,
                  sorted(os.listdir(self.root / "private")))
        self.evidence(3)
        after = (self.ledger.read_bytes(), os.stat(self.ledger).st_mtime_ns,
                 sorted(os.listdir(self.root / "private")))
        self.assertEqual(before, after)

    def test_symlinked_objects_dir_or_unreadable_ledger_stops(self):
        self.clean()
        moved = self.base / "objects-elsewhere"
        self.objects.rename(moved)
        self.objects.symlink_to(moved)
        self.assertIn("must not be symlinks", self.cli("evidence", code=1).stderr)
        self.objects.unlink()
        moved.rename(self.objects)
        self.ledger.write_bytes(b"synthetic garbage, not a database" * 40)
        self.assertTrue(self.cli("evidence", "--check", code=1).stderr.startswith("Stopped: ledger unreadable"))


if __name__ == "__main__":
    unittest.main()
