"""DF-4 alert-only legal-claim wording check, shared by every generated surface (Flock report, page and publish lints through
tools/flock_privacy.py, and the newsletter draft and site build in this package).

Nothing here raises or blocks: a flag is a ``review-required`` note for the person at the publish gate. It does not judge
whether a violation happened, only whether OUR wording asserts one.

DF2007-15 adds a second, separate tier beside it: ADVISORY wording warnings (``intent``, ``undated-count``), see ``legal_advisories``.
They are ``warning:`` notes, never errors or refusals, and never part of ``legal_claims``.
"""
import datetime
import re

# Flags generated text that ASSERTS a legal violation. It never refuses: callers print `review-required` notes and carry on
# (exit code, build, export and send are unchanged), so a false positive costs a human glance and cannot halt a run. It does
# not judge whether a violation happened, only whether OUR wording says one did. Callers report rule names, never the text.
#
# Where the boundary is. The text is split into sentences (then clauses, see below) and quoted spans ("..." and curly quotes) are blanked first, so a
# quoted statute or quoted record never flags. A sentence is then tested against ASSERTION shapes: a named party + a violation
# verb/predicate (`X violated`, `X is in violation of`, `X broke the law`, `X failed to comply with`, `X is non-compliant`,
# `X acted unlawfully`, `it was illegal`). Hedged speculation about the party (`may have violated`, `appears to have broken`)
# STILL flags: it remains an accusation about a named party. A sentence passes when it is NEUTRAL: it only states what a
# law requires (`SB 34 requires ...`, `it is unlawful to ...`), only states an observed fact (`declined to answer`, `did not
# respond in 10 days`), attributes the claim to someone else (`alleges`, `according to`), puts it as a question or condition
# (`whether`, `if`), or negates it (`did not violate`, `does not determine`, `not a claim`, `no violation`).
# Known gaps (alert-only, so accepted): an assertion with no verb of the shapes above (`SB 34 breach by X`) is missed, and an
# attributed claim (`the complaint alleges X violated`) passes although a reader may take it as ours.
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
# Clauses: a sentence is cut at commas, semicolons, colons and joining words, so a neutral clause ("declined to say whether it kept
# logs") cannot hide an assertion in the next one (", and it broke the law"), and the other way round.
SEGMENT_SPLIT = re.compile(r"[,;:]\s+")
JOINER_SPLIT = re.compile(r"\s+(?:and|but|yet|while|although|though|however)\s+", re.I)
QUOTED = re.compile(r'"[^"\n]*"|“[^”\n]*”|‘[^’\n]*’')
# Cues that make only what FOLLOWS them in the clause neutral: a condition, a question, an attribution, a statement of what a law
# requires, or a statement that nothing was determined. Text before the cue still counts: "X violated SB 34 even if it was
# accidental" flags, "whether X violated SB 34" and "SB 34 prohibits agencies from unlawfully sharing data" pass.
SCOPED = re.compile(r"\b(?:whether|if|unless|alleg\w*|according to|claims? that|claimed that|asserts? that|argues? that|"
                    r"(?:determin\w*|find(?:ing)?s?|found|conclu\w*) that|(?:requires?|required|prohibits?|forbids?|bars?|mandates?|permits?|"
                    r"makes? it|provides? that)|this (?:section|chapter|title|article|act|part|subdivision))\b", re.I)
# A conditional or question that OPENS a comma segment ("If X broke the law and violated SB 34, ...") covers the whole segment,
# across and/but joiners. A cue in the middle of a segment covers only its own clause.
LEADING = re.compile(r"\s*(?:if|whether|unless)\b", re.I)
# An attribution ("According to counsel, ...", "The complaint alleges ...; ...") also covers later comma segments of its sentence,
# but not a clause opened by a joining word ("..., and it broke the law"): that is a new statement.
ATTRIBUTION = re.compile(r"\b(?:alleg\w*|according to|claims? that|claimed that|asserts? that|argues? that)\b", re.I)
# A clause that is itself a question is neutral. Denials ("did not violate", "no violation") need no cue: they never match a rule,
# or are negated by NEGATED below, so unrelated denial words ("did not determine retention") cannot hide a real accusation.
NEUTRAL = re.compile(r"\?\s*$")
# A negation within three words before the matched wording negates that match ("did not break the law", "is not non-compliant",
# "did not act unlawfully"). "not only" is emphasis, not negation.
NEGATED = re.compile(r"\b(?:not(?! only)|never|nor|n['’]t|no)\W+(?:\w+\W+){0,3}$", re.I)
_BE = r"(?:is|are|was|were|has been|have been|remains?|stays?)"
RULES = (
    ("violation-verb", re.compile(r"\b(?:violated|violates|contravened|contravenes|infringed (?:on |upon )?(?:the |a )?(?:law|statute|act|code|right)s?)\b", re.I)),
    ("violation-noun", re.compile(r"\b(?:in (?:clear |apparent |serious |direct )?(?:violation|breach|contravention) of|"
                                r"(?:constitutes?|constituted|committed|commits|is|was|represents?) (?:a |an )?(?:clear |apparent |serious |"
                                r"direct |flagrant )?(?:violation|breach|contravention)\b)", re.I)),
    ("broke-the-law", re.compile(r"\b(?:broke|break|breaks|breaking|breached) (?:the|state|federal|california) (?:law|laws|statute|act|code)\b|"
                                 r"\b(?:un|il)lawfully\b", re.I)),
    ("non-compliant", re.compile(r"\b(?:failed|fails|failing) to comply with\b(?! (?:our|the|a|this|your) (?:public[- ]records )?(?:request|deadline)s?\b)|\bnon-?complian(?:t|ce)\b|"
                                 r"\b(?:not|never) in (?:full )?compliance with\b|\bout of compliance with\b|\bfailed to (?:follow|obey|"
                                 r"honor) (?:the |state |federal )?(?:law|statute|act|code)\b", re.I)),
    ("is-illegal", re.compile(r"\b" + _BE + r" (?:clearly |plainly |flagrantly |patently )?(?:un|il)lawful\b(?! (?:for|to|unless)\b)|"
                              r"\b" + _BE + r" (?:clearly |plainly |flagrantly |patently )?illegal\b(?! (?:for|to|unless)\b)", re.I)),
)


def _asserts(clause, inherited=False):
    """-> rule names asserted by one clause (a match counts unless negated before it or inside a scoped cue's reach)."""
    if inherited or NEUTRAL.search(clause):
        return set()
    cue = SCOPED.search(clause)
    limit = cue.start() if cue else len(clause)
    return {name for name, rx in RULES for m in rx.finditer(clause)
            if m.start() < limit and not NEGATED.search(clause[:m.start()])}


def _sentence_claims(sentence):
    found, attributed = set(), False
    for segment in SEGMENT_SPLIT.split(sentence):
        covered = bool(LEADING.match(segment))
        for n, clause in enumerate(JOINER_SPLIT.split(segment)):
            found |= _asserts(clause, inherited=covered or (attributed and n == 0))
        attributed = attributed or bool(ATTRIBUTION.search(segment))
    return found


def legal_claims(text):
    """-> sorted rule names of the legal-violation ASSERTIONS in text ([] when none). Never raises; a guard fault is itself a
    flag ("guard-error") so a human looks, and the caller still carries on. Alert-only: no caller may treat a flag as a failure."""
    try:
        found = set()
        for sentence in SENTENCE_SPLIT.split(QUOTED.sub(" ", text if isinstance(text, str) else "")):
            found |= _sentence_claims(sentence)
        return sorted(found)
    except Exception:  # noqa: BLE001 - alert-only: a broken guard must never stop the run, it asks for a human instead
        return ["guard-error"]


ADVISORY_FIX = {"intent": "Describe what the record says; do not infer intent.",
                "undated-count": "Add a date or describe the count as documented or mapped."}
ADVISORY_GUARD_ERROR = "warning: wording advisory (guard-error). Fix: Review the wording manually."
# --- DF2007-15 advisory wording tier (separate from the legal-violation guard above; never refuses, never in legal_claims) --------
# `intent` (secretly, covertly, hid, hiding, covered up, in secret, said of an agency or official) and `undated-count` (a number
# followed by cameras, readers or ALPRs with no `as of`, `documented`, `mapped` or date in the same sentence) are the two wordings
# that fail a correction (RESEARCH-CAMPAIGNS-1 section 6). An advisory is a WARNING with a fixed fix wording: it names the place and
# the category, never the text, and no caller may turn it into an error, a refusal, a withholding or an exit code. The scanner is an
# explicitly English heuristic, not named-entity recognition and not a claim that intent is unproven in fact. It has its own quote
# matcher and clause scan so the older legal scanner's results never change.
# Same sentence-boundary contract as SENTENCE_SPLIT (punctuation then whitespace, or a newline), compiled separately so the tiers fail independently.
ADV_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
# A quotation may run over a line break but never over a blank line (so one stray mark cannot swallow the rest of a document), and a
# mark between two word characters (agency's, agency’s) is an apostrophe inside the quotation, not its closing delimiter.
_ADV_NL = r"\n(?!\s*\n)"
ADV_QUOTED = re.compile(
    r'"(?:[^"\n]|' + _ADV_NL + r')*"|“(?:[^”\n]|' + _ADV_NL + r')*”'
    r"|‘(?:[^’\n]|(?<=\w)’(?=\w)|" + _ADV_NL + r")*’"
    r"|(?<!\w)'(?:[^'\n]|(?<=\w)'(?=\w)|" + _ADV_NL + r")*'(?!\w)")
INTENT = re.compile(r"\b(?:secretly|covertly|hid|hiding|covered\s+up|in\s+secret)\b", re.I)
_ROLE = (r"(?:agenc(?:y|ies)|departments?|police|sheriffs?|cit(?:y|ies)|count(?:y|ies)|councils?|mayors?|chiefs?|officials?|"
         r"officers?|staff)")
ROLE = re.compile(r"\b" + _ROLE + r"\b", re.I)
BY_ROLE = re.compile(r"\bby\s+(?:[\w'’.-]+\s+){0,6}?" + _ROLE + r"\b", re.I)
_NOUN = r"(?:cameras|readers|ALPRs)"
COUNT = re.compile(r"(?<![\w.,/-])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s+" + _NOUN + r"(?![\w-])", re.I)
# Same local boundary as NEGATED (three words before the match; "not only" is emphasis) but it also sees contractions: didn't, aren't.
ADV_NEGATED = re.compile(r"(?:\b(?:not(?! only)|never|nor|no)|n['’]t)\W+(?:\w+\W+){0,3}$", re.I)
QUALIFIER = re.compile(r"\b(?:as\s+of|documented|mapped)\b", re.I)
URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
_MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec"
ISO_DATE = re.compile(r"(?<![\w/.-])(\d{4})-(\d{2})-(\d{2})(?![\w/-])")
SLASH_DATE = re.compile(r"(?<![\w/.-])(\d{1,2})/(\d{1,2})/(\d{4})(?![\w/-])")
MONTH_DAY_YEAR = re.compile(r"\b(" + _MONTHS + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I)
MONTH_YEAR = re.compile(r"\b(" + _MONTHS + r")\.?,?\s+(\d{4})\b(?!\s+" + _NOUN + r"\b)", re.I)
BARE_YEAR = re.compile(r"(?<![\w./:+-])(?<!\d,)(19\d{2}|20\d{2})(?![\w/:-]|[.,]\d)(?!\s+" + _NOUN + r"\b)", re.I)
_MONTH_NUMBER = {m: n for n, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def _blank_quotes(text):
    """Blank every complete quoted span (same length, so nothing shifts); a span that ended a sentence keeps its final .!? so the
    sentence boundary survives, and line breaks inside a span stay. Apostrophes inside words are not quotation marks."""
    def blank(match):
        span = match.group(0)
        end = span[1:-1].rstrip()[-1:]
        return re.sub(r"[^\n]", " ", span[:-1]) + (end if end in (".", "!", "?") else " ")
    return ADV_QUOTED.sub(blank, text)


def _calendar_day(year, month, day):
    try:
        datetime.date(int(year), int(month), int(day))
        return True
    except ValueError:
        return False


def _dated(sentence):
    """True when the sentence holds a documented date shape: ISO YYYY-MM-DD, M/D/YYYY, `Month D, YYYY`, `Month YYYY` or a bare year
    1900-2099 (not the camera count itself). Full dates must be real calendar days; a URL qualifies nothing. Not a date parser."""
    text, ok = URL.sub(" ", sentence), False
    for rx, valid in ((ISO_DATE, lambda m: _calendar_day(m[1], m[2], m[3])), (SLASH_DATE, lambda m: _calendar_day(m[3], m[1], m[2])),
                      (MONTH_DAY_YEAR, lambda m: _calendar_day(m[3], _MONTH_NUMBER[m[1][:3].lower()], m[2])),
                      (MONTH_YEAR, lambda m: 1900 <= int(m[2]) <= 2099), (BARE_YEAR, lambda m: True)):
        ok = ok or any(valid(m) for m in rx.finditer(text))
        text = rx.sub(" ", text)  # an invalid date is removed, so its parts cannot qualify as a bare year
    return ok


def _intent(sentence):
    attributed = False
    for segment in SEGMENT_SPLIT.split(sentence):
        covered, earlier = bool(LEADING.match(segment)), ""
        for n, clause in enumerate(JOINER_SPLIT.split(segment)):
            inherited = covered or (attributed and n == 0)
            cue = SCOPED.search(clause)
            limit = cue.start() if cue else len(clause)
            if not (inherited or NEUTRAL.search(clause)):
                for m in INTENT.finditer(clause):
                    if m.start() < limit and not ADV_NEGATED.search(clause[:m.start()]) and (
                            ROLE.search(earlier + " " + clause[:m.start()]) or BY_ROLE.search(clause[m.end():])):
                        return True
            earlier += " " + clause
        attributed = attributed or bool(ATTRIBUTION.search(segment))
    return False


def _undated_count(sentence):
    text = URL.sub(" ", sentence)
    return any(not ADV_NEGATED.search(text[:m.start()]) for m in COUNT.finditer(text)) and not (QUALIFIER.search(text) or _dated(text))


def legal_advisories(text):
    """-> sorted advisory categories ("intent", "undated-count") of text; [] for empty or non-string input. Advice only: never a
    refusal. A scan fault returns ["guard-error"] (also advice) instead of raising or reporting a clean result."""
    if not isinstance(text, str) or not text:
        return []
    try:
        found = set()
        for sentence in ADV_SENTENCE_SPLIT.split(_blank_quotes(text)):
            if _intent(sentence):
                found.add("intent")
            if _undated_count(sentence):
                found.add("undated-count")
        return sorted(found)
    except Exception:  # noqa: BLE001 - advice only: a broken scan asks for a human, it never stops the run
        return ["guard-error"]


def advisory_notes(items):
    """items: iterable of (where, text) -> ["warning: <where>: wording advisory (<category>). Fix: <fixed wording>", ...] in item
    order then sorted category. Names the place and the category, never the text, a number or an exception. NOT errors: never
    add them to an error list, a refusal or an exit code, and never read them as permission to publish or send."""
    notes = []
    try:
        for where, text in items:
            for category in legal_advisories(text):
                note = ADVISORY_GUARD_ERROR if category == "guard-error" else \
                    "warning: %s: wording advisory (%s). Fix: %s" % (where, category, ADVISORY_FIX[category])
                if note not in notes or category != "guard-error":
                    notes.append(note)
    except Exception:  # noqa: BLE001
        if ADVISORY_GUARD_ERROR not in notes:
            notes.append(ADVISORY_GUARD_ERROR)
    return notes


def legal_notes(items):
    """items: iterable of (where, text) -> [\"review-required: <where>: legal-claim wording (<rules>)\", ...] to print and carry on.
    Names the place and the rules, never the text. These are NOTES, not errors: do not add them to an error list or exit code.
    The legal notes come first (unchanged); the DF2007-15 advisory warnings follow them, and only when something matched."""
    try:
        items = list(items)
        legal = ["review-required: %s: legal-claim wording (%s)" % (where, ",".join(rules))
                 for where, text in items if (rules := legal_claims(text))]
    except Exception:  # noqa: BLE001
        return ["review-required: legal-claim wording (guard-error)"]
    return legal + advisory_notes(items)


def faq_notes(entries):
    """entries: the validated ``content/faq.json`` entries -> review-required notes for EVERY answer_md (DF0022-01).
    Each answer is scanned on its own, so neutral question wording hides nothing; a rule citation suppresses nothing.
    Alert-only like ``legal_notes``: the notes name ``faq.json.entries[N].answer_md`` and the rules, never the text."""
    try:
        return legal_notes((f"faq.json.entries[{n}].answer_md", entry["answer_md"]) for n, entry in enumerate(entries or []))
    except Exception:  # noqa: BLE001
        return ["review-required: legal-claim wording (guard-error)"]


def elsewhere_notes(entries):
    """entries: the validated ``content/elsewhere.json`` entries -> review-required notes for EVERY as_stated (DF1107-04).
    Alert-only like ``legal_notes``: the notes name ``elsewhere.json[N].as_stated`` and the rules, never the text."""
    try:
        return legal_notes((f"elsewhere.json[{n}].as_stated", entry["as_stated"]) for n, entry in enumerate(entries or []))
    except Exception:  # noqa: BLE001
        return ["review-required: legal-claim wording (guard-error)"]


def statement_notes(rows):
    """rows: validated ``content/statements.json`` rows -> review-required notes for each campaign ``context_note`` (DF1107-07).
    The note is scanned raw, on its own; the attributed quote is the official's wording, not campaign text, and is not scanned.
    Alert-only like ``legal_notes``: the notes name ``statements.json[N].context_note`` and the rules, never the text."""
    try:
        return legal_notes((f"statements.json[{n}].context_note", row["context_note"])
                           for n, row in enumerate(rows or []) if row.get("context_note") is not None)
    except Exception:  # noqa: BLE001
        return ["review-required: legal-claim wording (guard-error)"]


def site_notes(root):
    """root: a campaign directory -> review-required notes for its finding content, meeting directions and agency action statements (the text the site build publishes).
    Used by the CLI build and the runner build_site job, so both carry the same flags to their human approval gate."""
    try:
        from .content import load_content
        model = load_content(root)
        walk = lambda v: [t for x in v.values() for t in walk(x)] if isinstance(v, dict) else \
            [t for x in v for t in walk(x)] if isinstance(v, (list, tuple)) else [v] if isinstance(v, str) else []
        asks = ((f"site.json.comment_asks[{n}]", ask) for n, ask in enumerate((model or {}).get("site", {}).get("comment_asks", []), 1))
        notes = (legal_notes([*((f"findings/{f.get('slug', '?')}", t) for f in (model or {}).get("findings", []) for t in walk(f)), *asks])
                 + faq_notes((model or {}).get("faq"))  # DF0022-01: every FAQ answer, scanned on its own
                 + elsewhere_notes((model or {}).get("elsewhere")))  # DF1107-04: every as_stated, scanned on its own
        # DF0022-08: one call per meeting on the raw direction text (never the attributed page wording); identical notes once
        found = []
        for meeting in (model or {}).get("meetings", []):
            direction = (meeting.get("outcome") or {}).get("direction")
            if direction is not None:
                found += legal_notes([(f"meetings/{meeting.get('id', '?')}.outcome.direction", direction)])
            if (meeting.get("submitted_comment") or {}).get("text") is not None:  # DF1407-06: hand-authored public copy, scanned raw
                found += legal_notes([(f"meetings/{meeting.get('id', '?')}.submitted_comment.text", meeting["submitted_comment"]["text"])])
        # DF0237-01: hand-edited agency actions get the same note as `agency-action add`, located by their stored row
        from .agency_actions import LABEL, read_actions
        stored = read_actions(root, (model or {}).get("agencies", [])) or []
        found += legal_notes([(f"{LABEL}.actions[{n}].as_stated", row["as_stated"]) for n, row in enumerate(stored)])
        if any(a.get("sharing") for a in (model or {}).get("agencies", [])):  # DF0237-04: the generated /sharing.html wording, scanned only when the page exists
            try:
                from .cli import read_config
                from .site import Site
                found += legal_notes([("sharing.html", Site(root, read_config(root), model).sharing_plaintext())])
            except Exception:  # noqa: BLE001 - alert-only, like the rest: a page that cannot be read is a flag, never a failure
                found.append("review-required: sharing.html: legal-claim wording (guard-error)")
        found += statement_notes((model or {}).get("statements"))  # DF1107-07: each campaign context note, raw
        return notes + list(dict.fromkeys(found))
    except Exception:  # noqa: BLE001 - alert-only: a scan that cannot run is itself a flag, never a failure
        return ["review-required: legal-claim wording (site content could not be scanned; read the findings before publishing)"]
