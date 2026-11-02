"""DF2237-02: `finding publish DRAFT --approved-by NAME`. Synthetic campaign and synthetic ingested bytes only.

Run from the repository root: python3 -B -m unittest tests.test_findings_publish -v
"""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import findings  # noqa: E402
from campaign_tool.content import load_content  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign" / "content"
RECORDS = [b"synthetic publish record %d\n" % index for index in range(3)]
NOW = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def draft(**changes):
    base = {"id": "syn-pub-1", "title": "Synthetic title", "slug": "synthetic-title",
            "summary": "Synthetic summary.", "classification": "documented_fact", "confidence": "verified",
            "event_date": "2026-01-02", "sources": [{"locator": "page:1", "sha256": sha(RECORDS[0])},
                                                    {"locator": "page:2", "sha256": sha(RECORDS[1])}],
            "published_at": None, "corrections": [], "state": "draft", "author": "Ada  Lovelace",
            "limitations": ["Synthetic limits."], "counterevidence": ["None found."]}
    base.update(changes)
    return base


class FindingPublishTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-finding-publish-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "campaign"
        self.cli("init", "--directory", self.root, "--name", "Synthetic", "--county", "Synthetic County",
                 "--state", "ca")
        self.content = self.root / "content"
        (self.content / "findings").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLE / "site.json", self.content / "site.json")
        self.objects = self.root / "private" / "objects"
        self.log = self.root / "private" / "publication-log.jsonl"
        self.draft = self.base / "draft.json"
        self.write_draft(draft())
        for data in RECORDS[:2]:
            path = self.base / ("record-%s.txt" % sha(data)[:8])
            path.write_bytes(data)
            self.cli("ingest", "--directory", self.root, "--file", path, "--source-id", "synthetic-source")
        self.register(*RECORDS[:2])

    def cli(self, *arguments, returncode=0):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, returncode, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        if returncode:
            self.assertTrue(result.stderr.startswith("Stopped: "), result.stderr)
        return result

    def register(self, *records):
        entries = [{"sha256": sha(data), "title": "Synthetic record", "agency_id": "ca-synthetic",
                    "received_at": "2026-01-20T16:30:00+00:00", "public_url": None,
                    "fragment": "synthetic-records"} for data in records]
        (self.content / "sources.json").write_text(json.dumps(entries), encoding="utf-8")

    def write_draft(self, document):
        self.draft.write_text(json.dumps(document), encoding="utf-8")

    def receipt(self, role, reviewer, decision="approve", document=None):
        findings.add_receipt(self.root, document or json.loads(self.draft.read_text()), role, decision, reviewer,
                             "Checked synthetic pages.")

    def approve_all(self, document=None):
        self.receipt("factual", "Rev One", document=document)
        self.receipt("legal", "Rev Two", document=document)
        self.receipt("privacy", "Rev One", document=document)

    def publish(self, approved_by="Pat Organizer", returncode=0):
        return self.cli("finding", "publish", self.draft, "--directory", self.root, "--approved-by", approved_by,
                        returncode=returncode)

    def published(self):
        return sorted(p.name for p in (self.content / "findings").iterdir())

    def log_lines(self):
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def refused(self, *needles, approved_by="Pat Organizer"):
        result = self.publish(approved_by, returncode=1)
        for needle in needles:
            self.assertIn(needle, result.stderr)
        self.assertNotIn("synthetic-title.json", self.published())
        self.assertEqual(self.log_lines(), [])
        return result

    def public_tree(self):
        public = self.root / "public"
        return {str(p.relative_to(public)): p.read_bytes() for p in sorted(public.rglob("*")) if p.is_file()}

    def test_happy_path_writes_file_and_log_without_building(self):
        self.approve_all()
        before = self.public_tree()
        result = self.publish()
        lines = result.stdout.splitlines()
        self.assertEqual(json.loads(lines[0]), {"published": "synthetic-title",
                                                "path": "content/findings/synthetic-title.json"})
        self.assertEqual(lines[1], "Not built or deployed. Run build, then review the site before any deploy.")
        path = self.content / "findings" / "synthetic-title.json"
        text = path.read_text(encoding="utf-8")
        document = json.loads(text)
        self.assertEqual(text, json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n")
        self.assertEqual((document["state"], document["corrections"]), ("published", []))
        self.assertRegex(document["published_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertNotIn("updated_at", document)
        self.assertEqual(findings.reviewed_view(document), findings.reviewed_view(draft()))
        self.assertEqual([f["slug"] for f in (load_content(self.root) or {})["findings"]], ["synthetic-title"])
        self.assertEqual(self.public_tree(), before)
        [line] = self.log_lines()
        entry = json.loads(line)
        self.assertEqual(entry, {"at": document["published_at"], "action": "publish", "id": "syn-pub-1",
                                 "slug": "synthetic-title", "content_sha256": findings.reviewed_hash(draft()),
                                 "approved_by": "Pat Organizer"})
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)  # umask 0o077 from main
        self.assertEqual(self.published(), ["synthetic-title.json"])
        self.cli("build", "--directory", self.root, "--check")
        self.assertTrue(any("synthetic-title" in name for name in self.public_tree()))

    def test_each_review_blocker_refuses_with_its_name(self):
        roles = {"factual": "Rev One", "legal": "Rev Two", "privacy": "Rev One"}
        for missing in roles:
            with self.subTest(missing=missing):
                shutil.rmtree(self.root / "private" / "reviews", ignore_errors=True)
                for role, reviewer in roles.items():
                    if role != missing:
                        self.receipt(role, "Rev Three" if role == "privacy" else reviewer)
                self.refused("review blockers", f"missing_{missing}_review")

    def test_changes_requested_receipt_refuses(self):
        self.approve_all()
        self.receipt("legal", "Rev Three", decision="changes_requested")
        self.refused("unresolved_challenge")

    def test_receipts_on_older_content_hash_refuse(self):
        self.approve_all()
        self.write_draft(draft(summary="Edited synthetic summary."))
        self.refused("missing_factual_review", "need_two_independent_reviewers")

    def test_one_reviewer_for_all_roles_refuses(self):
        for role in ("factual", "legal", "privacy"):
            self.receipt(role, "Rev One")
        self.refused("need_two_independent_reviewers")

    def test_approved_by_author_variants_or_empty_refuse(self):
        self.approve_all()
        for name in ("Ada Lovelace", " ADA   lovelace ", "ADA LOVELACE"):
            with self.subTest(name=name):
                self.refused("author cannot approve", approved_by=name)
        for name, needle in (("", "must not be empty"), ("   ", "must not be empty"), ("x" * 121, "longer than"),
                             ("Pat\x07", "control characters")):
            with self.subTest(name=name):
                self.refused("--approved-by", needle, approved_by=name)

    def test_cited_record_unregistered_not_ingested_or_object_missing_refuses(self):
        self.approve_all()
        self.register(RECORDS[0])
        self.refused(f"{sha(RECORDS[1])[:12]} unregistered")
        self.assertNotIn(sha(RECORDS[1]), self.refused().stderr)
        self.register(*RECORDS)
        self.write_draft(draft(sources=[{"locator": "page:1", "sha256": sha(RECORDS[2])}]))
        self.approve_all()
        self.refused(f"{sha(RECORDS[2])[:12]} not_ingested")
        self.write_draft(draft())
        (self.objects / sha(RECORDS[0])).unlink()
        self.refused(f"{sha(RECORDS[0])[:12]} object_missing")
        outside = self.base / "outside-copy"
        outside.write_bytes(RECORDS[0])
        (self.objects / sha(RECORDS[0])).symlink_to(outside)
        self.refused(f"{sha(RECORDS[0])[:12]} object_missing")

    def test_existing_slug_or_id_refuses(self):
        self.approve_all()
        self.publish()
        before = (self.content / "findings" / "synthetic-title.json").read_bytes()
        result = self.publish(returncode=1)
        self.assertIn("synthetic-title.json already exists", result.stderr)
        self.write_draft(draft(slug="other-slug"))
        self.approve_all()
        result = self.publish(returncode=1)
        self.assertIn("already published under another slug", result.stderr)
        self.assertEqual(self.published(), ["synthetic-title.json"])
        self.assertEqual((self.content / "findings" / "synthetic-title.json").read_bytes(), before)
        self.assertEqual(len(self.log_lines()), 1)

    def test_reserved_slug_refuses(self):
        self.write_draft(draft(slug="about"))
        self.approve_all()
        self.refused("'about' is reserved")
        self.assertEqual(self.published(), [])

    def test_withdrawn_id_or_slug_refuses(self):
        self.approve_all()
        withdrawn = self.root / "private" / "withdrawn"
        withdrawn.mkdir(mode=0o700)
        cases = (("synthetic-title.json", {"id": "syn-other", "slug": "synthetic-title"}),
                 ("old-slug.json", {"id": "syn-pub-1", "slug": "old-slug"}))
        for name, document in cases:
            with self.subTest(name=name):
                for old in withdrawn.iterdir():
                    old.unlink()
                (withdrawn / name).write_text(json.dumps(document), encoding="utf-8")
                self.refused("was withdrawn and stays taken")
        (withdrawn / "old-slug.json").write_text("not json", encoding="utf-8")
        self.refused("withdrawn finding is not valid JSON")

    def test_load_finding_refusal_surfaces_as_stopped(self):
        self.write_draft(draft(confidence="needs_attorney_review"))
        self.approve_all()
        self.refused("confidence needs_attorney_review blocks publication")

    def test_symlinked_findings_directory_refuses(self):
        self.approve_all()
        outside = self.base / "outside-findings"
        outside.mkdir()
        (self.content / "findings").rmdir()
        (self.content / "findings").symlink_to(outside)
        result = self.publish(returncode=1)
        self.assertIn("must be a real directory", result.stderr)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.log_lines(), [])

    def test_crash_between_temp_write_and_replace_leaves_no_partial_file(self):
        self.approve_all()
        with mock.patch("campaign_tool.findings.os.replace", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                findings.publish(self.root, self.draft, "Pat Organizer", now=NOW)
        self.assertEqual(self.published(), [])
        self.assertEqual(self.log_lines(), [])

    def test_published_file_follows_umask(self):
        self.approve_all()
        previous = os.umask(0o022)
        try:
            result = findings.publish(self.root, self.draft, "Pat Organizer", now=NOW)
        finally:
            os.umask(previous)
        path = self.root / result["path"]
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        self.assertEqual(json.loads(path.read_text())["published_at"], "2026-03-04T05:06:07Z")
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)

    def race(self, first, second):
        """Publish two drafts at once with the check-to-write window widened; return the one error."""
        barrier, outcomes = threading.Barrier(2), {}
        real = findings.cited_problems

        def slow_check(root, finding):
            time.sleep(0.3)  # without the lock both threads are past the uniqueness checks here
            return real(root, finding)

        def run(name, path):
            barrier.wait()
            try:
                outcomes[name] = findings.publish(self.root, path, "Pat Organizer", now=NOW)
            except ValueError as exc:
                outcomes[name] = exc

        with mock.patch("campaign_tool.findings.cited_problems", side_effect=slow_check):
            threads = [threading.Thread(target=run, args=item) for item in (("a", first), ("b", second))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        self.assertEqual(len(outcomes), 2)
        errors = [str(value) for value in outcomes.values() if isinstance(value, ValueError)]
        self.assertEqual(len(errors), 1, outcomes)
        self.assertEqual(len(self.log_lines()), 1)
        return errors[0]

    def test_concurrent_publish_of_same_slug_lets_only_one_succeed(self):
        self.approve_all()
        error = self.race(self.draft, self.draft)
        self.assertIn("synthetic-title.json already exists", error)
        self.assertEqual(self.published(), ["synthetic-title.json"])

    def test_concurrent_publish_of_same_id_under_two_slugs_lets_only_one_succeed(self):
        self.approve_all()
        other = self.base / "draft-other.json"
        other_document = draft(slug="other-slug")
        other.write_text(json.dumps(other_document), encoding="utf-8")
        self.approve_all(document=other_document)
        error = self.race(self.draft, other)
        self.assertIn("already published under another slug", error)
        self.assertEqual(len(self.published()), 1)
        self.assertEqual([f["id"] for f in (load_content(self.root) or {})["findings"]], ["syn-pub-1"])


if __name__ == "__main__":
    unittest.main()
