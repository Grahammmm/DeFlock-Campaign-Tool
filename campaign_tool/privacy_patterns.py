"""The one copy of the email, phone and plate privacy patterns (moved here from tools/flock_privacy.py, G decision 2026-11-04).

It lives in the campaign_tool package because the package ships in the wheel and tools/ does not. tools/flock_privacy.py re-exports
these names for the report, page and publish lints; the newsletter, site build and social drafts import them directly. Lints
only refuse: callers report the category name and a locator, never the matched text.
  EMAIL  local@domain.tld (tld 2+ letters, any case); `user@localhost` and `@handle` are not emails. Also the bracketed
         spelled-out forms `jdoe [at] slo [dot] gov`, `jdoe(at)slo(dot)gov`, `jdoe {at} slo.gov`, `jdoe@slo[dot]gov` (any case,
         spaces allowed inside and around the brackets; at least one bracketed [at] or [dot]). Plain words
         (`jdoe at slo dot gov`) are deliberately NOT emails: prose like "meet at city hall" would false-positive.
  PHONE  NANP 3-3-4 with space, dot or hyphen separators, `(805) 555-1234` / `(805)555-1234`, optional `+1`/`1` prefix.
         It must not touch another digit. Bare 10-digit runs are deliberately NOT phones: record numbers and document ids
         would false-positive.
  PLATE  California standard `7ABC123` (optional space or hyphen before the last three digits) and commercial `4A12345`;
         upper case only, whole word.

A clean result is not a clearance: the patterns have deliberate blind spots and a document can pass every one and still be
unfit to publish. They sit under the human publish gate, never in place of it.

PATTERN_EXCEPTIONS is the one visible list of other modules that keep their own, STRICTER or differently-shaped pattern. Each
entry is (path, category, reason); tests/test_publish_path_coverage.py pins this list and fails on any other module that defines
an email, phone or plate pattern. Never loosen an entry: every match it makes today must keep matching.
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

PATTERN_EXCEPTIONS = (
    ("campaign_tool/content.py", "email", "_email fullmatch validates intentional contact addresses with length bounds, not a privacy lint"),
    ("campaign_tool/newsletter/draft.py", "phone", "newsletter gate: also refuses a bare 10-digit run and unseparated groups, stricter than the shared phone"),
    ("campaign_tool/digest/redact.py", "email", "digest redaction: replaces with placeholders and over-redacts on purpose, a different job from a refusal lint"),
    ("campaign_tool/digest/redact.py", "phone", "digest redaction: also matches a bare 10-digit run"),
    ("campaign_tool/digest/redact.py", "plate", "digest redaction: also matches lower case and any 7-character letter-digit run"),
    ("tools/flock_email.py", "email", "recipient address check (CHECKS) for outbound drafts, not a privacy lint; the lint is the shared categories()"),
    ("campaign_tool/kit.py", "email", "address validator (fullmatch with length bounds) for the organizer's own records_email, not a privacy lint"),
    ("workers/workspace/src/executors/social_adapter.ts", "all", "TypeScript mirror for the Worker's pre-post lint; Python cannot be imported there"),
)


def categories(text):
    """-> the names of email, phone, plate (in that order) that match text."""
    return [name for name, rx in PATTERNS if rx.search(text)]


def refusal(where, text):
    """-> "privacy lint: <where>: <categories>" naming the place and the categories (never the text), or None when clean."""
    found = categories(text) if isinstance(text, str) else []
    return "privacy lint: %s: %s" % (where, ", ".join(found)) if found else None
