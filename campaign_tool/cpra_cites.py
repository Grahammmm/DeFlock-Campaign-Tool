"""Bounded English former-CPRA citation heuristic, not a section conversion.

Only explicit Code/section/§ markers qualify. Numbers and attached subdivisions
are spans in the original text; lists/ranges are not expanded. Discovery says
nothing about the existence, applicability or present counterpart of a section.
"""
import re
from decimal import Decimal

FORMER_CPRA_NOTE = ("The response cites a section number from before the Public Records Act was renumbered on "
                    "2023-01-01; this reply asks the agency to name the current section.")
CURRENT_CPRA_ASK = "Please identify the current section of the Public Records Act on which the agency relies."

_CITES = re.compile(
    r"(?<![\w.$,])(?:(?P<government>(?:Government|Gov\.?)\s+Code\b)\s*"
    r"(?:(?:§{1,2}|sections?\b)\s*)?|(?:§{1,2}\s*|sections?\s+))"
    r"(?P<number>[0-9]+(?:\.[0-9]+)?)(?![\w]|\.[0-9.]|,[0-9])"
    r"(?:\([a-z0-9]+\))*", re.IGNORECASE)
_CODE_SUFFIX = re.compile(r"\bCode", re.IGNORECASE)


def _after_other_code(text, start):
    """True if only whitespace separates a word-bounded "Code" from ``start``.

    Walks back over the whitespace run only; runs before distinct nonoverlapping
    candidates are disjoint, so no growing prefix is copied or searched.
    """
    end = start
    while end > 0 and text[end - 1].isspace():
        end -= 1
    return end >= 4 and _CODE_SUFFIX.fullmatch(text, end - 4, end) is not None


def find_former_cpra_cites(text) -> list[tuple[int, int]]:
    """Return ordered nonoverlapping half-open source spans; never bare numbers.

    An immediately explicit other-Code namespace excludes a marker-only match.
    Decimal bounds are inclusive and exact, with no source normalization or IO.
    """
    if not isinstance(text, str):
        return []
    spans = []
    for match in _CITES.finditer(text):
        if not match.group("government") and _after_other_code(text, match.start()):
            continue
        if Decimal("6250") <= Decimal(match.group("number")) <= Decimal("6276.48"):
            spans.append(match.span())
    return spans
