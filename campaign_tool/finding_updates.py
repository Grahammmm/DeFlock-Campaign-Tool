"""DF0022-06: dated, append-only updates of a published finding (new information that does not change what it says).

Pure and offline. An update is ``{date, note}``; the list lives in the finding's optional ``updates`` key, outside the
reviewed content hash (``findings.PUBLICATION_FIELDS``), so receipts stay bound. Every refusal names the indexed field
and a fixed reason, never the note itself.

The privacy lint is bounded pattern detection, not a claim that every personal name can be recognised: the redactor's
categories (plate, email, phone, address, SSN, date of birth, ranked or labelled name) refuse, and so does any pair of
adjacent title-cased words, which also refuses capitalised institution names. Write impersonal lower-case wording;
there is no override. The pair check runs on the note as typed and on a temporary NFC view of it, so a decomposed
spelling of a refused pair is refused too; the note itself is never normalised, only kept or refused.
"""
import re
import unicodedata
from datetime import datetime, timezone

UPDATE_KEYS = frozenset(("date", "note"))
NOTE_MAX = 400
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_WORD = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*")
# Characters a plain-text note may not hold: controls and formats (newlines included), line/paragraph separators,
# surrogates, private use, and the angle brackets of markup.
_REFUSED_CATEGORIES = frozenset(("Cc", "Cf", "Cs", "Co", "Zl", "Zp"))


class UpdateError(ValueError):
    """A refused update note, date or list; ``content.load_finding`` re-raises it as a ContentError."""


def _name_like(word):
    return len(word) >= 2 and word[0].isupper() and not word.isupper()


def _has_name_pair(note):
    previous = None
    for match in _WORD.finditer(note):
        if previous is not None and note[previous.end():match.start()].isspace() \
                and _name_like(previous.group()) and _name_like(match.group()):
            return True
        previous = match
    return False


def validate_update_note(note, label):
    """-> ``note`` unchanged (never stripped, normalised or truncated) or raise UpdateError naming ``label`` only."""
    if (not isinstance(note, str) or not 1 <= len(note) <= NOTE_MAX or not note.strip()
            or any(unicodedata.category(ch) in _REFUSED_CATEGORIES or ch in "<>" for ch in note)):
        raise UpdateError(f"{label}: update note must be 1-{NOTE_MAX} plain-text characters")
    from .digest.redact import redact  # local: the digest package is not needed by importers of this module
    # The NFC form is a temporary analysis view only (canonically equivalent spellings must not dodge the pair lint);
    # the original text is checked too and is what is returned.
    if redact(note).counts or _has_name_pair(note) or _has_name_pair(unicodedata.normalize("NFC", note)):
        raise UpdateError(f"{label}: update note contains private information")
    return note


def check_update_date(value, published_at, today, label):
    """-> ``value`` when it is a real ``YYYY-MM-DD`` between the publication's written calendar date and ``today``."""
    if not isinstance(value, str) or not _DAY.fullmatch(value):
        raise UpdateError(f"{label}: must be a real YYYY-MM-DD date")
    try:
        day = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise UpdateError(f"{label}: must be a real YYYY-MM-DD date") from None
    published_day = datetime.fromisoformat(published_at.replace("Z", "+00:00")).date()
    if day < published_day:
        raise UpdateError(f"{label}: is before the publication date {published_day}")
    if day > today:
        raise UpdateError(f"{label}: is in the future (today is {today} UTC)")
    return value


def load_updates(value, published_at, *, today=None, label):
    """Validate a finding's ``updates`` list and return a normalised copy, in stored order (no sorting, no deduplication:
    same-day and out-of-order backfill are allowed). ``today`` is the UTC day a date may not pass."""
    if not isinstance(value, list):
        raise UpdateError(f"{label}: must be a list")
    today = today or datetime.now(timezone.utc).date()
    loaded = []
    for index, item in enumerate(value):
        where = f"{label}[{index}]"
        if not isinstance(item, dict) or set(item) != UPDATE_KEYS:
            raise UpdateError(f"{where}: must be an object with exactly date and note")
        loaded.append({"date": check_update_date(item["date"], published_at, today, f"{where}.date"),
                       "note": validate_update_note(item["note"], f"{where}.note")})
    return loaded


def newest_first(updates):
    """Display order: newest date first, ties in reverse insertion order."""
    return [item for _, item in sorted(enumerate(updates), key=lambda pair: (pair[1]["date"], pair[0]), reverse=True)]


