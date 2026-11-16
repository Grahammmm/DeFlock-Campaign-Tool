"""Private, review-required proposal of follow-up coverage tables from one run.

`records coverage-propose --project P --run-id R [--apply]` derives, from the same
run's exact received-item evidence, the rows `followup_route` would otherwise need
an operator to stage in `P/followup-coverage/AGENCY_KEY.tsv`, and proposes them as
`AGENCY_KEY.proposed.tsv`. It reuses `followup_route._mapped`, the router's own
mapper. Every `confirmed_by` stays empty, so a proposal renamed unedited is refused
by the router (`coverage_table_invalid`); a person must read the draft and evidence,
fill numbers or an explicit `none`, and confirm. Unmapped or overlapping scope is
marked `needs a person`, never guessed. Dry run (default) writes nothing; the
confirmed table is never opened for writing. Nothing is sent, approved or published.
Output is counts only.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
from pathlib import Path
import re
import stat
import sys

from . import approval_queue, asks_propose, followup_route, followup_state
from .outcomes import received_items, response_items

SUMMARY_SCHEMA = "records-coverage-proposal-summary-v1"
COLUMNS = ["queue_id", "ask_numbers", "confirmed_by", "note"]
MAX_TABLE = 1024 * 1024  # the consumer's own read limit
MAPPED = "machine mapping; review required"
UNMAPPED = "needs a person: unmapped scope"
OVERLAP = "needs a person: overlapping scope"
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
QUEUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


class ProposalError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _need(condition, code):
    if not condition:
        raise ProposalError(code)


# ---------------------------------------------------------------- pure derivation

def _numbers(q, asks, need):
    """-> sorted unique ask numbers from the router's own mapper, or None when unmapped."""
    try:
        mapped = followup_route._mapped(q, asks, need)
    except (AttributeError, TypeError, ValueError):
        raise ProposalError("COVERAGE_QUEUE_INVALID") from None
    if not mapped:
        return None
    _need(all(type(n) is int and n > 0 and n in need for n in mapped) and len(set(mapped)) == len(mapped),
          "COVERAGE_INPUT_INVALID")
    return sorted(mapped)


def derive(states, response, queue_rows):
    """Pure: followup_state.classify_run result, response-items report, queue rows -> {agency: [row dict]}."""
    asks_by_request = {}
    for request in response["requests"]:
        asks_by_request[(request.get("agency_key"), request.get("request_id"))] = request.get("asks") or []
    active = [q for q in queue_rows if q.get("status") in ("pending", "approved")]
    _need(len({q["id"] for q in active}) == len(active), "COVERAGE_QUEUE_INVALID")
    out = {}
    for state in states["requests"]:
        key = (state["agency_key"], state["request_id"])
        matching = [q for q in active if (q.get("agency_id"), q.get("request_id")) == key]
        needs = state.get("item_needs") or {}
        opened = sorted({n for _, k in followup_route.CANDIDATES for n in needs.get(k, [])})
        rule = followup_route._rule(state["state"], state.get("flags", []), {q.get("kind") for q in matching}, opened)
        candidates = sorted((q for q in matching if q.get("kind") in followup_route.COVERERS),
                            key=lambda q: q["id"])
        if rule != 8 or not candidates:
            continue
        mapped = {}
        for q in candidates:
            _need(isinstance(q["id"], str) and QUEUE_ID.fullmatch(q["id"]), "COVERAGE_QUEUE_INVALID")
            mapped[q["id"]] = _numbers(q, asks_by_request.get(key, []), needs.get(followup_route.COVERERS[q["kind"]], []))
        for q in candidates:
            numbers = mapped[q["id"]]
            others = set().union(*(set(v or ()) for k, v in mapped.items() if k != q["id"]))
            if numbers is None:
                row = ("", UNMAPPED)
            elif others & set(numbers):
                row = ("", OVERLAP)  # every participant is blank: no arbitrary winner
            else:
                row = (",".join(str(n) for n in numbers), MAPPED)
            out.setdefault(key[0], []).append({"queue_id": q["id"], "ask_numbers": row[0],
                                               "confirmed_by": "", "note": row[1]})
    return out


def encode(rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, COLUMNS, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


# ---------------------------------------------------------------- IO

def _read(path, code):
    try:
        return response_items._read_json(path)
    except FileNotFoundError:
        raise ProposalError("COVERAGE_INPUT_UNAVAILABLE") from None
    except (ValueError, OSError):
        raise ProposalError(code) from None


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
    _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "COVERAGE_TARGET_UNSAFE")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            current = stream.read(len(payload) + 1)
    except OSError:
        raise ProposalError("COVERAGE_TARGET_UNSAFE") from None
    _need(current == payload, "COVERAGE_PROPOSAL_EXISTS")
    return True


def write_proposals(directory, payloads):
    """Create each {name: bytes} no-clobber, 0600, never replacing; roll back this call's files on failure."""
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
        raise ProposalError("COVERAGE_WRITE_FAILED") from None
    finally:
        os.umask(old)
    return len(created)


def _load(project, run_id):
    try:
        project = received_items._safe_dir(Path(project))
        run_dir = received_items._safe_dir(project / "run-outputs" / run_id)
        outcomes = received_items._safe_dir(run_dir / "outcomes")
    except (ValueError, OSError):
        raise ProposalError("COVERAGE_PATH_INVALID") from None
    register = _read(run_dir / "CURRENT-REQUEST-REGISTER.json", "COVERAGE_INPUT_INVALID")
    received = _read(outcomes / "received-items.json", "COVERAGE_INPUT_INVALID")
    response = _read(outcomes / "response-items.json", "COVERAGE_INPUT_INVALID")
    try:  # whole-input validator and canonical-scope oracle; its proposed ledger is discarded
        asks_propose.derive(register, received, response, run_id)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ProposalError("COVERAGE_INPUT_INVALID") from None
    try:
        states = followup_state.classify_run(project, run_id)
    except FileNotFoundError:
        raise ProposalError("COVERAGE_INPUT_UNAVAILABLE") from None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ProposalError("COVERAGE_INPUT_INVALID") from None
    _need(register["as_of"] == states["as_of"], "COVERAGE_INPUT_INVALID")
    try:
        queue = approval_queue.load_queue(project)
    except (OSError, ValueError, KeyError, TypeError, approval_queue.ApprovalQueueError):
        raise ProposalError("COVERAGE_QUEUE_INVALID") from None
    return project, states, response, queue


def propose(project, run_id, *, apply=False):
    _need(isinstance(run_id, str) and RUN_ID.fullmatch(run_id), "COVERAGE_PATH_INVALID")
    project, states, response, queue = _load(project, run_id)
    proposals = derive(states, response, queue)
    payloads = {k + ".proposed.tsv": encode(rows) for k, rows in proposals.items()}
    _need(all(len(p) <= MAX_TABLE for p in payloads.values()), "COVERAGE_PROPOSAL_TOO_LARGE")
    directory = project / "followup-coverage"
    present, fresh = set(), dict(payloads)
    if payloads:
        info = _lstat(directory)
        _need(info is None or (stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)), "COVERAGE_TARGET_UNSAFE")
        present = {n for n, payload in payloads.items() if info is not None and _existing(directory / n, payload)}
        fresh = {n: p for n, p in payloads.items() if n not in present}
    written = write_proposals(directory, fresh) if apply and fresh else 0
    rows = [r for agency_rows in proposals.values() for r in agency_rows]
    mapped = sum(r["note"] == MAPPED for r in rows)
    return {"schema": SUMMARY_SCHEMA, "run_id": run_id, "dry_run": not apply, "agencies": len(proposals),
            "rows": len(rows), "mapped": mapped, "needs_person": len(rows) - mapped,
            "written": written, "present": len(present)}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records coverage-propose", description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        summary = propose(args.project, args.run_id, apply=args.apply)
    except ProposalError as error:
        print(error.code, file=sys.stderr)
        return 2
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
