"""Read-only structural checks and checklist for a human law-package reviewer."""
import argparse
from datetime import date
import json
from pathlib import Path
import re
import sys

from campaign_tool import law

CHECKLIST = (
    "Confirm G's named human reviewer and independence from the package author.",
    "Read every cited primary source; check version, effective dates and applicability.",
    "Check each rule's duty, exceptions and remedy; retain unresolved review labels.",
    "Check records-law timing, source dates and request-scope coverage separately.",
    "Only the human may attest reviewed_by, reviewed_at and each verified label.",
    "Run this check and the existing law-review diff check; seek maintainer review.",
)
PLACEHOLDERS = {"unknown", "none", "null", "n/a", "tbd", "reviewer", "human", "person",
                "ai", "model", "assistant", "bot", "chatgpt", "claude", "codex", "qwen"}


def _person(value):
    # A name/handle is an assertion, not authenticated identity; the maintainer checks it.
    return (isinstance(value, str) and bool(value.strip())
            and any(char.isalpha() for char in value)
            and all(char.isprintable() for char in value)
            and value.strip().casefold() not in PLACEHOLDERS)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def check(path):
    """Return (validated package, review error codes); never edit the package."""
    path = Path(path)
    package = json.loads((path / "package.json").read_text(encoding="utf-8"), object_pairs_hook=_pairs)
    law.validate(package, law.load_schema())
    errors = []
    if package["status"] == "reviewed":
        names = package["reviewed_by"]
        if not names or not all(_person(name) for name in names):
            errors.append("REVIEWER_MISSING")
        elif len({name.strip().casefold() for name in names}) != len(names):
            errors.append("REVIEWER_DUPLICATE")
        try:
            value = package["reviewed_at"]
            if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                raise ValueError("date required")
            if date.fromisoformat(value) > date.today():
                raise ValueError("future review date")
        except ValueError:
            errors.append("REVIEW_DATE_INVALID")
        if not package["rules"]:
            errors.append("RULES_MISSING")
        for rule in package["rules"]:
            if rule["review"] != "verified":
                errors.append("RULE_UNVERIFIED " + rule["rule_id"])
            if not rule["citation"].strip():
                errors.append("RULE_CITATION_MISSING " + rule["rule_id"])
    # Reuse the canonical date, jurisdiction, duplicate-id and scope checks. A draft
    # copy permits our stable missing-reviewer/date codes without weakening validation.
    validation = dict(package, status="draft", reviewed_at=None) if errors else package
    law._semantic_checks(validation, path.name)
    return package, errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("check", help="Check a package directory; write nothing")
    command.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    print("Human reviewer's checklist (not legal acceptance):")
    for item in CHECKLIST:
        print("[ ] " + item)
    try:
        package, errors = check(args.directory)
    except (OSError, ValueError, RecursionError) as exc:
        print("PACKAGE_INVALID " + type(exc).__name__, file=sys.stderr)
        return 2
    rules = package["rules"]
    labeled = sum(rule["review"] == "verified" for rule in rules)
    if package["status"] == "draft":
        print(f"draft: 0 of {len(rules)} rules verified")
        print(f"Author labels: {labeled} of {len(rules)} verified; no package-level human acceptance.")
    for rule in rules:
        print(f"[ ] {rule['rule_id']} (label: {rule['review']})")
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 2
    if package["status"] == "reviewed":
        print("OK")
    print("Structural check only: does not authenticate a person or establish legal correctness.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
