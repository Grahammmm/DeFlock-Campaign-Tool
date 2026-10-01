"""Deterministic regex redaction for the ``redacted_cloud`` privacy tier.

``redact(text)`` replaces license plates, email addresses, phone numbers,
street addresses, SSN-like numbers, dates of birth after a label, names that
follow a rank title, and any caller-supplied denylist phrase with stable
placeholder tokens such as ``[PLATE-1]``. The same value always maps to the
same token inside one call, so a narrative written from redacted text can
still say "the vehicle [PLATE-2] appears on page 3 and again on page 9".

The function returns the redacted text and counts per category only. The
mapping from token to original value is never returned or logged. Over-
redaction is acceptable; under-redaction is the failure this module guards
against, so patterns are deliberately broad.
"""
import re
from dataclasses import dataclass, field

TITLES = r"(?:Officer|Deputy|Sheriff|Sgt\.?|Sergeant|Det\.?|Detective|Lt\.?|Lieutenant|Capt\.?|Captain|Chief|Cpl\.?|Corporal|Trooper|Agent|Investigator)"
STREET_SUFFIX = r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Court|Ct|Place|Pl|Highway|Hwy|Parkway|Pkwy|Circle|Cir|Terrace|Ter|Trail|Trl)"

# Order matters only for overlap resolution (earlier category wins on ties).
PATTERNS = [
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("DOB", re.compile(r"(?i)\b(?:DOB|D\.O\.B\.|date of birth|born)\s*[:\-]?\s*(\d{1,4}[/-]\d{1,2}[/-]\d{1,4})")),
    ("PHONE", re.compile(r"(?<![\w.-])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\w-])|(?<![\w.-])\d{10}(?![\w.-])")),
    ("ADDRESS", re.compile(r"\b\d{1,6}\s+(?:[A-Z][A-Za-z']+\s+){1,4}" + STREET_SUFFIX + r"\b\.?(?:,?\s+(?:Apt|Suite|Ste|Unit|#)\s*\w+)?")),
    ("NAME", re.compile(r"\b" + TITLES + r"\s+([A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+)?)")),
    # California 1ABC234 style, ABC1234 style, and any 7-char run mixing letters and digits.
    ("PLATE", re.compile(r"(?<![A-Za-z0-9-])(?:\d[A-Za-z]{3}\d{3}|[A-Za-z]{3}[- ]?\d{4}|(?=[A-Za-z0-9]{7}(?![A-Za-z0-9]))(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{7})(?![A-Za-z0-9-])")),
]
CATEGORIES = [name for name, _ in PATTERNS] + ["DENYLIST"]
PLACEHOLDER = re.compile(r"\[(?:" + "|".join(CATEGORIES) + r")-\d+\]")


@dataclass
class Redaction:
    text: str
    counts: dict = field(default_factory=dict)

    @property
    def total(self):
        return sum(self.counts.values())


def _collect(text, allow, deny):
    spans = []
    for category, pattern in PATTERNS:
        for match in pattern.finditer(text):
            if match.groups():
                start, end = match.span(1)
            else:
                start, end = match.span()
            value = text[start:end]
            if value.lower() in allow:
                continue
            spans.append((start, end, category, value))
    for phrase in deny:
        if not phrase:
            continue
        for match in re.finditer(re.escape(phrase), text, re.IGNORECASE):
            spans.append((match.start(), match.end(), "DENYLIST", match.group(0)))
    # Earliest start wins; on a tie the longest match wins; then category order.
    order = {name: index for index, name in enumerate(CATEGORIES)}
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0]), order[s[2]]))
    chosen = []
    cursor = 0
    for start, end, category, value in spans:
        if start < cursor:
            continue
        chosen.append((start, end, category, value))
        cursor = end
    return chosen


def redact(text, allowlist=(), denylist=()):
    """Return a :class:`Redaction` with placeholder tokens and per-category counts.

    ``allowlist`` holds values that must survive (agency names, vendor names,
    campaign mailboxes); ``denylist`` holds literal phrases that must always be
    replaced even when no pattern matches them (known personal names).
    Placeholders already present in the input are left untouched so the
    function is idempotent.
    """
    if not isinstance(text, str):
        raise TypeError("redact expects str")
    allow = {str(a).lower() for a in allowlist if a}
    deny = [str(d) for d in denylist if d]
    tokens = {}
    counters = {name: 0 for name in CATEGORIES}
    counts = {name: 0 for name in CATEGORIES}
    out = []
    cursor = 0
    for start, end, category, value in _collect(text, allow, deny):
        key = (category, value.strip().upper() if category != "DENYLIST" else value.lower())
        if key not in tokens:
            counters[category] += 1
            tokens[key] = f"[{category}-{counters[category]}]"
        counts[category] += 1
        out.append(text[cursor:start])
        out.append(tokens[key])
        cursor = end
    out.append(text[cursor:])
    return Redaction("".join(out), {k: v for k, v in counts.items() if v})


def contains_placeholder(text):
    return bool(PLACEHOLDER.search(text or ""))
