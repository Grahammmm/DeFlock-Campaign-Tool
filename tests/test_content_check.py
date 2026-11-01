"""content-check: every content/ problem in one pass, with exactly the rules load_content enforces."""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import content
from campaign_tool.content_check import content_check_cmd, content_problems

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign" / "content"
FINDING = "cedar-policy-posted.json"
MEETING = "2026-10-cedar-council.json"


class ContentCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-content-check-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "campaign"
        self.content = self.root / "content"
        shutil.copytree(EXAMPLE, self.content)

    # --- helpers -----------------------------------------------------------
    def read(self, relative):
        return json.loads((self.content / relative).read_text(encoding="utf-8"))

    def write(self, relative, document):
        path = self.content / relative
        path.write_text(document if isinstance(document, str) else json.dumps(document), encoding="utf-8")
        return path

    def finding_copy(self, name, **changes):
        document = self.read("findings/" + FINDING)
        stem = name.removesuffix(".json")
        document.update(slug=stem, id="fnd_" + stem.replace("-", "_"))
        document.update(changes)
        self.write("findings/" + name, document)

    def load_raised(self):
        try:
            content.load_content(self.root)
        except Exception as exc:  # noqa: BLE001 - parity means "raised anything"
            return exc
        return None

    def run_cmd(self):
        out = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out):
            try:
                content_check_cmd(self.root)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue().splitlines()

    def run_cli(self, root):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "content-check", "--directory", str(root)],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)

    def tree(self):
        return sorted(str(p.relative_to(self.base)) for p in self.base.rglob("*"))

    # --- edge paths --------------------------------------------------------
    def test_1_clean_example_has_no_problems_and_loads(self):
        before = self.tree()
        self.assertEqual(content_problems(self.root), [])
        self.assertIsNone(self.load_raised())
        result = self.run_cli(self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["content/: 0 problem(s)"])
        self.assertEqual(self.tree(), before)  # read-only: no public/ or private/
        self.assertFalse((self.root / "public").exists())
        self.assertFalse((self.root / "private").exists())

    def test_2_four_broken_files_named_in_one_pass_exit_3(self):
        self.write("findings/a-bad-json.json", "{not json")
        self.finding_copy("b-draft.json", state="draft")
        self.finding_copy("c-agency.json", agency_id="Not An Agency!")
        meeting = self.read("meetings/" + MEETING)
        meeting["status"] = "maybe"
        self.write("meetings/" + MEETING, meeting)
        problems = content_problems(self.root)
        self.assertEqual(len(problems), 4, problems)
        for problem, label in zip(problems, ["findings/a-bad-json.json:", "findings/b-draft.json:",
                                             "findings/c-agency.json.agency_id:", f"meetings/{MEETING}.status:"]):
            self.assertTrue(problem.startswith(label), problem)
        self.assertEqual(problems, sorted(problems))
        result = self.run_cli(self.root)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["content/: 4 problem(s)", *problems])
        self.assertTrue(any(p.endswith(str(self.load_raised())) for p in problems))  # build's first error

    def test_3_duplicate_slug_uses_load_content_text(self):
        document = self.read("findings/" + FINDING)
        document["id"] = "fnd_other"
        self.write("findings/zz-copy.json", document)
        problems = content_problems(self.root)
        self.assertEqual(problems, ["findings/zz-copy.json: duplicate finding slug or id"])
        self.assertEqual(str(self.load_raised()), problems[0])

    def test_3b_duplicate_id_and_invalid_twin_do_not_count(self):
        document = self.read("findings/" + FINDING)
        document["slug"] = "zz-same-id"
        self.write("findings/zz-same-id.json", document)
        document.update(slug="zz-invalid", state="draft")  # not loaded, so never a duplicate
        self.write("findings/zz-invalid.json", document)
        problems = content_problems(self.root)
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(problems[0].startswith("findings/zz-invalid.json: state is 'draft'"), problems)
        self.assertEqual(problems[1], "findings/zz-same-id.json: duplicate finding slug or id")

    def test_4_missing_site_does_not_stop_the_pass(self):
        (self.content / "site.json").unlink()
        self.write("findings/a-bad-json.json", "[")
        self.write("sources.json", {"not": "a list"})
        problems = content_problems(self.root)
        self.assertEqual(len(problems), 3, problems)
        self.assertEqual(problems[0], "content/site.json is required when content/ exists")
        self.assertTrue(problems[1].startswith("findings/a-bad-json.json: not valid UTF-8 JSON"), problems)
        self.assertEqual(problems[2], "sources.json: must be a list of library entries")
        self.assertEqual(str(self.load_raised()), "content/site.json is required when content/ exists")

    def test_5_symlink_and_oversize_named_and_link_never_read(self):
        target = self.base / "outside.json"
        target.write_text("{this would fail as JSON if it were read", encoding="utf-8")
        os.symlink(target, self.content / "findings" / "link.json")
        oversize = self.content / "meetings" / "huge.json"
        oversize.write_text(" " * (content.MAX_JSON_BYTES + 1), encoding="utf-8")
        problems = content_problems(self.root)
        self.assertEqual(problems, ["findings/link.json: symlinks are not read", "meetings/huge.json: file too large"])
        self.assertFalse(any("not valid" in p for p in problems))

    def test_6_fallback_not_a_city_agency_is_named(self):
        agencies = self.read("agencies.json")
        for agency in agencies:
            if agency.get("slug") == "cedar":
                agency["kind"] = "sheriff"
        self.write("agencies.json", agencies)
        problems = content_problems(self.root)
        expected = "map/map-config.json.bounding_box_fallbacks: 'cedar' is not a police/city_council agency slug"
        self.assertEqual(problems, [expected])
        self.assertEqual(str(self.load_raised()), expected)

    def test_6b_broken_agencies_skips_fallback_check(self):
        self.write("agencies.json", {"not": "a list"})
        self.assertEqual(content_problems(self.root), ["content/agencies.json: must be a list of agencies"])

    def test_6c_broken_map_skips_fallback_check(self):
        agencies = [a for a in self.read("agencies.json") if a.get("slug") != "cedar"]
        self.write("agencies.json", agencies)
        (self.content / "map" / "cameras.geojson").unlink()
        problems = content_problems(self.root)
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(problems[0].startswith("map/cameras.geojson is missing"), problems)

    def test_7_parity_sweep_single_defects(self):
        def bad_json(t): t.write("findings/x.json", "{")
        def draft(t): t.finding_copy("x.json", state="draft")
        def agency(t): t.finding_copy("x.json", agency_id="BAD ID")
        def meeting(t): t.write("meetings/x.json", {"status": "maybe"})
        def no_site(t): (t.content / "site.json").unlink()
        def sources(t): t.write("sources.json", {"a": 1})
        def agencies(t): t.write("agencies.json", "[{")
        def duplicate(t): shutil.copy(t.content / "findings" / FINDING, t.content / "findings" / "zz.json")
        def map_config(t):
            config = t.read("map/map-config.json")
            config["bounding_box_fallbacks"] = "cedar"
            t.write("map/map-config.json", config)
        def site_field(t):
            site = t.read("site.json")
            site["schema_version"] = 99
            t.write("site.json", site)
        defects = [bad_json, draft, agency, meeting, no_site, sources, agencies, duplicate, map_config, site_field]
        for defect in [None, *defects]:
            with self.subTest(defect=getattr(defect, "__name__", "clean")):
                shutil.rmtree(self.content)
                shutil.copytree(EXAMPLE, self.content)
                if defect:
                    defect(self)
                problems = content_problems(self.root)
                raised = self.load_raised()
                self.assertEqual(bool(problems), raised is not None, (problems, raised))
                self.assertEqual(bool(problems), defect is not None)
                if isinstance(raised, content.ContentError):
                    self.assertTrue(any(p.endswith(str(raised)) for p in problems), (problems, raised))

    def test_8_cap_at_200_lines(self):
        for index in range(250):
            self.write(f"findings/bad-{index:03d}.json", "{")
        problems = content_problems(self.root)
        self.assertEqual(len(problems), 201)
        self.assertEqual(problems[-1], "… and 50 more")
        code, lines = self.run_cmd()
        self.assertEqual(code, 3)
        self.assertEqual(lines[0], "content/: 250 problem(s)")
        self.assertEqual(lines[1:], problems)

    def test_9_no_content_directory_exit_0_creates_nothing(self):
        missing = self.base / "nowhere"
        self.assertIsNone(content_problems(missing))
        result = self.run_cli(missing)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "no content/ directory")
        self.assertFalse(missing.exists())
        shutil.rmtree(self.content)
        code, lines = self.run_cmd()
        self.assertEqual((code, lines), (0, ["no content/ directory"]))
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [])

    def test_10_field_value_never_printed_beyond_loader_message(self):
        planted = "leak-person@example.org"
        document = self.read("findings/" + FINDING)
        document["confidence"] = planted
        self.write("findings/" + FINDING, document)
        problems = content_problems(self.root)
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(problems[0].startswith(f"findings/{FINDING}.confidence: choose one of"))
        self.assertEqual(str(self.load_raised()), problems[0])
        code, lines = self.run_cmd()
        self.assertEqual(code, 3)
        self.assertNotIn(planted, "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
