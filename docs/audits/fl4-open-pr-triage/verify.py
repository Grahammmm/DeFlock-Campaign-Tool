"""Verify FL-4's frozen public Git evidence; never execute candidate code.

Run from the repository root with Python 3.11+; full Git objects are required.
Missing PR objects: git fetch --no-tags origin refs/pull/<number>/head
--check-remote checks that the cohort still has the captured heads and states.
"""
import argparse
import ast
from collections import Counter
import json
from pathlib import Path
import re
import subprocess

HERE = Path(__file__).resolve().parent
COHORT = [14, 39, 80, 81, 82, 83, 84, 85, 86, 87, 110, 130, 138, 154]
RELEASE = "602814408e255b3e0649d4c303570f465a6f2aad"  # pragma: allowlist secret


def load_evidence():
    """Expand zero-based JSON indexes into the frozen, unquoted full-ID table."""
    commits = (HERE / "commits.txt").read_text().splitlines()
    for oid in commits:
        if not re.fullmatch(r"[0-9a-f]{40}", oid):
            raise ValueError("Invalid full commit ID")
    if len(set(commits)) != len(commits):
        raise ValueError("Duplicate commit ID")
    used = set()

    def expand(row):
        for key in row.keys() & {"release", "headRefOid", "merge_base", "oid"}:
            index = row[key]
            if type(index) is not int or not 0 <= index < len(commits):
                raise ValueError("Invalid commit index")
            row[key] = commits[index]
            used.add(index)
        return row

    data = json.loads((HERE / "evidence.json").read_text(), object_hook=expand)
    if used != set(range(len(commits))):
        raise ValueError("Unreferenced commit IDs")
    return data


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def blob(ref, path):
    result = subprocess.run(["git", "rev-parse", "--verify", f"{ref}:{path}"],
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def method(ref, path, name):
    text = git("show", f"{ref}:{path}")
    return next(ast.get_source_segment(text, node) for node in ast.walk(ast.parse(text))
                if isinstance(node, ast.FunctionDef) and node.name == name)


def table_row(row):
    overlaps = "; ".join(f"`{path}`" for path in row["overlap_files"]) or "None"
    status = "draft" if row["isDraft"] else "ready"
    return (f"| Grahammmm/DeFlock-Campaign-Tool#{row['number']} ({status}) "
            f"| `{row['headRefOid'][:12]}` | `{row['merge_base'][:12]}` "
            f"| {overlaps} | **{row['verdict']}** — {row['reason']} |")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-remote", action="store_true")
    parser.add_argument("--gh", default="gh")
    args = parser.parse_args()
    data = load_evidence()
    report = (HERE / "README.md").read_text()
    assert data["release"] == RELEASE
    assert data["repository"] == "Grahammmm/DeFlock-Campaign-Tool"
    assert data["scope"] == COHORT
    assert [row["number"] for row in data["prs"]] == COHORT
    assert set(data["open_pr_numbers_at_capture"]) - set(COHORT) == set(data["additional_open_pr_numbers"])
    assert set(COHORT) <= set(data["open_pr_numbers_at_capture"])
    for integration in data["integration_prs"]:
        assert integration["state"] == "MERGED"
        subprocess.run(["git", "merge-base", "--is-ancestor",
                        integration["mergeCommit"]["oid"], RELEASE], check=True)
    for row in data["prs"]:
        head, base = row["headRefOid"], row["merge_base"]
        assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in (head, base))
        assert git("merge-base", "--all", RELEASE, head).splitlines() == [base]
        paths = git("diff", "--name-only", base, head).splitlines()
        release_paths = git("diff", "--name-only", base, RELEASE).splitlines()
        assert paths == row["pr_files"], row["number"]
        assert len(release_paths) == row["release_changed_path_count"]
        assert sorted(set(paths) & set(release_paths)) == row["overlap_files"]
        assert int(git("rev-list", "--count", f"{head}..{RELEASE}")) == row["commits_release_only"]
        assert int(git("rev-list", "--count", f"{RELEASE}..{head}")) == row["commits_pr_only"]
        same, different, absent = [], [], []
        for path in paths:
            current = blob(RELEASE, path)
            if current is None:
                absent.append(path)
            elif current == blob(head, path):
                same.append(path)
            else:
                different.append(path)
        assert same == row["identical_at_release"]
        assert different == row["different_at_release"]
        assert absent == row["absent_at_release"]
        assert row["verdict"] in {"close", "rebase", "supersede"}
        assert table_row(row) in report, row["number"]
        if "observed_checks" in row:
            assert dict(Counter(c["state"] for c in row["observed_checks"])) == row["observed_check_counts"]
        if row["number"] == 83:
            assert row["record_proposal_identical"]
            assert method(head, "campaign_tool/records/run.py", "_record_proposal") == method(RELEASE, "campaign_tool/records/run.py", "_record_proposal")
        if args.check_remote:
            remote = json.loads(subprocess.check_output([
                args.gh, "pr", "view", str(row["number"]), "--repo", data["repository"],
                "--json", "headRefOid,state,isDraft"], text=True))
            assert all(remote[key] == row[key] for key in remote), row["number"]
    pr85 = next(row for row in data["prs"] if row["number"] == 85)
    pr158 = next(row for row in data["integration_prs"] if row["number"] == 158)["mergeCommit"]["oid"]
    paths158 = git("diff", "--name-only", f"{pr158}^1", pr158).splitlines()
    assert paths158 == data["pr158_changed_paths"]
    assert sorted(set(paths158) & set(pr85["pr_files"])) == data["pr85_pr158_overlap"] == []
    assert len(re.findall(r"^\| Grahammmm/DeFlock-Campaign-Tool#", report, re.M)) == len(COHORT)
    counts = dict(sorted(Counter(row["verdict"] for row in data["prs"]).items()))
    print(f"PASS: {len(COHORT)}/14 PRs; merge bases, path overlaps, blob comparisons, successor ancestry, report rows and #85/#158 distinction verified")
    print(f"Verdicts: {json.dumps(counts, sort_keys=True)}")
    print("Semantic disposition and independent review: NOT validated by this script")
    if args.check_remote:
        print("PASS: all 14 remote heads, states and draft flags match the capture")


if __name__ == "__main__":
    main()
