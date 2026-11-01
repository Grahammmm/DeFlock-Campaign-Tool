"""Tests for tools/check_law_review.py on in-test copies of the us-ca package (unittest)."""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL = REPO_ROOT / "tools" / "check_law_review.py"
PACKAGE = REPO_ROOT / "jurisdictions" / "us-ca" / "package.json"
NOTE = "structural check only; it does not review the law or confirm reviewer independence"
TODAY = date.today().isoformat()


def base_package():
    return json.loads(PACKAGE.read_text(encoding="utf-8"))


def reviewed(package, names=("reviewer-a",), when=TODAY):
    package["status"], package["reviewed_by"], package["reviewed_at"] = "reviewed", list(names), when
    return package


class CheckLawReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.old = base_package()
        self.ids = [rule["rule_id"] for rule in self.old["rules"]]

    def write(self, name, document):
        path = self.dir / name
        path.write_bytes(document if isinstance(document, bytes) else json.dumps(document).encode())
        return path

    def invoke(self, old_path, new_path):
        return subprocess.run([sys.executable, "-B", str(TOOL), str(old_path), str(new_path)],
                              capture_output=True, text=True, cwd=REPO_ROOT, check=False)

    def check(self, new, old=None, code=None):
        done = self.invoke(self.write("old.json", old or self.old), self.write("new.json", new))
        self.assertEqual(done.stderr, "")
        result = json.loads(done.stdout)
        self.assertEqual(result["note"], NOTE)
        self.assertEqual(done.returncode, 0 if result["ok"] else 3)
        if code is not None:
            self.assertEqual(done.returncode, code, done.stdout)
        return result

    def refused(self, old_path, new_path):
        done = self.invoke(old_path, new_path)
        self.assertEqual(done.returncode, 2)
        self.assertEqual(done.stdout, "")
        self.assertNotIn("Traceback", done.stderr)
        self.assertEqual(len(done.stderr.strip().splitlines()), 1, done.stderr)
        self.assertTrue(done.stderr.startswith("check_law_review: error: "))
        return done

    # 1. A plain draft -> reviewed change with two review labels.
    def test_review_labels_and_status_are_allowed(self):
        new = reviewed(copy.deepcopy(self.old))
        new["rules"][0]["review"] = "verified"
        new["rules"][2]["review"] = "likely"
        result = self.check(new, code=0)
        self.assertEqual(sorted(result["allowed_changes"]), sorted([
            "$.status", "$.reviewed_by", "$.reviewed_at",
            f"$.rules[{self.ids[0]}].review", f"$.rules[{self.ids[2]}].review"]))
        self.assertEqual((result["disallowed"], result["errors"], result["downgrade"]), ([], [], False))

    # 2. Rule text corrections are allowed; citation and sources are not.
    def test_rule_text_allowed_citation_and_sources_disallowed(self):
        new = copy.deepcopy(self.old)
        new["rules"][1]["duty"] += " Corrected."
        new["rules"][1]["remedy"] += " Corrected."
        new["rules"][1]["exceptions"] = ["A corrected exception."]
        result = self.check(new, code=0)
        self.assertEqual(sorted(result["allowed_changes"]),
                         [f"$.rules[{self.ids[1]}].{f}" for f in ("duty", "exceptions", "remedy")])
        new["rules"][1]["citation"] = "Civ. Code § 9999"
        new["rules"][3]["sources"][0]["url"] = "https://example.org/other"
        result = self.check(new, code=3)
        self.assertEqual(result["disallowed"], [f"$.rules[{self.ids[1]}].citation",
                                                f"$.rules[{self.ids[3]}].sources[0].url"])

    def test_other_rule_fields_disallowed(self):
        for field, value in (("actor", "someone"), ("activity", "something"),
                             ("effective_from", "2001-01-01"), ("effective_to", "2099-01-01")):
            new = copy.deepcopy(self.old)
            new["rules"][4][field] = value
            self.assertEqual(self.check(new, code=3)["disallowed"], [f"$.rules[{self.ids[4]}].{field}"])

    def test_removing_an_allowed_field_is_disallowed(self):
        new = copy.deepcopy(self.old)
        del new["rules"][0]["review"]
        result = self.check(new, code=3)
        self.assertEqual(result["disallowed"], [f"$.rules[{self.ids[0]}].review"])
        self.assertTrue(result["errors"])

    # 3. Rule membership and order.
    def test_rule_added_removed_or_reordered(self):
        added = copy.deepcopy(self.old)
        added["rules"].append(dict(added["rules"][0], rule_id="ca-new-rule"))
        self.assertEqual(self.check(added, code=3)["disallowed"], ["$.rules[ca-new-rule] (added)"])
        removed = copy.deepcopy(self.old)
        del removed["rules"][-1]
        self.assertEqual(self.check(removed, code=3)["disallowed"], [f"$.rules[{self.ids[-1]}] (removed)"])
        swapped = copy.deepcopy(self.old)
        swapped["rules"][0], swapped["rules"][1] = swapped["rules"][1], swapped["rules"][0]
        self.assertEqual(self.check(swapped, code=3)["disallowed"], ["$.rules (order)"])

    def test_rule_id_rename_is_remove_plus_add(self):
        new = copy.deepcopy(self.old)
        new["rules"][0]["rule_id"] = "ca-renamed"
        result = self.check(new, code=3)
        self.assertEqual(result["disallowed"], ["$.rules[ca-renamed] (added)",
                                                f"$.rules[{self.ids[0]}] (removed)"])
        self.assertIn("unknown rule id", result["errors"][0])  # request_scopes still name it

    def test_duplicate_rule_id_falls_back_to_position_and_errors(self):
        new = copy.deepcopy(self.old)
        new["rules"][1]["rule_id"] = self.ids[0]
        result = self.check(new, code=3)
        self.assertEqual(result["disallowed"], ["$.rules[1].rule_id"])
        self.assertIn("duplicate", result["errors"][0])

    # 4. A reviewed package needs real reviewers and a date not after today.
    def test_reviewed_needs_reviewers_and_past_date(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        cases = (([], TODAY), (["a", "a"], TODAY), (["a", "a "], TODAY), ([" "], TODAY),
                 (["a", ""], TODAY), (["reviewer-a"], tomorrow), (["reviewer-a"], None))
        for names, when in cases:
            with self.subTest(names=names, when=when):
                result = self.check(reviewed(copy.deepcopy(self.old), names, when), code=3)
                self.assertEqual(len(result["errors"]), 1, result["errors"])
                self.assertEqual(result["disallowed"], [])

    def test_status_wording_bypass_is_a_schema_error(self):
        new = reviewed(copy.deepcopy(self.old))
        new["status"] = "Reviewed"
        self.assertIn("$.status", self.check(new, code=3)["errors"][0])

    # 5. records_law, schema_version and other top-level fields.
    def test_records_law_and_schema_version_disallowed(self):
        new = copy.deepcopy(self.old)
        new["records_law"]["determination_days"] += 1
        result = self.check(new, code=3)
        self.assertEqual((result["disallowed"], result["errors"]), (["$.records_law.determination_days"], []))
        new = copy.deepcopy(self.old)
        new["schema_version"] = 2
        result = self.check(new, code=3)
        self.assertEqual(result["disallowed"], ["$.schema_version"])
        self.assertIn("$.schema_version", result["errors"][0])

    def test_scopes_jurisdiction_and_extra_keys_disallowed(self):
        new = copy.deepcopy(self.old)
        new["request_scopes"][0]["title"] = "Changed"
        self.assertEqual(self.check(new, code=3)["disallowed"], ["$.request_scopes[0].title"])
        new = copy.deepcopy(self.old)
        new["request_scopes"].reverse()
        self.assertIn("$.request_scopes[0].scope_id", self.check(new, code=3)["disallowed"])
        new = copy.deepcopy(self.old)
        new["request_scopes"].pop()
        self.assertEqual(self.check(new, code=3)["disallowed"], ["$.request_scopes"])
        new = copy.deepcopy(self.old)
        new["jurisdiction"] = "us-or"
        self.assertEqual(self.check(new, code=3)["disallowed"], ["$.jurisdiction"])
        new = dict(copy.deepcopy(self.old), notes="extra")
        result = self.check(new, code=3)
        self.assertEqual(result["disallowed"], ["$.notes"])
        self.assertIn("unexpected field", result["errors"][0])

    def test_package_directory_must_match_jurisdiction(self):
        sub = self.dir / "us-or"
        sub.mkdir()
        new_path = sub / "package.json"
        new_path.write_text(json.dumps(self.old), encoding="utf-8")
        done = self.invoke(self.write("old.json", self.old), new_path)
        self.assertEqual(done.returncode, 3)
        self.assertIn("does not match directory", json.loads(done.stdout)["errors"][0])

    # 6. reviewed -> draft is a re-review downgrade.
    def test_downgrade_to_draft_is_allowed_and_noted(self):
        old = reviewed(copy.deepcopy(self.old))
        new = copy.deepcopy(self.old)
        result = self.check(new, old=old, code=0)
        self.assertTrue(result["downgrade"])
        self.assertEqual(sorted(result["allowed_changes"]), ["$.reviewed_at", "$.reviewed_by", "$.status"])
        self.assertFalse(self.check(old, old=old, code=0)["downgrade"])

    # 7. Unreadable input exits 2 with one line and no traceback.
    def test_unreadable_inputs_exit_2(self):
        good = self.write("good.json", self.old)
        self.refused(good, self.dir / "missing.json")
        self.refused(self.dir / "missing.json", good)
        self.refused(good, self.write("bad.json", b"{not json"))
        self.refused(good, self.write("list.json", [self.old]))
        self.refused(good, self.write("latin.json", b'{"a": "\xff"}'))
        link = self.dir / "link.json"
        os.symlink(good, link)
        self.refused(good, link)
        self.refused(link, good)
        self.refused(good, self.dir)
        self.refused(good, self.write("big.json", b'{"pad": "' + b"x" * (3 * 1024 * 1024) + b'"}'))
        for depth in (40, 2000):  # 40 parses but is too deep; 2000 overflows the JSON decoder
            deep = self.write(f"deep{depth}.json", b'{"a": ' + b"[" * depth + b"]" * depth + b"}")
            for args in ((good, deep), (deep, good)):
                self.assertIn("nested deeper than 32 levels", self.refused(*args).stderr)

    def test_usage_error_exits_2(self):
        self.assertEqual(subprocess.run([sys.executable, "-B", str(TOOL), str(PACKAGE)],
                                        capture_output=True, check=False).returncode, 2)

    # 8. The real package compared with itself.
    def test_real_package_with_itself(self):
        done = self.invoke(PACKAGE, PACKAGE)
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertEqual(json.loads(done.stdout), {"ok": True, "allowed_changes": [], "disallowed": [],
                                                   "errors": [], "downgrade": False, "note": NOTE})


if __name__ == "__main__":
    unittest.main()
