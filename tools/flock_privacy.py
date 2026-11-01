#!/usr/bin/env python3
"""Flock privacy patterns: the one copy of the email, phone and plate regexes the report, page and publish lints share.
Tools load this file by path (importlib, like their other siblings); it has no CLI and no side effects. Lints only refuse:
callers report the category name, never the matched text.
  EMAIL  local@domain.tld (tld 2+ letters, any case); `user@localhost` and `@handle` are not emails. Also the bracketed
         spelled-out forms `jdoe [at] slo [dot] gov`, `jdoe(at)slo(dot)gov`, `jdoe {at} slo.gov`, `jdoe@slo[dot]gov` (any case,
         spaces allowed inside and around the brackets; at least one bracketed [at] or [dot]). Plain words
         (`jdoe at slo dot gov`) are deliberately NOT emails: prose like "meet at city hall" would false-positive.
  PHONE  NANP 3-3-4 with space, dot or hyphen separators, `(805) 555-1234` / `(805)555-1234`, optional `+1`/`1` prefix.
         It must not touch another digit. Bare 10-digit runs are deliberately NOT phones: record numbers and document ids
         would false-positive.
  PLATE  California standard `7ABC123` (optional space or hyphen before the last three digits) and commercial `4A12345`;
         upper case only, whole word.
"""
import re

AT = r"\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\})\s*"
BDOT = r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\})\s*"
DOT, LABEL = r"(?:%s|\s*\.\s*)" % BDOT, r"[a-z0-9-]+"
# Both forms start only where a local-part run starts (same matches, fewer start points), and the @ form takes its first
# bracketed dot without a choice, so a failed search stays linear (no catastrophic backtracking on long lines).
SPELLED = (r"(?<![a-z0-9._%%+-])[a-z0-9._%%+-]+(?:%s%s(?:%s%s)*%s[a-z]{2,}|@(?:%s\s*\.\s*)*%s%s(?:%s%s)*[a-z]{2,})"
           % (AT, LABEL, DOT, LABEL, DOT, LABEL, LABEL, BDOT, LABEL, DOT))
EMAIL = re.compile(r"(?<![^\s@])[^\s@]+@[^\s@]+\.[a-z]{2,}|" + SPELLED, re.I)
PHONE = re.compile(r"(?<!\d)(?:\+?1[ .-]?)?(?:\(\d{3}\)\s?|\d{3}[ .-])\d{3}[ .-]\d{4}(?!\d)")
PLATE = re.compile(r"\b(?:\d[A-Z]{3}[ -]?\d{3}|\d[A-Z]\d{5})\b")
PATTERNS = (("email", EMAIL), ("phone", PHONE), ("plate", PLATE))


def categories(text):
    """-> the names of email, phone, plate (in that order) that match text."""
    return [name for name, rx in PATTERNS if rx.search(text)]
