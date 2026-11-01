"""Structural check for a law-package review change (docs/LAW-PACKAGES.md step 1).

Usage: python3 tools/check_law_review.py OLD.json NEW.json
Exit 0 only when NEW differs from OLD in review fields alone (top-level status, reviewed_by,
reviewed_at; per rule, matched by rule_id: review, duty, exceptions, remedy) and NEW is
schema-valid; 3 lists each other changed JSON path or error; 2 means unreadable input.
"""
import argparse
import json
import os
import stat
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from campaign_tool import law  # noqa: E402

MAX_BYTES = 2 * 1024 * 1024
TOP_ALLOWED = ("status", "reviewed_by", "reviewed_at")
RULE_ALLOWED = ("review", "duty", "exceptions", "remedy")
NOTE = "structural check only; it does not review the law or confirm reviewer independence"
MISSING = object()


class InputError(Exception):
    """Refused or unreadable input (exit 2)."""


def read_file(path):
    if not stat.S_ISREG(os.lstat(path).st_mode):  # lstat: a symlink is not regular
        raise InputError(f"{path}: symlinks and non-regular files are refused")
    with open(path, "rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise InputError(f"{path}: larger than {MAX_BYTES} bytes")
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise InputError(f"{path}: not valid UTF-8 JSON") from None
    if not isinstance(document, dict):
        raise InputError(f"{path}: top level is not a JSON object")
    return document


def _diff(old, new, where, out):
    """Append every JSON path where ``old`` and ``new`` differ (MISSING = absent key)."""
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            _diff(old.get(key, MISSING), new.get(key, MISSING), f"{where}.{key}", out)
    elif isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        for index, (left, right) in enumerate(zip(old, new)):
            _diff(left, right, f"{where}[{index}]", out)
    elif type(old) is not type(new) or old != new:
        out.append(where)


def _rules_by_id(rules):
    """Map rule_id -> rule, or None when rules cannot be matched by a unique id."""
    if not isinstance(rules, list):
        return None
    ids = [r.get("rule_id") if isinstance(r, dict) else None for r in rules]
    ok = all(isinstance(rid, str) for rid in ids) and len(set(ids)) == len(ids)
    return dict(zip(ids, rules)) if ok else None


def compare(old, new, jurisdiction=None, today=None):
    allowed, disallowed, errors = [], [], []
    for key in sorted(set(old) | set(new)):
        where, left, right = f"$.{key}", old.get(key, MISSING), new.get(key, MISSING)
        if key in TOP_ALLOWED:
            if type(left) is not type(right) or left != right:
                allowed.append(where)
            continue
        old_rules = _rules_by_id(left) if key == "rules" else None
        new_rules = _rules_by_id(right) if key == "rules" else None
        if old_rules is None or new_rules is None:
            _diff(left, right, where, disallowed)
            continue
        disallowed += [f"$.rules[{rid}] (added)" for rid in new_rules if rid not in old_rules]
        disallowed += [f"$.rules[{rid}] (removed)" for rid in old_rules if rid not in new_rules]
        common = [rid for rid in old_rules if rid in new_rules]
        if common != [rid for rid in new_rules if rid in old_rules]:
            disallowed.append("$.rules (order)")
        for rid in common:
            for field in sorted(set(old_rules[rid]) | set(new_rules[rid])):
                path = f"$.rules[{rid}].{field}"
                a, b = old_rules[rid].get(field, MISSING), new_rules[rid].get(field, MISSING)
                if field in RULE_ALLOWED and a is not MISSING and b is not MISSING:
                    if a != b:
                        allowed.append(path)
                else:
                    _diff(a, b, path, disallowed)
    try:
        law.validate(new, law.load_schema())
        law._semantic_checks(new, jurisdiction or new["jurisdiction"])
    except ValueError as exc:
        errors.append(str(exc))
    if not errors and new["status"] == "reviewed":
        names = [name.strip() for name in new["reviewed_by"]]
        if not names or not all(names):
            errors.append("$.reviewed_by: a reviewed package needs non-blank reviewer names")
        elif len(set(names)) != len(names):
            errors.append("$.reviewed_by: reviewer names must be distinct")
        if law.parse_date(new["reviewed_at"], "$.reviewed_at") > (today or date.today()):
            errors.append("$.reviewed_at: review date is after today")
    downgrade = old.get("status") == "reviewed" and new.get("status") == "draft"
    return {"ok": not (disallowed or errors), "allowed_changes": allowed,
            "disallowed": disallowed, "errors": errors, "downgrade": downgrade, "note": NOTE}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Structural check for a law-package review change.")
    parser.add_argument("old", metavar="OLD.json")
    parser.add_argument("new", metavar="NEW.json")
    args = parser.parse_args(argv)
    try:
        old, new = read_file(args.old), read_file(args.new)
    except (InputError, OSError) as exc:
        print(f"check_law_review: error: {exc}", file=sys.stderr)
        return 2
    directory = Path(args.new).parent.name
    result = compare(old, new, directory if law._valid_jurisdiction(directory) else None)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
