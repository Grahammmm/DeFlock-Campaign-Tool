"""DF1822-01: `finding new --agency ID --title T [--source PREFIX ...]`. Synthetic campaign and hashes only.

Run from the repository root: python3 -B -m unittest tests.test_finding_new -v
"""
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import findings  # noqa: E402
from campaign_tool.content import FINDING_KEYS, FINDING_REQUIRED, ContentError, load_finding  # noqa: E402
from campaign_tool.review import review_blockers  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign" / "content"
AGENCY = "ca-sheriff-x"
SHA_A, SHA_B = "a1" * 32, "b2" * 32
TWIN_1, TWIN_2 = "c3c3c3c3c3c3" + "0" * 52, "c3c3c3c3c3c3" + "1" * 52  # share a 12-hex prefix
MARKED = ["edit_marker:author", "edit_marker:body_md", "edit_marker:classification", "edit_marker:confidence",
          "edit_marker:counterevidence", "edit_marker:event_date", "edit_marker:limitations",
          "edit_marker:sources[0].locator", "edit_marker:sources[1].locator", "edit_marker:summary"]


def tree(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(path).rglob("*")) if p.is_file()} if Path(path).exists() else None


class FindingNewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-finding-new-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "campaign"
        self.cli("init", "--directory", self.root, "--name", "Synthetic", "--county", "Synthetic County",
                 "--state", "ca")
        self.content = self.root / "content"
        (self.content / "findings").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLE / "site.json", self.content / "site.json")
        (self.content / "agencies.json").write_text(json.dumps([{"agency_id": AGENCY, "name": "Synthetic Sheriff"}]))
        (self.content / "sources.json").write_text(json.dumps([
            {"sha256": sha, "title": f"Synthetic record {n}", "agency_id": AGENCY,
             "received_at": "2026-01-20T16:30:00+00:00", "public_url": None, "fragment": "synthetic-records"}
            for n, sha in enumerate((SHA_A, SHA_B, TWIN_1, TWIN_2))]))
        self.drafts = self.root / "private" / "drafts"
        self.before = (tree(self.content), tree(self.root / "public"))

    def tearDown(self):
        self.assertEqual((tree(self.content), tree(self.root / "public")), self.before, "content/ or public/ changed")

    def cli(self, *arguments, returncode=0):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, returncode, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def new(self, title="Contract renewal not disclosed", sources=(SHA_A[:12], SHA_B), **kwargs):
        return findings.new_draft(self.root, kwargs.pop("agency", AGENCY), title, list(sources), **kwargs)

    def refused(self, pattern, **kwargs):
        with self.assertRaisesRegex(ValueError, pattern):
            self.new(**kwargs)
        self.assertEqual(sorted(os.listdir(self.drafts)) if self.drafts.exists() else [], [])

    def test_happy_path_skeleton_loads_and_lists_marker_blockers(self):
        out = self.cli("finding", "new", "--directory", self.root, "--agency", AGENCY, "--title",
                       "Contract renewal not disclosed", "--source", SHA_A[:12], "--source", SHA_B).stdout.splitlines()
        result = json.loads(out[0])
        self.assertIn("Nothing was published.", out[1])
        self.assertRegex(result["id"], r"\Afnd_[0-9a-f]{12}\Z")
        path = self.drafts / (result["id"] + ".json")
        self.assertEqual(result["written"], str(path.resolve()))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.drafts.stat().st_mode), 0o700)
        draft = findings.load_draft(path)
        self.assertLessEqual(FINDING_REQUIRED, set(draft))
        self.assertLessEqual(set(draft), FINDING_KEYS)
        self.assertEqual((draft["state"], draft["published_at"], draft["corrections"], draft["agency_id"]),
                         ("draft", None, [], AGENCY))
        self.assertEqual(draft["slug"], "contract-renewal-not-disclosed")
        self.assertEqual([(s["sha256"], s["title"]) for s in draft["sources"]],
                         [(SHA_A, "Synthetic record 0"), (SHA_B, "Synthetic record 1")])
        status = json.loads(self.cli("finding", "review-status", path, "--directory", self.root).stdout)
        self.assertEqual([b for b in status["review_blockers"] if b.startswith("edit_marker:")], MARKED)

    def test_editing_every_marked_field_clears_only_marker_blockers(self):
        path = Path(self.new()["written"])
        draft = findings.load_draft(path)
        before = findings.review_status(self.root, draft)["review_blockers"]
        edits = {"author": "Ada Lovelace", "body_md": "Body.", "classification": "documented_fact",
                 "confidence": "verified", "counterevidence": ["None found."], "event_date": "2026-01-02",
                 "limitations": ["Two records only."], "summary": "The records show a renewal."}
        draft.update(edits)
        draft["sources"][0]["locator"] = "page:1"
        self.assertEqual(review_blockers(draft, []).count("edit_marker:sources[1].locator"), 1)
        draft["sources"][1]["locator"] = "page:2"
        path.write_text(json.dumps(draft), encoding="utf-8")
        after = findings.review_status(self.root, findings.load_draft(path))["review_blockers"]
        self.assertEqual(after, [b for b in before if not b.startswith("edit_marker:") and b != "unknown_classification"])
        self.assertIn("need_two_independent_reviewers", after)
        for sneaky in ("[edit me: later]", "Done. [ EDIT  ME : tidy]"):  # bypass by case or spacing
            self.assertIn("edit_marker:summary", review_blockers(dict(draft, summary=sneaky), []))
        self.assertEqual(review_blockers(dict(draft, limitations=["[EDIT ME: a]", "[EDIT ME: b]"]), [])
                         .count("edit_marker:limitations"), 1)

    def test_unknown_agency_or_bad_source_prefix_refused_nothing_written(self):
        self.refused("--agency 'ca-nobody'", agency="ca-nobody")
        self.refused("d4d4d4d4d4d4: matches no", sources=["d4" * 6])
        self.refused("c3c3c3c3c3c3: is ambiguous", sources=["c3c3c3c3c3c3"])
        self.refused("'a1a1a1a1a1a': give 12-64", sources=[SHA_A[:11]])
        self.refused("'A1A1A1A1A1A1': give 12-64", sources=[SHA_A[:12].upper()])
        self.refused("'a1a1a1a1a1ag': give 12-64", sources=[SHA_A[:11] + "g"])
        self.refused("already cited", sources=[SHA_A[:12], SHA_A])
        result = self.cli("finding", "new", "--directory", self.root, "--agency", AGENCY, "--title", "T x",
                          "--source", "c3c3c3c3c3c3", returncode=1)
        self.assertTrue(result.stderr.startswith("Stopped: "), result.stderr)
        self.assertIn("c3c3c3c3c3c3", result.stderr)
        self.assertFalse(self.drafts.exists())

    def test_two_entries_sharing_one_sha256_are_ambiguous(self):
        # Same file listed twice (different fragment and title): exactly one entry is required, never "the last".
        library = json.loads((self.content / "sources.json").read_text())
        library.append(dict(library[0], title="Synthetic record 0, second fragment", fragment="other-records"))
        (self.content / "sources.json").write_text(json.dumps(library))
        self.before = (tree(self.content), tree(self.root / "public"))
        self.refused(f"--source {SHA_A[:12]}: is ambiguous \\(2 entries match\\)", sources=[SHA_A[:12]])
        self.refused(f"--source {SHA_A}: is ambiguous \\(2 entries match\\)", sources=[SHA_A])
        self.assertEqual([s["title"] for s in findings.load_draft(self.new(sources=[SHA_B])["written"])["sources"]],
                         ["Synthetic record 1"])

    def test_existing_id_skipped_and_five_collisions_refused(self):
        self.drafts.mkdir(parents=True, mode=0o700)
        taken = self.drafts / "fnd_aaaaaaaaaaaa.json"
        taken.write_bytes(b"organizer bytes\n")
        os.symlink(self.base / "outside.json", self.drafts / "fnd_cccccccccccc.json")  # dangling: not followed
        with mock.patch.object(findings.secrets, "token_hex", side_effect=["aaaaaaaaaaaa", "cccccccccccc", "bbbbbbbbbbbb"]):
            self.assertEqual(self.new()["id"], "fnd_bbbbbbbbbbbb")
        self.assertFalse((self.base / "outside.json").exists())
        listing = sorted(os.listdir(self.drafts))
        with mock.patch.object(findings.secrets, "token_hex", return_value="aaaaaaaaaaaa") as token:
            with self.assertRaisesRegex(ValueError, "5 generated ids already exist; nothing written"):
                self.new()
        self.assertEqual(token.call_count, 5)
        self.assertEqual(taken.read_bytes(), b"organizer bytes\n")
        self.assertEqual(sorted(os.listdir(self.drafts)), listing)

    def test_symlinked_private_or_drafts_refused_and_mode_is_0600(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.root / "private").mkdir(exist_ok=True)
        self.drafts.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "drafts: must be a real directory"):
            self.new()
        self.assertEqual(os.listdir(outside), [])
        self.drafts.unlink()
        moved = self.base / "private-real"
        (self.root / "private").rename(moved)
        (self.root / "private").symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "private: must be a real directory"):
            self.new()
        self.assertFalse((moved / "drafts").exists())
        (self.root / "private").unlink()
        moved.rename(self.root / "private")
        mask = os.umask(0)
        try:
            path = Path(self.new()["written"])
        finally:
            os.umask(mask)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_title_empty_too_long_or_reserved_slug_refused(self):
        for title, pattern in (("", "--title must not be empty"), ("   ", "--title must not be empty"),
                               ("x" * 201, "longer than 200"), ("About", "'about', which is reserved"),
                               ("Elsewhere", "'elsewhere', which is reserved"),
                               ("  Index!  ", "'index', which is reserved"), ("<b>Bold</b>", "angle brackets"),
                               ("2024 renewal", "start its slug with a letter"), ("!!!", "start its slug"),
                               ("a\nb", "control characters")):
            with self.subTest(title=title):
                self.refused(pattern, title=title)
        draft = findings.load_draft(self.new(title="Q" * 200)["written"])
        self.assertEqual(draft["slug"], "q" * 64)
        self.assertTrue(re.fullmatch(r"[a-z][a-z0-9-]{0,63}", draft["slug"]))

    def test_published_finding_with_marker_refused_by_build(self):
        good = {"id": "syn-new-1", "title": "T", "slug": "synthetic-new", "summary": "Real summary.",
                "classification": "documented_fact", "confidence": "verified", "event_date": "2026-01-02",
                "sources": [{"sha256": SHA_A, "locator": "page:1"}], "published_at": "2026-01-03T00:00:00Z",
                "corrections": [], "state": "published", "author": "Ada", "limitations": ["L."],
                "counterevidence": ["None found."]}
        load_finding(good, "good")  # the control publishes
        marked = dict(good, summary="[EDIT ME: one plain sentence]")
        with self.assertRaisesRegex(ContentError, r"edit_marker:summary"):
            load_finding(marked, "marked")
        target = self.content / "findings" / "synthetic-new.json"
        target.write_text(json.dumps(marked), encoding="utf-8")
        try:
            result = self.cli("build", "--directory", self.root, returncode=1)
            self.assertIn("edit_marker:summary", result.stderr)
        finally:
            target.unlink()


if __name__ == "__main__":
    unittest.main()
