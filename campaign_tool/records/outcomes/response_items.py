"""Private per-asked-item rating of a records response (REC-AUTO-8).

For each request in the same run's request register, the original ask is split
into numbered items and every joined received response is read, sentence by
sentence, against each item with fixed rules. The result says, per asked item,
whether the response text answered it, cited an exemption, withheld it, said no
records exist, left it unmentioned or contradicted itself, each with the exact
quote and page.

Deterministic rules only: no model, network, OCR, ledger write, kit write,
outbox or send, and it drafts nothing. A rating is a machine reading that needs
human review; a quoted citation is never judged valid here. Text is read only
from the current extraction membership through received_items._current_units
(the same hash-bound reader REC-AUTO-3 uses), never a second extraction reader.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from ..understanding.evidence import read_pinned, sha
from . import received_items as _ri

SCHEMA = "records-response-items-v1"
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_ASKS = 200
MAX_ASK = 500
MAX_QUOTE = 600
RESPONSE_KINDS = frozenset({"response", "records", "denial", "extension", "fee"})
RATINGS = ("answered", "withheld", "exemption_cited", "no_records_stated", "missing", "unclear")
CUE_RATINGS = ("exemption_cited", "withheld", "no_records_stated", "answered")
UNKNOWN_SCOPE = "Unknown — request scope not recorded"
# Extraction problems that mean the page text may not be the whole response.
UNREVIEWED_DIAGNOSTICS = frozenset({"page_locator_unavailable", "no_usable_text",
                                    "current_extraction_unavailable"})
LIMITS = (
    "Machine rating by fixed rules over the extracted page text; review required before any follow-up. "
    "Use docs/RESPONSE-REVIEW-CHECKLIST.md.",
    "A citation is quoted exactly as the agency wrote it and is never judged valid or sufficient here.",
    "missing means the asked item was not mentioned in the response text read; it is not proof that "
    "records exist or that the agency failed a duty.",
    "Not a legal finding; the us-ca law package is draft. Private; not for publication.",
)

_I = re.I
STOPWORDS = frozenset("""
about above after again against also among been before being below between both cannot could does doing
down during each either else ever every from further have having here herein hereby hers herself himself
into itself just like made make many more most much must only other ought over same shall should since some
such than that their theirs them themselves then there these they this those through under until upon very
were what when where which while whom whose will with within without would your yours yourself
agency agencies record records request requests requested including include includes related relating
regarding concerning copies copy document documents any all pursuant california public
""".split())

_MARK = re.compile(r"(?:^|(?<=; ))[ \t]*(?:\(\d{1,3}\)|\d{1,3}[.)]|\([A-Za-z]\)|[A-Za-z][.)])[ \t]+", re.M)
_BULLET = re.compile(r"^[ \t]*[-*•][ \t]+(.*\S)[ \t]*$", re.M)
_WORD = re.compile(r"[^\W\d_]{4,}")
_BOUND = re.compile(r"(?<!\bGov)(?<!\bSec)(?<!\bNo)(?<!\bCal)(?<!\bCode)(?<!\bInc)[.?!]+(?=\s|$)")
CITE = re.compile(r"\bGov(?:ernment|\.)?\.?\s+Code\b|§|\bsection\s+\d{4}|\b79\d{2}\.\d{3}\b|\b6254\b|\bexempt", _I)
WITHHELD = re.compile(r"\bwithh[eo]ld\w*|\bredact(?:ed)? in (?:full|its entirety)\b|"
                      r"\bwill not (?:be )?(?:produce|release|provide)\b|\bden(?:y|ied|ies|ying)\b", _I)
NO_RECORDS = re.compile(r"\bno (?:responsive )?records\b|\bdo(?:es)? not (?:have|possess|maintain)\b|"
                        r"\bunable to locate\b", _I)
ANSWERED = re.compile(r"\battached\b|\benclosed\b|\bproduc(?:ed|ing)\b|\bprovid(?:ed|ing)\b|"
                      r"\bplease find\b|\bresponsive records\b", _I)
NEGATORS = frozenset({"not", "no", "never", "without"})


def _need(condition, code):
    if not condition:
        raise ValueError(code)


def canon(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _clip(text, flags, flag):
    if len(text) > MAX_ASK:
        flags.append(flag)
        return text[:MAX_ASK - 1] + "…"
    return text


def split_asked(text):
    """-> [{ask_id, text, flags}] in document order; never guessed from unknown scope."""
    if not isinstance(text, str) or not text.strip() or text.strip() == UNKNOWN_SCOPE:
        return []
    text = text.strip()
    marks = list(_MARK.finditer(text))
    if marks:
        parts = []
        for index, mark in enumerate(marks):
            end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
            parts.append(text[mark.end():end])
    else:
        bullets = _BULLET.findall(text)
        parts = bullets if bullets else (text.split(";") if ";" in text else [text])
    out = []
    for part in parts:
        part = part.strip().rstrip(";").strip()
        if part:
            out.append(part)
    out = out[:MAX_ASKS]
    asks = []
    for number, part in enumerate(out, 1):
        flags = []
        asks.append({"ask_id": "ask-%d" % number, "text": _clip(part, flags, "ask_truncated"),
                     "flags": flags, "_full": part})
    return asks


def content_words(text):
    return {w for w in (m.group(0).casefold() for m in _WORD.finditer(text)) if w not in STOPWORDS}


def sentences(text):
    """Exact-substring sentences of one page: newline and . ? ! boundaries."""
    out = []
    for line in re.finditer(r"[^\n]+", text):
        raw, start = line.group(0), 0
        for bound in list(_BOUND.finditer(raw)) + [None]:
            end = bound.end() if bound else len(raw)
            piece = raw[start:end].strip()
            if piece:
                out.append(piece)
            start = end
    return out


def _negated(sentence, start):
    words = re.findall(r"[\w']+", sentence[:start].casefold())[-3:]
    return any(w in NEGATORS or w.endswith("n't") for w in words)


def _hit(pattern, sentence, spans=()):
    for found in pattern.finditer(sentence):
        if any(a <= found.start() < b for a, b in spans):
            continue
        if not _negated(sentence, found.start()):
            return True
    return False


def cues(sentence):
    """-> set of CUE_RATINGS this sentence carries (negation-guarded)."""
    found = set()
    cited = _hit(CITE, sentence)
    if cited:
        found.add("exemption_cited")
    elif _hit(WITHHELD, sentence):
        found.add("withheld")
    # no_records cues carry their own negation; their span is not also an "answered" cue.
    none_spans = [m.span() for m in NO_RECORDS.finditer(sentence)]
    if none_spans:
        found.add("no_records_stated")
    if _hit(ANSWERED, sentence, none_spans):
        found.add("answered")
    return found


def _matches(words, sentence):
    if not words:
        return False
    shared = words & content_words(sentence)
    return len(shared) >= 2 if len(words) >= 2 else shared == words


def _rate(words, responses):
    """responses: [(item_id, [(page, text)])] -> (rating, evidence, flags)."""
    evidence = {name: [] for name in CUE_RATINGS}
    mentioned = False
    flags = []
    for item_id, pages in responses:
        for page, text in pages:
            for sentence in sentences(text):
                if not _matches(words, sentence):
                    continue
                mentioned = True
                for name in cues(sentence):
                    quote = sentence
                    if len(quote) > MAX_QUOTE:
                        quote = quote[:MAX_QUOTE]
                        flags.append("quote_truncated")
                    entry = {"item_id": item_id, "page": page, "quote": quote}
                    if entry not in evidence[name]:
                        evidence[name].append(entry)
    present = [name for name in CUE_RATINGS if evidence[name]]
    if not present:
        if mentioned:
            flags.append("mentioned_without_status")
        return "missing", [], flags
    if len(present) > 1:
        merged = []
        for name in present:
            merged += [e for e in evidence[name] if e not in merged]
        return "unclear", merged, flags + ["conflicting_cues:" + "+".join(present)]
    return present[0], evidence[present[0]], flags


def _unreviewed(item, pages):
    return bool(item.get("needs_visual_review_unresolved")) or not pages or \
        bool(UNREVIEWED_DIAGNOSTICS & set(item.get("diagnostics") or []))


def analyse(register, received, page_text, *, run_id):
    """Pure. register: parsed CURRENT-REQUEST-REGISTER.json; received: parsed
    received-items.json; page_text: {item_id: [(page, text), ...]}."""
    items = {}
    for item in received.get("items", []):
        items.setdefault(item["item_id"], item)
    unlinked = 0
    joined = {}
    for item_id in sorted(items):
        item = items[item_id]
        if item.get("kind") not in RESPONSE_KINDS:
            continue
        if item.get("agency_id") is None or item.get("request_id") is None:
            unlinked += 1
            continue
        joined.setdefault((item["agency_id"], item["request_id"]), []).append(item_id)
    counts = {name: 0 for name in RATINGS}
    requests = []
    for row in sorted(register.get("requests", []), key=lambda r: (str(r.get("agency_key")), str(r.get("request_id")))):
        ledger_id = row.get("ledger_request_id")
        ids = joined.get((row.get("agency_key"), ledger_id), []) if ledger_id is not None else []
        if not ids:
            continue  # REC-AUTO-7 owns no-response
        responses, any_unreviewed = [], False
        for item_id in ids:
            pages = [(int(p), t) for p, t in page_text.get(item_id, [])]
            pages.sort(key=lambda pt: pt[0])
            responses.append((item_id, pages))
            any_unreviewed = any_unreviewed or _unreviewed(items[item_id], pages)
        asked = split_asked(row.get("asked"))
        flags = [] if asked else ["scope_unknown"]
        if any_unreviewed:
            flags.append("text_unreviewed")
        asks = []
        for ask in asked:
            rating, evidence, aflags = _rate(content_words(ask["_full"]), responses)
            aflags = list(ask["flags"]) + aflags
            if any_unreviewed:
                aflags.append("text_unreviewed")
                if rating == "missing":
                    rating = "unclear"
                    aflags.append("unclear_reason:text_unreviewed")
            counts[rating] += 1
            asks.append({"ask_id": ask["ask_id"], "text": ask["text"], "rating": rating,
                         "evidence": evidence, "flags": sorted(set(aflags))})
        requests.append({"agency_key": row.get("agency_key"), "request_id": row.get("request_id"),
                         "ledger_request_id": ledger_id, "flags": flags, "responses": list(ids), "asks": asks})
    return {"schema": SCHEMA, "run_id": run_id, "review_required": True,
            "semantic_assessment": "machine_rated_review_required", "requests": requests,
            "counts": counts, "unlinked_items": unlinked, "limits": list(LIMITS)}


# ---------------------------------------------------------------- IO

def _read_json(path):
    """Regular, single-link, bounded, duplicate-key-free JSON or ValueError/FileNotFoundError."""
    info = path.lstat()
    _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= MAX_INPUT_BYTES,
          "response_items_input_file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    _need(len(raw) <= MAX_INPUT_BYTES, "response_items_input_file")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            _need(key not in result, "response_items_duplicate_key")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (json.JSONDecodeError, UnicodeError, RecursionError):
        raise ValueError("response_items_input_json") from None


def _page_text(db, processing, received):
    out = {}
    for item in received.get("items", []):
        if item.get("kind") in RESPONSE_KINDS and item.get("agency_id") and item.get("request_id"):
            units, _, _ = _ri._current_units(db, processing, item["original_sha256"])
            out[item["item_id"]] = [(u["page"], u["text"]) for u in units]
    return out


def write_run_response_items(project, run_id):
    """Outputs-stage hook, after the register and received items exist.

    Writes only run-outputs/<run>/outcomes/response-items.json (0700/0600).
    Missing register or received file: nothing written. Identical replay is a
    no-op; a different existing artifact is refused.
    """
    from ..run_stages import LedgerBusy, snapshot_ro
    try:
        _need(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id) is not None, "response_items_run_id")
        project = _ri._safe_dir(project)
        run_dir = _ri._safe_dir(project / "run-outputs" / run_id)
        outcomes = _ri._safe_dir(run_dir / "outcomes")
        try:
            register = _read_json(run_dir / "CURRENT-REQUEST-REGISTER.json")
            received = _read_json(outcomes / "received-items.json")
        except FileNotFoundError:
            return {"requests": [], "skipped": "inputs_missing"}
        _need(type(register) is dict and type(received) is dict and received.get("run_id") == run_id
              and received.get("schema") == _ri.SCHEMA and type(register.get("requests")) is list
              and type(received.get("items")) is list, "response_items_input_shape")
        processing = _ri._safe_dir(project / "processing-v1")
        _need(not (processing / "ledger.sqlite").is_symlink(), "response_items_symlink_path")
        with snapshot_ro(processing / "ledger.sqlite") as db:
            db.row_factory = sqlite3.Row
            pages = _page_text(db, processing, received)
        report = analyse(register, received, pages, run_id=run_id)
        encoded = (canon(report) + "\n").encode()
        _need(len(encoded) <= MAX_OUTPUT_BYTES, "response_items_output_bound")
        os.chmod(outcomes, 0o700)
        target = outcomes / "response-items.json"
        if os.path.lexists(target):
            info = target.lstat()
            _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "response_items_run_already_exists")
            try:
                same = read_pinned(target, sha(encoded), MAX_OUTPUT_BYTES) == encoded
            except (ValueError, OSError):
                same = False
            _need(same, "response_items_run_already_exists")
            return report
        temporary = outcomes / ".response-items.json.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            if os.path.lexists(target):
                raise ValueError("response_items_run_already_exists")
            os.replace(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return report
    except (ValueError, sqlite3.Error, LedgerBusy) as error:
        if isinstance(error, ValueError) and str(error) == "response_items_run_already_exists":
            raise OSError("response_items_run_already_exists") from error
        raise OSError("RESPONSE_ITEMS_UNAVAILABLE") from error
