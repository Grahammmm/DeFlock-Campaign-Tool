"""Pure cited-fact checks; structural grounding is never semantic acceptance."""
# Port: /home/user/agent-work/flock-mvp/dossier.py, sha256 750dfeb1…8c553723.
# Source lines 42–46, 48, 56, 498–592, 596–751, 770–802, 987–1001, 1163–1228.
import re
import unicodedata
from . import evidence

CATEGORIES = (
    "vendor_contract", "cost", "cameras", "retention", "sharing", "audits", "search_logs", "policy_date",
)
MIN_QUOTE = 12
MAX_QUOTE = 600

MAX_CLAIM = 1000              # verbatim-sentence claims must not be clipped again

LEGAL_WORDS = re.compile(r"\b(non-?compliant|compliant|compliance|violat\w*|unlawful|illegal|lawful)\b", re.I)

def page_texts(row: dict, text: str) -> list[tuple[int, int, int]]:
    """[(page, start, end)] using F1 page_spans (character offsets into the text file)."""
    spans = row.get("page_spans") or [{"page": 1, "start": 0, "end": len(text)}]
    return [(int(s["page"]), int(s["start"]), int(s["end"])) for s in spans]

_TRANS = {ord(c): "'" for c in "\u2018\u2019\u201a\u201b\u2032`\u00b4"}
_TRANS.update({ord(c): '"' for c in "\u201c\u201d\u201e\u201f\u2033"})
_TRANS.update({ord(c): "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"})
_TRANS.update({ord(c): None for c in "\u00ad\u200b\u200c\u200d\ufeff"})

def _norm_char(ch: str) -> str:
    ch = ch.translate(_TRANS)
    if not ch:
        return ""
    if ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf"):
        return " "
    return ch.lower()

def normalize_with_map(s: str) -> tuple[str, list[int]]:
    """Normalized string plus, for each normalized char, the index of the original char."""
    out, idx = [], []
    prev_space = True
    for i, ch in enumerate(s):
        n = _norm_char(ch)
        for c in n:
            if c == " ":
                if prev_space:
                    continue
                prev_space = True
            else:
                prev_space = False
            out.append(c)
            idx.append(i)
    while out and out[-1] == " ":
        out.pop()
        idx.pop()
    return "".join(out), idx

def normalize(s: str) -> str:
    return normalize_with_map(s)[0]

_SPACE_CLASS = r"[\s\x00-\x1f\x7f-\x9f\u00ad\u200b-\u200d\ufeff]+"
_ZW_OPT = r"[\u00ad\u200b-\u200d\ufeff]*"
_VARIANTS = {"'": "['\u2018\u2019\u201a\u201b\u2032`\u00b4]", '"': "[\"\u201c\u201d\u201e\u201f\u2033]",
             "-": "[-\u2010\u2011\u2012\u2013\u2014\u2015\u2212]"}

def quote_pattern(quote: str) -> re.Pattern | None:
    """Regex equivalent of normalize(page).find(normalize(quote)), returning original offsets.

    Whitespace/control runs match any whitespace/control run, typographic quotes and dashes match their ASCII
    forms, zero-width characters and soft hyphens are ignored, case-insensitive. Runs in C, so it is fast even on
    the very large single-page spreadsheet texts."""
    nq = normalize(quote).strip()
    if len(nq) < MIN_QUOTE:
        return None
    parts = []
    for ch in nq:
        if ch == " ":
            parts.append(_SPACE_CLASS)
        else:
            parts.append((_VARIANTS.get(ch) or re.escape(ch)) + _ZW_OPT)
    return re.compile("".join(parts), re.IGNORECASE)

def locate_quote(text: str, start: int, end: int, quote: str) -> tuple[int, int] | None:
    """Find quote inside text[start:end]; return absolute (qs, qe) of the original span or None."""
    pat = quote_pattern(quote)
    if pat is None:
        return None
    m = pat.search(text, start, end)
    if not m:
        return None
    qs, qe = m.start(), m.end()
    while qe > qs and text[qe - 1] in "\u00ad\u200b\u200c\u200d\ufeff":
        qe -= 1
    return qs, qe

def verify_citation(text: str, spans: list[tuple[int, int, int]], cit: dict) -> str | None:
    """Return None if the citation re-opens to its quote, else an error string."""
    page = {p: (s, e) for p, s, e in spans}.get(cit["page"])
    if page is None:
        return "page_not_in_document"
    s, e = page
    if not (s <= cit["start"] < cit["end"] <= e):
        return "span_outside_page"
    if text[cit["start"]:cit["end"]] != cit["quote"]:
        return "quote_mismatch"
    return None

MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december")
_MONTH_RE = re.compile(r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
                       r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b\.?", re.I)
NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
}
# In claims, "one" is too often non-numeric ("one of", "one sentence"); it is not demanded from the quote.
_CLAIM_WORD_SKIP = {"zero", "one", "hundred"}
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
_WORD_RE = re.compile(r"\b(" + "|".join(NUMBER_WORDS) + r")\b", re.I)

def _canon_num(tok: str) -> str:
    """'15,000.00' -> '15000', '03' -> '3', '462.3' -> '462.3', '1798.90.51' -> '1798.90.51'."""
    t = tok
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", t):
        t = t.replace(",", "")
    if re.fullmatch(r"\d+(?:\.\d+)?", t):
        if "." in t:
            t = t.rstrip("0").rstrip(".")
        t = t.lstrip("0") or "0"
    return t

def _num_tokens(s: str) -> set[str]:
    out = set()
    for m in _NUM_RE.finditer(s):
        tok = m.group(0).rstrip(".,")
        out.add(_canon_num(tok))
        # "10,000" written "10.000" or a comma list "1,2" — also index the parts
        for part in re.split(r"[.,]", tok):
            if part:
                out.add(_canon_num(part) + "#part")
    return out

def _month_numbers(s: str) -> set[int]:
    found = set()
    for m in _MONTH_RE.finditer(s):
        w = m.group(1).lower()[:3]
        if w in ("may", "mar", "dec", "jun", "jan", "aug", "sep", "oct", "nov", "feb", "apr", "jul") \
                and len(m.group(1)) <= 4:
            # short forms double as words ("may", "mar") - count them only next to a day/year number
            near = s[max(0, m.start() - 4):m.start()] + "|" + s[m.end():m.end() + 6]
            if not re.search(r"\d", near):
                continue
        for i, name in enumerate(MONTHS, 1):
            if name.startswith(w):
                found.add(i)
                break
    return found

def unsupported_tokens(claim: str, value: str | None, quote: str) -> list[str]:
    """Numbers, number words and month names in claim/value that the quote does not contain.

    A claim may only restate what its quote says (review round 1, must-fix 1). Allowed equivalences: thousands
    separators and trailing zeros ('$15,000' ~ '15,000.00'), leading zeros ('03' ~ '3'), number words in the
    quote ('thirty (30)', 'nine'), a month number backed by a month name in the quote or vice versa, and a
    4-digit year backed by its 2-digit form in a numeric date ('1/5/24')."""
    q_nums = _num_tokens(quote)
    q_full = {t for t in q_nums if not t.endswith("#part")}
    q_parts = {t[:-5] for t in q_nums if t.endswith("#part")}
    for m in _WORD_RE.finditer(quote):
        q_full.add(str(NUMBER_WORDS[m.group(1).lower()]))
    q_months = _month_numbers(quote)
    q_all = q_full | q_parts
    target = claim + " \n " + (value or "")
    missing: list[str] = []
    for m in _NUM_RE.finditer(target):
        tok = m.group(0).rstrip(".,")
        c = _canon_num(tok)
        if c in q_full:
            continue
        parts = [_canon_num(p) for p in re.split(r"[.,]", tok) if p]
        if len(parts) > 1 and all(p in q_all for p in parts) and "," not in tok:
            continue  # e.g. section '462.3' quoted as '462.3' split by OCR spacing
        n = int(c) if c.isdigit() else None
        if n is not None:
            if c in q_parts:
                continue
            if 1 <= n <= 12 and n in q_months:
                continue
            if 1900 <= n <= 2099 and _canon_num(str(n % 100)) in q_all and re.search(r"\d[/-]\d", quote):
                continue
        missing.append(tok)
    for m in _WORD_RE.finditer(claim):
        w = m.group(1).lower()
        if w in _CLAIM_WORD_SKIP:
            continue
        if str(NUMBER_WORDS[w]) not in q_all:
            missing.append(w)
    for mo in _month_numbers(target):
        if mo in q_months:
            continue
        if re.search(rf"(?<!\d)0?{mo}[/-]\d", quote) or re.search(rf"\d{{4}}-0?{mo}-\d", quote):
            continue
        missing.append(MONTHS[mo - 1])
    return missing

# --- modality (review round 2, must-fix 1): a requirement/permission/intention/responsibility in the quote must
# stay one in the claim; "shall conduct an audit" is not evidence that an audit was conducted.
MODAL_QUOTE = re.compile(
    r"\b(?:shall|must|should|ought\s+to|will|would|may(?!\s*\d)|might|can(?:not)?|could|"
    r"(?:is|are|be|was|were)\s+(?:not\s+)?to\s+be|(?:is|are)\s+(?:not\s+)?to\s+\w+|"
    r"responsib\w*|requir\w*|obligat\w*|agrees?\s+to|intends?\s+to|plans?\s+to|"
    r"permit\w*|authori[sz]\w*|allow\w*|entitle\w*|expected\s+to|up\s+to|no\s+(?:more|longer)\s+than|"
    r"not\s+(?:to\s+)?exceed\w*|minimum|maximum|at\s+least|at\s+most)\b", re.I)
MODAL_CLAIM = re.compile(
    r"\b(?:shall|must|should|will|would|may|might|can(?:not)?|could|to\s+be|is\s+to|are\s+to|"
    r"responsib\w*|requir\w*|obligat\w*|agrees?|intends?|plans?|permit\w*|authori[sz]\w*|allow\w*|"
    r"entitle\w*|expected|up\s+to|no\s+(?:more|longer)\s+than|not\s+(?:to\s+)?exceed\w*|minimum|maximum|"
    r"at\s+least|at\s+most|assign\w*|direct\w*|mandat\w*|provid\w*\s+for|right\s+to|duty|"
    r"calls?\s+for|subject\s+to|limit\w*|prohibit\w*|unless|if)\b", re.I)

def modality_lost(claim: str, quote: str) -> bool:
    """True when the quote is modal (requirement, permission, intention, responsibility, bound) but the claim
    asserts it as plain fact."""
    return bool(MODAL_QUOTE.search(normalize(quote))) and not MODAL_CLAIM.search(normalize(claim))

# --- clause coverage: every substantive claim term must occur in the quote (catches added clauses such as
# "then purged unless it is or may become evidence" on a quote that ends at "retention schedule").
FRAMING_WORDS = set("""
a an and or nor but the of to in on at by for from with within without into onto over under per as is are was were
be been being it its it's this that these those which who whom whose what when where there their they them than then
so such also both either neither each every any all some no not only other another same own via about after before
during between among through up down out off again further once here more most less least very
document documents record records states state stated stating says said lists listed list listing shows show shown
showing includes include included including contains contain containing provides provide provided specifies specify
specified indicates indicate indicated describes describe described notes note noted mentions mention identifies
identify identified sets set reads read refers refer reflects reflect gives give confirms confirm references
reference indicating explains explain explained reports report reported entry entries item items line row rows
column columns header heading table section page pages text passage clause paragraph term terms title titled
labeled labelled named entitled called dated date
agreement agreements contract contracts policy policies invoice invoices email emails letter letters memo
memorandum mou schedule schedules spreadsheet log logs form forms order quote quotation response responses
attachment attachments notice report reports manual procedure procedures exhibit appendix addendum amendment
retention statement work sow moa purchase footer headed headings file files filename csv xlsx pdf workbook worksheet
feature signature logo numbered followed portal screen screenshot recipient entered carries carry
shall must should will would may might can cannot could require requires required requiring requirement
requirements responsible responsibility responsibilities obligation obligations obligated obligates agree agrees
agreed intend intends intended plan plans planned permit permits permitted permission allow allows allowed
authorize authorizes authorized authorise authorises authorised entitled expected assign assigns assigned direct
directs directed mandate mandates mandated duty duties right rights calls subject limit limits limited prohibit
prohibits prohibited unless if minimum maximum least most exceed exceeds provides providing
""".split())
# Neutral glosses a claim may use without the quote spelling them out (subject/system, never an attribute).
GLOSS_WORDS = set("has have had does did carries carry bears appears appear alpr lpr data agency agencies city "
                  "county department its".split())

def _stem(w: str) -> str:
    w = w.lower()
    for suf in ("ations", "ation", "ments", "ment", "ings", "ing", "ies", "ied", "ers", "er", "ed", "es", "ly",
                "s"):
        if len(w) - len(suf) >= 4 and w.endswith(suf):
            return w[:-len(suf)]
    return w

def claim_terms_missing(claim: str, value: str | None, quote: str, allowed: set[str] | frozenset = frozenset()
                        ) -> list[str]:
    """Substantive alphabetic terms of claim/value (>= 3 letters, not framing/modality words) whose stem does not
    occur in the quote. Numbers/dates are handled by unsupported_tokens. `allowed` adds words that may come from
    the document identity rather than the quote (the subject agency's name)."""
    q = re.sub(r"[^a-z0-9]+", " ", normalize(quote))
    q_compact = q.replace(" ", "")
    q_stems = {_stem(w) for w in q.split()}
    missing = []
    for w in re.findall(r"[A-Za-z][A-Za-z'\u2019]*", claim + " " + (value or "")):
        lw = w.lower().replace("\u2019", "'")
        if lw.endswith("'s"):
            lw = lw[:-2]
        lw = lw.replace("'", "")
        if len(lw) < 3 or lw in FRAMING_WORDS or lw in GLOSS_WORDS or lw in allowed or lw in NUMBER_WORDS \
                or lw in MONTHS:
            continue
        st = _stem(lw)
        if st in q_stems or any(s.startswith(st) or st.startswith(s) for s in q_stems if len(s) >= 4) \
                or (len(lw) >= 4 and lw in q_compact):
            continue
        if lw not in missing:
            missing.append(lw)
    return missing

REQUESTER_CLAIM = re.compile(
    r"\b(?:the|this|your|a|our|their)\s+(?:(?:original|follow[- ]up|public|records?|cpra|pra)\s+){0,3}"
    r"request(?:er)?\s*(?:#\s*\S+\s+)?(?:states?|stated|asks?|asked|seeks?|sought|says|said|requests?|requested|"
    r"describes?|described|notes?|noted|cites?|cited|quotes?|quoted|asserts?|claims?)\b|\brequester\b|"
    r"\bdeflock\b", re.I)
REQUESTER_QUOTE = re.compile(r"\bdeflock\b|\b(?:i|we)\s+(?:am|are)\s+requesting\b", re.I)

def requester_marker(claim: str, quote: str) -> str | None:
    if REQUESTER_CLAIM.search(claim):
        return "claim_restates_request"
    if REQUESTER_QUOTE.search(quote):
        return "quote_is_requester_text"
    return None

def check_fact(f: dict, sha: str, row: dict, text: str, strict: bool = True,
               allowed: set[str] | frozenset = frozenset(), max_quote: int = MAX_QUOTE
               ) -> tuple[dict | None, str | None]:
    """Turn a raw model fact into a verified fact, or return a drop reason.

    strict=False only locates the quote (stage 1, before the support audit); strict=True also requires every
    number/date/month in claim and value to be in the quote, rejects requester text, rejects a claim that states
    as fact what the quote states as a requirement/permission/intention (modality_lost), and rejects claim terms
    absent from the quote (claim_terms_not_in_quote; `allowed` = subject-agency name words). max_quote is raised to
    MAX_PROVISION for a quote that complete_provision() widened to a full operative sentence."""
    cat = f.get("category")
    if cat not in CATEGORIES:
        return None, "unknown_category"
    quote = f.get("quote")
    if not isinstance(quote, str):
        return None, "no_quote"
    if not (MIN_QUOTE <= len(quote.strip()) <= max_quote):
        return None, "quote_length"
    claim = f.get("claim")
    if not isinstance(claim, str) or not claim.strip():
        return None, "no_claim"
    spans = page_texts(row, text)
    try:
        page = int(f.get("page"))
    except (TypeError, ValueError):
        page = None
    relocated = False
    loc = None
    by_page = {p: (s, e) for p, s, e in spans}
    if page in by_page:
        loc = locate_quote(text, *by_page[page], quote)
    if loc is None:
        hits = [(p, locate_quote(text, s, e, quote)) for p, s, e in spans]
        hits = [(p, h) for p, h in hits if h]
        if len(hits) != 1:
            return None, "quote_not_found" if not hits else "quote_ambiguous_page"
        page, loc = hits[0]
        relocated = True
    qs, qe = loc
    cit = {"sha256": sha, "page": page, "start": qs, "end": qe, "quote": text[qs:qe]}
    if relocated:
        cit["page_relocated_from"] = f.get("page")
    err = verify_citation(text, spans, cit)
    if err:
        return None, err
    quote_norm = normalize(cit["quote"])
    for m in LEGAL_WORDS.finditer(claim):
        if m.group(0).lower() not in quote_norm:
            return None, "legal_conclusion_wording"
    value = f.get("value")
    value = value.strip()[:120] if isinstance(value, str) and value.strip() else None
    if strict and unsupported_tokens(claim, value, cit["quote"]):
        return None, "claim_value_not_in_quote"
    if strict and requester_marker(claim, cit["quote"]):
        return None, "requester_text"
    if strict and modality_lost(claim, cit["quote"]):
        return None, "modality_lost"
    if strict and claim_terms_missing(claim, value, cit["quote"], allowed):
        return None, "claim_terms_not_in_quote"
    return {
        "category": cat,
        "claim": claim.strip()[:MAX_CLAIM],
        "value": value,
        "citations": [cit],
        "status": "review_required",
    }, None


class FactsError(ValueError):
    """A fixed input-validation code; never includes caller text."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _pages(units):
    """Build snapshot character spans and a reversible map back to retained units."""
    units = sorted(units, key=lambda u: (u['ordinal'], u['receipt_sha256']))
    lines = [u for u in units if u['kind'] == 'text_line']
    pages = [(u['ordinal'], u['locator']['page'], [u]) for u in units
             if u['kind'] == 'pdf_page' and type(u['locator'].get('page')) is int]
    if lines:
        pages.append((lines[0]['ordinal'], 1, lines))
    text, spans, segments = '', [], []
    for _, page, group in sorted(pages, key=lambda p: p[0]):
        start = len(text)
        for u in group:
            s = len(text)
            text += u['text']
            segments.append((s, len(text), u))
            text += '\n'
        # Snapshot maps are page-keyed; receipt-local physical pages can repeat.
        spans.append({'page': len(spans) + 1, 'source_page': page, 'start': start, 'end': len(text) - 1})
    # An explicitly empty page list must not trigger page_texts' whole-text fallback.
    return text, {'page_spans': spans or [{'page': 1, 'start': 0, 'end': 0}]}, segments


def validate(bundle, candidates, *, strict=True, allowed=frozenset()) -> dict:
    """Validate all input shapes before producing any review-required facts; no I/O."""
    try:
        evidence.validate_bundle(bundle)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise FactsError('invalid_bundle') from None
    if type(strict) is not bool or type(allowed) not in (set, frozenset) or any(type(w) is not str for w in allowed):
        raise FactsError('invalid_options')
    if type(candidates) is not list:
        raise FactsError('candidate_list')
    if len(candidates) > 2000:
        raise FactsError('candidate_limit')
    for f in candidates:
        if type(f) is not dict or set(f) != {'original_sha256', 'category', 'claim', 'value', 'quote', 'page'}:
            raise FactsError('candidate_shape')
        if (any(type(f[k]) is not str for k in ('original_sha256', 'category', 'claim', 'quote'))
                or not evidence.is_hash(f['original_sha256']) or type(f['value']) not in (str, type(None))
                or type(f['page']) not in (int, type(None))):
            raise FactsError('candidate_fields')
        try:
            evidence.canonical(f)
        except (ValueError, UnicodeError):
            raise FactsError('candidate_fields') from None
    documents = {}
    for u in bundle['units']:
        documents.setdefault(u['original_sha256'], []).append(u)
    prepared = {sha: _pages(units) for sha, units in documents.items()}
    verified, dropped, reasons = [], [], {}
    for index, f in enumerate(candidates):
        item, reason = None, 'unknown_document'
        if f['original_sha256'] in prepared:
            text, row, segments = prepared[f['original_sha256']]
            hits = [p['page'] for p in row['page_spans'] if p.get('source_page') == f['page']
                    and locate_quote(text, p['start'], p['end'], f['quote'])]
            adapted = dict(f, page=hits[0] if len(hits) == 1 else None)
            item, reason = check_fact(adapted, f['original_sha256'], row, text, strict, allowed)
            if item is not None:
                cit = item['citations'][0]
                matches = [(s, u) for s, e, u in segments if s <= cit['start'] < cit['end'] <= e]
                if not matches:
                    item, reason = None, 'quote_spans_units'
                else:
                    s, u = matches[0]
                    start, end = cit['start'] - s, cit['end'] - s
                    item['citations'] = [evidence.citation(u, len(u['text'][:start].encode()), len(u['text'][:end].encode()))]
                    item['page'] = u['locator']['page'] if u['kind'] == 'pdf_page' else 1
                    if item['page'] != f['page']:
                        item['page_relocated_from'] = f['page']
        if reason:
            dropped.append({'index': index, 'reason': reason})
            reasons[reason] = reasons.get(reason, 0) + 1
        else:
            verified.append(item)
    return dict(schema='records-understanding-facts-v1', verified=verified, dropped=dropped,
                counts=dict(candidates=len(candidates), verified=len(verified), by_reason=reasons),
                review_required=True, accepted=False)
