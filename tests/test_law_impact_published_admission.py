"""RF2100-4: `law impact` admits published finding identities on its reread and keeps unreadable paths (synthetic only).

Run from the repository root: python3 -B -m unittest tests.test_law_impact_published_admission -q
"""
import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import findings, law_impact  # noqa: E402
from tests.test_law_impact import LawImpactTests as Base, rule_of, snapshot  # noqa: E402

SENTINEL = "SYNTHETIC-SENTINEL-DO-NOT-ECHO"


class PublishedAdmissionTests(Base):
    # reuse the fixture helpers only; do not re-run the inherited tests
    def setUp(self):
        super().setUp()
        rule_of(self.new, "ca-alpha")["duty"] = "Synthetic duty, amended."

    def raw(self, name, text):
        path = self.root / "content" / "findings" / name
        path.write_text(text if isinstance(text, str) else json.dumps(text), encoding="utf-8")
        return path

    def test_unhashable_published_ids_are_unreadable_not_tracebacks(self):
        for bad in ([], [SENTINEL], {}, {"k": SENTINEL}):
            with self.subTest(bad=bad):
                path = self.raw("bad.json", {"id": bad, "event_date": "2024-05-01", "rule_ids": ["ca-alpha"]})
                code, out, err = self.run_impact(expect=0)
                self.assertIn("unreadable files not checked: 1", out)
                self.assertEqual(out.count(f"unreadable {path}"), 1)
                self.assertNotIn(SENTINEL, out + err)
                self.assertNotIn("Traceback", err)
                data = json.loads(self.run_impact("--json", expect=0)[1])
                self.assertEqual(data["unreadable"], [str(path)])
                self.assertEqual(data["affected"], [])

    def test_all_existing_identity_refusals_share_published_admission(self):
        for index, bad in enumerate([None, 5, True, "", "x" * 65, "bad id!", "../x", [1]]):
            self.raw(f"bad{index}.json", {"id": bad, "rule_ids": ["ca-alpha"]})
        self.raw("nonobject.json", [1, 2])
        data = json.loads(self.run_impact("--json", expect=0)[1])
        self.assertEqual(data["counts"]["unreadable"], 9)
        self.assertEqual(data["unreadable"], sorted(set(data["unreadable"])))
        self.assertEqual(data["affected"], [])

    def test_healthy_affected_neighbour_survives_malformed_published_input(self):
        self.finding("fnd_ok")
        self.finding("fnd_other", rule_ids=("ca-beta",))
        bad = self.raw("bad.json", {"id": ["x"], "rule_ids": ["ca-alpha"]})
        code, out, _ = self.run_impact(expect=3)
        self.assertIn("fnd_ok published", out)
        self.assertNotIn("fnd_other", out)
        self.assertIn(f"unreadable {bad}", out)
        data = json.loads(self.run_impact("--json", expect=3)[1])
        self.assertEqual([h["id"] for h in data["affected"]], ["fnd_ok"])
        self.assertEqual(data["unreadable"], [str(bad)])

    def test_module_subprocess_reports_unreadable_with_exit_0(self):
        bad = self.raw("bad.json", {"id": {"k": SENTINEL}})
        old, new = self.write("old.json", self.old), self.write("new.json", self.new)
        for extra in ([], ["--json"]):
            run = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.law", "impact", str(old), str(new),
                                  "--directory", str(self.root), *extra], cwd=REPO_ROOT, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn(str(bad), run.stdout)
            self.assertNotIn(SENTINEL, run.stdout + run.stderr)
            self.assertNotIn("Traceback", run.stderr)

    def second_read(self, mutate):
        """Run collect with a genuine status pass, mutating the synthetic file before the genuine reread."""
        real = findings.status_rows

        def status(*args, **kwargs):
            rows = real(*args, **kwargs)
            mutate()
            return rows
        with mock.patch.object(findings, "status_rows", status):
            return law_impact.collect(self.root)

    def test_second_read_failures_are_validated_and_counted(self):
        path = self.finding("fnd_ok")
        mutations = {
            "list-id": lambda: path.write_text(json.dumps({"id": [SENTINEL]}), encoding="utf-8"),
            "invalid-id": lambda: path.write_text(json.dumps({"id": "bad id"}), encoding="utf-8"),
            "bad-json": lambda: path.write_text("{not json", encoding="utf-8"),
            "nonobject": lambda: path.write_text("[1]", encoding="utf-8"),
            "bad-utf8": lambda: path.write_bytes(b"\xff\xfe"),
            "oversize": lambda: path.write_bytes(b" " * (findings.MAX_DRAFT_BYTES + 1)),
        }
        original = path.read_text(encoding="utf-8")
        for name, mutate in mutations.items():
            with self.subTest(name):
                path.write_text(original, encoding="utf-8")
                found, unreadable = self.second_read(mutate)
                self.assertEqual([f for f in found if f[0] == "published"], [])
                self.assertEqual(unreadable, [str(path)])

    def test_second_read_symlink_is_not_followed(self):
        path = self.finding("fnd_ok")
        target = self.tmp / "target.json"
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

        def swap():
            path.unlink()
            path.symlink_to(target)
        found, unreadable = self.second_read(swap)
        self.assertEqual(found, [])
        self.assertEqual(unreadable, [str(path)])

    def test_paths_are_sorted_and_deduplicated_and_reporting_is_read_only(self):
        zed = self.raw("zed.json", "{not json")
        aaa = self.raw("aaa.json", {"id": []})
        self.finding("fnd_ok")
        before = snapshot(self.tmp)
        data = json.loads(self.run_impact("--json", expect=3)[1])
        self.assertEqual(data["unreadable"], [str(aaa), str(zed)])
        self.assertEqual(data["counts"]["unreadable"], 2)
        after = snapshot(self.tmp)
        before.pop("old.json", None), after.pop("old.json", None), before.pop("new.json", None), after.pop("new.json", None)
        self.assertEqual(before, after)
        aaa.write_text(json.dumps({"id": "fnd_fixed", "rule_ids": ["ca-beta"], "event_date": "2024-05-01"}))
        data = json.loads(self.run_impact("--json", expect=3)[1])
        self.assertEqual(data["unreadable"], [str(zed)])

    def test_unaffected_package_with_valid_findings_exits_0(self):
        self.finding("fnd_ok")
        self.new = copy.deepcopy(self.old)
        self.run_impact(expect=0)


for _name in [n for n in dir(Base) if n.startswith("test_") and n not in PublishedAdmissionTests.__dict__]:
    setattr(PublishedAdmissionTests, _name, None)  # inherited suite tests run in their own module only
del Base  # keep the loader from collecting the shared fixture class a second time

if __name__ == "__main__":
    unittest.main()
