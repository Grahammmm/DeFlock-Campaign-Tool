"""Private, review-required proposal of an agency asks ledger from one run.

`records asks-propose --project P --run-id R [--agency K] [--apply]` reads only
the same run's request register, received-items and optional response-items
reports and proposes `P/agency-asks/K.proposed.tsv` in case_builder's column
layout. Dry run (default) is read-only. The confirmed `K.tsv` is never written
and case_builder never reads a proposal: an operator reviews and renames it.
Nothing is sent, approved or published. Output is counts only.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import urllib.parse

from .case_builder import ASK_COLUMNS
from .outcomes import received_items, register as register_mod, response_items

SUMMARY_SCHEMA = "records-asks-proposal-summary-v1"
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
SHA = re.compile(r"[0-9a-f]{64}")
ASK_ID = re.compile(r"ask-([1-9][0-9]*)")
MAX_EVIDENCE = 50
BAD_FLAGS = frozenset({"ask_truncated", "quote_truncated", "text_unreviewed", "scope_unknown"})
_I = re.I
_REF = re.compile(r"(?:\bitems?|\brequests?|\bno\.?|\bnumbers?|#)\s*#?\s*(\d{1,3}(?:\s*(?:,|and|&|through|to|-)\s*\d{1,3})*)\b", _I)
_PAREN = re.compile(r"\((\d{1,3})\)")
_PAST = re.compile(r"\battached\b|\benclosed\b|\bproduced\b|\bprovided\b", _I)
_FUTURE = re.compile(r"\bwill\s+(?:be\s+)?(?:produced?|provided?)\b", _I)
_NEGATION = re.compile(r"\b(?:not|no|never|without|cannot|unable)\b|n't\b", _I)


class ProposalError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _need(condition, code):
    if not condition:
        raise ProposalError(code)


def _read(path, code, *, optional=False):
    try:
        return response_items._read_json(path)
    except FileNotFoundError:
        if optional:
            return None
        raise ProposalError("ASKS_INPUT_UNAVAILABLE") from None
    except (ValueError, OSError):
        raise ProposalError(code) from None


def _clean_text(value):
    return isinstance(value, str) and bool(value.strip())


# ---------------------------------------------------------------- pure derivation

def _numbers(quote):
    found = set()
    for match in _REF.finditer(quote):
        found.update(int(n) for n in re.findall(r"\d+", match.group(1)))
    found.update(int(m.group(1)) for m in _PAREN.finditer(quote))
    return found


def quote_cue(quote, number):
    """-> 'produced' | 'pending' | None for one evidence quote and canonical ask number."""
    if not isinstance(quote, str) or "\n" in quote or len(response_items.sentences(quote)) != 1:
        return None
    if _numbers(quote) != {number} or _NEGATION.search(quote):
        return None
    if response_items.WITHHELD.search(quote) or response_items.NO_RECORDS.search(quote) \
            or response_items.CITE.search(quote):
        return None
    future = bool(_FUTURE.search(quote))
    past = bool(_PAST.search(_FUTURE.sub(" ", quote)))
    if future == past:
        return None
    return "pending" if future else "produced"


def _state(ask, response_ask, request_flags, evidence_items):
    """Conservative machine state from exact joined evidence only."""
    if response_ask is None or not evidence_items:
        return "unknown"
    flags = set(response_ask.get("flags") or []) | set(request_flags)
    if flags & BAD_FLAGS or any(f.startswith(("conflicting_cues", "unclear_reason")) for f in flags):
        return "unknown"
    cues = []
    for quote, item in evidence_items:
        if item.get("needs_visual_review_unresolved") or \
                response_items.UNREVIEWED_DIAGNOSTICS & set(item.get("diagnostics") or []):
            return "unknown"
        cues.append(quote_cue(quote, ask))
    if len(response_ask["evidence"]) != len(cues) or None in cues or len(set(cues)) != 1:
        return "unknown"
    if cues[0] == "produced":
        return "produced" if response_ask.get("rating") == "answered" else "unknown"
    return "pending" if response_ask.get("rating") == "answered" else "unknown"


def _split(row):
    asked = row["asked"]
    _need(_clean_text(asked) and asked.strip() != response_items.UNKNOWN_SCOPE, "ASKS_REGISTER_INVALID")
    asks = response_items.split_asked(asked)
    _need(asks, "ASKS_REGISTER_INVALID")
    _need(len(asks) < response_items.MAX_ASKS and all(
        not a["flags"] and len(a["_full"]) <= response_items.MAX_ASK and a["text"] == a["_full"]
        for a in asks), "ASKS_SCOPE_INCOMPLETE")
    return asks


def _check_register(report):
    _need(type(report) is dict and report.get("schema") == register_mod.SCHEMA
          and type(report.get("requests")) is list
          and report.get("request_count") == len(report["requests"]), "ASKS_REGISTER_INVALID")
    try:
        datetime.date.fromisoformat(report.get("as_of"))
    except (TypeError, ValueError):
        raise ProposalError("ASKS_REGISTER_INVALID") from None
    seen, rows = set(), {}
    for row in report["requests"]:
        _need(type(row) is dict and isinstance(row.get("agency_key"), str)
              and KEY.fullmatch(row["agency_key"]) and _clean_text(row.get("request_id"))
              and type(row.get("issues")) is list and "asked" in row
              and (row.get("ledger_request_id") is None or _clean_text(row["ledger_request_id"])),
              "ASKS_REGISTER_INVALID")
        key = (row["agency_key"], row["request_id"])
        _need(key not in seen, "ASKS_REGISTER_INVALID")
        seen.add(key)
        rows[key] = row
    return rows


def _check_received(report, run_id):
    _need(type(report) is dict and report.get("schema") == received_items.SCHEMA
          and report.get("run_id") == run_id and type(report.get("items")) is list, "ASKS_RECEIVED_INVALID")
    by_id = {}
    for item in report["items"]:
        _need(type(item) is dict and _clean_text(item.get("item_id")), "ASKS_RECEIVED_INVALID")
        by_id.setdefault(item["item_id"], []).append(item)
    return by_id


def _check_response(report, run_id, rows, sources):
    """-> {(agency, request): {number: (response_ask, [(quote, item, locator)])}} or {}."""
    if report is None:
        return {}, {}
    _need(type(report) is dict and report.get("schema") == response_items.SCHEMA
          and report.get("run_id") == run_id and type(report.get("requests")) is list,
          "ASKS_RESPONSE_INVALID")
    joined, flags = {}, {}
    for request in report["requests"]:
        _need(type(request) is dict and type(request.get("asks")) is list
              and type(request.get("flags")) is list, "ASKS_RESPONSE_INVALID")
        key = (request.get("agency_key"), request.get("request_id"))
        row = rows.get(key)
        _need(key not in joined, "ASKS_RESPONSE_INVALID")
        _need(row is not None and request.get("ledger_request_id") == row.get("ledger_request_id"),
              "ASKS_EVIDENCE_INVALID")
        source = sources[key]
        joined[key], flags[key] = {}, list(request["flags"])
        for ask in request["asks"]:
            _need(type(ask) is dict and type(ask.get("evidence")) is list and type(ask.get("flags")) is list
                  and ask.get("rating") in response_items.RATINGS and isinstance(ask.get("ask_id"), str),
                  "ASKS_RESPONSE_INVALID")
            match = ASK_ID.fullmatch(ask["ask_id"])
            _need(match and int(match[1]) <= len(source) and int(match[1]) not in joined[key]
                  and source[int(match[1]) - 1]["text"] == ask.get("text"), "ASKS_EVIDENCE_INVALID")
            _need(len(ask["evidence"]) <= MAX_EVIDENCE, "ASKS_EVIDENCE_INVALID")
            joined[key][int(match[1])] = ask
    return joined, flags


def _locators(key, row, ask, by_id):
    """Validate every evidence entry of one ask -> [(quote, received_item, locator)]."""
    out, seen = [], set()
    for entry in ask["evidence"]:
        _need(type(entry) is dict and _clean_text(entry.get("item_id")) and _clean_text(entry.get("quote"))
              and type(entry.get("page")) is int and entry["page"] > 0, "ASKS_EVIDENCE_INVALID")
        matches = by_id.get(entry["item_id"], [])
        _need(len(matches) == 1, "ASKS_EVIDENCE_INVALID")
        item = matches[0]
        _need(item.get("agency_id") == key[0] and row.get("ledger_request_id") is not None
              and item.get("request_id") == row["ledger_request_id"]
              and isinstance(item.get("original_sha256"), str) and SHA.fullmatch(item["original_sha256"]),
              "ASKS_EVIDENCE_INVALID")
        locator = {"item_id": entry["item_id"], "page": entry["page"],
                   "quote_sha256": hashlib.sha256(entry["quote"].encode("utf-8")).hexdigest(),
                   "original_sha256": item["original_sha256"]}
        ident = tuple(locator.values())
        _need(ident not in seen, "ASKS_EVIDENCE_INVALID")
        seen.add(ident)
        out.append((entry["quote"], item, locator))
    return out


def derive(register, received, response, run_id, agency=None):
    """Pure: parsed reports -> {agency_key: [row dict in ASK_COLUMNS]}. Raises ProposalError."""
    rows = _check_register(register)
    by_id = _check_received(received, run_id)
    if agency is not None:
        _need(isinstance(agency, str) and KEY.fullmatch(agency), "ASKS_AGENCY_INVALID")
        _need(any(k[0] == agency for k in rows), "ASKS_AGENCY_UNKNOWN")
    sources = {}
    selected = sorted(k for k in rows if agency is None or k[0] == agency)
    for key in selected:
        _need(not rows[key]["issues"], "ASKS_REGISTER_INVALID")
        sources[key] = _split(rows[key])
    for key in rows:
        if key not in sources:  # unselected requests: only need a well-formed response join
            sources[key] = response_items.split_asked(rows[key].get("asked")) \
                if isinstance(rows[key].get("asked"), str) else []
    joined, request_flags = _check_response(response, run_id, rows, sources)
    out = {}
    for key in selected:
        row = rows[key]
        for number, source in enumerate(sources[key], 1):
            ask = joined.get(key, {}).get(number)
            evidence = _locators(key, row, ask, by_id) if ask else []
            state = _state(number, ask, request_flags.get(key, []), [(q, i) for q, i, _ in evidence])
            held = sorted({loc["original_sha256"] for q, i, loc in evidence
                           if quote_cue(q, number) == "produced"}) if state == "produced" else []
            out.setdefault(key[0], []).append({
                "ask_id": "%s#ask-%d" % (urllib.parse.quote(key[1], safe=""), number),
                "message": key[1], "item_no": str(number), "item_text": source["_full"], "period": "",
                "response": json.dumps([loc for _, _, loc in evidence], sort_keys=True, ensure_ascii=False,
                                       separators=(",", ":")),
                "state": state, "held_doc_ids": json.dumps(held, separators=(",", ":"))})
    return out


def encode(rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, ASK_COLUMNS, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


# ---------------------------------------------------------------- IO

def _lstat(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _existing(path, payload):
    """-> True when an identical regular proposal exists, False when absent."""
    info = _lstat(path)
    if info is None:
        return False
    _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "ASKS_TARGET_UNSAFE")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            current = stream.read(len(payload) + 1)
    except OSError:
        raise ProposalError("ASKS_TARGET_UNSAFE") from None
    _need(current == payload, "ASKS_PROPOSAL_EXISTS")
    return True


def write_proposals(directory, payloads):
    """Create each {name: bytes} no-clobber, 0600, never replacing; roll back on failure."""
    created, made_dir = [], False
    old = os.umask(0o077)
    try:
        if _lstat(directory) is None:
            os.mkdir(directory, 0o700)
            made_dir = True
        for name, payload in sorted(payloads.items()):
            target = directory / name
            temporary = directory / ("." + name + ".%d.tmp" % os.getpid())
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(temporary, target)  # atomic and refuses an existing target
                created.append(target)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
    except (OSError, ProposalError):
        for target in created:
            try:
                os.unlink(target)
            except OSError:
                pass
        if made_dir:
            try:
                os.rmdir(directory)
            except OSError:
                pass
        raise ProposalError("ASKS_WRITE_FAILED") from None
    finally:
        os.umask(old)
    return len(created)


def propose(project, run_id, agency=None, *, apply=False):
    _need(isinstance(run_id, str) and RUN_ID.fullmatch(run_id), "ASKS_RUN_INVALID")
    try:
        project = received_items._safe_dir(Path(project))
        run_dir = received_items._safe_dir(project / "run-outputs" / run_id)
        outcomes = received_items._safe_dir(run_dir / "outcomes")
    except (ValueError, OSError):
        raise ProposalError("ASKS_INPUT_UNAVAILABLE") from None
    register = _read(run_dir / "CURRENT-REQUEST-REGISTER.json", "ASKS_REGISTER_INVALID")
    received = _read(outcomes / "received-items.json", "ASKS_RECEIVED_INVALID")
    response = _read(outcomes / "response-items.json", "ASKS_RESPONSE_INVALID", optional=True)
    proposals = derive(register, received, response, run_id, agency)
    payloads = {k + ".proposed.tsv": encode(rows) for k, rows in proposals.items()}
    directory = project / "agency-asks"
    info = _lstat(directory)
    _need(info is None or (stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)), "ASKS_TARGET_UNSAFE")
    present = {n for n, payload in payloads.items() if info is not None and _existing(directory / n, payload)}
    fresh = {n: p for n, p in payloads.items() if n not in present}
    written = write_proposals(directory, fresh) if apply and fresh else 0
    rows = [r for agency_rows in proposals.values() for r in agency_rows]
    return {"schema": SUMMARY_SCHEMA, "run_id": run_id, "dry_run": not apply, "agencies": len(proposals),
            "rows": len(rows), "written": written, "present": len(present),
            "unknown": sum(r["state"] == "unknown" for r in rows)}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records asks-propose", description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--agency")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        summary = propose(args.project, args.run_id, args.agency, apply=args.apply)
    except ProposalError as error:
        print(error.code, file=sys.stderr)
        return 2
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
