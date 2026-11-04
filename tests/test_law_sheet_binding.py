"""Offline tests for the law worksheet's package binding (RF0200-4).

A returned worksheet may update confidence labels only against the exact package content it was made
from. Synthetic packages under a temporary ``--base``; no network. apply-sheet is a transcription, not
a review. Run from the repository root: python3 -B -m unittest tests.test_law_sheet_binding -v
"""

import copy
import csv
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import law  # noqa: E402
from tests.test_law_review_sheet import synthetic_package  # noqa: E402

TODAY = date.today().isoformat()
REGENERATE = "regenerate review-sheet or review-packet, nothing written"
DIFFERENT = f"worksheet belongs to a different law package; {REGENERATE}"
INVALID = f"invalid package_sha256; {REGENERATE}"
LEGACY = f"--sheet has no package binding; {REGENERATE}"
NOTE = "SYNTHETIC-PRIVATE-NOTE"


def amended(**changes):
    """The synthetic package with one valid amendment applied."""
    package = synthetic_package()
    if "citation" in changes:
        package["rules"][0]["citation"] = changes["citation"]
    if "other_rule" in changes:
        package["rules"][2]["duty"] = changes["other_rule"]
    if "scope" in changes:
        package["request_scopes"][0]["rule_ids"] = ["ca-a", "ca-b"]
    if "labels" in changes:
        package["rules"][1]["review"] = "needs_attorney_review"
    if "status" in changes:
        package.update(status="reviewed", reviewed_by=["synthetic-reviewer"], reviewed_at="2026-10-01")
    return package


class BindingBase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-law-binding-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.base = self.tmp / "base"
        (self.base / "us-ca").mkdir(parents=True)
        self.package_file = self.base / "us-ca" / "package.json"
        self.write_package(synthetic_package())
        (self.tmp / "sheets").mkdir()
        self.outdir = self.tmp / "out"
        self.outdir.mkdir()
        self.out = self.outdir / "proposed.json"

    def write_package(self, package):
        self.package_file.write_text(json.dumps(package, indent=2), encoding="utf-8")

    def run_cli(self, module, *arguments, returncode=0):
        result = subprocess.run([sys.executable, "-B", "-m", *module, *arguments], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, returncode, result.stderr)
        return result

    def run_law(self, command, *arguments, returncode=0):
        return self.run_cli(("campaign_tool.law", command), *arguments, "--base", str(self.base),
                            returncode=returncode)

    def make_sheet(self, name="blank.csv", as_of="2026-01-11"):
        path = self.tmp / "sheets" / name
        self.run_law("review-sheet", "us-ca", "--out", str(path), "--as-of", as_of)
        return path

    def rows(self, path):
        with open(path, encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))

    def write_table(self, name, header, table, bom=False):
        path = self.tmp / "sheets" / name
        with open(path, "w", encoding="utf-8-sig" if bom else "utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(table)
        return path

    def write_rows(self, name, rows, bom=False):
        return self.write_table(name, law.SHEET_COLUMNS, [[row[c] for c in law.SHEET_COLUMNS] for row in rows], bom)

    def filled(self, sheet, **by_rule):
        rows = self.rows(sheet)
        for row in rows:
            row.update(by_rule.get(row["rule_id"].replace("-", "_"), {}))
        return rows

    def conclusion(self, label, **extra):
        return {"reviewer_conclusion": label, "reviewer_name": "Synthetic Reviewer", "reviewed_on": TODAY,
                "reviewer_notes": NOTE, **extra}

    def snapshot(self, *paths):
        return [(Path(p).read_bytes(), os.stat(p).st_mtime_ns) for p in paths]

    def apply_cli(self, sheet, returncode=0):
        return self.run_law("apply-sheet", "us-ca", "--sheet", str(sheet), "--out", str(self.out),
                            returncode=returncode)

    def refused(self, sheet, message):
        result = self.apply_cli(sheet, returncode=1)
        self.assertEqual(result.stderr, "Stopped: " + message + "\n")
        self.assertEqual(result.stdout, "")
        self.assertEqual(list(self.outdir.iterdir()), [])
        self.assertNotIn(NOTE, result.stderr)
        return result


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_is_canonical_and_covers_whole_package(self):
        package = synthetic_package()
        package["rules"][0]["remedy"] = "Remedy \u00e9 \u65e5\u672c"
        canonical = json.dumps(package, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        digest = law.package_sha256(package)
        self.assertEqual(digest, hashlib.sha256(canonical).hexdigest())
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertFalse(canonical.endswith(b"\n"))
        base = law.package_sha256(synthetic_package())
        # Object key order and pretty-printing do not change identity.
        shuffled = {key: value for key, value in reversed(list(synthetic_package().items()))}
        shuffled["rules"] = [dict(reversed(list(rule.items()))) for rule in shuffled["rules"]]
        self.assertEqual(law.package_sha256(shuffled), base)
        self.assertEqual(law.package_sha256(json.loads(json.dumps(synthetic_package(), indent=4))), base)

        def change(edit):
            candidate = synthetic_package()
            edit(candidate)
            return law.package_sha256(candidate)

        edits = {
            "rule order": lambda p: p["rules"].reverse(),
            "source order": lambda p: p["rules"][2]["sources"].reverse(),
            "exception order": lambda p: p["rules"][0]["exceptions"].reverse(),
            "value whitespace": lambda p: p["rules"][0].update(duty=p["rules"][0]["duty"] + " "),
            "citation": lambda p: p["rules"][0].update(citation="Synthetic changed"),
            "actor": lambda p: p["rules"][0].update(actor="Other agency"),
            "activity": lambda p: p["rules"][0].update(activity="Other activity"),
            "duty": lambda p: p["rules"][1].update(duty="Other duty"),
            "exceptions": lambda p: p["rules"][1].update(exceptions=["new exception"]),
            "remedy": lambda p: p["rules"][2].update(remedy="Other remedy"),
            "effective_from": lambda p: p["rules"][0].update(effective_from="2020-01-02"),
            "effective_to": lambda p: p["rules"][0].update(effective_to="2030-01-01"),
            "source url": lambda p: p["rules"][0]["sources"][0].update(url="https://example.invalid/other"),
            "access date": lambda p: p["rules"][0]["sources"][0].update(accessed="2026-01-02"),
            "records timing": lambda p: p["records_law"].update(
                determination_days=p["records_law"]["determination_days"] + 1),
            "records source": lambda p: p["records_law"]["sources"][0].update(title="other law"),
            "request scope": lambda p: p["request_scopes"][0].update(rule_ids=["ca-a", "ca-b"]),
            "schema version": lambda p: p.update(schema_version=p["schema_version"] + 1),
            "review label": lambda p: p["rules"][0].update(review="verified"),
            "status": lambda p: p.update(status="reviewed"),
            "reviewed_by": lambda p: p.update(reviewed_by=["synthetic-reviewer"]),
            "reviewed_at": lambda p: p.update(reviewed_at="2026-10-01"),
        }
        digests = {}
        for name, edit in edits.items():
            with self.subTest(change=name):
                digests[name] = change(edit)
                self.assertNotEqual(digests[name], base)
        self.assertEqual(len(set(digests.values())), len(edits))

    def test_non_finite_numbers_cannot_be_fingerprinted(self):
        package = synthetic_package()
        package["records_law"]["determination_days"] = float("nan")
        with self.assertRaises(ValueError):
            law.package_sha256(package)


class SheetBindingTests(BindingBase):
    def test_old_conclusion_cannot_upgrade_amended_citation(self):
        sheet = self.write_rows("filled.csv", self.filled(self.make_sheet(), ca_a=self.conclusion("verified")))
        self.write_package(amended(citation="Synthetic amended citation"))
        before = self.snapshot(self.package_file, sheet)
        self.refused(sheet, f"row 1 (records_law): {DIFFERENT}")
        self.assertEqual(self.snapshot(self.package_file, sheet), before)
        # The same sheet against the package it was made from still works.
        self.write_package(synthetic_package())
        self.assertEqual(json.loads(self.apply_cli(sheet).stdout)["changed"],
                         [{"rule_id": "ca-a", "from": "likely", "to": "verified"}])

    def test_other_rule_and_metadata_drift_refuse_blank_or_partial_sheet(self):
        blank = self.make_sheet()
        partial = self.write_rows("partial.csv", [r for r in self.filled(blank, ca_a=self.conclusion("verified"))
                                                  if r["rule_id"] == "ca-a"])
        header_only = self.write_table("empty.csv", law.SHEET_COLUMNS, [])
        drifts = {"other_rule": amended(other_rule="Changed duty"), "scope": amended(scope=True),
                  "labels": amended(labels=True), "status": amended(status=True)}
        for drift, drifted in drifts.items():
            with self.subTest(drift=drift):
                self.write_package(drifted)
                before = self.snapshot(self.package_file, blank, partial)
                for sheet, row in ((blank, "row 1 (records_law)"), (partial, "row 1 (ca-a)")):
                    self.refused(sheet, f"{row}: {DIFFERENT}")
                self.assertEqual(self.snapshot(self.package_file, blank, partial), before)
                # Fresh, partial, reordered sheet from the drifted package is accepted as before.
                fresh = self.make_sheet(f"fresh-{drift}.csv")
                chosen = {r["rule_id"]: r for r in self.filled(fresh, ca_a=self.conclusion("verified"))}
                reordered = self.write_rows(f"reordered-{drift}.csv", [chosen["ca-b"], chosen["ca-a"]])
                self.assertEqual(json.loads(self.apply_cli(reordered).stdout)["changed"][0]["rule_id"], "ca-a")
                self.out.unlink()
        # An empty sheet binds nothing: the existing harmless no-op.
        proposed, report = law.apply_sheet(drifts["status"], [])
        self.assertEqual(proposed, drifts["status"])
        self.assertEqual((report["changed"], report["skipped"], report["refused"]), ([], 0, []))
        self.assertEqual(json.loads(self.apply_cli(header_only).stdout)["skipped"], 0)

    def test_bad_missing_and_mixed_bindings_refuse_before_changes(self):
        good = law.package_sha256(synthetic_package())
        other = law.package_sha256(amended(citation="elsewhere"))
        blank = self.make_sheet()
        bad_values = {"blank": "", "uppercase": good.upper(), "newline": good + "\n", "space": good + " ",
                      "leading space": " " + good, "short": good[:63], "long": good + "0", "not hex": "g" * 64}
        for name, value in bad_values.items():
            with self.subTest(binding=name):
                rows = self.filled(blank, ca_a=self.conclusion("verified"))
                rows[3]["package_sha256"] = value  # a valid early conclusion, then the bad row
                before = self.snapshot(self.package_file)
                result = self.refused(self.write_rows(f"bad-{name}.csv", rows), f"row 4 (ca-c): {INVALID}")
                self.assertNotIn(good, result.stderr)
                self.assertEqual(self.snapshot(self.package_file), before)
        mixed = self.filled(blank, ca_a=self.conclusion("verified"))
        mixed[2]["package_sha256"] = other  # one old digest among valid rows
        result = self.refused(self.write_rows("mixed.csv", mixed), f"row 3 (ca-b): {DIFFERENT}")
        self.assertNotIn(other, result.stderr)
        first = self.filled(blank)
        first[1]["package_sha256"], first[3]["package_sha256"] = "", other
        self.refused(self.write_rows("first.csv", first), f"row 2 (ca-a): {INVALID}")
        # Direct calls: null, non-string and missing cells raise without touching package or rows.
        package, rows = synthetic_package(), self.filled(blank, ca_a=self.conclusion("verified"))
        for name, value in (("null", None), ("integer", 5), ("list", [good]), ("bytes", good.encode()),
                            ("missing", KeyError)):
            with self.subTest(direct=name):
                bad = [dict(row) for row in rows]
                if value is KeyError:
                    del bad[1]["package_sha256"]
                else:
                    bad[1]["package_sha256"] = value
                package_before, rows_before = copy.deepcopy(package), copy.deepcopy(bad)
                with self.assertRaises(ValueError) as caught:
                    law.apply_sheet(package, bad)
                self.assertEqual(str(caught.exception), f"row 2 (ca-a): {INVALID}")
                self.assertEqual((package, bad), (package_before, rows_before))
        self.assertFalse(list(self.outdir.iterdir()))

    def test_legacy_header_requires_regeneration_without_override(self):
        blank = self.make_sheet()
        rows = self.filled(blank, ca_a=self.conclusion("verified"))
        legacy = self.write_table("legacy.csv", law.LEGACY_SHEET_COLUMNS,
                                  [[r[c] for c in law.LEGACY_SHEET_COLUMNS] for r in rows])
        self.refused(legacy, LEGACY)
        with self.assertRaisesRegex(ValueError, "^" + LEGACY + "$"):
            law.read_sheet(legacy, synthetic_package())
        legacy_bom = self.write_table("legacy-bom.csv", law.LEGACY_SHEET_COLUMNS,
                                      [[r[c] for c in law.LEGACY_SHEET_COLUMNS] for r in rows], bom=True)
        self.refused(legacy_bom, LEGACY)
        columns = list(law.SHEET_COLUMNS)
        swapped = list(columns)
        swapped[0], swapped[1] = swapped[1], swapped[0]
        for name, header, fragment in (
                ("missing", [c for c in columns if c != "reviewed_on"], "missing ['reviewed_on'], unexpected []"),
                ("legacy-and-missing", [c for c in law.LEGACY_SHEET_COLUMNS if c != "reviewed_on"],
                 "missing ['package_sha256', 'reviewed_on'], unexpected []"),
                ("reordered", swapped, "header must equal the review-sheet columns in order"),
                ("duplicate", columns + ["package_sha256"], "header must equal the review-sheet columns in order"),
                ("extra", columns + ["extra"], "unexpected ['extra']")):
            with self.subTest(header=name):
                path = self.write_table(f"{name}.csv", header, [])
                with self.assertRaises(ValueError) as caught:
                    law.read_sheet(path, synthetic_package())
                self.assertIn(fragment, str(caught.exception))
                self.assertNotIn("no package binding", str(caught.exception))
        # Shape and identity problems are reported before any binding check.
        def blank_row(rule_id):
            return [rule_id] + [""] * (len(columns) - 1)

        stale = blank_row("records_law")
        stale[columns.index("package_sha256")] = law.package_sha256(amended(citation="elsewhere"))
        for name, table, fragment in (
                ("short-cells", [stale, ["ca-a", "x"]], "row 2: expected 19 cells"),
                ("unknown", [stale, blank_row("ca-zz")], "row 2: unknown rule_id 'ca-zz'"),
                ("duplicate-id", [stale, stale], "row 2: duplicate rule_id 'records_law'")):
            with self.subTest(structure=name):
                path = self.write_table(f"{name}.csv", columns, table)
                with self.assertRaisesRegex(ValueError, fragment):
                    law.read_sheet(path, synthetic_package())
                self.refused(path, fragment)

    def test_direct_apply_cannot_bypass_binding(self):
        package = synthetic_package()
        sheet = self.write_rows("filled.csv", self.filled(self.make_sheet(), ca_a=self.conclusion("verified")))
        rows = law.read_sheet(sheet, package)
        rows[2]["duty"] = "A reviewer rewrote this duty"
        newer = amended(citation="Synthetic amended citation")
        before = copy.deepcopy((newer, rows))
        with self.assertRaisesRegex(ValueError, "row 1 \\(records_law\\): " + DIFFERENT):
            law.apply_sheet(newer, rows)
        self.assertEqual((newer, rows), before)
        proposed, report = law.apply_sheet(package, rows)
        self.assertEqual(report["changed"], [{"rule_id": "ca-a", "from": "likely", "to": "verified"}])
        self.assertEqual(report["text_edited"], ["ca-b"])
        expected = synthetic_package()
        expected["rules"][0]["review"] = "verified"
        self.assertEqual(proposed, expected)
        proposed_file = self.tmp / "proposed-direct.json"
        proposed_file.write_text(json.dumps(proposed, indent=2), encoding="utf-8")
        check = subprocess.run([sys.executable, "-B", "tools/check_law_review.py", str(self.package_file),
                                str(proposed_file)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertEqual(json.loads(check.stdout)["allowed_changes"], ["$.rules[ca-a].review"])

    def test_edits_and_spreadsheet_transport_preserve_binding(self):
        first = self.make_sheet("first.csv", as_of="2026-01-11")
        later = self.make_sheet("later.csv", as_of="2026-06-01")
        self.assertNotEqual(self.rows(first)[1]["source_age_days"], self.rows(later)[1]["source_age_days"])
        self.assertEqual({r["package_sha256"] for r in self.rows(first) + self.rows(later)},
                         {law.package_sha256(synthetic_package())})
        notes = 'comma, "quote"\nsecond line \u00e9 \u65e5\u672c'
        rows = self.filled(first, ca_a={**self.conclusion("needs_attorney_review", reviewer_notes=notes),
                                        "duty": "Reviewer rewrote this duty"},
                           ca_c={"remedy": "Changed remedy"})
        for name, bom in (("bom.csv", True), ("plain.csv", False)):
            with self.subTest(name=name):
                sheet = self.write_rows(name, rows, bom=bom)
                if bom:  # spreadsheet export: CRLF row ends
                    sheet.write_bytes(sheet.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
                output = json.loads(self.apply_cli(sheet).stdout)
                self.assertEqual(output["text_edited"], ["ca-a", "ca-c"])
                self.assertEqual(output["changed"], [{"rule_id": "ca-a", "from": "likely",
                                                      "to": "needs_attorney_review"}])
                self.assertEqual(output["reviewers_named"], ["Synthetic Reviewer"])
                proposed = json.loads(self.out.read_text(encoding="utf-8"))
                self.assertEqual(proposed["rules"][0]["duty"], "Duty, ca-a")  # edit listed, not applied
                self.assertNotIn("package_sha256", self.out.read_text(encoding="utf-8"))
                original = json.loads(self.package_file.read_text(encoding="utf-8"))
                for key in ("status", "reviewed_by", "reviewed_at"):
                    self.assertEqual(proposed[key], original[key])
                self.assertEqual(stat.S_IMODE(os.stat(self.out).st_mode), 0o600)
                self.out.unlink()

    def test_packet_binding_and_regeneration(self):
        standalone = self.make_sheet("standalone.csv", as_of="2026-11-03")
        packet = self.tmp / "packet"
        args = ("--jurisdiction", "us-ca", "--out", str(packet), "--as-of", "2026-11-03", "--base", str(self.base))
        self.run_cli(("campaign_tool", "law", "review-packet"), *args)
        self.assertEqual((packet / "WORKSHEET.csv").read_bytes(), standalone.read_bytes())
        self.assertEqual(stat.S_IMODE(packet.stat().st_mode), 0o700)
        self.assertEqual({p.name for p in packet.iterdir()}, {"PACKET.md", "RULES.md", "WORKSHEET.csv", "RETURN.md"})
        for path in packet.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)
        digest = law.package_sha256(synthetic_package())
        instructions = (packet / "RETURN.md").read_text(encoding="utf-8")
        self.assertIn("package_sha256", instructions)
        self.assertIn("never edit or copy it", instructions)
        self.assertIn("builds a new packet", instructions)
        self.assertNotIn(digest, instructions)
        stale = self.write_rows("stale-return.csv", self.filled(packet / "WORKSHEET.csv", ca_a=self.conclusion("verified")))
        self.write_package(amended(citation="Synthetic amended citation"))
        before = self.snapshot(self.package_file, stale)
        self.refused(stale, f"row 1 (records_law): {DIFFERENT}")
        self.assertEqual(self.snapshot(self.package_file, stale), before)
        # A fresh packet from the amended package, with a new human conclusion, succeeds.
        fresh = self.tmp / "packet-fresh"
        self.run_cli(("campaign_tool", "law", "review-packet"), "--jurisdiction", "us-ca", "--out", str(fresh),
                     "--as-of", "2026-11-03", "--base", str(self.base))
        renewed = self.write_rows("fresh-return.csv", self.filled(fresh / "WORKSHEET.csv", ca_a=self.conclusion("verified")))
        self.assertEqual(json.loads(self.apply_cli(renewed).stdout)["changed"],
                         [{"rule_id": "ca-a", "from": "likely", "to": "verified"}])
        self.assertEqual(json.loads(self.out.read_text(encoding="utf-8"))["rules"][0]["citation"],
                         "Synthetic amended citation")


if __name__ == "__main__":
    unittest.main()
