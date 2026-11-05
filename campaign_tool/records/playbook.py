"""Records request playbook: a data file, its loader and a banned-phrase lint.

The playbook (``playbooks/<id>.json``) holds the tier list, the first-request item
order, the follow-up patterns and the wording rules. Nothing here is specific to one
campaign or agency: all of that is data. This module only reads the file, assigns
text to a tier and checks drafts for banned phrases. It sends, posts and publishes
nothing, reads no credentials and writes no file.
"""
import argparse
import json
import re
import sys
from pathlib import Path

SCHEMA = "records-playbook-v1"
DEFAULT_PATH = Path(__file__).resolve().parents[2] / "playbooks" / "records-alpr-v1.json"
MAX_BYTES = 512 * 1024
CITE_STATUS = ("verified-in-package", "unverified")
PATTERN_KEYS = ("id", "when", "goal", "questions", "cite", "attach", "do_not")
ITEM_KEYS = ("order", "key", "tier", "ask")


class PlaybookError(Exception):
    """Carries a fixed code and a short detail naming the offending field."""

    def __init__(self, code, detail=""):
        super().__init__(code + (": " + detail if detail else ""))
        self.code = code
        self.detail = detail


def _need(condition, code, detail=""):
    if not condition:
        raise PlaybookError(code, detail)


def _strings(value, code, detail):
    _need(isinstance(value, list) and value and all(isinstance(v, str) and v.strip() for v in value), code, detail)
    return value


def _compile(entry):
    _need(isinstance(entry, dict), "banned_invalid", "entry")
    ident, pattern = entry.get("id"), entry.get("pattern")
    _need(isinstance(ident, str) and ident and isinstance(pattern, str) and pattern
          and isinstance(entry.get("reason"), str), "banned_invalid", str(ident))
    try:
        return re.compile(pattern, 0 if entry.get("case_sensitive") is True else re.IGNORECASE)
    except re.error:
        raise PlaybookError("banned_pattern_invalid", ident) from None


def validate(playbook):
    """Check structure and cross references; returns the playbook or raises PlaybookError."""
    _need(isinstance(playbook, dict), "not_object")
    _need(playbook.get("schema") == SCHEMA, "schema_mismatch")
    for key in ("id", "topic"):
        _need(isinstance(playbook.get(key), str) and playbook[key], "field_missing", key)
    _need(isinstance(playbook.get("version"), int) and not isinstance(playbook["version"], bool), "field_missing", "version")
    _strings(playbook.get("jurisdictions"), "field_missing", "jurisdictions")

    tiers = playbook.get("tiers")
    _need(isinstance(tiers, list) and tiers, "tiers_invalid")
    tier_ids = [t.get("id") if isinstance(t, dict) else None for t in tiers]
    _need(tier_ids == list(range(1, len(tiers) + 1)), "tiers_invalid", "ids must run 1..n in priority order")
    for tier in tiers:
        _need(isinstance(tier.get("name"), str) and tier["name"], "tiers_invalid", "name")
        _strings(tier.get("keywords"), "tiers_invalid", "keywords")

    cites = playbook.get("citations")
    _need(isinstance(cites, dict) and cites, "citations_invalid")
    for name, cite in cites.items():
        _need(isinstance(cite, dict) and isinstance(cite.get("ref"), str) and cite["ref"]
              and cite.get("status") in CITE_STATUS, "citations_invalid", str(name))

    first = playbook.get("first_request")
    _need(isinstance(first, dict) and isinstance(first.get("exit_ramp"), str) and first["exit_ramp"], "first_request_invalid", "exit_ramp")
    items = first.get("items")
    _need(isinstance(items, list) and items, "first_request_invalid", "items")
    keys = set()
    for position, item in enumerate(items, 1):
        _need(isinstance(item, dict) and all(k in item for k in ITEM_KEYS), "first_request_invalid", "item " + str(position))
        _need(item["order"] == position, "first_request_invalid", "order must run 1..n")
        _need(item["tier"] in tier_ids and isinstance(item["key"], str) and item["key"] and item["key"] not in keys
              and isinstance(item["ask"], str) and item["ask"], "first_request_invalid", "item " + str(position))
        keys.add(item["key"])
        for ref in item.get("cite", []):
            _need(ref in cites, "citation_unknown", str(ref))
    _need([i["tier"] for i in items] == sorted(i["tier"] for i in items), "first_request_invalid", "items must not put a lower tier first")

    _strings(playbook.get("wording_rules"), "wording_rules_invalid", "wording_rules")
    patterns = playbook.get("followup_patterns")
    _need(isinstance(patterns, list) and patterns, "patterns_invalid")
    seen = set()
    for pattern in patterns:
        _need(isinstance(pattern, dict) and all(k in pattern for k in PATTERN_KEYS), "patterns_invalid", "keys")
        _need(isinstance(pattern["id"], str) and pattern["id"] and pattern["id"] not in seen, "pattern_duplicate", str(pattern.get("id")))
        seen.add(pattern["id"])
        for key in ("when", "goal"):
            _need(isinstance(pattern[key], str) and pattern[key], "patterns_invalid", pattern["id"] + "." + key)
        _strings(pattern["questions"], "patterns_invalid", pattern["id"] + ".questions")
        _strings(pattern["do_not"], "patterns_invalid", pattern["id"] + ".do_not")
        _need(isinstance(pattern["attach"], list), "patterns_invalid", pattern["id"] + ".attach")
        _need(isinstance(pattern["cite"], list), "patterns_invalid", pattern["id"] + ".cite")
        for ref in pattern["cite"]:
            _need(ref in cites, "citation_unknown", str(ref))

    banned = playbook.get("banned_phrases")
    _need(isinstance(banned, list) and banned, "banned_invalid", "empty")
    ids = [b.get("id") if isinstance(b, dict) else None for b in banned]
    _need(len(set(ids)) == len(ids), "banned_invalid", "duplicate id")
    for entry in banned:
        _compile(entry)
    return playbook


def load(path=None):
    """Read and validate a playbook file; the default is the shipped ALPR playbook."""
    target = Path(path) if path else DEFAULT_PATH
    try:
        with open(target, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except OSError:
        raise PlaybookError("unreadable", target.name) from None
    _need(len(raw) <= MAX_BYTES, "too_large")
    try:
        playbook = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise PlaybookError("not_json", target.name) from None
    return validate(playbook)


def pattern(playbook, pattern_id):
    for entry in playbook["followup_patterns"]:
        if entry["id"] == pattern_id:
            return entry
    raise PlaybookError("pattern_unknown", str(pattern_id))


def _has_word(text, word):
    return re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", text) is not None


def tier_for(playbook, text):
    """Tier id for an asked item: the lowest-numbered (highest priority) tier with a keyword hit, else None."""
    lowered = _normalise(text).lower()
    for tier in playbook["tiers"]:
        if any(_has_word(lowered, keyword.lower()) for keyword in tier["keywords"]):
            return tier["id"]
    return None


def _normalise(text):
    return (text or "").replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')


def lint(playbook, text):
    """Banned-phrase check. Returns {'status': 'pass'|'needs edit', 'hits': [...]}.

    Each hit is {id, reason, start, text}. The check is strict: it does not know
    which words are quoted from an agency, so the drafter must reword or review.
    """
    _need(isinstance(text, str), "text_invalid")
    body = _normalise(text)
    hits = []
    for entry in playbook["banned_phrases"]:
        for match in _compile(entry).finditer(body):
            hits.append({"id": entry["id"], "reason": entry["reason"], "start": match.start(), "text": match.group(0)})
    hits.sort(key=lambda h: (h["start"], h["id"]))
    return {"status": "needs edit" if hits else "pass", "hits": hits}


def main():
    parser = argparse.ArgumentParser(prog="records playbook", description="Validate a records playbook or lint a draft against it.")
    parser.add_argument("action", choices=("check", "lint"))
    parser.add_argument("file", nargs="?", help="draft text file for lint ('-' reads standard input)")
    parser.add_argument("--playbook", help="playbook JSON; default: the shipped records-alpr-v1")
    args = parser.parse_args()
    try:
        book = load(args.playbook)
        if args.action == "check":
            print("playbook ok: " + book["id"] + " v" + str(book["version"]) + ", "
                  + str(len(book["followup_patterns"])) + " patterns, " + str(len(book["banned_phrases"])) + " banned")
            return 0
        if not args.file:
            parser.error("lint needs a draft file")
        try:
            text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise PlaybookError("draft_unreadable") from None
        result = lint(book, text)
    except PlaybookError as error:
        print("records playbook: refused " + error.code + (" (" + error.detail + ")" if error.detail else ""), file=sys.stderr)
        return 2
    for hit in result["hits"]:
        print("  " + hit["id"] + " at " + str(hit["start"]) + ": " + hit["reason"])
    print(result["status"])
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
