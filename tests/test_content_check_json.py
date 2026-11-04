"""content-check --json: the content-check-v1 document (synthetic fixtures only; text form covered elsewhere)."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from campaign_tool import law
from campaign_tool.content_check import LIMIT, problem_record

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SCHEMA = json.loads((REPO_ROOT / "schemas" / "content-check.schema.json").read_text(encoding="utf-8"))
FINDING = "content/findings/cedar-policy-posted.json"
TOP_KEYS = {"schema", "count", "problems", "truncated", "law_package", "no_content"}


class ContentCheckJsonTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="synthetic-content-check-json-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root)

    def write(self, rel, value):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")

    def drafts(self, count):
        base = json.loads((self.root / FINDING).read_text(encoding="utf-8"))
        for index in range(count):
            self.write(f"content/findings/d{index:03d}.json",
                       {**base, "id": f"fnd_d{index}", "slug": f"d{index}", "state": "draft"})

    def cli(self, *flags, root=None):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "content-check", *flags,
                               "--directory", str(root or self.root)],
                              cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120)

    def document(self, result):
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(result.stdout.splitlines()), 1, result.stdout)  # single JSON line, nothing else
        return json.loads(result.stdout)

    def text_lines(self):
        """(exit code, count, problem lines after the count and optional package line) of the text form."""
        result = self.cli()
        lines = result.stdout.splitlines()
        count = int(lines[0].split()[1])
        rest = [line for line in lines[1:] if not line.startswith("law package ")]
        return result.returncode, count, rest

    def test_parity_with_text_for_clean_two_problem_and_205_problem_trees(self):
        for name, setup, code in (("clean", lambda: None, 0),
                                  ("two", lambda: (self.write("content/findings/a-bad.json", "{not json"),
                                                   self.write("content/meetings/d.json", {"status": "postponed"})), 3),
                                  ("many", lambda: self.drafts(205), 3)):
            with self.subTest(name=name):
                setup()
                exit_text, count, lines = self.text_lines()
                result = self.cli("--json")
                doc = self.document(result)
                self.assertEqual((result.returncode, exit_text), (code, code))
                self.assertEqual(doc["count"], count)
                rejoined = [r["message"] if r["file"] is None else r["file"] + ": " + r["message"]
                            for r in doc["problems"]]
                self.assertEqual(rejoined, [line for line in lines if not line.startswith("\u2026 and ")])
                self.assertEqual(doc["law_package"], "law package us-ca: draft")
                self.assertEqual(doc["schema"], "content-check-v1")
                shutil.rmtree(self.root)
                shutil.copytree(EXAMPLE, self.root)

    def test_205_problems_give_200_records_truncated_and_full_count(self):
        self.drafts(205)
        result = self.cli("--json")
        doc = self.document(result)
        self.assertEqual((result.returncode, doc["count"], len(doc["problems"]), doc["truncated"]),
                         (3, 205, LIMIT, True))
        self.assertEqual(doc["problems"][0], {"file": "findings/d000.json",
                                                "message": "state is 'draft'; only published findings are rendered"})

    def test_exactly_the_limit_is_not_truncated(self):
        self.drafts(LIMIT)
        doc = self.document(self.cli("--json"))
        self.assertEqual((doc["count"], len(doc["problems"]), doc["truncated"]), (LIMIT, LIMIT, False))

    def test_line_without_a_path_prefix_has_no_file(self):
        (self.root / "content/map/cameras.geojson").unlink()
        doc = self.document(self.cli("--json"))
        missing = [r for r in doc["problems"] if r["message"].startswith("map/cameras.geojson is missing")]
        self.assertEqual(len(missing), 1, doc)
        self.assertIsNone(missing[0]["file"])

    def test_problem_record_never_splits_a_line_that_names_no_file(self):
        for line in ("map/cameras.geojson is missing; run tools/fetch_osm_alprs.py or supply reviewed data",
                     "content/site.json is required when content/ exists",
                     "something else: entirely", "findings/x.json is odd: because", "", "no colon"):
            with self.subTest(line=line):
                self.assertEqual(problem_record(line), {"file": None, "message": line})
        self.assertEqual(problem_record("map/map-config.json.county_bounds: bad"),
                         {"file": "map/map-config.json.county_bounds", "message": "bad"})
        self.assertEqual(problem_record("supporters.json: ab: no consent record"),
                         {"file": "supporters.json", "message": "ab: no consent record"})  # DF2207-13 prefix
        self.assertEqual(problem_record("public-assets/a.png: has metadata: EXIF"),
                         {"file": "public-assets/a.png", "message": "has metadata: EXIF"})

    def test_non_ascii_text_is_not_escaped(self):
        self.write("content/findings/se\u00f1or.json", "{not json")
        result = self.cli("--json")
        self.assertIn("se\u00f1or.json", result.stdout)
        self.assertNotIn("\\u00f1", result.stdout)
        self.assertEqual(self.document(result)["problems"][0]["file"], "findings/se\u00f1or.json")

    def test_unreadable_campaign_json_gives_null_package_and_a_listed_problem(self):
        (self.root / "campaign.json").write_text("{", encoding="utf-8")
        finding = json.loads((self.root / FINDING).read_text(encoding="utf-8"))
        self.write(FINDING, {**finding, "rule_ids": ["ca-open"]})  # campaign.json is read only when a rule is cited
        result = self.cli("--json")
        doc = self.document(result)
        self.assertEqual(result.returncode, 3)
        self.assertIsNone(doc["law_package"])
        self.assertTrue(any((r["file"] or "").startswith("campaign.json") for r in doc["problems"]), doc)
        self.assertEqual(doc["count"], len(doc["problems"]))

    def test_no_campaign_json_gives_null_package(self):
        (self.root / "campaign.json").unlink()
        doc = self.document(self.cli("--json"))
        self.assertIsNone(doc["law_package"])

    def test_no_content_directory_exits_zero_with_no_content_flag(self):
        shutil.rmtree(self.root / "content")
        result = self.cli("--json")
        doc = self.document(result)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(doc, {"schema": "content-check-v1", "count": 0, "problems": [], "truncated": False,
                               "law_package": None, "no_content": True})
        law.validate(doc, SCHEMA)
        self.assertEqual(self.cli().stdout, "no content/ directory\n")

    def test_json_writes_nothing_and_keys_are_sorted(self):
        self.write("content/findings/a-bad.json", "{not json")
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        result = self.cli("--json")
        self.assertEqual(sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")), before)
        doc = self.document(result)
        self.assertEqual(result.stdout.strip(), json.dumps(doc, sort_keys=True, ensure_ascii=False))

    def test_text_form_is_unchanged_without_the_flag(self):
        result = self.cli()
        self.assertEqual(result.stdout.splitlines(), ["content/: 0 problem(s)", "law package us-ca: draft"])

    def test_schema_keys_and_types_match_the_emitted_documents(self):
        self.assertIs(SCHEMA["additionalProperties"], False)
        self.assertIs(SCHEMA["properties"]["problems"]["items"]["additionalProperties"], False)
        self.assertEqual(set(SCHEMA["properties"]), TOP_KEYS)
        self.assertEqual(set(SCHEMA["required"]), TOP_KEYS - {"no_content"})
        record = SCHEMA["properties"]["problems"]["items"]
        self.assertEqual(set(record["properties"]), {"file", "message"})
        self.write("content/findings/a-bad.json", "{not json")
        problem_doc = self.document(self.cli("--json"))
        shutil.rmtree(self.root / "content")
        empty_doc = self.document(self.cli("--json"))
        self.assertEqual(set(problem_doc), TOP_KEYS - {"no_content"})
        self.assertEqual(set(empty_doc), TOP_KEYS)
        for doc in (problem_doc, empty_doc):
            law.validate(doc, SCHEMA)
        self.assertEqual(set(problem_doc["problems"][0]), set(record["properties"]))
        for broken in ({**problem_doc, "extra": 1}, {**problem_doc, "count": "1"},
                       {**problem_doc, "truncated": 0}, {**problem_doc, "schema": "content-check-v2"},
                       {**problem_doc, "problems": [{"file": 1, "message": "m"}]},
                       {**problem_doc, "problems": [{"file": None, "message": "m", "x": 1}]},
                       {k: v for k, v in problem_doc.items() if k != "law_package"}):
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                law.validate(broken, SCHEMA)


if __name__ == "__main__":
    unittest.main()
