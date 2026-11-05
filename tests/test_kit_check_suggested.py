"""kit-check names an edited draft that differs from its fresh draft in kit/requests.suggested/ (read-only)."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit_check  # noqa: E402

LEAD = "# Draft records request: Synthetic PD\n\nDRAFT ONLY. Law package status: reviewed.\n\n"


def draft(scopes=("## Policies",), date="2023-01-01 through today", fee="$50", status="reviewed",
          rules=("- r1: Cite A (review: ok)",), body=""):
    lines = ["# Draft records request: Synthetic PD", "", "DRAFT ONLY. Nothing has been sent. Verify the recipient",
             "source and review. Law package status: " + status + ".", "", "To: records@example.org",
             "Subject: Records concerning ALPR, " + date, ""]
    for scope in scopes:
        lines += [scope, "", "- item"]
    lines += ["", f"The date range for this request is {date}.", "", "withholding. Please advise",
              f"before incurring fees beyond {fee}. Rolling production is welcome.", "", body,
              "## Statutory context (for the requester, not for sending)", "", "- Fee basis: x", "",
              "Rules the scopes relate to:", *rules, "", "Operator checklist: x", ""]
    return "\n".join(lines)


def run(*arguments):
    out, err, code = io.StringIO(), io.StringIO(), 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def snapshot(root):
    return {str(p.relative_to(root)): (p.is_symlink(), p.lstat().st_mode, p.lstat().st_mtime_ns,
                                       None if p.is_symlink() or p.is_dir() else p.read_bytes())
            for p in sorted(Path(root).rglob("*"))}


class SuggestedTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        tmp = tempfile.TemporaryDirectory(prefix="synthetic-suggested-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "campaign"
        (self.root / "kit" / "requests").mkdir(parents=True)
        self.set_agencies([self.agency("ca-a-police")])
        self.write("ca-a-police", draft())

    @staticmethod
    def agency(agency_id, **extra):
        return {"agency_id": agency_id, "name": agency_id, "kind": "police", "selected": True, "verified": True,
                **extra}

    def set_agencies(self, entries):
        (self.root / "kit" / "agencies.json").write_text(json.dumps(entries), encoding="utf-8")

    def write(self, agency_id, text, suggested=False):
        folder = self.root / "kit" / ("requests.suggested" if suggested else "requests")
        folder.mkdir(exist_ok=True)
        (folder / (agency_id + ".md")).write_text(text, encoding="utf-8")

    def report(self, agency_id="ca-a-police"):
        report = kit_check.check(self.root)
        return next(row for row in report["agencies"] if row["agency_id"] == agency_id), report

    def test_no_suggestion_is_byte_identical_text_and_json(self):
        self.set_agencies([self.agency("ca-a-police"), self.agency("ca-b-police", verified=False),
                           self.agency("ca-c-police"), self.agency("ca-d-police", selected=False)])
        self.write("ca-b-police", draft(fee="[explicit approved cap, e.g. $50]"))
        code, out, err = run("kit-check", "--directory", self.root)
        self.assertEqual((code, err), (3, "Read-only. Nothing was sent.\n"))
        self.assertEqual(out, "ca-a-police: OK\n"
                              "ca-b-police: line 16 [explicit approved cap, e.g. $50]\n"
                              "ca-b-police: fee cap missing\nca-b-police: recipient not verified\n"
                              "ca-c-police: draft missing or not a regular file\n"
                              "2 of 3 drafts need attention\n")
        code, out, _ = run("kit-check", "--directory", self.root, "--json")
        self.assertEqual(code, 3)
        self.assertNotIn("suggested_differs", out)
        self.assertEqual(json.loads(out)["agencies"][2], {"agency_id": "ca-c-police", "recipient_verified": True,
                                                          "draft": None, "ok": False, "error": kit_check.MISSING})

    def test_identical_suggestion_has_no_extra_key_or_warning(self):
        self.write("ca-a-police", draft(), suggested=True)
        row, report = self.report()
        self.assertTrue(row["ok"])
        self.assertNotIn("suggested_differs", row)
        self.assertEqual(report["problems"], 0)
        self.assertEqual(run("kit-check", "--directory", self.root)[:2], (0, "ca-a-police: OK\n0 of 1 drafts need attention\n"))

    def test_scopes_settings_status_and_citations_are_directional(self):
        self.write("ca-a-police", draft(scopes=("## Policies", "## Mine only", "## Policies"),
                                        rules=("- r1: Cite A (review: ok)", "- r2: Gone (review: ok)")))
        self.write("ca-a-police", draft(scopes=("## Policies", "### Sub", "## Fresh only"), date="2024-01-01 through today",
                                        fee="$75", status="draft",
                                        rules=("- r1: Cite A (review: pending)", "- r3: New (review: ok)")),
                   suggested=True)
        row, report = self.report()
        self.assertEqual((row["ok"], report["problems"]), (False, 1))
        self.assertEqual(row["suggested_differs"], {
            "path": "kit/requests.suggested/ca-a-police.md",
            "scopes_added": ["## Fresh only"], "scopes_removed": ["## Mine only"],
            "date_range_differs": True, "fee_cap_differs": True, "law_package_status_differs": True,
            "rule_citations_added": ["- r1: Cite A (review: pending)", "- r3: New (review: ok)"],
            "rule_citations_removed": ["- r1: Cite A (review: ok)", "- r2: Gone (review: ok)"],
            "compare_command": "diff -u -- kit/requests/ca-a-police.md kit/requests.suggested/ca-a-police.md"})
        code, out, err = run("kit-check", "--directory", self.root)
        self.assertEqual(code, 3)
        self.assertIn("ca-a-police: edited draft differs from the fresh draft in requests.suggested/", out)
        self.assertIn("scopes only in the fresh draft: ## Fresh only", out)
        self.assertIn("date range line differs: yes; fee cap lines differ: yes; law package status line differs: yes", out)
        self.assertIn("compare with: diff -u -- kit/requests/ca-a-police.md kit/requests.suggested/ca-a-police.md", out)
        self.assertIn("1 of 1 drafts need attention", out)

    def test_single_setting_changes_and_fallback_template(self):
        for field, key in (("date", "date_range_differs"), ("fee", "fee_cap_differs"), ("status", "law_package_status_differs")):
            value = {"date": "2024-02-02 through today", "fee": "$99", "status": "other"}[field]
            self.write("ca-a-police", draft(**{field: value}), suggested=True)
            diff = self.report()[0]["suggested_differs"]
            self.assertEqual({k for k in ("date_range_differs", "fee_cap_differs", "law_package_status_differs")
                              if diff[k]}, {key}, field)
            self.assertEqual((diff["scopes_added"], diff["rule_citations_added"]), ([], []))
        fallback = ("# Draft records request: X\n\nNO REVIEWED LAW PACKAGE: none found\nnothing cited.\n\n"
                    "Subject: Records, 2023-01-01 through today\n\nPlease advise\nbefore incurring fees beyond $50.\n")
        self.write("ca-a-police", fallback)
        self.write("ca-a-police", fallback.replace("none found", "still none"), suggested=True)
        diff = self.report()[0]["suggested_differs"]
        self.assertEqual((diff["law_package_status_differs"], diff["date_range_differs"], diff["fee_cap_differs"],
                          diff["scopes_added"], diff["rule_citations_removed"]), (True, False, False, [], []))

    def test_unstructured_edit_still_warns_and_counts_agency_once(self):
        self.set_agencies([self.agency("ca-a-police", verified=False)])
        self.write("ca-a-police", draft(fee="[explicit approved cap, e.g. $50]"))
        self.write("ca-a-police", draft(body="Extra words."), suggested=True)
        row, report = self.report()
        self.assertEqual(report["problems"], 1)
        diff = row["suggested_differs"]
        self.assertEqual((diff["scopes_added"], diff["fee_cap_differs"], diff["date_range_differs"]), ([], True, False))
        self.write("ca-a-police", draft(), suggested=True)
        self.write("ca-a-police", draft(body="Extra words\n"))
        diff = self.report()[0]["suggested_differs"]
        self.assertEqual([k for k, v in diff.items() if v not in ([], False)],
                         ["path", "compare_command"])

    def test_only_selected_law_enforcement_first_id_is_checked(self):
        self.set_agencies([self.agency("ca-a-police"), self.agency("ca-a-police", verified=False),
                           self.agency("ca-b-police", selected=False), self.agency("ca-c-clerk", kind="other")])
        for agency_id in ("ca-b-police", "ca-c-clerk"):
            self.write(agency_id, draft())
            self.write(agency_id, draft(fee="$1"), suggested=True)
        self.write("ca-zzz-police", draft(fee="$1"), suggested=True)
        report = kit_check.check(self.root)
        self.assertEqual([row["agency_id"] for row in report["agencies"]], ["ca-a-police"])
        self.assertEqual(report["problems"], 0)

    def test_unsafe_suggestion_is_named_never_followed(self):
        canary = self.tmp / "canary.md"
        canary.write_text("SECRET-CANARY " + draft(fee="$1"), encoding="utf-8")
        suggestion = self.root / "kit" / "requests.suggested"
        suggestion.mkdir()
        target = suggestion / "ca-a-police.md"

        def expect(error):
            code, out, _ = run("kit-check", "--directory", self.root)
            self.assertEqual(code, 3)
            self.assertNotIn("SECRET-CANARY", out)
            self.assertIn(f"ca-a-police: kit/requests.suggested/ca-a-police.md not compared: {error}", out)
            row = self.report()[0]
            self.assertEqual((row["ok"], row["suggested_differs"]), (False, {
                "path": "kit/requests.suggested/ca-a-police.md", "error": error, "compare_command": None}))

        target.symlink_to(canary)
        expect("suggestion is not a regular file")
        target.unlink()
        target.symlink_to(self.tmp / "missing.md")
        expect("suggestion is not a regular file")
        target.unlink()
        target.mkdir()
        expect("suggestion is not a regular file")
        target.rmdir()
        os.mkfifo(target)
        expect("suggestion is not a regular file")
        target.unlink()
        target.write_text(draft(fee="$1"), encoding="utf-8")
        with mock.patch("os.open", side_effect=PermissionError(13, "denied")):
            expect("suggestion unreadable")
        with mock.patch("os.open", side_effect=OSError(40, "loop")):  # swapped for a link after lstat
            expect("suggestion is not a regular file")
        with mock.patch("os.fstat", return_value=os.stat(self.tmp)):  # descriptor is not a regular file
            expect("suggestion is not a regular file")
        target.unlink()
        suggestion.rmdir()
        suggestion.symlink_to(self.tmp)
        (self.tmp / "ca-a-police.md").write_text("SECRET-CANARY", encoding="utf-8")
        expect("suggestion is not a regular file")
        self.assertEqual((self.tmp / "ca-a-police.md").read_text(encoding="utf-8"), "SECRET-CANARY")

    def test_primary_unavailable_and_terminal_controls(self):
        self.write("ca-a-police", draft(), suggested=True)
        (self.root / "kit" / "requests" / "ca-a-police.md").unlink()
        row = self.report()[0]
        self.assertEqual((row["error"], row["suggested_differs"]["error"]),
                         (kit_check.MISSING, "primary draft unavailable for comparison"))
        out = run("kit-check", "--directory", self.root)[1]
        self.assertIn("ca-a-police: draft missing or not a regular file\n", out)
        self.assertIn("not compared: primary draft unavailable for comparison", out)
        self.write("ca-a-police", draft(scopes=("## Policies",)))
        self.write("ca-a-police", draft(scopes=("## Bell\x07\x1b[31m heading",)), suggested=True)
        out = run("kit-check", "--directory", self.root)[1]
        self.assertNotIn("\x07", out)
        self.assertNotIn("\x1b", out)
        self.assertIn("## Bell?? [31m heading", out.replace("?[", "? ["))
        json.loads(run("kit-check", "--directory", self.root, "--json")[1])

    def test_read_only_and_default_gate_compatibility(self):
        self.write("ca-a-police", draft(fee="$1"), suggested=True)
        before = snapshot(self.root)
        with mock.patch("subprocess.run", side_effect=AssertionError("no diff process")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("no diff process")):
            for flags in ((), ("--json",)):
                self.assertEqual(run("kit-check", "--directory", self.root, *flags)[0], 3)
        self.assertEqual(snapshot(self.root), before)
        entry = self.agency("ca-a-police")
        plain = kit_check.agency_report(self.root, entry)
        self.assertTrue(plain["ok"])
        self.assertNotIn("suggested_differs", plain)
        self.assertIn("suggested_differs", kit_check.agency_report(self.root, entry, include_suggestion=True))


if __name__ == "__main__":
    unittest.main()
