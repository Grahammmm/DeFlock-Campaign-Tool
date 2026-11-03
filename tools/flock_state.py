#!/usr/bin/env python3
"""Flock state (BF-df-status-fields): one explicit state per request and per email run, from recorded fields only.
  flock_state.py state STORE [--json] [--agency SLUG]...    STORE is a private folder outside the repository
--agency (1-20 distinct plain slugs) reads only those agency folders, so another agency's broken file cannot hide them; an
unknown slug is refused before any folder is read (exit 1), a malformed or repeated one is usage (exit 2).
  flock_state.py diff OLD.json NEW.json [--json]    two saved `state --json` snapshots: what is new, gone or moved
Request states come from REQUESTS.csv cells (flock_requests.load decides what is an error); run states come from flock_email's
stage with flock_approve's signature check. Free text (evidence, a draft body, a `Sent by:` sentence) never changes a state.
Prints and reads only. Fields only; not a legal status; the canonical ledger wins. JSON schema flock-state-v1 is review_required.
Exit 0 clean; 1 on any record-error, run-malformed, run-inconsistent, approval-unverified (also an approved draft edited
afterwards), sent-unverified, link-refused, unmapped stage or store refusal (flock_email status's fail-closed convention); 2 usage.
diff checks both snapshots against the v1 shape and vocabularies (the first failure names its side and field path, never a
value) and prints one line per request or run that is new, gone or changed state, then a totals delta. Rows masked as
(unsafe-label) or `row N`, and requests with no id (-), cannot be matched and are only counted (unmatched=K). A basis change
with the same state is not a change. JSON schema flock-state-diff-v1 is review_required. Exit 0 nothing changed; 3 something
changed (a change line or a totals delta); 1 unusable snapshot."""
import csv
import collections
import importlib.util
import json
import os
import re
import stat
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
REQUEST_STATES = ("open-receipt-unknown", "open-receipt-recorded", "open-extension-noticed", "determined", "produced",
                  "closed", "withdrawn", "record-error")
RUN_STATES = ("follow-up-drafted", "email-drafted", "approved-not-sent", "sent-recorded", "run-malformed",
              "run-inconsistent", "approval-unverified", "sent-unverified", "link-refused")
STAGE_STATE = {"follow-up drafted": "follow-up-drafted", "email drafted": "email-drafted",
               "approval recorded": "approved-not-sent", "sent recorded": "sent-recorded", "malformed": "run-malformed",
               "inconsistent: sent without approval": "run-inconsistent", "approval unverified": "approval-unverified",
               "sent unverified": "sent-unverified", "refused: link": "link-refused"}
FAILING = {"record-error", "run-malformed", "run-inconsistent", "approval-unverified", "sent-unverified", "link-refused"}
SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
BANNED = re.compile(r"late|overdue|violat|unlawful|illegal|non-complian|breach", re.I)  # no output line may carry these
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
NOTE = "recorded fields only; not a legal status; the canonical ledger wins"
USAGE = ("usage: flock_state.py state STORE [--json] [--agency SLUG]... | diff OLD.json NEW.json [--json]  (one state per request and email run; "
         "prints only, writes nothing)")
SNAP_MAX = 5 * 1024 * 1024
SNAP_KEYS = {"schema", "review_required", "requests", "runs", "totals", "errors"}
SNAP_ROWS = (("requests", "request", "request_id", REQUEST_STATES, {"agency", "request_id", "state", "basis"}),
             ("runs", "run", "run", RUN_STATES, {"agency", "run", "state"}))
ROW_N = re.compile(r"row [0-9]+")


class Refuse(Exception): pass


def _tool(name):  # siblings are loaded by file path, never copied
    spec = importlib.util.spec_from_file_location("_state_" + name, TOOLS / (name + ".py"))
    return spec.loader.exec_module(module := importlib.util.module_from_spec(spec)) or module


requests_tool = _tool("flock_requests")


def stage_fn(): return requests_tool._stage()  # flock_requests' own _stage: flock_email.stage wrapped with flock_approve.verified


def label(text):  # an id or folder name is shown only when it is a plain label (no address, no 64-hex, no free text, no banned word)
    return text if SAFE.fullmatch(text) and not BANNED.search(text) else "-" if not text else "(unsafe-label)"


def request_state(r):
    if r["status"] != "open":
        return r["status"], ["status"]
    if r["extension_notice_date"]:
        return "open-extension-noticed", ["status", "extension_notice_date"]
    if r["receipt_evidence"]:
        return "open-receipt-recorded", ["status", "receipt_evidence"]
    return "open-receipt-unknown", ["status", "receipt_evidence"]


def folder_requests(folder):
    """[(request_id label, state, basis)] for one agency folder; a row with any load() error is record-error."""
    f, pre = folder / "REQUESTS.csv", re.escape(folder.name) + r": row ([0-9]+): (\S+)"
    if f.is_symlink():
        raise Refuse("REQUESTS.csv must not be a link (%s)" % label(folder.name))
    if not f.exists():
        return []
    rows, errs = requests_tool.load(f, folder.name)
    bad = collections.defaultdict(set)
    for e in errs:
        m = re.match(pre, e)
        if not m or m[1] == "1":  # an unreadable file or a header that is not v1 has no rows to state
            raise Refuse("REQUESTS.csv unreadable or not v1 (%s)" % label(folder.name))
        bad[int(m[1])].add(m[2])
    with open(f, encoding="utf-8-sig", newline="") as fh:
        table = list(csv.reader(fh))
    nums = [n for n, cells in enumerate(table[1:], 2) if cells and len(cells) == len(requests_tool.HEADER)]
    if len(nums) != len(rows):
        raise Refuse("REQUESTS.csv rows unclear (%s)" % label(folder.name))
    seen = collections.Counter((requests_tool.slug(r["agency"]), r["request_id"]) for r in rows)
    out = []
    for n, r in zip(nums, rows):
        fields = set(bad.pop(n, ())) | ({"request_id"} if r["request_id"] and seen[(requests_tool.slug(r["agency"]), r["request_id"])] > 1 else set())
        state, basis = ("record-error", sorted(fields)) if fields else request_state(r)
        out.append((label(r["request_id"]), state, basis))
    return out + [("row %d" % n, "record-error", sorted(fields)) for n, fields in sorted(bad.items())]  # rows with no id


def stale_approval(run):  # a draft stage() calls malformed that is itself well formed and holds a signed approval record
    email = requests_tool._tool("flock_email")
    try:
        lines, rec = (run / "EMAIL-DRAFT.md").read_text(encoding="utf-8").splitlines(), json.loads((run / email.APPROVAL).read_text(encoding="utf-8"))
        email.approved_text(run)  # ValueError unless the draft ends with its one Sent by: line
        return lines[:1] == [email.HEAD] and len(email.marks(lines)) == 1 and isinstance(rec, dict) and set(rec) == email.KEYS \
            and rec["state"] == "approved" and "BEGIN SSH SIGNATURE" in str(rec["signature"])
    except (OSError, ValueError):
        return False


def folder_runs(folder, stage):
    """[(run date label, state, mapped)] for one agency folder; links are named and never entered."""
    runs = folder / "runs"
    if folder.is_symlink():
        return [("-", "link-refused", True)]
    if runs.is_symlink():
        return [("runs", "link-refused", True)]
    if not runs.is_dir():
        return []
    out = []
    for run in sorted(p for p in runs.iterdir() if DATE.fullmatch(p.name) and (p.is_symlink() or p.is_dir())):
        try:
            got = stage(run)
        except OSError:
            got = "malformed"
        got = "approval unverified" if got == "malformed" and stale_approval(run) else got  # edited after approval
        if got is not None:
            out.append((run.name, STAGE_STATE.get(got, "run-malformed"), got in STAGE_STATE))
    return out


def collect(store, only=None):
    root = Path(store).resolve()
    if root == REPO or REPO in root.parents:
        raise Refuse("store is inside the repository")
    base = root / "agencies"
    if not root.is_dir() or base.is_symlink() or not base.is_dir():
        raise Refuse("store is not a folder with an agencies folder")
    for s in only or ():  # every named agency must exist before any agency folder is read
        if not ((base / s).is_symlink() or (base / s).is_dir()):
            raise Refuse("unknown agency (%s)" % s)
    folders = sorted(base / s for s in only) if only else sorted(p for p in base.iterdir() if p.is_symlink() or p.is_dir())
    stage, reqs, runs = stage_fn(), [], []
    for folder in folders:
        if not folder.is_symlink():
            reqs += [(label(folder.name),) + r for r in folder_requests(folder)]
        runs += [(label(folder.name),) + r for r in folder_runs(folder, stage)]
    return sorted(reqs, key=lambda r: r[:2]), sorted(runs, key=lambda r: r[:2])


def render(reqs, runs, as_json, only=None):
    states = collections.Counter([r[2] for r in reqs] + [r[2] for r in runs])
    totals = {s: states[s] for s in REQUEST_STATES + RUN_STATES}  # every state, zero included, in vocabulary order
    unmapped = [r for r in runs if not r[3]]
    errors = sum(totals[s] for s in FAILING) + len(unmapped)
    if as_json:
        print(json.dumps({"schema": "flock-state-v1", "review_required": True,
                          "requests": [{"agency": a, "request_id": i, "state": s, "basis": b} for a, i, s, b in reqs],
                          "runs": [{"agency": a, "run": d, "state": s} for a, d, s, _ in runs],
                          "totals": totals, "errors": errors, **({"agency_filter": sorted(only)} if only else {})}, ensure_ascii=True))
    else:
        lines = ["state | request | %s | %s | state=%s | basis=%s" % (a, i, s, ",".join(b)) for a, i, s, b in reqs]
        lines += ["state | run | %s | %s | state=%s | basis=email-stage" % (a, d, s) for a, d, s, _ in runs]
        lines += ["state | unmapped stage | %s | %s" % (a, d) for a, d, _, _ in unmapped]
        lines.append("state | requests=%d runs=%d%s %s | %s" % (len(reqs), len(runs), " agencies=%d" % len(only) if only else "", " ".join("%s=%d" % kv for kv in totals.items()), NOTE))
        print("\n".join(lines))
    return 1 if errors else 0


def load_snapshot(path, side):
    """[(kind, agency, id, state, matchable)] from one saved `state --json` document; refusals name a field path, never a value."""
    try:
        if Path(path).is_symlink():
            raise Refuse("snapshot must not be a link (%s)" % side)
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise Refuse("snapshot is not a file (%s)" % side)
            raw = fh.read(SNAP_MAX + 1)
        if len(raw) > SNAP_MAX:
            raise Refuse("snapshot is over 5 MiB (%s)" % side)
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        raise Refuse("snapshot unreadable (%s)" % side) from None
    bad = lambda where, what: Refuse("%s %s (%s)" % (where, what, side))
    if not isinstance(data, dict) or set(data) != SNAP_KEYS:
        raise bad("snapshot", "keys are not flock-state-v1")
    if data["schema"] != "flock-state-v1":
        raise bad("schema", "is not flock-state-v1")
    if data["review_required"] is not True:
        raise bad("review_required", "is not true")
    out, seen = [], set()
    for field, kind, key, vocab, keys in SNAP_ROWS:
        if not isinstance(data[field], list):
            raise bad(field, "is not a list")
        for n, r in enumerate(data[field]):
            at = "%s[%d]" % (field, n)
            if not isinstance(r, dict) or set(r) != keys:
                raise bad(at, "keys are not v1")
            if not isinstance(r["state"], str) or r["state"] not in vocab:
                raise bad(at + ".state", "is not in the vocabulary")
            if kind == "request" and not (isinstance(r["basis"], list) and all(isinstance(b, str) for b in r["basis"])):
                raise bad(at + ".basis", "is not a list of field names")
            for k in ("agency", key):  # exactly what label() prints, or one of the masks state itself writes
                if not (isinstance(r[k], str) and (label(r[k]) == r[k] or r[k] == "-" or ROW_N.fullmatch(r[k]))):
                    raise bad(at + "." + k, "is not a plain label")
            ident = (kind, r["agency"], r[key])
            matchable = "(unsafe-label)" not in ident and not ROW_N.fullmatch(r[key]) and ident[::2] != ("request", "-")  # no id
            if matchable and ident in seen:
                raise bad(at, "repeats an (agency, %s) pair" % key)
            seen.add(ident)
            out.append(ident + (r["state"], matchable))
    return out


def diff(old, new):
    """(changes [(kind, agency, id, old state|None, new state|None)], totals delta {state: (old, new)}, unmatched count)."""
    a, b = ({r[:3]: r[3] for r in rows if r[4]} for rows in (old, new))
    changes = [k + (a.get(k), b.get(k)) for k in sorted(a.keys() | b.keys()) if a.get(k) != b.get(k)]
    count = [collections.Counter(r[3] for r in rows) for rows in (old, new)]
    delta = {s: (count[0][s], count[1][s]) for s in REQUEST_STATES + RUN_STATES if count[0][s] != count[1][s]}
    return changes, delta, sum(not r[4] for r in old + new)


def render_diff(old, new, as_json):
    changes, delta, unmatched = diff(old, new)
    if as_json:
        print(json.dumps({"schema": "flock-state-diff-v1", "review_required": True,
                          "changes": [{"kind": k, "agency": a, "id": i, "old": o, "new": n} for k, a, i, o, n in changes],
                          "totals_delta": {s: {"old": o, "new": n} for s, (o, n) in delta.items()}, "unmatched": unmatched},
                         ensure_ascii=True))
    else:
        what = lambda o, n: "new: " + n if o is None else "gone (was %s)" % o if n is None else "%s -> %s" % (o, n)
        lines = ["diff | %s | %s | %s | %s" % (k, a, i, what(o, n)) for k, a, i, o, n in changes]
        moved = (sum(o is not None and n is not None for *_, o, n in changes), sum(o is None for *_, o, n in changes),
                 sum(n is None for *_, o, n in changes), unmatched)
        lines.append("diff | changed=%d new=%d gone=%d unmatched=%d | totals: %s | recorded fields only; not a legal status" % (
            moved + (" ".join("%s=%d->%d" % (s, o, n) for s, (o, n) in delta.items()) or "none",)))
        print("\n".join(lines))
    return 3 if changes or delta else 0


def main(argv):
    if argv in (["--help"], ["-h"]):
        return print(USAGE) or 0
    if argv[:1] == ["diff"]:
        if not (len(argv) in (3, 4) and argv[3:] in ([], ["--json"]) and not any(p.startswith("-") for p in argv[1:3])):
            return print(USAGE, file=sys.stderr) or 2
        try:
            return render_diff(load_snapshot(argv[1], "OLD"), load_snapshot(argv[2], "NEW"), argv[3:] == ["--json"])
        except Refuse as exc:
            return print("diff: " + str(exc), file=sys.stderr) or 1
    if not (len(argv) >= 2 and argv[0] == "state" and (opts := state_opts(argv[2:]))):
        return print(USAGE, file=sys.stderr) or 2
    try:
        reqs, runs = collect(argv[1], opts[1])
    except (Refuse, OSError, UnicodeDecodeError, csv.Error) as exc:
        return print("state: " + (str(exc) if isinstance(exc, Refuse) else "store unreadable (%s)" % type(exc).__name__), file=sys.stderr) or 1
    return render(reqs, runs, *opts)


def state_opts(rest):  # (as_json, [slug]): at most one --json and 1-20 distinct plain --agency slugs in any order; None is usage
    as_json, only, words = False, [], iter(rest)
    for word in words:
        s = next(words, None) if word == "--agency" else None
        if word == "--json" and not as_json:
            as_json = True
        elif s is None or s in only or len(only) == 20 or not SAFE.fullmatch(s) or BANNED.search(s):
            return None
        else:
            only.append(s)
    return as_json, only


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
