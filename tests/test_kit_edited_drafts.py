"""kit re-runs keep organizer-edited drafts; fresh drafts go to requests.suggested/. Offline, synthetic."""
import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit  # noqa: E402

DRAFT = "ca-morro-bay-police.md"
OTHER = "ca-san-luis-obispo-county-sheriff.md"
UNREADABLE = "kit/drafts-written.json unreadable; treating differing drafts as edited\n"
KEPT = f"kept edited draft requests/{DRAFT}; fresh draft in requests.suggested/{DRAFT}\n"
NOT_SELECTED = f"kept edited draft requests/{DRAFT} (agency not selected)\n"


def cli(*arguments):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)],
                          cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class EditedDraftTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-edited-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        result = cli("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                     "--state", "CA")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.kit, self.outside = self.root / "kit", Path(temporary.name) / "outside"
        self.requests, self.suggested = self.kit / "requests", self.kit / "requests.suggested"
        self.outside.mkdir()
        (self.outside / "keep.md").write_text("organizer note\n")

    def build(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            summary = kit.build_kit(self.root)
        self.assertEqual(list(self.kit.rglob(".*.tmp")), [])
        self.assertEqual(json.loads((self.kit / "summary.json").read_text())["drafts_kept_edited"],
                         summary["drafts_kept_edited"])
        return summary, out.getvalue()

    def record(self):
        document = json.loads((self.kit / "drafts-written.json").read_text())
        self.assertEqual(document["schema_version"], 1)
        return document["drafts"]

    def edit(self, name=DRAFT):
        path = self.requests / name
        path.write_bytes(path.read_bytes() + b"Fee cap: 50\n")
        return path.read_bytes()

    def set_agency(self, agency_id, **fields):
        path = self.kit / "agencies.json"
        agencies = json.loads(path.read_text())
        for entry in agencies:
            if entry["agency_id"] == agency_id:
                entry.update(fields)
        path.write_text(json.dumps(agencies, indent=2) + "\n")

    def test_first_build_records_every_draft_hash(self):
        summary, out = self.build()
        drafts = sorted(p.name for p in self.requests.glob("*.md"))
        self.assertEqual(len(drafts), summary["requests_drafted"])
        self.assertEqual(self.record(), {name: sha(self.requests / name) for name in drafts})
        self.assertEqual((out, summary["drafts_kept_edited"]), ("", 0))
        self.assertEqual(list(self.suggested.iterdir()), [])

    def test_edited_selected_draft_kept_and_fresh_draft_beside_it(self):
        self.build()
        original, original_hash = (self.requests / DRAFT).read_bytes(), sha(self.requests / DRAFT)
        edited = self.edit()
        self.set_agency(OTHER[:-3], records_email="records@example.invalid")
        for _ in range(2):
            summary, out = self.build()
            self.assertEqual((out, summary["drafts_kept_edited"]), (KEPT, 1))
            self.assertEqual((self.requests / DRAFT).read_bytes(), edited)
            self.assertEqual((self.suggested / DRAFT).read_bytes(), original)
            self.assertEqual(sorted(p.name for p in self.suggested.iterdir()), [DRAFT])
            self.assertEqual(self.record()[DRAFT], original_hash)
            self.assertEqual(summary["requests_drafted"], len(self.record()))
        self.assertIn("records@example.invalid", (self.requests / OTHER).read_text())
        self.assertEqual(self.record()[OTHER], sha(self.requests / OTHER))

    def test_edited_then_deselected_draft_kept_with_record_and_suggestion(self):
        self.build()
        original_hash = sha(self.requests / DRAFT)
        edited = self.edit()
        self.build()
        self.set_agency(DRAFT[:-3], selected=False)
        summary, out = self.build()
        self.assertEqual((out, summary["drafts_kept_edited"]), (NOT_SELECTED, 1))
        self.assertEqual((self.requests / DRAFT).read_bytes(), edited)
        self.assertEqual(self.record()[DRAFT], original_hash)
        self.assertTrue((self.suggested / DRAFT).is_file())

    def test_unedited_deselected_draft_removed_and_dropped_from_record(self):
        self.build()
        self.set_agency(DRAFT[:-3], selected=False)
        summary, out = self.build()
        self.assertEqual((out, summary["drafts_kept_edited"]), ("", 0))
        self.assertFalse((self.requests / DRAFT).exists())
        self.assertNotIn(DRAFT, self.record())

    def test_organizer_note_kept_on_every_run(self):
        self.requests.mkdir(parents=True)
        note = self.requests / "notes.md"
        note.write_text("call the clerk first\n")
        for _ in range(3):
            summary, out = self.build()
            self.assertEqual(out, "kept requests/notes.md (not written by kit)\n")
            self.assertEqual((note.read_text(), summary["drafts_kept_edited"]), ("call the clerk first\n", 0))
            self.assertNotIn("notes.md", self.record())

    def test_reverted_edit_rewritten_and_suggestion_removed(self):
        self.build()
        original = (self.requests / DRAFT).read_bytes()
        self.edit()
        self.assertEqual(self.build()[1], KEPT)
        (self.suggested / "linked.md").symlink_to(self.outside / "keep.md")
        (self.requests / DRAFT).write_bytes(original)
        summary, out = self.build()
        self.assertEqual((out, summary["drafts_kept_edited"]), ("", 0))
        self.assertFalse((self.suggested / DRAFT).exists())
        self.assertTrue((self.suggested / "linked.md").is_symlink())
        self.assertEqual(((self.requests / DRAFT).read_bytes(), self.record()[DRAFT]),
                         (original, hashlib.sha256(original).hexdigest()))

    def test_missing_record_keeps_only_drafts_that_differ_from_fresh_render(self):
        self.build()
        untouched = (self.requests / OTHER).read_bytes()
        edited = self.edit()
        (self.kit / "drafts-written.json").unlink()
        for _ in range(2):
            summary, out = self.build()
            self.assertEqual((out, summary["drafts_kept_edited"]), (KEPT, 1))
            self.assertEqual((self.requests / DRAFT).read_bytes(), edited)
            self.assertEqual((self.requests / OTHER).read_bytes(), untouched)
            self.assertEqual(self.record()[OTHER], sha(self.requests / OTHER))
            self.assertNotIn(DRAFT, self.record())

    def test_missing_record_deselected_drafts(self):
        self.build()
        edited = self.edit()
        (self.kit / "drafts-written.json").unlink()
        self.set_agency(DRAFT[:-3], selected=False)
        self.set_agency(OTHER[:-3], selected=False)
        _, out = self.build()
        self.assertEqual(out, f"kept requests/{DRAFT} (not written by kit)\n")
        self.assertEqual((self.requests / DRAFT).read_bytes(), edited)
        self.assertFalse((self.requests / OTHER).exists())

    def test_bad_record_named_once_and_rewritten(self):
        self.build()
        for content in (b"[]", b"{", b'{"schema_version": 2, "drafts": {}}', b"\xff",
                        b'{"schema_version": 1, "drafts": {"x.md": 1}}', b'{"schema_version": "1", "drafts": {}}'):
            (self.kit / "drafts-written.json").write_bytes(content)
            summary, out = self.build()
            self.assertEqual((out, summary["drafts_kept_edited"]), (UNREADABLE, 0), content)
            self.assertEqual(len(self.record()), summary["requests_drafted"])
        edited = self.edit()
        (self.kit / "drafts-written.json").write_text("{")
        self.assertEqual(self.build()[1], UNREADABLE + KEPT)
        self.assertEqual((self.requests / DRAFT).read_bytes(), edited)

    def test_linked_files_unchanged(self):
        self.build()
        (self.requests / "old.md").symlink_to(self.outside / "keep.md")
        (self.requests / DRAFT).unlink()
        (self.requests / DRAFT).symlink_to(self.outside / "keep.md")
        summary, out = self.build()
        self.assertEqual((out, summary["drafts_kept_edited"]), ("kept linked file requests/old.md\n", 0))
        self.assertFalse((self.requests / DRAFT).is_symlink())
        self.assertEqual(self.record()[DRAFT], sha(self.requests / DRAFT))
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ["keep.md"])
        self.assertEqual((self.outside / "keep.md").read_text(), "organizer note\n")

    def test_linked_or_file_suggested_folder_refused_draft_untouched(self):
        self.build()
        edited = self.edit()
        self.suggested.rmdir()
        for make, message in ((lambda: self.suggested.symlink_to(self.outside, target_is_directory=True),
                               "requests.suggested/ must not be a symlink"),
                              (lambda: self.suggested.write_text("x\n"), "requests.suggested/ is not a folder")):
            make()
            result = cli("kit", "--directory", self.root)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Stopped: " + message, result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual((self.requests / DRAFT).read_bytes(), edited)
            self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ["keep.md"])
            self.suggested.unlink()

    def test_failed_suggested_write_keeps_edited_draft_and_no_temp(self):
        self.build()
        edited = self.edit()
        before = self.record()
        real_replace = kit.os.replace

        def failing(src, dst):
            if Path(dst).parent.name == "requests.suggested":
                raise OSError("synthetic replace failure")
            return real_replace(src, dst)

        with mock.patch("os.replace", side_effect=failing), self.assertRaises(OSError), \
                contextlib.redirect_stdout(io.StringIO()):
            kit.build_kit(self.root)
        self.assertEqual((self.requests / DRAFT).read_bytes(), edited)
        self.assertEqual((list(self.kit.rglob(".*.tmp")), self.record()), ([], before))
        self.assertFalse((self.suggested / DRAFT).exists())


if __name__ == "__main__":
    unittest.main()
