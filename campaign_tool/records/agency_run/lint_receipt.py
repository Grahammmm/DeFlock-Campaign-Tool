"""records-agency-lint-receipt-v1: ties one agency-lint run to the exact letter body, rules and inputs it ran on.

A local operator artifact (not a signature, not an approval). It holds hashes, fixed role names, fixed result codes and a
timestamp only: never a path, a file name, an option value other than the agency key's hash, or matched text. The same
option set (add_options) feeds `records agency-lint` and `records agency-draft-export`, so the two cannot drift.
Inputs must stay stable while the lints run; a change between the start and the end of the run refuses the receipt.
"""
import hashlib
import importlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

from .. import playbook

SCHEMA = "records-agency-lint-receipt-v1"
KEYS = {"schema", "body_sha256", "config_sha256", "inputs", "results", "created_at"}
ROLES = ("team_list", "allow", "agencies", "quote_ledger", "inventory", "ask_inventory", "asks", "events",
         "package", "pages", "originals", "playbook")
NOT_GIVEN = "not given"
MAX_RECEIPT, MAX_FILE, MAX_MEMBERS = 64 * 1024, 5 * 1024 * 1024, 5000
HEX, STAMP = re.compile(r"[0-9a-f]{64}"), re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
REASON, VERSION = re.compile(r"[a-z0-9_]{1,60}"), re.compile(r"[A-Za-z0-9._-]{1,32}")
BANNED = "banned phrases"


class Unverifiable(Exception):
    """An input, option set or receipt that cannot be read or checked; carries no detail on purpose."""


def add_options(parser, agency_dir_required):
    """The one lint input/option set shared by agency-lint and agency-draft-export."""
    parser.add_argument("--agency-dir", required=agency_dir_required)
    for name in ("package", "team-list", "allow", "agencies", "agency", "inventory", "original-root"):
        parser.add_argument("--" + name)
    parser.add_argument("--quote-ledger", help="quote-verify.tsv; with --inventory and --original-root adds quote rule Q3")


def _read(path, cap=MAX_FILE):
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Unverifiable
        with os.fdopen(fd, "rb") as handle:
            fd = None
            raw = handle.read(cap + 1)
    except OSError:
        raise Unverifiable from None
    finally:
        if fd is not None:
            os.close(fd)
    if len(raw) > cap:
        raise Unverifiable
    return raw


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _canon(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _members(pairs):
    return _sha(_canon(sorted(pairs)))


def input_hashes(opts):
    """{role: sha256 | 'not given'} read from the current files; Unverifiable if a supplied input cannot be read."""
    q3 = (opts.quote_ledger, opts.inventory, opts.original_root)
    if any(q3) and not all(q3):
        raise Unverifiable
    one = lambda path: _sha(_read(path)) if path else NOT_GIVEN  # noqa: E731
    out = {"team_list": one(opts.team_list), "allow": one(opts.allow), "agencies": one(opts.agencies),
           "quote_ledger": one(opts.quote_ledger), "inventory": one(opts.inventory), "originals": NOT_GIVEN,
           "package": NOT_GIVEN, "pages": NOT_GIVEN, "playbook": _sha(_read(playbook.DEFAULT_PATH))}
    package = opts.package
    if package:
        out["package"] = one(os.path.join(package, "package.json") if os.path.isdir(package) else package)
    agency = opts.agency_dir
    for role, name in (("ask_inventory", "inventory.tsv"), ("asks", "asks.tsv"), ("events", "events.tsv")):
        path = os.path.join(agency, name) if agency else None
        out[role] = one(path) if path and os.path.lexists(path) else NOT_GIVEN
    pages = Path(agency) / "pages" if agency else None
    if pages is not None and pages.is_dir():
        found = sorted(pages.glob("*_p[0-9][0-9][0-9]*.txt"))
        if len(found) > MAX_MEMBERS:
            raise Unverifiable
        if found:
            out["pages"] = _members((p.name, _sha(_read(p))) for p in found)
    if opts.original_root:
        from .lint_quotes import Refusal, load_inventory, resolve_original
        try:
            inventory = load_inventory(opts.inventory)
        except Refusal:
            raise Unverifiable from None
        if len(inventory) > MAX_MEMBERS:
            raise Unverifiable
        pairs = []
        for doc, row in inventory.items():
            try:
                pairs.append((doc, _sha(_read(resolve_original(opts.original_root, row), 1 << 30))))
            except (Refusal, Unverifiable):
                pairs.append((doc, "absent"))
        out["originals"] = _members(pairs)
    return out


def lint_names():
    from . import lint_all
    return [name for name, _, _ in lint_all._LINTS] + [BANNED]


def config_hash(opts):
    """sha256 of the canonical registered-lint list with each live RULES_VERSION, plus the non-file options."""
    from . import lint_all
    lints = []
    for name, module, _ in lint_all._LINTS:
        lints.append([name, getattr(importlib.import_module("." + module, lint_all.__package__), "RULES_VERSION", None)])
    lints.append([BANNED, getattr(playbook, "RULES_VERSION", None)])
    if not all(isinstance(v, str) and VERSION.fullmatch(v) for _, v in lints):
        raise Unverifiable
    return _sha(_canon({"lints": lints, "options": {"agency": opts.agency}}))


def snapshot(opts, body):
    return {"body_sha256": _sha(body), "config_sha256": config_hash(opts), "inputs": input_hashes(opts)}


def structured(status):
    """One lint's fixed-code result from its internal status string; operational errors raise Unverifiable."""
    if status.startswith("skipped: "):
        return {"status": "skipped", "reason": re.sub(r"[^a-z0-9]+", "_", status[9:].lower()).strip("_") or "skipped"}
    if status.startswith("incomplete"):
        return {"status": "skipped", "reason": "message_quotes_unchecked"}
    if status.startswith("FAIL"):
        match = re.match(r"FAIL, ([0-9]+) hits", status)
        return {"status": "fail", "hit_count": int(match.group(1))}
    if status.startswith(("pass", "note")):
        if "Q3 original-page ledger not run" in status:
            return {"status": "skipped", "reason": "q3_not_run"}
        return {"status": "pass"}
    raise Unverifiable


def write(path, snap, results):
    """Create a new private receipt at path (never overwrites); Unverifiable if it cannot."""
    doc = {"schema": SCHEMA, **snap, "results": results,
           "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(doc, sort_keys=True, indent=2) + "\n")
    except OSError:
        raise Unverifiable from None


def _no_dupes(items):
    if len({k for k, _ in items}) != len(items):
        raise ValueError
    return dict(items)


def load(path):
    """Strictly parsed receipt dict; Unverifiable on any unknown schema, lint, role or malformed value."""
    try:
        doc = json.loads(_read(path, MAX_RECEIPT).decode("utf-8"), object_pairs_hook=_no_dupes)
        names = lint_names()
        inputs, results = doc["inputs"], doc["results"]
        ok = (set(doc) == KEYS and doc["schema"] == SCHEMA and HEX.fullmatch(doc["body_sha256"])
              and HEX.fullmatch(doc["config_sha256"]) and isinstance(inputs, dict) and set(inputs) == set(ROLES)
              and all(v == NOT_GIVEN or (isinstance(v, str) and HEX.fullmatch(v)) for v in inputs.values())
              and isinstance(results, dict) and set(results) <= set(names)
              and isinstance(doc["created_at"], str) and STAMP.fullmatch(doc["created_at"]))
        datetime.strptime(doc["created_at"], "%Y-%m-%dT%H:%M:%SZ")
        for value in results.values():
            kind = value["status"]
            ok = ok and ((kind == "pass" and set(value) == {"status"})
                         or (kind == "fail" and set(value) == {"status", "hit_count"} and type(value["hit_count"]) is int
                             and 0 <= value["hit_count"] <= 10 ** 7)
                         or (kind == "skipped" and set(value) == {"status", "reason"} and isinstance(value["reason"], str)
                             and REASON.fullmatch(value["reason"])))
        if not ok:
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError, RecursionError):
        raise Unverifiable from None
    return doc


def stale_reason(receipt, snap):
    """First fixed STALE reason for this receipt against the current snapshot, or None when it still matches."""
    if receipt["body_sha256"] != snap["body_sha256"]:
        return "body_changed"
    if receipt["config_sha256"] != snap["config_sha256"]:
        return "config_changed"
    for role in sorted(ROLES):
        old, new = receipt["inputs"][role], snap["inputs"][role]
        if old != new:
            return ("input_added:" if old == NOT_GIVEN else "input_missing:" if new == NOT_GIVEN else "input_changed:") + role
    if set(receipt["results"]) != set(lint_names()):  # same configuration but a result is missing: cannot pass
        raise Unverifiable
    return None


def why_prefix(receipt=None):
    """The review-page line: 'not run' without a receipt, else the mechanical state at export, stated even when failing."""
    if receipt is None:
        return "agency lints: not run"
    rows = receipt["results"]
    failing = ["%s %d hits" % (n, r["hit_count"]) for n, r in rows.items() if r["status"] == "fail"]
    skipped = ["%s %s" % (n, r["reason"]) for n, r in rows.items() if r["status"] == "skipped"]
    state = "failed" if failing else "incomplete" if skipped else "passed"
    detail = ("; failing: " + ", ".join(failing) if failing else "") + ("; skipped: " + ", ".join(skipped) if skipped else "")
    return "agency lints: %s (at export; checked %s%s)" % (state, receipt["created_at"], detail)
