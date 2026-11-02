"""Findings law check (DF1507-01): cited rules exist in the campaign's law package and are in force on event_date.

Synthetic package only, written under a temporary jurisdictions base (``law.JURISDICTIONS`` patched); the shipped
us-ca package serves only as a schema-valid shape whose rules and scopes are all replaced. Hex fixtures are
generated at test time. Run from the repository root: python3 -B -m unittest tests.test_finding_law -v
"""
import contextlib
import copy
import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import cli, law
from campaign_tool.content import ContentError, load_content
from campaign_tool.content_check import content_problems, run_cli
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SLUG = "cedar-policy-posted"
WHERE = f"findings/{SLUG}"
RULES = (("ca-open", "2020-01-01", None), ("ca-window", "2021-03-01", "2021-06-30"))


def synthetic_package(status="reviewed"):
    package = copy.deepcopy(law.load_package("us-ca"))
    template = package["rules"][0]
    package["rules"] = [dict(copy.deepcopy(template), rule_id=rule_id, citation="Synthetic " + rule_id,
                             effective_from=start, effective_to=end) for rule_id, start, end in RULES]
    for scope in package["request_scopes"]:
        scope["rule_ids"] = ["ca-open"]
    package.update(status=status, reviewed_by=["Synthetic Reviewer"] if status == "reviewed" else [],
                   reviewed_at="2026-01-01" if status == "reviewed" else None)
    return package


def sha(n):
    return hashlib.sha256(f"synthetic-df1507-01-{n}".encode()).hexdigest()


class FindingLawTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-finding-law-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name) / "jurisdictions"
        (self.base / "us-ca").mkdir(parents=True)
        self.write_package(synthetic_package())
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        patcher = mock.patch.object(law, "JURISDICTIONS", self.base)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_package(self, value):
        text = value if isinstance(value, str) else json.dumps(value)
        (self.base / "us-ca" / "package.json").write_text(text, encoding="utf-8")

    def finding(self, slug=SLUG, fid="fnd_0000000000000001", source_rules=(None, None, None), **changes):
        path = EXAMPLE / "content" / "findings" / f"{SLUG}.json"  # always the pristine example as the base
        document = json.loads(path.read_text(encoding="utf-8"))
        document["sources"] = [{"sha256": sha(f"{slug}-{i}"), "locator": f"page:{i + 1}", "title": "Synthetic record",
                                "rule_id": rule} for i, rule in enumerate(source_rules)]
        document.update(slug=slug, id=fid, title=f"Synthetic finding {slug}", **changes)
        (self.root / "content" / "findings" / f"{slug}.json").write_text(json.dumps(document), encoding="utf-8")

    def load_error(self):
        try:
            load_content(self.root)
        except ContentError as exc:
            return str(exc)
        return None

    def parity(self):
        """content_problems == [] iff load_content returns; the first law line is build's refusal."""
        problems, error = content_problems(self.root), self.load_error()
        self.assertEqual(problems == [], error is None, (problems, error))
        if error is not None:
            self.assertIn(error, problems)
        return problems, error

    def check_cli(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                run_cli(self.root)
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue().splitlines()

    def build_cli(self):
        err = io.StringIO()
        argv = ["campaign_tool", "build", "--directory", str(self.root)]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                cli.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, err.getvalue()

    def test_cited_rule_in_force_builds_and_package_line_is_printed(self):
        self.finding(rule_ids=["ca-open"], source_rules=(None, "ca-window", None), event_date="2021-04-01")
        self.assertEqual(self.parity(), ([], None))
        self.assertEqual(load_content(self.root)["findings"][0]["rule_ids"], ["ca-open"])
        self.assertTrue(build_site(self.root))
        self.assertEqual(self.check_cli(), (0, ["content/: 0 problem(s)", "law package us-ca: reviewed"]))
        self.write_package(synthetic_package(status="draft"))
        self.assertEqual(self.check_cli(), (0, ["content/: 0 problem(s)", "law package us-ca: draft"]))

    def test_unknown_rule_in_rule_ids_and_in_a_source_are_both_named(self):
        self.finding(rule_ids=["ca-open", "ca-made-up"])
        self.finding(slug="second", fid="fnd_second", source_rules=(None, None, "ca-ghost"))
        first = f"{WHERE}: rule ca-made-up is not in the us-ca law package"
        second = "findings/second: rule ca-ghost is not in the us-ca law package"
        problems, error = self.parity()
        self.assertEqual(problems, sorted([first, second]))
        with self.assertRaisesRegex(ContentError, "^findings/(cedar-policy-posted|second): rule ca-(made-up|ghost) "):
            build_site(self.root)
        code, stderr = self.build_cli()
        self.assertEqual(code, 1)
        self.assertTrue(stderr.startswith(f"Stopped: {error}"), stderr)
        self.assertFalse((self.root / "public").exists())
        self.assertEqual(self.check_cli(), (3, ["content/: 2 problem(s)", "law package us-ca: reviewed", *problems]))

    def test_window_bounds_are_inclusive_and_one_day_outside_is_refused(self):
        window = "(effective 2021-03-01 to 2021-06-30)"
        for day, refused in (("2021-02-28", True), ("2021-03-01", False), ("2021-06-30", False),
                             ("2021-07-01", True)):
            with self.subTest(day=day):
                self.finding(rule_ids=["ca-window"], event_date=day)
                expected = [f"{WHERE}: rule ca-window is not in force on {day} {window}"] if refused else []
                self.assertEqual(self.parity()[0], expected)

    def test_open_ended_rule_accepts_far_future_and_refuses_before_start(self):
        self.finding(source_rules=("ca-open", None, None), event_date="2999-12-31")
        self.assertEqual(self.parity(), ([], None))
        self.finding(source_rules=("ca-open", None, None), event_date="2019-12-31")
        expected = f"{WHERE}: rule ca-open is not in force on 2019-12-31 (effective 2020-01-01 to open)"
        self.assertEqual(self.parity(), ([expected], expected))

    def test_missing_or_invalid_package_refuses_citing_findings_only(self):
        self.finding()
        self.finding(slug="citing", fid="fnd_citing", rule_ids=["ca-open"])
        expected = "findings/citing: cites rule ca-open but there is no usable us-ca law package"
        self.write_package("{not json")
        self.assertEqual(self.parity(), ([expected], expected))
        self.assertEqual(self.check_cli(), (3, ["content/: 1 problem(s)", "law package us-ca: missing", expected]))
        (self.base / "us-ca" / "package.json").unlink()
        self.assertEqual(self.parity(), ([expected], expected))
        code, stderr = self.build_cli()
        self.assertEqual(code, 1)
        self.assertIn(f"Stopped: {expected}", stderr)
        (self.root / "content" / "findings" / "citing.json").unlink()
        self.assertEqual(self.parity(), ([], None))
        self.assertTrue(build_site(self.root))

    def test_no_rule_cited_never_reads_the_package(self):
        self.finding()
        with mock.patch.object(law, "load_package", side_effect=AssertionError("package read")) as loader:
            self.assertEqual(self.parity(), ([], None))
            self.assertTrue(build_site(self.root))
        loader.assert_not_called()

    def test_no_campaign_json_means_no_law_check_and_no_package_line(self):
        (self.root / "campaign.json").unlink()
        self.finding(rule_ids=["ca-made-up"])
        self.assertEqual(self.parity(), ([], None))
        self.assertEqual(self.check_cli(), (0, ["content/: 0 problem(s)"]))

    def test_unreadable_campaign_json_is_named_when_a_rule_is_cited(self):
        (self.root / "campaign.json").write_text("{", encoding="utf-8")
        self.finding(rule_ids=["ca-open"])
        problems, error = self.parity()
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(error.startswith("campaign.json: "), error)
        self.assertEqual(self.check_cli()[1][:1], ["content/: 1 problem(s)"])

    def test_bad_rule_ids_are_refused_with_the_field_name(self):
        for value, message in ((["ca-open", "ca-open"], "duplicate rule id"), ([""], "non-empty"),
                               (["   "], "non-empty"), (["ca-" + "x" * 118], "at most 120"),
                               ("ca-open", "list of plain-text"), ([7], "list of plain-text")):
            with self.subTest(value=value):
                self.finding(rule_ids=value)
                problems, error = self.parity()
                self.assertEqual(len(problems), 1, problems)
                self.assertTrue(error.startswith(f"findings/{SLUG}.json.rule_ids: "), error)
                self.assertIn(message, error)
        self.finding(rule_ids=["ca-" + "x" * 117])  # exactly 120 characters is a well-formed (unknown) id
        self.assertEqual(self.parity()[0], [f"{WHERE}: rule ca-{'x' * 117} is not in the us-ca law package"])


if __name__ == "__main__":
    unittest.main()
