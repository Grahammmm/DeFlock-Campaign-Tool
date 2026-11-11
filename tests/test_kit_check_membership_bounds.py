"""kit-check scope/citation differences use membership indexes, not repeated opposite-list scans."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit_check  # noqa: E402

STATUS_LINE = "DRAFT ONLY. Law package status: reviewed."


class Counted(str):
    """Built-in string semantics; counts equality and hash operations in the shared ``ops`` list."""
    ops = None

    def __eq__(self, other):
        self.ops[0] += 1
        return str.__eq__(self, other)

    def __ne__(self, other):
        self.ops[0] += 1
        return str.__ne__(self, other)

    def __hash__(self):
        self.ops[0] += 1
        return str.__hash__(self)


def draft(scopes=(), rules=(), body=""):
    lines = ["# Draft records request: Synthetic PD", "", STATUS_LINE, "", "To: records@example.org",
             "Subject: Records concerning ALPR, 2023-01-01", ""]
    for scope in scopes:
        lines += [scope, "", "- item"]
    lines += ["", "The date range for this request is 2023-01-01 through today.", "", "withholding. Please advise",
              "before incurring fees beyond $50.", "", body,
              "## Statutory context (for the requester, not for sending)", "", "- Fee basis: x", "",
              "Rules the scopes relate to:", *rules, "", "Operator checklist: x", ""]
    return "\n".join(lines).encode("utf-8")


def differs(primary, suggested, counted=False):
    """(suggested_differs object, operation count) with the real _summary values wrapped when counted."""
    ops = [0]
    real = kit_check._summary

    def wrapper(data):
        summary = real(data)
        if counted:
            kind = type("C", (Counted,), {"ops": ops})
            for key in ("scopes", "citations"):
                summary[key] = [kind(x) for x in summary[key]]
        return summary

    with tempfile.TemporaryDirectory(prefix="synthetic-membership-") as tmp:
        folder = Path(tmp) / "kit" / "requests.suggested"
        folder.mkdir(parents=True)
        (folder / "ca-a-police.md").write_bytes(suggested)
        with mock.patch.object(kit_check, "_summary", wrapper):
            result = kit_check._suggested_differs(Path(tmp), "ca-a-police", primary)
    return result, ops[0]


def items(prefix, count, fmt):
    return [fmt.format(prefix=prefix, i=i) for i in range(count)]


class MembershipTests(unittest.TestCase):
    def check_linear(self, n, mine_scopes, fresh_scopes, mine_rules, fresh_rules):
        primary = draft(mine_scopes, mine_rules)
        suggested = draft(fresh_scopes, fresh_rules)
        total = len(mine_scopes) + len(fresh_scopes) + len(mine_rules) + len(fresh_rules)
        plain, _ = differs(primary, suggested)
        counted, ops = differs(primary, suggested, counted=True)
        self.assertEqual(counted, plain)
        self.assertLessEqual(ops, 40 * total + 40, f"n={n}: {ops} string operations")
        return plain

    def test_disjoint_scope_and_citation_membership_work_is_linear(self):
        for n in (128, 256, 512):
            ms, fs = items("mine", n, "## {prefix}{i}"), items("fresh", n, "## {prefix}{i}")
            mr, fr = items("m", n, "- {prefix}{i}: Cite"), items("f", n, "- {prefix}{i}: Cite")
            for label, args in (("scopes", (ms, fs, [], [])), ("citations", ([], [], mr, fr)),
                                ("both", (ms, fs, mr, fr))):
                with self.subTest(n=n, case=label):
                    result = self.check_linear(n, *args)
                    self.assertEqual(result["scopes_added"], args[1])
                    self.assertEqual(result["scopes_removed"], args[0])
                    self.assertEqual(result["rule_citations_added"], args[3])
                    self.assertEqual(result["rule_citations_removed"], args[2])

    def test_overlapping_reordered_and_mixed_memberships_preserve_source_order(self):
        a = ["## zeta", "## alpha", "## mid", "## alpha"]
        reordered = self.check_linear(0, a, ["## mid", "## alpha", "## zeta"], ["- r2: B", "- r1: A"],
                                      ["- r1: A", "- r2: B"])
        self.assertEqual((reordered["scopes_added"], reordered["scopes_removed"], reordered["rule_citations_added"],
                          reordered["rule_citations_removed"]), ([], [], [], []))
        mixed = self.check_linear(0, ["## b", "## a", "## c"], ["## z", "## a", "## y", "## z"],
                                  ["- r9: X", "- r1: Same", "- r5: Old"], ["- r7: N2", "- r1: Same", "- r3: N1"])
        self.assertEqual(mixed["scopes_added"], ["## z", "## y"])
        self.assertEqual(mixed["scopes_removed"], ["## b", "## c"])
        self.assertEqual(mixed["rule_citations_added"], ["- r7: N2", "- r3: N1"])
        self.assertEqual(mixed["rule_citations_removed"], ["- r9: X", "- r5: Old"])

    def test_category_and_exact_text_identity_remain_distinct(self):
        result, _ = differs(draft(["## Policies", "## policies", "## Pol  icies"], ["- r1: Cite A", "- r2: Same"]),
                            draft(["## Policies", "## Policies ", "## Pölicies"], ["- r1: Cite B", "- r2: Same"]))
        self.assertEqual(result["scopes_added"], ["## Policies ", "## Pölicies"])
        self.assertEqual(result["scopes_removed"], ["## policies", "## Pol  icies"])
        self.assertEqual(result["rule_citations_added"], ["- r1: Cite B"])
        self.assertEqual(result["rule_citations_removed"], ["- r1: Cite A"])

    def test_body_only_difference_still_warns_with_empty_deltas(self):
        result, _ = differs(draft(["## A"], ["- r1: X"], body="one"), draft(["## A"], ["- r1: X"], body="two"))
        self.assertEqual({k: result[k] for k in result if k != "path" and k != "compare_command"},
                         {"scopes_added": [], "scopes_removed": [], "date_range_differs": False,
                          "fee_cap_differs": False, "law_package_status_differs": False,
                          "rule_citations_added": [], "rule_citations_removed": []})

    def test_identical_and_absent_do_not_build_summaries(self):
        with mock.patch.object(kit_check, "_summary", side_effect=AssertionError("summary built")):
            with tempfile.TemporaryDirectory(prefix="synthetic-membership-") as tmp:
                self.assertIsNone(kit_check._suggested_differs(Path(tmp), "ca-a-police", draft(["## A"])))
                folder = Path(tmp) / "kit" / "requests.suggested"
                folder.mkdir(parents=True)
                (folder / "ca-a-police.md").write_bytes(draft(["## A"]))
                self.assertIsNone(kit_check._suggested_differs(Path(tmp), "ca-a-police", draft(["## A"])))

    def test_large_admitted_real_cli_is_complete_and_read_only(self):
        n = 512
        ms, fs = items("mine", n, "## {prefix}{i}"), items("fresh", n, "## {prefix}{i}")
        mr, fr = items("m", n, "- {prefix}{i}: Cite"), items("f", n, "- {prefix}{i}: Cite")
        with tempfile.TemporaryDirectory(prefix="synthetic-membership-") as tmp:
            root = Path(tmp)
            (root / "kit" / "requests").mkdir(parents=True)
            (root / "kit" / "requests.suggested").mkdir()
            (root / "kit" / "agencies.json").write_text(json.dumps([
                {"agency_id": "ca-a-police", "name": "a", "kind": "police", "selected": True, "verified": True}]),
                encoding="utf-8")
            (root / "kit" / "requests" / "ca-a-police.md").write_bytes(draft(ms, mr))
            (root / "kit" / "requests.suggested" / "ca-a-police.md").write_bytes(draft(fs, fr))
            before = {str(p): (p.lstat().st_mode, p.lstat().st_mtime_ns, None if p.is_dir() else p.read_bytes())
                      for p in sorted(root.rglob("*"))}
            out, err, code = io.StringIO(), io.StringIO(), 0
            argv = ["campaign_tool", "kit-check", "--directory", str(root), "--json"]
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err), \
                    mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used")):
                try:
                    cli.main()
                except SystemExit as exc:
                    code = exc.code
            after = {str(p): (p.lstat().st_mode, p.lstat().st_mtime_ns, None if p.is_dir() else p.read_bytes())
                     for p in sorted(root.rglob("*"))}
        self.assertEqual((code, err.getvalue()), (3, "Read-only. Nothing was sent.\n"))
        self.assertTrue(before == after)
        report = json.loads(out.getvalue())
        self.assertEqual(report["problems"], 1)
        diff = report["agencies"][0]["suggested_differs"]
        self.assertTrue(diff["scopes_added"] == fs and diff["scopes_removed"] == ms)
        self.assertTrue(diff["rule_citations_added"] == fr and diff["rule_citations_removed"] == mr)


if __name__ == "__main__":
    unittest.main()
