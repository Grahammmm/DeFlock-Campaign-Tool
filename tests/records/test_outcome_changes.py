"""Contracts for ``records outcomes-diff`` (DF1117L-13): identities only, section-local refusal, strict run order.

``write_run_pair(root)`` is the reusable synthetic handoff (old, new run folders) for the console view.
"""
import contextlib
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.records import cli
from campaign_tool.records.outcomes import changes

OLD_ID, NEW_ID = "run_20300101T000000Z_aaaaaaaa", "run_20300102T000000Z_bbbbbbbb"
FILES = {"agency-factsheets.json": ("records-agency-factsheets-v1", True),
         "comparison.json": ("records-agency-comparison-v1", True),
         "CURRENT-REQUEST-REGISTER.json": ("records-request-register-v1", False),
         "outcomes/received-items.json": ("records-received-item-v1", True),
         "outcomes/gap-proposals.json": ("records-gap-proposals-v1", True)}
A, A2, B = "a" * 12 + "0" * 52, "a" * 12 + "1" * 52, "b" * 64  # A and A2 share a 12-hex prefix
SECRETS = ("QUOTE-ONE", "SECRETNAME", "SECRETEXPECTED", "SECRETEVID", "SECRETKIND", "SECRET TRACKER")


def digest(name):
    return hashlib.sha256(name.encode()).hexdigest()


def statement(sha, page, quote="QUOTE-ONE"):
    return {"quote": quote, "citation": {"document_sha256": sha, "page": page, "unit_id": "u"}}


def row(agency, field, status, reason, **request):
    return {"agency_id": agency, "field": field, "status": status, "reason": reason,
            "expected": "SECRETEXPECTED", "evidence": [{"quote": "SECRETEVID"}], **request}


def request(key, rid, state, due, determined=None):
    return {"agency_key": key, "request_id": rid, "agency": "SECRETNAME",
            "deadline": {"state": state, "due": due, "determination_at": determined}}


def proposal(name, agency, rid):
    return {"proposal_id": digest(name), "agency_id": agency, "request_id": rid, "quote": "QUOTE-ONE"}


def content(new):
    """Producer-shaped bodies; `new` applies exactly one change of each kind to the old state."""
    vendor = ([statement(A, 1), statement(A, 1, "QUOTE-ONE-again"), statement(A, 4), statement(A2, 1)]
              if new else [statement(A, 1), statement(A, 2)])
    tracker = "tracker:SECRET TRACKER NAME"
    return {
        "agency-factsheets.json": {"agencies": [
            {"agency_id": "a1", "agency": "SECRETNAME", "fields": {
                "vendor": {"statements": vendor}, "cost": {"statements": [statement(B, 3)]}}},
            {"agency_id": "a2", "agency": "SECRETNAME", "fields": {}}]},
        "comparison.json": {"rows": [
            row("a1", "retention", "gap" if new else "unknown", "documentary_criterion_discrepancy" if new else "missing_or_unqualified_evidence"),
            row("a1", "audits", "meets", "documentary_criteria_match_v2" if new else "documentary_criteria_match"),
            row("a1", "cpra_response_deadline", "unknown", "b_reason" if new else "a_reason", request_id="R1"),
            row("a2", "cpra_response_deadline", "meets" if new else "unknown", "r", request_id=None),
            row("a2", "vendor", "unknown", "same")]},
        "CURRENT-REQUEST-REGISTER.json": {"requests": [
            request("a1", "R1", "past_due_reminder" if new else "running", "2030-01-10"),
            request("a1", "R2", "determined" if new else "running", "2030-01-10", "2030-01-09" if new else None),
            request(tracker, "T1", "past_due_reminder" if new else "running", None),
            request("a2", "R3", "running", "2030-02-01")]},
        "outcomes/received-items.json": {"items": [
            {"item_id": digest("i1"), "agency_id": "a1", "kind": "SECRETKIND"},
            {"item_id": digest("i2"), "agency_id": "a1" if new else None, "kind": "SECRETKIND"}]
            + ([{"item_id": digest("i3"), "agency_id": "a2", "kind": "SECRETKIND"}] if new else [])},
        "outcomes/gap-proposals.json": {"agencies": [{"agency_id": "a1", "requests": [
            {"request_id": "R1", "proposals": [proposal("p1", "a1", "R1")] + [proposal("p4" if new else "p2", "a1", "R1")]},
            {"request_id": None, "proposals": [] if new else [proposal("p3", "a1", None)]}]}]}}


def flip(value):
    if isinstance(value, list):
        return [flip(item) for item in reversed(value)]
    return {key: flip(item) for key, item in value.items()} if isinstance(value, dict) else value


def write_files(directory, new, reverse=False):
    directory = Path(directory)
    for name, body in content(new).items():
        schema, bound = FILES[name]
        body = {"schema": schema, **({"run_id": directory.name} if bound else {}), **(flip(body) if reverse else body)}
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(json.dumps(body), encoding="utf-8")


def write_run_pair(root, same=False, reverse=False):
    """Old and new run folders under root/run-outputs; `same` gives both the old content."""
    old, new = (Path(root) / "run-outputs" / run for run in (OLD_ID, NEW_ID))
    write_files(old, False)
    write_files(new, not same, reverse)
    return old, new


def tree(root):
    return {str(p): p.read_bytes() for p in sorted(Path(root).rglob("*")) if p.is_file()}


class ChangesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_each_section_reports_exact_identity_delta(self):
        old, new = write_run_pair(self.root)
        def pre(name):
            return digest(name)[:12]
        result = changes.diff(old, new)
        self.assertEqual(result, {
            "schema": "records-outcome-changes-v1", "previous_run_id": OLD_ID, "current_run_id": NEW_ID,
            "facts": {"added": [{"agency_id": "a1", "field": "vendor", "document_prefix": "a" * 12, "page": 4},
                                {"agency_id": "a1", "field": "vendor", "document_prefix": "a" * 12, "page": 1}],
                      "removed": [{"agency_id": "a1", "field": "vendor", "document_prefix": "a" * 12, "page": 2}]},
            "comparison": {"changed": [
                {"agency_id": "a1", "field": "audits", "request_id": None,
                 "before": {"status": "meets", "reason": "documentary_criteria_match"},
                 "after": {"status": "meets", "reason": "documentary_criteria_match_v2"}},
                {"agency_id": "a1", "field": "cpra_response_deadline", "request_id": "R1",
                 "before": {"status": "unknown", "reason": "a_reason"}, "after": {"status": "unknown", "reason": "b_reason"}},
                {"agency_id": "a1", "field": "retention", "request_id": None,
                 "before": {"status": "unknown", "reason": "missing_or_unqualified_evidence"},
                 "after": {"status": "gap", "reason": "documentary_criterion_discrepancy"}},
                {"agency_id": "a2", "field": "cpra_response_deadline", "request_id": None,
                 "before": {"status": "unknown", "reason": "r"}, "after": {"status": "meets", "reason": "r"}}]},
            "register": {"changed": [
                {"agency_id": "a1", "request_id": "R1",
                 "before": {"state": "running", "due": "2030-01-10", "determination_at": None},
                 "after": {"state": "past_due_reminder", "due": "2030-01-10", "determination_at": None}},
                {"agency_id": "a1", "request_id": "R2",
                 "before": {"state": "running", "due": "2030-01-10", "determination_at": None},
                 "after": {"state": "determined", "due": "2030-01-10", "determination_at": "2030-01-09"}},
                {"agency_id": None, "request_id": "T1",
                 "before": {"state": "running", "due": None, "determination_at": None},
                 "after": {"state": "past_due_reminder", "due": None, "determination_at": None}}]},
            "received": {"added": [{"agency_id": "a1", "item_prefix": pre("i2")}, {"agency_id": "a2", "item_prefix": pre("i3")}]},
            "proposals": {"added": [{"agency_id": "a1", "request_id": "R1", "proposal_prefix": pre("p4")}],
                          "removed": [{"agency_id": "a1", "request_id": None, "proposal_prefix": pre("p3")},
                                      {"agency_id": "a1", "request_id": "R1", "proposal_prefix": pre("p2")}]}})
        # Input order never changes the output (identity sort, not file order).
        flipped = self.root / "flipped"
        self.assertEqual(result, changes.diff(*write_run_pair(flipped, reverse=True)))

    def test_identical_outcomes_with_distinct_run_ids_have_empty_lists(self):
        old, new = write_run_pair(self.root, same=True, reverse=True)
        result = changes.diff(old, new)
        empty = {"facts": ["added", "removed"], "comparison": ["changed"], "register": ["changed"],
                 "received": ["added"], "proposals": ["added", "removed"]}
        self.assertEqual({k: {n: [] for n in v} for k, v in empty.items()}, {k: result[k] for k in empty})
        self.assertEqual(json.dumps(result, sort_keys=True), json.dumps(changes.diff(old, new), sort_keys=True))

    def test_missing_previous_file_does_not_zero_other_sections(self):
        old, new = write_run_pair(self.root)
        (old / "comparison.json").unlink()
        (new / "outcomes" / "gap-proposals.json").unlink()
        result = changes.diff(old, new)
        self.assertEqual(result["comparison"], "unavailable: previous_missing")
        self.assertEqual(result["proposals"], "unavailable: current_missing")
        self.assertEqual(len(result["register"]["changed"]), 3)
        (new / "outcomes" / "received-items.json").unlink()
        (new / "outcomes").rmdir()
        self.assertEqual(changes.diff(old, new)["received"], "unavailable: current_missing")

    def test_schema_run_and_shape_errors_are_section_local(self):
        def mutate(directory, name, edit):
            path = directory / name
            path.write_text(edit(path.read_text(encoding="utf-8")), encoding="utf-8")
        cases = [("comparison.json", "current_schema_mismatch", lambda t: t.replace("agency-comparison-v1", "agency-comparison-v2")),
                 ("agency-factsheets.json", "current_run_mismatch", lambda t: t.replace(NEW_ID, OLD_ID)),
                 ("agency-factsheets.json", "current_invalid", lambda t: t[:40]),
                 ("agency-factsheets.json", "current_invalid", lambda t: t.replace('{"schema"', '{"run_id": "x", "run_id": "y", "schema"', 1)),
                 ("agency-factsheets.json", "current_invalid", lambda t: t.replace(B, "B" * 64)),
                 ("agency-factsheets.json", "current_invalid", lambda t: t.replace('"page": 3', '"page": true')),
                 ("comparison.json", "current_invalid", lambda t: t.replace('"unknown"', '"SECRET-STATUS"', 1)),
                 ("CURRENT-REQUEST-REGISTER.json", "current_invalid", lambda t: t.replace("2030-02-01", "2030-2-1")),
                 ("CURRENT-REQUEST-REGISTER.json", "previous_schema_mismatch", None)]
        for name, code, edit in cases:
            with self.subTest(name=name, code=code), tempfile.TemporaryDirectory() as root:
                old, new = write_run_pair(root)
                if edit is None:
                    mutate(old, name, lambda t: t.replace("request-register-v1", "other-v1"))
                else:
                    mutate(new, name, edit)
                result = changes.diff(old, new)
                section = {"agency-factsheets.json": "facts", "comparison.json": "comparison",
                           "CURRENT-REQUEST-REGISTER.json": "register"}[name]
                self.assertEqual(result[section], "unavailable: " + code)
                self.assertEqual(result["received"]["added"][0]["agency_id"], "a1")  # other sections still diff
                self.assertNotIn("SECRET-STATUS", json.dumps(result))
        old, new = write_run_pair(self.root)
        (new / "comparison.json").unlink()
        (new / "comparison.json").symlink_to(old / "comparison.json")
        (new / "agency-factsheets.json").unlink()
        (new / "agency-factsheets.json").mkdir()
        result = changes.diff(old, new)
        self.assertEqual((result["comparison"], result["facts"]), ("unavailable: current_invalid",) * 2)
        self.assertIsInstance(result["register"], dict)

    def test_previous_must_be_strictly_older(self):
        old, new = write_run_pair(self.root)
        same_second = new.parent / "run_20300101T000000Z_cccccccc"
        same_second.mkdir()
        bad_calendar = new.parent / "run_20300230T000000Z_dddddddd"
        bad_calendar.mkdir()
        link = new.parent / "run_20300103T000000Z_eeeeeeee"
        link.symlink_to(new)
        other = self.root / "elsewhere" / NEW_ID
        other.mkdir(parents=True)
        for previous, current, code in ((new, old, "run_order_invalid"), (old, old, "run_order_invalid"),
                                        (old, same_second, "run_order_invalid"), (same_second, old, "run_order_invalid"),
                                        (old, bad_calendar, "run_id_invalid"), (old, other, "run_id_invalid"),
                                        (old, link, "run_directory_unavailable"),
                                        (old, new.parent / "run_20300104T000000Z_ffffffff", "run_directory_unavailable"),
                                        (old, self.root / ".." / "run-outputs" / NEW_ID, "run_id_invalid")):
            with self.subTest(code=code, current=current.name), self.assertRaises(changes.ChangesError) as caught:
                changes.diff(previous, current)
            self.assertEqual(caught.exception.code, code)

    def test_cli_stdout_only_suppresses_quotes_and_hashes(self):
        old, new = write_run_pair(self.root)
        before = tree(self.root)
        self.addCleanup(os.umask, os.umask(0o022))

        def run(*arguments):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(sys, "argv", ["records", "outcomes-diff", *arguments]), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main()
            self.assertEqual(err.getvalue(), "")
            return code, out.getvalue()
        code, text = run("--previous", str(old), "--current", str(new))
        self.assertEqual(code, 0)
        self.assertIn("facts: 2 added, 1 removed", text)
        self.assertIn("~ a1 R1 running/2030-01-10/- -> past_due_reminder/2030-01-10/-", text)
        code, raw = run("--previous", str(old), "--current", str(new), "--json")
        self.assertEqual((code, json.loads(raw)["schema"], raw.endswith("\n")), (0, "records-outcome-changes-v1", True))
        for output in (text, raw):
            self.assertIsNone(re.search(r"[0-9a-f]{13,}", output))
            for secret in SECRETS + (str(self.root),):
                self.assertNotIn(secret, output)
        self.assertEqual(run("--previous", str(new), "--current", str(old)), (1, "refused: run_order_invalid\n"))
        self.assertEqual(run("--previous", str(new), "--current", str(old), "--json"), (1, '{"error": "run_order_invalid"}\n'))
        self.assertEqual(tree(self.root), before)


if __name__ == "__main__":
    unittest.main()
