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

Limits (also stated in docs/RUNNER.md): names are matched only after a rank
title or a name-like label (Name:, Requester:, Sincerely, ...) and from the
caller's denylist. A bare personal name in running text is not recognised by
any regex, so the ``redacted_cloud`` tier is an explicit opt-in and the
denylist should carry every name the organizers know of; ``strict_local``
is the default.
"""
import re
from dataclasses import dataclass, field

TITLES = r"(?:Officer|Deputy|Sheriff|Sgt\.?|Sergeant|Det\.?|Detective|Lt\.?|Lieutenant|Capt\.?|Captain|Chief|Cpl\.?|Corporal|Trooper|Agent|Investigator|Mr\.?|Mrs\.?|Ms\.?|Dr\.?)"
# Labels that introduce a person's name in correspondence and forms.
NAME_LABELS = r"(?:Name|Requester|Requestor|Applicant|Complainant|Witness|Driver|Registered owner|Owner|Signed|Signature|Attn\.?|Attention|Dear|Sincerely|Regards|Best regards|Respectfully|Contact|Prepared by|Submitted by|Reviewed by|Approved by|Custodian|From|cc|CC|Cc)"
STREET_SUFFIX = r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Court|Ct|Place|Pl|Highway|Hwy|Parkway|Pkwy|Circle|Cir|Terrace|Ter|Trail|Trl)"

# Order matters only for overlap resolution (earlier category wins on ties).
PATTERNS = [
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("DOB", re.compile(r"(?i)\b(?:DOB|D\.O\.B\.|date of birth|born)\s*[:\-]?\s*(\d{1,4}[/-]\d{1,2}[/-]\d{1,4})")),
    ("PHONE", re.compile(r"(?<![\w.-])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\w-])|(?<![\w.-])\d{10}(?![\w.-])")),
    ("ADDRESS", re.compile(r"\b\d{1,6}\s+(?:[A-Z][A-Za-z']+\s+){1,4}" + STREET_SUFFIX + r"\b\.?(?:,?\s+(?:Apt|Suite|Ste|Unit|#)\s*\w+)?")),
    ("NAME", re.compile(r"\b" + TITLES + r"\s+([A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+)?)")),
    ("NAME", re.compile(r"\b" + NAME_LABELS + r"\s*[:,]\s*([A-Z][a-z'\-]+(?:\s+(?:[A-Z]\.|[A-Z][a-z'\-]+)){1,3})")),
    # California 1ABC234 style, ABC1234 style, and any 7-char run mixing letters and digits.
    ("PLATE", re.compile(r"(?<![A-Za-z0-9-])(?:\d[A-Za-z]{3}\d{3}|[A-Za-z]{3}[- ]?\d{4}|(?=[A-Za-z0-9]{7}(?![A-Za-z0-9]))(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{7})(?![A-Za-z0-9-])")),
]
CATEGORIES = list(dict.fromkeys(name for name, _ in PATTERNS)) + ["DENYLIST"]
PLACEHOLDER = re.compile(r"\[(" + "|".join(CATEGORIES) + r")-(\d+)\]")


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


class Redactor:
    """One placeholder table shared across several texts (the units of one record).

    Placeholders already present in a text are reserved first, so a literal
    ``[PLATE-1]`` in the input (or a re-run over redacted text) can never
    collide with a freshly minted token for a different value.
    """

    def __init__(self, allowlist=(), denylist=()):
        self.allow = {str(a).lower() for a in allowlist if a}
        self.deny = [str(d) for d in denylist if d]
        self.tokens = {}
        self.counters = {name: 0 for name in CATEGORIES}

    def reserve(self, text):
        for match in PLACEHOLDER.finditer(text):
            category, number = match.group(1), int(match.group(2))
            self.counters[category] = max(self.counters[category], number)

    def redact(self, text):
        if not isinstance(text, str):
            raise TypeError("redact expects str")
        self.reserve(text)
        counts = {name: 0 for name in CATEGORIES}
        out = []
        cursor = 0
        for start, end, category, value in _collect(text, self.allow, self.deny):
            key = (category, value.strip().upper() if category != "DENYLIST" else value.lower())
            if key not in self.tokens:
                self.counters[category] += 1
                self.tokens[key] = f"[{category}-{self.counters[category]}]"
            counts[category] += 1
            out.append(text[cursor:start])
            out.append(self.tokens[key])
            cursor = end
        out.append(text[cursor:])
        return Redaction("".join(out), {k: v for k, v in counts.items() if v})


def redact(text, allowlist=(), denylist=()):
    """Return a :class:`Redaction` with placeholder tokens and per-category counts.

    ``allowlist`` holds values that must survive (agency names, vendor names,
    campaign mailboxes); ``denylist`` holds literal phrases that must always be
    replaced even when no pattern matches them (known personal names).
    Placeholders already present in the input are reserved, never re-used,
    so the function is idempotent and collision-free.
    """
    return Redactor(allowlist=allowlist, denylist=denylist).redact(text)


def contains_placeholder(text):
    return bool(PLACEHOLDER.search(text or ""))
