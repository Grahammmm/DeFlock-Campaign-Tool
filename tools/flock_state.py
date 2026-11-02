#!/usr/bin/env python3
"""Flock state (BF-df-status-fields): one explicit state per request and per email run, from recorded fields only.
  flock_state.py state STORE [--json]    STORE is a private folder outside the repository
Request states come from REQUESTS.csv cells (flock_requests.load decides what is an error); run states come from
flock_email's stage with flock_approve's signature check. Free text (receipt_evidence, a draft body, a `Sent by:` sentence)
never changes a state. Prints and reads only: no file, no network, no send, no approval. Fields only; not a legal status.
Exit 0 clean; 1 on any record-error, run-malformed, run-inconsistent, approval-unverified, sent-unverified, link-refused,
unmapped stage or store refusal (flock_email status's fail-closed convention; an edited-after-approval draft is run-malformed);
2 usage. The canonical ledger wins over this report. JSON schema flock-state-v1 is always review_required."""
import csv
import collections
import importlib.util
import json
import re
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
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
NOTE = "recorded fields only; not a legal status; the canonical ledger wins"
USAGE = "usage: flock_state.py state STORE [--json]  (one state per request and email run; prints only, writes nothing)"


class Refuse(Exception):
    pass


def _tool(name):  # siblings are loaded by file path, never copied
    spec = importlib.util.spec_from_file_location("_state_" + name, TOOLS / (name + ".py"))
    return spec.loader.exec_module(module := importlib.util.module_from_spec(spec)) or module


requests_tool = _tool("flock_requests")


def stage_fn():  # flock_requests' own _stage: flock_email.stage wrapped with flock_approve.verified
    return requests_tool._stage()


def label(text):  # an id or folder name is shown only when it is a plain label (no address, no 64-hex, no free text)
    return text if SAFE.fullmatch(text) else "-" if not text else "(unsafe-label)"


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
        if got is not None:
            out.append((run.name, STAGE_STATE.get(got, "run-malformed"), got in STAGE_STATE))
    return out


def collect(store):
    root = Path(store).resolve()
    if root == REPO or REPO in root.parents:
        raise Refuse("store is inside the repository")
    base = root / "agencies"
    if not root.is_dir() or base.is_symlink() or not base.is_dir():
        raise Refuse("store is not a folder with an agencies folder")
    folders, stage, reqs, runs = sorted(p for p in base.iterdir() if p.is_symlink() or p.is_dir()), stage_fn(), [], []
    for folder in folders:
        if not folder.is_symlink():
            reqs += [(label(folder.name),) + r for r in folder_requests(folder)]
        runs += [(label(folder.name),) + r for r in folder_runs(folder, stage)]
    return sorted(reqs, key=lambda r: r[:2]), sorted(runs, key=lambda r: r[:2])


def render(reqs, runs, as_json):
    states = collections.Counter([r[2] for r in reqs] + [r[2] for r in runs])
    totals = {s: states[s] for s in REQUEST_STATES + RUN_STATES}  # every state, zero included, in vocabulary order
    unmapped = [r for r in runs if not r[3]]
    errors = sum(totals[s] for s in FAILING) + len(unmapped)
    if as_json:
        print(json.dumps({"schema": "flock-state-v1", "review_required": True,
                          "requests": [{"agency": a, "request_id": i, "state": s, "basis": b} for a, i, s, b in reqs],
                          "runs": [{"agency": a, "run": d, "state": s} for a, d, s, _ in runs],
                          "totals": totals, "errors": errors}, ensure_ascii=True))
    else:
        lines = ["state | request | %s | %s | state=%s | basis=%s" % (a, i, s, ",".join(b)) for a, i, s, b in reqs]
        lines += ["state | run | %s | %s | state=%s | basis=email-stage" % (a, d, s) for a, d, s, _ in runs]
        lines += ["state | unmapped stage | %s | %s" % (a, d) for a, d, _, _ in unmapped]
        lines.append("state | requests=%d runs=%d %s | %s" % (len(reqs), len(runs), " ".join("%s=%d" % kv for kv in totals.items()), NOTE))
        print("\n".join(lines))
    return 1 if errors else 0


def main(argv):
    if argv in (["--help"], ["-h"]):
        return print(USAGE) or 0
    if not (len(argv) in (2, 3) and argv[0] == "state" and argv[2:] in ([], ["--json"])):
        return print(USAGE, file=sys.stderr) or 2
    try:
        reqs, runs = collect(argv[1])
    except (Refuse, OSError, UnicodeDecodeError, csv.Error) as exc:
        return print("state: " + (str(exc) if isinstance(exc, Refuse) else "store unreadable (%s)" % type(exc).__name__), file=sys.stderr) or 1
    return render(reqs, runs, argv[2:] == ["--json"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
