"""content-check: every content/ problem in one pass, same rules as build (synthetic fixtures only)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from campaign_tool.content import ContentError, load_content
from campaign_tool.content_check import content_problems

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
FINDING = "content/findings/cedar-policy-posted.json"
MEETING = "content/meetings/2026-10-cedar-council.json"
EMAIL = "planted.person@example.invalid"


class ContentCheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def read(self, rel):
        return json.loads((self.root / rel).read_text())

    def write(self, rel, value):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value))

    def finding(self, name, **changes):
        self.write(f"content/findings/{name}", {**self.read(FINDING), **changes})

    def cli(self, root=None):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "content-check",
                               "--directory", str(root or self.root)],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)

    def load_error(self):
        try:
            load_content(self.root)
        except ContentError as exc:
            return str(exc)
        return None

    def tree(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))

    def test_example_is_clean_and_build_loads_it(self):
        self.assertEqual(content_problems(self.root), [])
        self.assertIsNotNone(load_content(self.root))
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["content/: 0 problem(s)"])

    def test_every_broken_file_named_in_one_pass_and_nothing_written(self):
        self.write("content/findings/a-bad-json.json", "{not json")
        self.finding("b-draft.json", id="fnd_b", slug="b-draft", state="draft")
        self.finding("c-agency.json", id="fnd_c", slug="c-agency", agency_id="Not An Id")
        self.write("content/meetings/d-meeting.json", {**self.read(MEETING), "status": "postponed"})
        before = self.tree()
        problems = content_problems(self.root)
        self.assertEqual(len(problems), 4, problems)
        for label, line in zip(["findings/a-bad-json.json: not valid UTF-8 JSON",
                                "findings/b-draft.json: state is 'draft'",
                                "findings/c-agency.json.agency_id: invalid agency identifier",
                                "meetings/d-meeting.json.status: must be scheduled or cancelled"], problems):
            self.assertTrue(line.startswith(label), line)
        result = self.cli()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["content/: 4 problem(s)", *problems])
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.root / "public").exists())
        self.assertFalse((self.root / "private").exists())

    def test_duplicate_slug_or_id_uses_load_content_text(self):
        for name, changes in (("z-same-slug.json", {"id": "fnd_other"}),
                              ("z-same-id.json", {"slug": "another-slug"})):
            with self.subTest(name=name):
                self.finding(name, **changes)
                expected = f"findings/{name}: duplicate finding slug or id"
                self.assertEqual(content_problems(self.root), [expected])
                self.assertEqual(self.load_error(), expected)
                (self.root / "content/findings" / name).unlink()
        self.assertEqual(content_problems(self.root), [])

    def test_missing_site_json_does_not_stop_the_other_checks(self):
        (self.root / "content/site.json").unlink()
        self.finding("x-bad.json", id="fnd_x", slug="x-bad", confidence="maybe")
        self.write("content/sources.json", {"not": "a list"})
        problems = content_problems(self.root)
        self.assertIn("content/site.json is required when content/ exists", problems)
        self.assertIn("findings/x-bad.json.confidence: choose one of ['verified', 'likely', 'needs_attorney_review']",
                      problems)
        self.assertIn("sources.json: must be a list of library entries", problems)
        self.assertEqual(len(problems), 3, problems)

    def test_symlink_and_oversize_files_are_named_not_read(self):
        outside = Path(self._tmp.name) / "outside.json"
        outside.write_text(json.dumps({**self.read(FINDING), "id": "fnd_link", "slug": "linked", "title": EMAIL}))
        os.symlink(outside, self.root / "content/findings/link.json")
        self.write("content/findings/big.json", " " * (2 * 1024 * 1024 + 1))
        self.write("content/map/cities.json", " " * (2 * 1024 * 1024 + 1))
        problems = content_problems(self.root)
        self.assertEqual(problems, ["cities.json: file too large",
                                    "findings/big.json: file too large",
                                    "findings/link.json: symlinks are not read"])
        result = self.cli()
        self.assertEqual(result.returncode, 3)
        self.assertNotIn(EMAIL, result.stdout + result.stderr)

    def test_map_fallback_named_and_skipped_when_agencies_broken(self):
        agencies = self.read("content/agencies.json")
        for entry in agencies:
            if entry.get("slug") == "cedar":
                entry["kind"] = "sheriff"
        self.write("content/agencies.json", agencies)
        expected = ("map/map-config.json.bounding_box_fallbacks: 'cedar' is not a police/city_council "
                    "agency slug")
        self.assertEqual(content_problems(self.root), [expected])
        self.assertEqual(self.load_error(), expected)
        self.write("content/agencies.json", "[broken")
        problems = content_problems(self.root)
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(problems[0].startswith("content/agencies.json: not valid UTF-8 JSON"), problems)
        (self.root / "content/agencies.json").unlink()  # no kit/ either: an empty list still gets checked
        self.assertEqual(content_problems(self.root), [expected])
        self.assertEqual(self.load_error(), expected)

    def test_parity_with_load_content_for_single_defects(self):
        def bad_json():
            self.write(FINDING, "{")

        def wrong_state():
            self.write(FINDING, {**self.read(FINDING), "state": "retracted"})

        def no_site():
            (self.root / "content/site.json").unlink()

        def symlinked_meeting():
            target = self.root / MEETING
            moved = Path(self._tmp.name) / "meeting.json"
            target.rename(moved)
            os.symlink(moved, target)

        def sources_not_list():
            self.write("content/sources.json", "{}")

        def agency_kind():
            self.write("content/agencies.json", [{**self.read("content/agencies.json")[0], "kind": "army"}])

        def fallback_without_bounds():
            (self.root / "content/map/cities.json").unlink()

        def meeting_filename_slug():
            meeting = self.read(MEETING)
            del meeting["id"]
            (self.root / MEETING).unlink()
            self.write("content/meetings/Bad_Name.json", meeting)

        def cameras_missing():
            (self.root / "content/map/cameras.geojson").unlink()

        defects = [bad_json, wrong_state, no_site, symlinked_meeting, sources_not_list, agency_kind,
                   fallback_without_bounds, meeting_filename_slug, cameras_missing]
        for defect in [None, *defects]:
            with self.subTest(defect=getattr(defect, "__name__", "clean")):
                shutil.rmtree(self.root)
                shutil.copytree(EXAMPLE, self.root)
                if defect:
                    defect()
                problems = content_problems(self.root)
                error = self.load_error()
                self.assertEqual(bool(problems), error is not None, (problems, error))
                self.assertEqual(bool(problems), defect is not None)

    def test_many_broken_files_are_capped_at_200_lines(self):
        for index in range(250):
            self.write(f"content/findings/bad-{index:03}.json", "{")
        problems = content_problems(self.root)
        self.assertEqual(len(problems), 201)
        self.assertEqual(problems[-1], "\u2026 and 50 more")
        self.assertEqual(len(content_problems(self.root, limit=None)), 250)
        result = self.cli()
        self.assertEqual(result.returncode, 3)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "content/: 250 problem(s)")
        self.assertEqual(lines[1:], problems)

    def test_no_content_directory_exits_zero_and_creates_nothing(self):
        shutil.rmtree(self.root / "content")
        before = self.tree()
        self.assertIsNone(content_problems(self.root))
        result = self.cli()
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "no content/ directory"))
        self.assertEqual(self.tree(), before)
        missing = Path(self._tmp.name) / "absent"
        result = self.cli(missing)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "no content/ directory"))
        self.assertFalse(missing.exists())

    def test_planted_value_never_printed_beyond_loader_message(self):
        self.finding("p-email.json", id="fnd_p", slug="p-email", agency_id=EMAIL)
        self.write("content/sources.json", [{**self.read("content/sources.json")[0], "agency_id": EMAIL}])
        problems = content_problems(self.root)
        self.assertEqual(problems, ["findings/p-email.json.agency_id: invalid agency identifier",
                                    "sources.json[0].agency_id: invalid agency identifier"])
        result = self.cli()
        self.assertEqual(result.returncode, 3)
        self.assertNotIn(EMAIL, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
