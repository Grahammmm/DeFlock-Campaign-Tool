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
# Where the boundary is. The text is split into sentences and quoted spans ("..." and curly quotes) are blanked first, so a
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
QUOTED = re.compile(r'"[^"\n]*"|“[^”\n]*”|‘[^’\n]*’')
NEUTRAL = re.compile(
    r"\b(?:whether|if|unless|alleg\w*|according to|claims? that|claimed that|asserts? that|argues? that|"
    r"(?:did|does|do|has|have|had|is|are|was|were|will|would|can|could)(?: not|n['’]t)(?: \w+){0,3}? "
    r"(?:violat\w*|determin\w*|assert\w*|claim\w*|alleg\w*|conclud\w*|find|found)|not (?:a |an )?(?:claim|determination|finding)|"
    r"no (?:violation|determination|finding)s?|not been determined|without determin\w*|this (?:section|chapter|title|article|act|part|subdivision))\b|\?\s*$", re.I)
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


def legal_claims(text):
    """-> sorted rule names of the legal-violation ASSERTIONS in text ([] when none). Never raises; a guard fault is itself a
    flag ("guard-error") so a human looks, and the caller still carries on. Alert-only: no caller may treat a flag as a failure."""
    try:
        found = set()
        for sentence in SENTENCE_SPLIT.split(QUOTED.sub(" ", text if isinstance(text, str) else "")):
            if sentence.strip() and not NEUTRAL.search(sentence):
                found.update(name for name, rx in RULES if rx.search(sentence))
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
