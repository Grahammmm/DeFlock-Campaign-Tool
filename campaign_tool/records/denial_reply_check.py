"""Advisory refusal-reply heuristics; never edit, approve or block a draft."""

import re

from ..cpra_cites import find_former_cpra_cites

# AGENCY-RUN-SKILL §1: both cores of G's privacy wording are required.
REDACTION_CORES = ("may be redacted", "produce every other")
# AGENCY-RUN-SKILL §5: request a per-field withholding basis.
FIELD_BASIS = ("redaction log", "field-by-field", "field-specific")
# AGENCY-RUN-SKILL §5, send-ready rule 1: omit purpose paragraphs.
PURPOSE = ("our objective", "we seek to determine", "not alleging misconduct",
           "the purpose of this request")
# G 2026-11-06 07:55: no preservation demand.
PRESERVATION = ("preserve", "preserving", "preservation", "litigation hold")
# AGENCY-RUN-SKILL §5, send-ready rule 12: counsel clears statute citations.
STATUTES = ("§", "Gov. Code", "Government Code", "Civ. Code", "Penal Code")
# AGENCY-RUN-SKILL send-ready text must not expose internal identifiers.
INTERNAL_IDS = r"\b(?:D\d+|M\d+)\b|\bINBOX:\d+\b|\bSent:\d+\b"
# AGENCY-RUN-SKILL Sheriff send was shortened to 1,229 words.
WORD_LIMIT = 1300


def _phrase(text):
    return r"\s+".join(re.escape(word) for word in text.split())


def _find(body, phrases, *, words=False):
    pattern = "|".join(_phrase(phrase) for phrase in phrases)
    if words:
        pattern = rf"(?<!\w)(?:{pattern})(?!\w)"
    return re.search(pattern, body, re.IGNORECASE)


def _own_former_cite(body):
    """Mask only the narrow generated quotation/blockquotes, preserving offsets.

    Direct historical qualifiers exempt only their following citation. This is
    not general attribution, truth assessment or counsel clearance; ambiguous
    unmarked assertions may warn conservatively. Old rules see the full body.
    """
    masked, agency_quote = [], False
    for line in body.splitlines(keepends=True):
        if line.rstrip("\r\n") == "The agency stated these reasons, as written:":
            agency_quote = True
        if not line.strip():
            agency_quote = False
        hide = agency_quote or line.lstrip().startswith(">")
        masked.append(re.sub(r"[^\r\n]", " ", line) if hide else line)
    visible = "".join(masked)
    for start, end in find_former_cpra_cites(visible):
        if not re.search(r"(?<!\w)(?:former|historical|pre-2023)\s+$", visible[:start], re.IGNORECASE):
            return start
    return None


def check(body, *, subject_agency, other_agency_names=()):
    """Return warnings ordered by 1-based line then code, one per rule.

    Missing-content and whole-document length warnings use line 1; matched
    content uses its first occurrence. These heuristics are not legal review.
    """
    warnings = []
    former = _own_former_cite(body)
    if former is not None:
        warnings.append({"code": "former_cpra_citation", "message":
                         "Former CPRA section cited as current law; ask the agency for the current section "
                         "(renumbered 2023-01-01).", "line": body.count("\n", 0, former) + 1})

    def warn(code, message, match=None):
        line = body.count("\n", 0, match.start()) + 1 if match else 1
        warnings.append({"code": code, "message": message, "line": line})

    if not all(_find(body, (core,)) for core in REDACTION_CORES):
        warn("redaction_paragraph_missing", "include G's redaction paragraph")
    if not _find(body, FIELD_BASIS):
        warn("field_basis_missing", "ask for a redaction log or field-specific basis")
    for code, phrases, message, words in (
        ("purpose_paragraph", PURPOSE, "omit the purpose paragraph", False),
        ("preservation_demand", PRESERVATION, "omit preservation demands", False),
        ("statute_citation", STATUTES, "needs counsel clearance", False),
    ):
        match = _find(body, phrases, words=words)
        if match:
            warn(code, message, match)
    # G8 denied 2026-11-05: other agencies are not precedents for this ask.
    names = tuple(name.strip() for name in other_agency_names
                  if name.strip() and name.strip().casefold() != subject_agency.strip().casefold())
    match = _find(body, names, words=True) if names else None
    if match:
        warn("precedent_agency", "another agency is named; allowed only when it is the subject of the ask", match)
    match = re.search(INTERNAL_IDS, body, re.IGNORECASE)
    if match:
        warn("internal_id", "omit internal identifiers", match)
    if len(body.split()) > WORD_LIMIT:
        warn("too_long", "shorten the reply to 1,300 words or fewer")
    return sorted(warnings, key=lambda warning: (warning["line"], warning["code"]))
