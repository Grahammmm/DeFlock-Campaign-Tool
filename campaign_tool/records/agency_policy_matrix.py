"""Policy-and-law requirement skeleton for a new agency (POLICY-MATRIX.tsv).

Lists the law-package rules and one agency's stored policy sections as requirements.
The evidence, verdict and confidence columns are always empty: a person fills them.
Reads the policy register and the law package; writes only a new --out file. It sends,
publishes and approves nothing, and keeps no network access.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

from campaign_tool.records import agency_policies, law_package_check, playbook

HEADER = ["id", "tier", "family", "clause", "obligation", "cite", "review", "evidence", "verdict", "confidence"]


def _cell(value):
    return re.sub(r" +", " ", re.sub(r"[\t\r\n]", " ", str(value)))


def _tier(book, text):
    tier = playbook.tier_for(book, text)
    return "" if tier is None else str(tier)


def build_matrix(rules, policy_rows, book):
    """Law rows first (package order), then policy rows (file order); last three cells empty."""
    out = [list(HEADER)]
    for rule in rules:
        out.append([rule["rule_id"], _tier(book, rule["activity"] + " " + rule["duty"]), "law", rule["citation"],
                    rule["duty"], "VERIFIED-IN-PACKAGE" if rule["review"] == "verified" else "UNVERIFIED",
                    rule["review"], "", "", ""])
    for row in policy_rows:
        rid = ("POLICY:%s:%s" % (row["policy_id"], row["section"])).replace(" ", "-")
        out.append([rid, _tier(book, row["quote"]), "agency-policy", agency_policies.cite(row), row["quote"],
                    "UNVERIFIED", "n/a", "", "", ""])
    return [[_cell(c) for c in line] for line in out]


def _refuse(code):
    print("refused " + code, file=sys.stderr)
    return 2


def _write_new(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="records agency-policy-matrix", description=__doc__)
    ap.add_argument("--policies", required=True)
    ap.add_argument("--agency", required=True)
    ap.add_argument("--package", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    if not agency_policies.AGENCY_RE.match(args.agency):
        return _refuse("policy_matrix_agency")
    if os.path.lexists(args.out):
        return _refuse("policy_matrix_out_exists")
    rows, problems = agency_policies.load(args.policies)
    if problems:
        print("refused policy_matrix_policies (%d problems)" % len(problems), file=sys.stderr)
        return 2
    mine = agency_policies.sections(rows, args.agency)
    if not mine:
        text = "no stored policy: %s\n" % args.agency
        status, nlaw = None, 0
    else:
        package_dir = Path(args.package)
        if package_dir.name == "package.json":
            package_dir = package_dir.parent
        try:
            package, errors = law_package_check.check(package_dir)
            if errors:
                return _refuse("policy_matrix_package")
            book = playbook.load()
        except (OSError, ValueError, KeyError, TypeError, playbook.PlaybookError):
            return _refuse("policy_matrix_package")
        matrix = build_matrix(package["rules"], mine, book)
        text = "".join("\t".join(line) + "\n" for line in matrix)
        status = package["status"]
        nlaw = len(package["rules"])
    try:
        _write_new(args.out, text)
    except OSError:
        return _refuse("policy_matrix_out_exists")
    if status is None:
        print("policy-matrix: 0 law rows, 0 policy rows, package status not read")
    else:
        print("policy-matrix: %d law rows, %d policy rows, package status %s" % (nlaw, len(mine), status))
    return 0


if __name__ == "__main__":
    sys.exit(main())
