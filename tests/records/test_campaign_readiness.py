"""FLK-CMP-1: records campaign-readiness is a read-only, private inventory with fixed codes."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import cli as campaign_cli
from campaign_tool.records import campaign_readiness, cli

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "fictional-campaign" / "content"
SENTINEL = "SENTINEL-PRIVATE-7731"
HEADER = ("agency,system,vendor,data_layer,stated_period,period_days,bound,source_kind,quote,doc_sha256,page,"
          "confidence,checked")


def write(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)


def profile(agency, slug, method):
    return {"schema": "records-review-profile-v1", "records_agency_id": agency, "slug": slug,
            "fact_agency": "x", "agency_name": SENTINEL, "portal": "x",
            "review": {"method_dir": str(method), "method_python": str(method / "python"), "added_by": "x"},
            "rules_intake": {"law_specs": [], "policy_specs": [], "extra_specs": [], "f2_law_map": {}, "sources": []},
            "compare_f3": {"applicability": "x"}}


def snapshot(*roots):
    seen = {}
    for base in roots:
        for path in [base, *base.rglob("*")]:
            info = path.lstat()
            seen[str(path)] = (info.st_mode, info.st_mtime_ns, info.st_size,
                               path.read_bytes() if path.is_file() and not path.is_symlink() else None)
    return seen


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="campaign-readiness-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.campaign, self.project = self.tmp / "campaign", self.tmp / "project"

    def run_cli(self, *args, project=None):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "campaign-readiness",
                               "--project", str(project or self.project), *args],
                              cwd=ROOT, capture_output=True, text=True, timeout=120)

    def report(self, *args):
        done = self.run_cli("--json", *args)
        self.assertEqual(done.stderr, "")
        return json.loads(done.stdout), done.returncode

    def codes(self, report, agency=None):
        return {i["code"] for i in report["issues"] if agency is None or i.get("agency") == agency}

    def slo(self, ids=("ca-slo-county", "ca-slo-city", "ca-slo-town")):
        write(self.campaign / "content" / "agencies.json",
              json.dumps([{"agency_id": i, "name": SENTINEL + i} for i in ids]))
        self.project.mkdir(mode=0o700)
        write(self.project / ".campaign-root", str(self.campaign) + "\n")
        write(self.project / "campaign.json", json.dumps(
            {"schema_version": 1, "campaign_id": "slo-test", "display_name": SENTINEL, "law_package": "us-ca",
             "public_base_url": "https://example.org", "records_mailbox_label": SENTINEL, "timezone": "America/Los_Angeles"}))
        write(self.project / "followup-settings.json", json.dumps(
            {"schema": "records-followup-settings-v1", "default": {"signature": SENTINEL + " signature"},
             "agencies": {ids[0]: {"autonomy": "draft"}}}))
        write(self.project / "agency-asks" / (ids[0] + ".tsv"), "x\n")
        write(self.project / "agency-asks" / (ids[1] + ".tsv"), "x\n")
        write(self.project / "agency-asks" / (ids[1] + ".proposed.tsv"), "x\n")
        write(self.project / "agency-asks" / (ids[2] + ".proposed.tsv"), "x\n")
        write(self.project / "followup-coverage" / (ids[0] + ".tsv"), "x\n")
        write(self.project / "agency-retention.csv",
              HEADER + "\n%s,sys,flock,detections,30 days,30,exact,policy,%s,%s,1,medium,UNVERIFIED\n" % (ids[0], SENTINEL, "a" * 64))
        self.analysis, self.method = self.tmp / "analysis", self.tmp / "method"
        write(self.method / "python", "#!/bin/sh\n", 0o700)
        write(self.analysis / "profiles" / "county.json", json.dumps(profile(ids[0], "county", self.method)))
        write(self.analysis / "profiles" / "city.proposed.json", json.dumps({"records_agency_id": ids[1]}))
        self.approval = self.tmp / "approval.json"
        write(self.approval, SENTINEL + " not json {")
        return ids


class SloShapedTests(Base):
    def test_all_linked_agencies_including_unselected_are_reported(self):
        ids = self.slo()
        write(self.campaign / "content" / "agencies.json", json.dumps([
            {"agency_id": i, "name": SENTINEL, "selected": n == 0} for n, i in enumerate(ids)]))
        report, code = self.report()
        self.assertEqual(code, 0)
        self.assertEqual([r["agency"] for r in report["agencies"]], sorted(ids))

    def test_duplicate_agencies_are_refused_even_when_unselected(self):
        ids = self.slo()
        for selections in ((False, True), (False, False), (True, False)):
            with self.subTest(selections=selections):
                write(self.campaign / "content" / "agencies.json", json.dumps([
                    {"agency_id": ids[0], "name": SENTINEL, "selected": selected} for selected in selections]))
                done = self.run_cli()
                self.assertEqual((done.returncode, done.stdout, done.stderr.strip()),
                                 (2, "", "READINESS_AGENCIES_INVALID"))

    def test_refused_table_ancestry_is_invalid_not_missing(self):
        self.slo()
        for folder, field, issue in (("agency-asks", "asks", "READINESS_ASKS_UNAVAILABLE"),
                                     ("followup-coverage", "coverage", "READINESS_COVERAGE_UNAVAILABLE")):
            parent = self.project / folder
            saved = self.tmp / folder
            parent.rename(saved)
            for kind in ("symlink", "file", "missing"):
                with self.subTest(folder=folder, kind=kind):
                    if kind == "symlink":
                        parent.symlink_to(saved, target_is_directory=True)
                    elif kind == "file":
                        write(parent, SENTINEL)
                    before = snapshot(self.project, saved)
                    report, code = self.report()
                    self.assertEqual(code, 0 if kind == "missing" else 1)
                    self.assertEqual({r[field] for r in report["agencies"]},
                                     {"missing" if kind == "missing" else "invalid"})
                    self.assertEqual(issue in self.codes(report), kind != "missing")
                    self.assertEqual(snapshot(self.project, saved), before)
                if kind != "missing":
                    parent.unlink()
            saved.rename(parent)

    def test_invalid_profiles_directory_is_degraded(self):
        self.slo()
        parent = self.analysis / "profiles"
        saved = self.tmp / "saved-profiles"
        parent.rename(saved)
        for kind in ("symlink", "file", "missing"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    parent.symlink_to(saved, target_is_directory=True)
                elif kind == "file":
                    write(parent, SENTINEL)
                before = snapshot(self.analysis, saved)
                report, code = self.report("--analysis-root", str(self.analysis))
                self.assertEqual(code, 0 if kind == "missing" else 1)
                self.assertEqual({r["analysis_profile"] for r in report["agencies"]},
                                 {"missing" if kind == "missing" else "invalid"})
                self.assertEqual("READINESS_PROFILE_INVALID" in self.codes(report), kind != "missing")
                self.assertEqual(before, snapshot(self.analysis, saved))
            if kind != "missing":
                parent.unlink()

    def test_missing_configured_method_has_agency_remediation(self):
        ids = self.slo()
        for kind in ("python", "directory"):
            with self.subTest(kind=kind):
                if kind == "python":
                    (self.method / "python").unlink()
                else:
                    self.method.rmdir()
                before = snapshot(self.project, self.analysis)
                report, code = self.report("--analysis-root", str(self.analysis))
                self.assertEqual(code, 0)
                row = next(r for r in report["agencies"] if r["agency"] == ids[0])
                self.assertEqual((row["analysis_profile"], row["analysis_method"]), ("present", "missing"))
                issues = [i for i in report["issues"]
                          if i.get("agency") == ids[0] and i["field"] == "analysis_method"]
                self.assertEqual(len(issues), 1)
                self.assertEqual((issues[0]["code"], issues[0]["kind"]), ("READINESS_METHOD_MISSING", "command"))
                self.assertIn("analysis-config --root R --slug S --check-only", issues[0]["remediation"])
                self.assertNotIn(str(self.tmp), json.dumps(report))
                self.assertEqual(before, snapshot(self.project, self.analysis))

    def test_states_privacy_parity_and_no_writes(self):
        ids = self.slo()
        before = snapshot(self.project, self.campaign, self.analysis, self.method, self.approval)
        args = ("--analysis-root", str(self.analysis), "--portal-approval-file", str(self.approval))
        report, code = self.report(*args)
        text = self.run_cli(*args)
        self.assertEqual((code, text.returncode, text.stderr), (0, 0, ""))
        rows = {r["agency"]: r for r in report["agencies"]}
        self.assertEqual(list(rows), sorted(ids))
        county, city, town = (rows[i] for i in ids)
        self.assertEqual((county["autonomy"], city["autonomy"], county["signature"]), ("draft", "off", "present"))
        self.assertEqual((county["asks"], city["asks"], town["asks"]), ("present", "present", "proposed"))
        self.assertEqual((county["coverage"], city["coverage"]), ("present", "missing"))
        self.assertEqual((county["retention"], city["retention"]), ("present", "missing"))
        self.assertEqual((county["analysis_profile"], county["analysis_method"]), ("present", "present"))
        self.assertEqual((city["analysis_profile"], town["analysis_profile"]), ("proposed", "missing"))
        self.assertEqual((report["law_package"], report["portal_approval"]), ("draft", "present"))
        self.assertTrue(all(v == "present" for v in report["campaign_fields"].values()))
        self.assertEqual(sorted(report), ["agencies", "campaign_fields", "issues", "law_package", "portal_approval", "schema"])
        for item in report["issues"]:
            self.assertTrue(item["remediation"] and item["code"].startswith("READINESS_"))
        for line in campaign_readiness.render(report).splitlines():
            self.assertIn(line, text.stdout)
        for marker in (SENTINEL, str(self.tmp), "example.org"):
            self.assertNotIn(marker, text.stdout)
            self.assertNotIn(marker, json.dumps(report))
        self.assertEqual(before, snapshot(self.project, self.campaign, self.analysis, self.method, self.approval))

    def test_unreadable_inputs_have_named_codes(self):
        ids = self.slo()
        write(self.project / "followup-settings.json", '{"schema": "records-followup-settings-v1", "default": {"autonomy": "send"}}')
        write(self.project / "agency-retention.csv", "agency\nbroken\n")
        write(self.project / "campaign.json", '{"schema_version": 1, "campaign_id": "BAD ID"}')
        (self.project / "agency-asks" / (ids[0] + ".tsv")).unlink()
        os.symlink(self.approval, self.project / "agency-asks" / (ids[0] + ".tsv"))
        os.mkfifo(self.project / "followup-coverage" / (ids[1] + ".tsv"))
        write(self.analysis / "profiles" / "dup.json", json.dumps(profile(ids[0], "dup", self.method)))
        link = self.tmp / "link.json"
        os.symlink(self.approval, link)
        report, code = self.report("--analysis-root", str(self.analysis), "--portal-approval-file", str(link))
        self.assertEqual(code, 1)
        county = {r["agency"]: r for r in report["agencies"]}[ids[0]]
        self.assertEqual((county["autonomy"], county["signature"], county["retention"]), ("invalid", "unknown", "invalid"))
        self.assertEqual(county["asks"], "invalid")
        self.assertEqual({r["agency"]: r for r in report["agencies"]}[ids[1]]["coverage"], "invalid")
        self.assertEqual(county["analysis_profile"], "invalid")
        self.assertEqual(report["portal_approval"], "invalid")
        self.assertEqual(report["law_package"], "invalid")
        self.assertTrue({"READINESS_SETTINGS_INVALID", "READINESS_RETENTION_INVALID", "READINESS_CONFIG_INVALID",
                         "READINESS_ASKS_UNAVAILABLE", "READINESS_COVERAGE_UNAVAILABLE", "READINESS_PROFILE_INVALID",
                         "READINESS_PORTAL_APPROVAL_UNAVAILABLE"} <= self.codes(report))

    def test_refusals_print_only_a_code_and_nothing_on_stdout(self):
        done = self.run_cli(project=self.tmp / "none")
        self.assertEqual((done.returncode, done.stdout, done.stderr.strip()), (2, "", "READINESS_PROJECT_UNAVAILABLE"))
        self.slo()
        (self.project / ".campaign-root").unlink()
        self.assertEqual(self.run_cli().stderr.strip(), "READINESS_CAMPAIGN_ROOT_UNAVAILABLE")
        write(self.project / ".campaign-root", "relative\n")
        self.assertEqual(self.run_cli().stderr.strip(), "READINESS_CAMPAIGN_ROOT_UNAVAILABLE")
        write(self.project / ".campaign-root", str(self.campaign) + "\n")
        for text in ("[]x", '[{"agency_id": "ca-a", "name": "n"}, {"agency_id": "ca-a", "name": "n"}]',
                     '[{"agency_id": "../x", "name": "n"}]'):
            write(self.campaign / "content" / "agencies.json", text)
            done = self.run_cli()
            self.assertEqual((done.returncode, done.stdout, done.stderr.strip()), (2, "", "READINESS_AGENCIES_INVALID"))
        (self.campaign / "content" / "agencies.json").unlink()
        self.assertEqual(self.run_cli().stderr.strip(), "READINESS_AGENCIES_INVALID")

    def test_portal_and_method_files_are_never_opened(self):
        self.slo()
        opened = []
        real = os.open

        def spy(path, flags, *a, **kw):
            opened.append((str(path), flags))
            return real(path, flags, *a, **kw)
        with mock.patch("os.open", spy):
            report = campaign_readiness.collect(str(self.project), analysis_root=str(self.analysis),
                                                portal_approval_file=str(self.approval))
        self.assertEqual(report["portal_approval"], "present")
        leaves = {os.path.basename(p) for p, flags in opened if not flags & os.O_DIRECTORY}
        self.assertNotIn("approval.json", leaves)
        self.assertNotIn("python", leaves)

    def test_registered_and_no_operands_means_not_configured(self):
        self.assertEqual(cli.COMMANDS["campaign-readiness"], ("campaign_readiness", "main"))
        self.slo()
        report, code = self.report()
        self.assertEqual(code, 0)
        self.assertEqual(report["portal_approval"], "not_configured")
        self.assertEqual({r["analysis_profile"] for r in report["agencies"]}, {"not_configured"})
        self.assertNotIn("READINESS_PROFILE_MISSING", self.codes(report))
        self.assertIn("READINESS_ANALYSIS_ROOT_UNSPECIFIED", self.codes(report))


class CedarProjectInitTests(Base):
    def test_fresh_project_reports_everything_off_or_missing_with_a_fix(self):
        init = [sys.executable, "-B", "-m"]
        run = lambda *a: subprocess.run([*init, *a], cwd=ROOT, capture_output=True, text=True, timeout=120)  # noqa: E731
        self.assertEqual(run("campaign_tool", "init", "--directory", str(self.campaign), "--name", "Synthetic Cedar",
                             "--county", "Cedar", "--state", "CA").returncode, 0)
        shutil.copytree(EXAMPLE, self.campaign / "content")
        done = run("campaign_tool.records", "project-init", "--project", str(self.project), "--campaign-root", str(self.campaign))
        self.assertEqual(done.returncode, 0, done.stderr)
        before = snapshot(self.project, self.campaign)
        report, code = self.report()
        self.assertEqual(code, 0)
        self.assertEqual(snapshot(self.project, self.campaign), before)
        self.assertEqual(len(report["agencies"]), 3)
        fields = report["campaign_fields"]
        self.assertEqual(sorted(k for k, v in fields.items() if v == "present"),
                         ["campaign_id", "display_name", "law_package", "schema_version"])
        self.assertEqual({fields[k] for k in ("public_base_url", "records_mailbox_label", "timezone")}, {"missing"})
        self.assertEqual((report["law_package"], report["portal_approval"]), ("draft", "not_configured"))
        for row in report["agencies"]:
            self.assertEqual((row["autonomy"], row["signature"], row["asks"], row["coverage"], row["retention"]),
                             ("off", "missing", "missing", "missing", "missing"))
            self.assertEqual(self.codes(report, row["agency"]), {
                "READINESS_AUTONOMY_OFF", "READINESS_ASKS_MISSING", "READINESS_COVERAGE_MISSING", "READINESS_RETENTION_MISSING"})
        self.assertTrue(all(i["kind"] in ("command", "human") and i["remediation"] for i in report["issues"]))
        self.assertTrue(any("agency-retention" in i["remediation"] for i in report["issues"]))
        self.assertNotIn("Cedar", run("campaign_tool.records", "campaign-readiness", "--project", str(self.project)).stdout)


if __name__ == "__main__":
    unittest.main()
