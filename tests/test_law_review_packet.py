"""Offline tests for ``campaign_tool law review-packet`` (BF-df-law-review).

The packet is a question for a human reviewer, not a review: these tests check that it is built from the
existing review-sheet output, orders rules by their own ``review`` field, asserts nothing and changes nothing.
Run from the repository root: python3 -B -m unittest tests.test_law_review_packet -v
"""

import argparse
import copy
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, law, law_packet  # noqa: E402

AS_OF = "2026-11-03"
FILES = {"PACKET.md", "RULES.md", "WORKSHEET.csv", "RETURN.md"}
ASSERTS = re.compile(r"violat|unlawful|illegal|non-?complian|breach", re.I)
ADVISES = re.compile(r"\b(sue|lawsuit|file (a|an|suit)|petition|writ|demand|complain|you should)\b", re.I)


def tree_digest(root):
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def generated_text(text, package):
    """The packet's own sentences: every string quoted from the package removed, longest first."""
    for quoted in sorted(set(strings(package)), key=len, reverse=True):
        if len(quoted) > 2:
            text = text.replace(quoted, "")
    return text


class ReviewPacketTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-law-packet-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()

    def cli(self, *arguments, returncode=0):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "law", "review-packet", *arguments],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, returncode, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def packet(self, name="packet", *extra):
        out = self.tmp / name
        result = json.loads(self.cli("--jurisdiction", "us-ca", "--out", str(out), "--as-of", AS_OF, *extra).stdout)
        return out, result

    def synthetic_base(self, package):
        base = self.tmp / "base"
        (base / "us-ca").mkdir(parents=True)
        (base / "us-ca" / "package.json").write_text(json.dumps(package), encoding="utf-8")
        return base

    def test_clean_run_writes_four_private_files_and_worksheet_equals_review_sheet(self):
        out, result = self.packet()
        self.assertEqual(result["status"], "review_required")
        self.assertEqual(result["package_status"], "draft")
        self.assertEqual({p.name for p in out.iterdir()}, FILES)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
        for path in out.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)
        sheet = self.tmp / "sheet.csv"
        subprocess.run([sys.executable, "-B", "-m", "campaign_tool.law", "review-sheet", "us-ca", "--out", str(sheet),
                        "--as-of", AS_OF], cwd=REPO_ROOT, check=True, capture_output=True, timeout=30)
        self.assertEqual((out / "WORKSHEET.csv").read_bytes(), sheet.read_bytes())

    def test_apply_sheet_round_trips_the_packet_worksheet_unchanged(self):
        out, _ = self.packet()
        package = law.load_package("us-ca")
        rows = law.read_sheet(out / "WORKSHEET.csv", package)
        self.assertEqual([row["rule_id"] for row in rows],
                         ["records_law"] + [rule["rule_id"] for rule in package["rules"]])
        proposed, report = law.apply_sheet(package, rows)
        self.assertEqual(proposed, package)
        self.assertEqual((report["changed"], report["refused"], report["text_edited"]), ([], [], []))
        self.assertEqual(report["skipped"], len(rows))
        cli_out = self.tmp / "proposed.json"
        subprocess.run([sys.executable, "-B", "-m", "campaign_tool.law", "apply-sheet", "us-ca", "--sheet",
                        str(out / "WORKSHEET.csv"), "--out", str(cli_out)], cwd=REPO_ROOT, check=True,
                       capture_output=True, timeout=30)
        self.assertEqual(json.loads(cli_out.read_text(encoding="utf-8")), package)

    def test_return_instructions_bind_the_worksheet_and_a_stale_return_is_refused(self):
        out, _ = self.packet()
        instructions = (out / "RETURN.md").read_text(encoding="utf-8")
        for phrase in ("`package_sha256`", "never edit or copy it", "builds a new packet", "reassess"):
            self.assertIn(phrase, instructions)
        package = law.load_package("us-ca")
        self.assertNotIn(law.package_sha256(package), instructions)
        rows = law.read_sheet(out / "WORKSHEET.csv", package)
        self.assertEqual({row["package_sha256"] for row in rows}, {law.package_sha256(package)})
        amended = copy.deepcopy(package)
        amended["rules"][0]["citation"] += " (amended)"
        with self.assertRaisesRegex(ValueError, r"row 1 \(records_law\): worksheet belongs to a different law "
                                                r"package; regenerate review-sheet or review-packet, nothing written"):
            law.apply_sheet(amended, rows)

    def test_rules_md_lists_needs_attorney_review_first_then_likely_then_verified(self):
        out, result = self.packet()
        self.assertEqual(result["review_counts"], {"verified": 8, "likely": 1, "needs_attorney_review": 9})
        listed = re.findall(r"^## \d+\. (\S+)$", (out / "RULES.md").read_text(encoding="utf-8"), re.M)
        labels = {rule["rule_id"]: rule["review"] for rule in law.load_package("us-ca")["rules"]}
        self.assertEqual(sorted(listed), sorted(labels))
        self.assertEqual([labels[rule_id] for rule_id in listed],
                         ["needs_attorney_review"] * 9 + ["likely"] + ["verified"] * 8)

    def test_order_comes_from_the_review_field_not_the_text(self):
        package = copy.deepcopy(law.load_package("us-ca"))
        decoy = copy.deepcopy(package["rules"][1])
        decoy.update(rule_id="ca-verified-decoy", citation="Synthetic verified citation",
                     duty="Verified. This reading is verified.", review="needs_attorney_review")
        for rule in package["rules"]:
            rule["review"] = "verified"
        package["rules"].append(decoy)
        out, _ = self.packet("decoy", "--base", str(self.synthetic_base(package)))
        listed = re.findall(r"^## \d+\. (\S+)$", (out / "RULES.md").read_text(encoding="utf-8"), re.M)
        self.assertEqual(listed[0], "ca-verified-decoy")
        self.assertEqual(listed[1:], [rule["rule_id"] for rule in package["rules"][:-1]])

    def test_reviewed_package_leaves_doctor_unchanged_and_packet_still_asserts_nothing(self):
        with mock.patch.object(law, "package_status", return_value=("draft", None)):
            draft = cli.law_package_report({"state": "CA"})
        self.assertEqual(draft, {"law_package_status": "draft", "reviewed_law_package": False})
        root = self.tmp / "campaign"
        subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "init", "--directory", str(root), "--name",
                        "Synthetic", "--county", "Synthetic", "--state", "CA"], cwd=REPO_ROOT, check=True,
                       capture_output=True, timeout=30)
        actions = cli.doctor_report(root)["next_actions"]
        self.assertEqual(actions.count(cli.LAW_REVIEW_ACTION), 1)
        self.assertIn("independent of the author", cli.LAW_REVIEW_ACTION)
        self.assertIn("campaign-tool law review-packet", cli.LAW_REVIEW_ACTION)
        with mock.patch.object(law, "package_status", return_value=("reviewed", None)):
            reviewed = cli.doctor_report(root)
        self.assertIs(reviewed["reviewed_law_package"], True)
        self.assertEqual(reviewed["next_actions"], [a for a in actions if a != cli.LAW_REVIEW_ACTION])
        package = copy.deepcopy(law.load_package("us-ca"))
        package.update(status="reviewed", reviewed_by=["synthetic-reviewer"], reviewed_at="2026-10-01")
        out, result = self.packet("reviewed", "--base", str(self.synthetic_base(package)))
        self.assertEqual(result["package_status"], "reviewed")
        for name in ("PACKET.md", "RULES.md", "RETURN.md"):
            own = generated_text((out / name).read_text(encoding="utf-8"), package)
            self.assertIsNone(ASSERTS.search(own), name)

    def test_refusals_create_nothing(self):
        existing = self.tmp / "exists"
        existing.mkdir()
        real = self.tmp / "real"
        real.mkdir()
        (self.tmp / "link").symlink_to(real, target_is_directory=True)
        for out in (existing, self.tmp / "link" / "packet", REPO_ROOT / "packet-in-repo", Path("relative-packet"),
                    Path(str(self.tmp) + "/x/../packet"), self.tmp / "missing-parent" / "packet"):
            with self.subTest(out=str(out)):
                before = sorted(os.listdir(self.tmp)), sorted(os.listdir(REPO_ROOT))
                result = self.cli("--jurisdiction", "us-ca", "--out", str(out), returncode=2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(len(result.stderr.splitlines()), 1)
                self.assertTrue(result.stderr.startswith("Stopped: --out"))
                self.assertEqual((sorted(os.listdir(self.tmp)), sorted(os.listdir(REPO_ROOT))), before)
        self.assertEqual(os.listdir(existing), [])
        self.assertEqual(os.listdir(real), [])
        missing = self.cli("--jurisdiction", "us-zz", "--out", str(self.tmp / "zz"), returncode=2)
        self.assertTrue(missing.stderr.startswith("Stopped: No law package"))
        self.assertFalse((self.tmp / "zz").exists())

    def test_mid_packet_write_failure_leaves_no_out_and_retry_succeeds(self):
        out = self.tmp / "nospace"
        real_open = os.open

        def failing_open(target, *rest):
            if Path(target).name == "RULES.md":
                raise OSError(28, "Synthetic no space left on device")
            return real_open(target, *rest)

        args = argparse.Namespace(jurisdiction="us-ca", out=str(out), as_of=AS_OF, base=None)
        with mock.patch.object(law_packet.os, "open", failing_open), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(law_packet.main(args), 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "Stopped: [Errno 28] Synthetic no space left on device\n")
        self.assertFalse(os.path.lexists(out))
        _, result = self.packet("nospace")
        self.assertEqual({p.name for p in out.iterdir()}, FILES)
        self.assertEqual(result["out"], str(out))

    def test_generated_text_asserts_no_breach_and_advises_no_legal_action(self):
        out, _ = self.packet()
        package = law.load_package("us-ca")
        for name in ("PACKET.md", "RULES.md", "RETURN.md"):
            own = generated_text((out / name).read_text(encoding="utf-8"), package)
            self.assertIsNone(ASSERTS.search(own), name)
            self.assertIsNone(ADVISES.search(own), name)
        self.assertNotIn("@", (out / "RETURN.md").read_text(encoding="utf-8"))

    def test_jurisdictions_unchanged_package_still_draft_and_launch_counts_match(self):
        roots = [REPO_ROOT / "jurisdictions", law.JURISDICTIONS]
        before = [tree_digest(root) for root in roots]
        self.packet()
        self.assertEqual([tree_digest(root) for root in roots], before)
        self.assertEqual(law.package_status("us-ca"), ("draft", None))
        counts = law.summary(law.load_package("us-ca"))["review_counts"]
        launch = (REPO_ROOT / "LAUNCH.md").read_text(encoding="utf-8")
        found = re.search(r"(\d+) rules verified against leginfo text, (\d+) marked likely, (\d+) marked "
                          r"needs_attorney_review", launch)
        self.assertEqual(tuple(map(int, found.groups())) if found else "LAUNCH.md counts line missing",
                         (counts["verified"], counts["likely"], counts["needs_attorney_review"]))

    def test_ordered_rules_is_stable_within_a_label(self):
        rules = [{"rule_id": n, "review": r} for n, r in (("a", "verified"), ("b", "likely"), ("c", "verified"),
                                                          ("d", "needs_attorney_review"))]
        self.assertEqual([r["rule_id"] for r in law_packet.ordered_rules({"rules": rules})], ["d", "b", "a", "c"])


if __name__ == "__main__":
    unittest.main()
