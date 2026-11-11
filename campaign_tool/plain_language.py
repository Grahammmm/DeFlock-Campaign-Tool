"""DF0022-04: `finding check-plain DRAFT|ID --directory D [--json]` - an advisory plain-language check.

Reports four readability warnings for a finding's title, summary and body_md, with reproducible positions:
a sentence over 30 words, a summary over 60 words, an unexplained all-capital abbreviation of 2-6 letters and a
paragraph over 150 words. It never rewrites text, records a verdict or writes a file, and nothing here is read by
build, publish, review receipts or content hashes: this module is imported only by the `finding check-plain` command
and the `finding preview` advisory lines. It is a transparent heuristic (no NLP, dictionary or fetching).

Counting: a word is ``[^\\W_]+(?:['’-][^\\W_]+)*``; paragraphs are blocks of non-blank lines; a sentence ends at a
run of ``.``, ``!`` or ``?`` followed by whitespace or the end. Text inside matched double quotes ("..." or “...”)
and any text equal to one of the finding's source titles is skipped: its words are not counted and it is never
reported, but its words still count in a word's position number so the number can be found by counting the field.
"""
import os
import re
import unicodedata
from pathlib import Path

from . import findings

FIELDS = ("title", "summary", "body_md")
SENTENCE_LIMIT, SUMMARY_LIMIT, PARAGRAPH_LIMIT = 30, 60, 150
RULES = ("long_sentence", "long_summary", "unexplained_abbreviation", "long_paragraph")
EXIT_ROWS = 3  # advisory rows exist (the same code `finding diff` uses for "differences found")
HEADING = "Plain-language suggestions (advisory only; not a publication gate)."
PREVIEW_HEADING = "Plain language (advisory)"
_WORD = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*")
_ABBREVIATION = re.compile(r"[A-Z]{2,6}")
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_QUOTED = re.compile(r'"[^"]*"|“[^”]*”')
_SENTENCE_END = re.compile(r"[.!?]+(?=\s|$)")
# _CLOSE is matched with ``match(text, offset)`` on the original field: no ``\A`` (it would anchor to the field start), no tail slice.
_OPEN, _CLOSE = re.compile(r"\s*\(\s*\Z"), re.compile(r"\s*\)")
_BETWEEN_OPEN, _BETWEEN_CLOSE = re.compile(r"\A\s*\(\s*\Z"), re.compile(r"\A\s*\)")


def _blank(text):
    return "".join(char if char.isspace() else " " for char in text)


def _skipped_text(text, titles):
    """``text`` with every quoted span and source-title occurrence blanked to spaces (same length, same newlines)."""
    # One substitution assembles the result: only matched spans are blanked and no per-quote whole-field copy is made.
    kept = _QUOTED.sub(lambda match: _blank(match.group()), text)
    # Longest title first; each title is one literal left-to-right nonoverlapping replace over the already-masked field
    # with its blank built once, so a repeated title never rebuilds the whole field per occurrence.
    for title in sorted(titles, key=len, reverse=True):
        if title:
            kept = kept.replace(title, _blank(title))
    return kept


def _paragraphs(text):
    """[(start, end)] of every block of non-blank lines, in the original text."""
    blocks, begin, end, offset = [], None, None, 0
    for line in text.splitlines(keepends=True):
        if line.strip():
            begin = offset if begin is None else begin
            end = offset + len(line.rstrip())
        elif begin is not None:
            blocks.append((begin, end))
            begin = None
        offset += len(line)
    if begin is not None:
        blocks.append((begin, end))
    return blocks


def _tokens(field, text, titles):
    """Every word of ``field`` in order as dicts: its position numbers, offsets and whether it is skipped."""
    shown = _skipped_text(text, titles)
    found, sentence, number = [], 0, 0
    for paragraph, (begin, end) in enumerate(_paragraphs(text), 1):
        ends = [match.end() + begin for match in _SENTENCE_END.finditer(shown[begin:end])]
        current, index = None, 0
        for match in _WORD.finditer(text, begin, end):
            number += 1
            skipped = shown[match.start():match.end()] != match.group()
            # Word starts and sentence ends are ordered; consume each boundary once.
            while index < len(ends) and ends[index] <= match.start():
                index += 1
            if not skipped and (current is None or current[0] != (paragraph, index)):
                sentence += 1
                current = ((paragraph, index), sentence)
            found.append({"field": field, "paragraph": paragraph, "word": number, "start": match.start(),
                          "end": match.end(), "text": match.group(), "skipped": skipped,
                          "sentence": current[1] if current and not skipped else None,
                          "sentence_key": (paragraph, index)})
    return found


def _is_introduction(text, tokens, position):
    """True when ``tokens[position]`` (an ABC token) is written as `expanded words (ABC)` or `ABC (expanded words)`."""
    token, size = tokens[position], len(tokens[position]["text"])
    letters = token["text"].lower()

    def spelled(words):
        return (len(words) == size and not any(word["skipped"] for word in words)
                and "".join(word["text"][0].lower() for word in words) == letters
                and all(text[a["end"]:b["start"]].isspace() for a, b in zip(words, words[1:])))

    before = tokens[position - size:position] if position >= size else []
    if before and spelled(before) and _OPEN.match(text[before[-1]["end"]:token["start"]]) \
            and _CLOSE.match(text, token["end"]):
        return True
    after = tokens[position + 1:position + 1 + size]
    if len(after) == size and spelled(after) and _BETWEEN_OPEN.match(text[token["end"]:after[0]["start"]]) \
            and _CLOSE.match(text, after[-1]["end"]):
        return True
    return False


def _glossary_acronyms(glossary):
    found = set()
    for term in (glossary or {}).get("terms", ()):
        found.update(alias for alias in term.get("aliases", ()) if isinstance(alias, str) and _ABBREVIATION.fullmatch(alias))
    return found


def _span(rows):
    return rows[0]["start"], rows[-1]["end"]


def _warning(rule, rows, count=None, limit=None, abbreviation=None):
    start, end = _span(rows)
    first = rows[0]
    return {"rule": rule, "field": first["field"], "paragraph": first["paragraph"], "sentence": first["sentence"],
            "word": first["word"], "start": start, "end": end, "word_count": count, "limit": limit,
            "abbreviation": abbreviation}


def check_fields(items, titles=(), aliases=frozenset(), summary_rule=False):
    """Ordered warning dicts for ``items`` ((field name, text) pairs checked in that order).

    An abbreviation spelled out in an earlier item is explained in later ones. ``summary_rule`` adds the finding-only
    ``long_summary`` rule for an item named ``summary``. Never touches the text."""
    explained, warnings = set(), []
    for field, text in items:
        tokens = _tokens(field, text, titles)
        counted = [token for token in tokens if not token["skipped"]]
        found = []
        sentences, paragraphs = {}, {}
        # Token order also orders both group keys; each token joins each group once.
        for token in counted:
            sentences.setdefault(token["sentence_key"], []).append(token)
            paragraphs.setdefault(token["paragraph"], []).append(token)
        for words in sentences.values():
            if len(words) > SENTENCE_LIMIT:
                found.append(_warning("long_sentence", words, len(words), SENTENCE_LIMIT))
        if summary_rule and field == "summary" and len(counted) > SUMMARY_LIMIT:
            found.append(_warning("long_summary", counted, len(counted), SUMMARY_LIMIT))
        for words in paragraphs.values():
            if len(words) > PARAGRAPH_LIMIT:
                found.append(_warning("long_paragraph", words, len(words), PARAGRAPH_LIMIT))
        for position, token in enumerate(tokens):
            if token["skipped"] or not _ABBREVIATION.fullmatch(token["text"]):
                continue
            name = token["text"]
            if name in aliases:
                continue
            if _is_introduction(text, tokens, position):
                explained.add(name)
            elif name not in explained:
                found.append(_warning("unexplained_abbreviation", [token], abbreviation=name))
        warnings.extend(sorted(found, key=lambda row: (row["start"], RULES.index(row["rule"]))))
    return warnings


def check_plain(finding, glossary=None):
    """Ordered warning dicts for ``finding`` (title, summary, body_md in that order); never touches the text."""
    sources = finding.get("sources")
    titles = [source["title"] for source in sources if isinstance(source, dict) and isinstance(source.get("title"), str)
              and source["title"]] if isinstance(sources, list) else []
    items = [(field, finding.get(field) if isinstance(finding.get(field), str) else "") for field in FIELDS]
    return check_fields(items, titles, _glossary_acronyms(glossary), summary_rule=True)


def row_text(row):
    where = f"{row['field']} paragraph {row['paragraph']}, sentence {row['sentence']}, word {row['word']}"
    if row["rule"] == "unexplained_abbreviation":
        return f"{where}: unexplained_abbreviation {row['abbreviation']} (not spelled out earlier)"
    return f"{where}: {row['rule']} ({row['word_count']} words; limit {row['limit']})"


def render_rows(warnings):
    return [row_text(row) for row in warnings]


def render_report(report):
    rows = report["warnings"]
    return [HEADING, *render_rows(rows), f"{len(rows)} suggestion(s). Nothing was changed."]


def _is_path(target):
    return (os.path.lexists(os.path.expanduser(target)) or os.sep in target or "/" in target
            or target.endswith(".json") or target[:1] in (".", "~"))


def _checked(document):
    for field in ("title", "summary", "body_md"):
        if (field in document or field != "body_md") and not isinstance(document.get(field), str):
            raise ValueError(f"check-plain: {field} must be a string")
    return document


def _published(root, finding_id):
    folder = Path(root) / "content" / "findings"
    candidates = []
    findings._real_dir(folder.parent)  # a symlinked or non-folder content/ refuses rather than reading through it
    if findings._real_dir(folder):
        for path in sorted(folder.glob("*.json")):
            document = findings.load_draft(path)
            if document.get("id") == finding_id and document.get("state") == "published":
                candidates.append(document)
    if not candidates:
        raise ValueError(f"no published finding with id {finding_id!r} in content/findings/")
    if len(candidates) > 1:
        raise ValueError(f"id {finding_id!r} is published more than once")
    return candidates[0]


def load_target(root, target):
    """The finding to check: a draft path, or the one published finding with that id (never a silent fallback)."""
    if _is_path(target):
        path = Path(os.path.expanduser(target))
        return _checked(findings.load_draft(path))
    if not _ID.fullmatch(target):
        raise ValueError("check-plain: expected a draft path or finding ID")
    return _checked(_published(root, target))


def _glossary(root):
    from .content import read_glossary
    return read_glossary(Path(root))


def check_plain_report(root, target):
    finding = load_target(root, target)
    return {"advisory": True, "finding_id": finding["id"], "warnings": check_plain(finding, _glossary(root))}


def run_cli(root, target, as_json=False):
    """Print the report once (text, or one JSON object with ``as_json``); 3 when there are rows, else 0."""
    import json
    report = check_plain_report(root, target)
    print(json.dumps(report, ensure_ascii=False) if as_json else "\n".join(render_report(report)))
    return EXIT_ROWS if report["warnings"] else 0


def preview_lines(root, draft_path):
    """The `finding preview` advisory block: heading plus the same rows, or [] when there are none or the check
    cannot run (a preview never fails because of this advisory)."""
    try:
        finding = findings.load_draft(draft_path)
        for field in ("title", "summary"):
            if not isinstance(finding.get(field), str):
                return []
        if "body_md" in finding and not isinstance(finding["body_md"], str):
            return []
        rows = check_plain(finding, _glossary(root))
    except (OSError, ValueError):
        return []
    return [PREVIEW_HEADING, *render_rows(rows)] if rows else []
