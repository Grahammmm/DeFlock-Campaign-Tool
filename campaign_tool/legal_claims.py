"""DF-4 alert-only legal-claim wording check, shared by every generated surface (Flock report, page and publish lints through
tools/flock_privacy.py, and the newsletter draft and site build in this package).

Nothing here raises or blocks: a flag is a ``review-required`` note for the person at the publish gate. It does not judge
whether a violation happened, only whether OUR wording asserts one.
"""
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


def legal_notes(items):
    """items: iterable of (where, text) -> ["review-required: <where>: legal-claim wording (<rules>)", ...] to print and carry on.
    Names the place and the rules, never the text. These are NOTES, not errors: do not add them to an error list or exit code."""
    try:
        return ["review-required: %s: legal-claim wording (%s)" % (where, ",".join(rules))
                for where, text in items if (rules := legal_claims(text))]
    except Exception:  # noqa: BLE001
        return ["review-required: legal-claim wording (guard-error)"]


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
        return notes + list(dict.fromkeys(found))
    except Exception:  # noqa: BLE001 - alert-only: a scan that cannot run is itself a flag, never a failure
        return ["review-required: legal-claim wording (site content could not be scanned; read the findings before publishing)"]
