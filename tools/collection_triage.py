#!/usr/bin/env python3
"""Unattended first-pass triage of a returned records collection (collection-triage-v1).

  collection_triage.py run --collection C --out NEWDIR
  collection_triage.py report OUTDIR

C holds collection.json (schema collection-triage-input-v1: a name and a members list of {id, text}) and the extracted-text
files it names (paths relative to C, no '..', no symlinks; text null = no extracted text). The run is read-only over C,
deterministic and model-free: explicit phrase counts and structural signals decide each member's kind, so a verdict can be
argued with. It is a first pass, not a review: every row carries review_state 'unreviewed' and nothing here sets any other.

NEWDIR must be absolute, normalized, absent, outside the repository and the collection, with a link-free existing parent. It is
built in memory, then written 0700/0600 with O_EXCL|O_NOFOLLOW:
  collection-triage-v1.json  one row per member: kind, confidence, reason {signal, locator, text_sha256}, review_state
  QUEUE.md                   every member, read order, one reason line each (nothing is omitted, nothing is hidden)
  triage-status.txt          the status line      triage-notes.json  alert-only privacy and legal-claim notes (no text)
Kinds: responsive, policy, contract, search-log, boilerplate, duplicate, unsorted; confidence high|medium|low is its own field.
duplicate = identical SHA-256 of the text bytes only (never title, name or page count). No text, or an unreadable file, is
'unsorted' (never boilerplate) and is named in the queue. Rows carry ids, locators, hashes, enums and counts, never document text.
Exit 0 clean; 1 any member unreadable (named, the others still triaged); 2 usage (one line printed, nothing created).
"""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("_triage_flock_privacy", Path(__file__).resolve().parent / "flock_privacy.py")
_spec.loader.exec_module(privacy := importlib.util.module_from_spec(_spec))

SCHEMA, INPUT_SCHEMA, REVIEW_STATE = "collection-triage-v1", "collection-triage-input-v1", "unreviewed"
KINDS = ("responsive", "policy", "contract", "search-log", "boilerplate", "duplicate", "unsorted")
STATUS_TAIL = "unreviewed; machine triage, not a review"
MAX_BYTES = 2 * 1024 * 1024
# Rule families: (kind, phrases, distinct phrases needed to fire). Confidence is medium at need+1 and high at need+3.
RULES = (
    ("policy", ("policy", "effective date", "all employees", "shall", "section", "board of directors", "retention",
                "public records act"), 3),
    ("contract", ("agreement", "parties", "whereas", "witnesseth", "hereinafter", "term of this", "vendor", "indemnif",
                  "governing law"), 3),
    ("responsive", ("in response to", "your request", "enclosed please find", "herewith", "attached please", "you requested",
                    "public records request", "pursuant to"), 2),
    ("boilerplate", ("sincerely", "very truly", "kind regards", "please be advised", "this letter is to", "you are hereby"), 1),
)
LOG_WORDS = ("timestamp", "user", "query", "hits", "date/time", "search string", "results", "log")
DATE_RE = re.compile(r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b")
# Queue order: responsive, policy, contract, search-log (not low), unsorted, any other low confidence, boilerplate, duplicate.
GROUP = {"responsive": 0, "policy": 1, "contract": 2, "search-log": 3, "unsorted": 4, "boilerplate": 6, "duplicate": 7}
CONF = {"high": 0, "medium": 1, "low": 2}


class Refused(Exception):
    pass


class Unreadable(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refused("usage_invalid")


def _linked(path):
    return any(p.is_symlink() for p in (path, *path.parents))


def _inside(path, parent):
    return path == parent or parent in path.parents


def checked_run_args(argv):
    parser = _Parser(prog="collection_triage.py run", add_help=False)
    parser.add_argument("--collection", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    collection, out = Path(args.collection), Path(args.out)
    if not collection.is_absolute() or not out.is_absolute():
        raise Refused("absolute_paths_required")
    if any(Path(os.path.normpath(str(p))) != p for p in (collection, out)):
        raise Refused("normalized_paths_required")
    if not collection.is_dir() or _linked(collection):
        raise Refused("collection_dir_unavailable")
    if os.path.lexists(out):
        raise Refused("output_dir_exists")
    if not out.parent.is_dir() or _linked(out.parent):
        raise Refused("output_parent_unavailable")
    if _inside(out, collection) or _inside(collection, out):
        raise Refused("output_dir_overlaps_collection")
    if _inside(out, ROOT):
        raise Refused("output_dir_inside_repository")
    return collection, out


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def strict_json(raw):
    def nonfinite(token):
        raise ValueError("nonfinite_constant")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs, parse_constant=nonfinite)


def load_collection(collection):
    manifest = collection / "collection.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise Refused("manifest_missing")
    try:
        document = strict_json(manifest.read_bytes())
    except (ValueError, UnicodeError, RecursionError):
        raise Refused("manifest_json_invalid") from None
    if not isinstance(document, dict) or set(document) != {"schema", "name", "members"}:
        raise Refused("manifest_shape_invalid")
    if document["schema"] != INPUT_SCHEMA or not isinstance(document["name"], str) or not document["name"]:
        raise Refused("manifest_schema_invalid")
    members, seen = document["members"], set()
    if not isinstance(members, list) or not members:
        raise Refused("members_empty")
    for member in members:
        if not isinstance(member, dict) or set(member) != {"id", "text"}:
            raise Refused("member_shape_invalid")
        member_id, text = member["id"], member["text"]
        if not isinstance(member_id, str) or not member_id or member_id != member_id.strip() or member_id in seen:
            raise Refused("member_id_invalid")
        seen.add(member_id)
        if text is not None and (not isinstance(text, str) or not text or text.startswith("/") or ".." in text.split("/")):
            raise Refused("member_text_invalid")
    return document["name"], members


def read_text(collection, member):
    """-> the member's text bytes, or None when it has no extracted text (null path or an empty file)."""
    if member["text"] is None:
        return None
    path = collection / member["text"]
    if any((collection.joinpath(*path.relative_to(collection).parts[:n])).is_symlink()
           for n in range(1, len(path.relative_to(collection).parts) + 1)):
        raise Unreadable("text_is_link")
    if not path.is_file():
        raise Unreadable("text_missing")
    if path.stat().st_size > MAX_BYTES:
        raise Unreadable("text_too_large")
    try:
        data = path.read_bytes()
        data.decode("utf-8")
    except (OSError, UnicodeError):
        raise Unreadable("text_not_readable_utf8") from None
    return data or None


def _distinct(text, phrases):
    return sum(1 for phrase in phrases if re.search(r"\b" + re.escape(phrase), text))


def classify(data):
    """-> (kind, confidence, signal, locator) from one member's text bytes (duplicates are decided by the caller)."""
    if data is None:
        return "unsorted", "low", "no-text", "text=null"
    text = data.decode("utf-8").lower()
    scores = [(kind, _distinct(text, phrases), need) for kind, phrases, need in RULES]
    lines = [line for line in text.splitlines() if line.strip()]
    cells = [c for line in lines for c in re.split(r"\t|,|;", line) if c.strip()]
    dates = len(DATE_RE.findall(text))
    if len(cells) >= 24 and len(cells) >= 3 * len(lines):
        scores.append(("search-log", _distinct(text, LOG_WORDS), 2))
    fired = [(distinct - need, -n, kind, distinct, need) for n, (kind, distinct, need) in enumerate(scores) if distinct >= need]
    if not fired:
        return "unsorted", "low", "no-rule", "words=%d" % len(text.split())
    _, _, kind, distinct, need = max(fired)
    if kind == "search-log":
        confidence = "high" if len(cells) >= 40 and dates >= 3 else "medium"
        return kind, confidence, "table-log-words", "table:cells=%d,dates=%d,words=%d" % (len(cells), dates, distinct)
    confidence = "high" if distinct >= need + 3 else "medium" if distinct >= need + 1 else "low"
    return kind, confidence, kind + "-phrases", "text:distinct=%d,need=%d" % (distinct, need)


def verdict(member_id, kind, confidence, signal, locator, digest):
    return {"id": member_id, "kind": kind, "confidence": confidence, "review_state": REVIEW_STATE,
            "reason": {"signal": signal, "locator": locator, "text_sha256": digest}}


def queue_key(v):
    if v["kind"] in ("responsive", "policy", "contract", "search-log") and v["confidence"] == "low":
        return (5, CONF[v["confidence"]], v["id"])
    return (GROUP[v["kind"]], CONF[v["confidence"]], v["id"])


def label(v):
    return v["kind"] + {"no-text": " (no text)", "unreadable": " (unreadable)"}.get(v["reason"]["signal"], "")


def render_queue(name, ordered):
    lines = ["# Queue: " + name, "", "Read order, most likely responsive first. Every member is listed; nothing is hidden.", ""]
    lines += ["%d. %s: %s (%s confidence): %s, %s" % (n, v["id"], label(v), v["confidence"], v["reason"]["signal"],
                                                    v["reason"]["locator"]) for n, v in enumerate(ordered, 1)]
    return "\n".join(lines) + "\n"


def status_line(counts):
    return "triage | members=%d %s | %s" % (sum(counts.values()), " ".join("%s=%d" % (k, counts[k]) for k in KINDS),
                                            STATUS_TAIL)


def alert_notes(sentences, texts):
    """Alert-only: names the place and the category or rule, never the text; a flag never halts the run."""
    items = [("sentence:%d" % n, s) for n, s in enumerate(sentences) if s.strip()]
    items += [("member:" + member_id, text) for member_id, text in texts]
    notes = ["review-required: %s: %s pattern" % (where, name) for where, text in items for name, rx in privacy.PATTERNS
             if rx.search(text)]
    flagged = [(where, len(privacy.legal_claims(text))) for where, text in items]
    return notes + ["review-required: %s: legal-claim wording flagged (%d rule)" % (where, n) for where, n in flagged if n]


def build(collection, name, members):
    rows, first, unreadable, texts = {}, {}, [], []
    for member in members:
        member_id = member["id"]
        try:
            data = read_text(collection, member)
        except Unreadable as error:
            unreadable.append(member_id)
            rows[member_id] = verdict(member_id, "unsorted", "low", "unreadable", "text=" + str(error), None)
            continue
        digest = hashlib.sha256(data).hexdigest() if data is not None else None
        if data is not None:
            texts.append((member_id, data.decode("utf-8")))
        if digest is not None and digest in first:
            rows[member_id] = verdict(member_id, "duplicate", "high", "byte-hash", "first=" + first[digest], digest)
        else:
            first.setdefault(digest, member_id)
            rows[member_id] = verdict(member_id, *classify(data), digest)
    ordered = sorted(rows.values(), key=queue_key)
    counts = {kind: sum(1 for v in rows.values() if v["kind"] == kind) for kind in KINDS}
    document = {"schema": SCHEMA, "name": name, "members": [rows[m["id"]] for m in members], "unreadable": unreadable,
                "counts": counts, "generated_at": datetime.now(timezone.utc).isoformat()}
    queue, status = render_queue(name, ordered), status_line(counts)
    sentences = [status] + [line for line in queue.splitlines() if line[:1].isdigit()]
    return document, queue, status, alert_notes(sentences, texts)


def write_outputs(out, document, queue, status, notes):
    dump = lambda value: json.dumps(value, sort_keys=True, indent=2) + "\n"
    out.mkdir(mode=0o700)
    for file_name, text in (("collection-triage-v1.json", dump(document)), ("QUEUE.md", queue),
                            ("triage-status.txt", status + "\n"), ("triage-notes.json", dump({"notes": notes}))):
        fd = os.open(out / file_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv in (["--help"], ["-h"]):
            print(__doc__)
            return 0
        if len(argv) < 2 or argv[0] not in ("run", "report"):
            raise Refused("usage_invalid")
        if argv[0] == "run":
            collection, out = checked_run_args(argv[1:])
            document, queue, status, notes = build(collection, *load_collection(collection))
            write_outputs(out, document, queue, status, notes)
        else:
            out = Path(argv[1])
            if len(argv) != 2 or not out.is_absolute() or Path(os.path.normpath(str(out))) != out \
                    or not (out / "collection-triage-v1.json").is_file():
                raise Refused("outdir_not_a_triage_dir")
            try:
                document = strict_json((out / "collection-triage-v1.json").read_bytes())
                status = status_line(document["counts"])
            except (ValueError, UnicodeError, KeyError, TypeError, RecursionError):
                raise Refused("outdir_not_a_triage_dir") from None
    except Refused as error:
        print(json.dumps({"schema": SCHEMA, "status": "refused", "error": str(error)}))
        return 2
    print(status)
    for member_id in document["unreadable"]:
        print("unreadable: " + member_id)
    return 1 if document["unreadable"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
