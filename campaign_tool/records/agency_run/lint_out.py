"""Refuse an outgoing agency letter that carries our own team's names or contacts, or internal working marks.

REC-LINT-OUT-1 rules L8 (team strings, contacts not on the allow file) and L10 (internal residue). A mechanical check only:
nothing is written, and output is `LINE RULE` rows - never the matched text, a list entry, or an input path. Exit 1 on a hit,
0 when clean (no output), 2 on a bad argument or file (one fixed stderr line). A clean result is not a send approval.
"""
import argparse
import re
import sys

from ..denial_reply_check import INTERNAL_IDS
from ..draft_residue import codes
from ...privacy_patterns import AT, BDOT, EMAIL, PHONE
from .lint_cites import LABEL

RULES_VERSION = "1"  # bump on any rule change: agency lint receipts bind it
MAX_DRAFT, MAX_LIST = 2 * 1024 * 1024, 64 * 1024
REFUSED = "refused agency_lint_out_input"
PREFIX = re.compile(r"\b(?:INBOX|Sent):")
RUN = re.compile(r"[a-z0-9._%+\-/!#$&'*=?^`{|}~]*$")  # every RFC 5322 atext character, including the valid "/"
ROW_ID = re.compile(r"(?<![\w-])[A-Z]{2,6}-[0-9]{3,}(?![\w-])")


class QuietParser(argparse.ArgumentParser):
    """Argument errors print one fixed line, never the offending value."""

    def __init__(self, *args, refusal=REFUSED, **kwargs):
        super().__init__(*args, allow_abbrev=False, **kwargs)
        self.refusal = refusal

    def error(self, message):
        print(self.refusal, file=sys.stderr)
        raise SystemExit(2)


def _read(path, limit):
    with open(path, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError
    return data.decode("utf-8-sig")


def _entries(path):
    lines = (line.strip() for line in _read(path, MAX_LIST).splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def _email(text):
    text = re.sub(BDOT, ".", re.sub(AT, "@", text, flags=re.I), flags=re.I)
    text = re.sub(r"\s+", "", text).lower().strip("<>()[]{}\"',;:")
    local, at, domain = text.rpartition("@")
    # The shared EMAIL match can swallow a preceding label ("Contact:records@..", "mailto:records@..") into the local part;
    # keep only the trailing run of address characters, so the whole address is still compared but the label is not part of it.
    return (RUN.search(local).group() + at + domain) if at else text


def _phone(text):
    digits = re.sub(r"\D", "", text)
    return digits[1:] if len(digits) == 11 and digits[0] == "1" else digits


def _canon(text):
    return ("email", _email(text)) if EMAIL.fullmatch(text) else ("phone", _phone(text)) if PHONE.fullmatch(text) else None


def _literal(entry):
    body = r"\s+".join(re.escape(part) for part in entry.split())
    return re.compile(("(?<!\\w)" if re.match(r"\w", entry) else "") + body + ("(?!\\w)" if re.search(r"\w$", entry) else ""), re.I)


def _first_line(text, lines, code):
    low, high = 1, len(lines)
    while low < high:
        mid = (low + high) // 2
        if code in codes("\n".join(lines[:mid]) + "\n"):
            high = mid
        else:
            low = mid + 1
    return low


def scan(text, team, allow):
    """-> sorted unique (line, rule) rows for text; team and allow are lists of literal strings."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    rows = set()

    def at(offset):
        return text.count("\n", 0, offset) + 1

    for entry in team:
        rows.update((at(m.start()), "L8_TEAM") for m in _literal(entry).finditer(text))
    allowed = {_canon(entry) for entry in allow}
    team_contacts = {_canon(entry) for entry in team} - {None}
    for rx, kind, rule in ((EMAIL, "email", "L8_EMAIL"), (PHONE, "phone", "L8_PHONE")):
        for m in rx.finditer(text):
            key = (kind, (_email if kind == "email" else _phone)(m.group()))
            if key not in allowed:
                rows.add((at(m.start()), rule))
            if key in team_contacts:
                rows.add((at(m.start()), "L8_TEAM"))
    seen = set()
    for number, line in enumerate(lines, 1):
        for code in codes(line):
            seen.add(code)
            rows.add((number, "L10_" + code.upper()))
        if re.search(INTERNAL_IDS, line, re.I) or PREFIX.search(line):
            rows.add((number, "L10_INTERNAL_ID"))
        if LABEL.search(line):
            rows.add((number, "L10_LABEL"))
        if ROW_ID.search(line):
            rows.add((number, "L10_ROW_ID"))
    for code in set(codes(text)) - seen:
        rows.add((_first_line(text, lines, code), "L10_" + code.upper()))
    return sorted(rows)


def main(argv=None):
    parser = QuietParser(prog="records agency-lint-out", description=__doc__)
    for name in ("--draft", "--team-list", "--allow"):
        parser.add_argument(name, required=True)
    try:
        args = parser.parse_args(argv)
        text = _read(args.draft, MAX_DRAFT)
        team, allow = _entries(args.team_list), _entries(args.allow)
        if not team or any(_canon(entry) is None for entry in allow):
            raise ValueError
        rows = scan(text, team, allow)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    except Exception:
        print(REFUSED, file=sys.stderr)
        return 2
    for line, rule in rows:
        print(line, rule)
    return 1 if rows else 0


if __name__ == "__main__":
    sys.exit(main())
