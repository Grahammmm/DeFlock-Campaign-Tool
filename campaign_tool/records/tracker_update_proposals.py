"""Propose a tracker update from agency replies already received. Review-required; nothing is applied.

    records tracker-proposals --project P --run-id R --out DIR

Reads the run's CURRENT-REQUEST-REGISTER.json (DF2307-01 ``answer_arrived`` rows only), its
outcomes/received-items.json and the project's MASTER-REQUEST-TRACKER.csv. Writes a NEW private directory
holding MASTER-REQUEST-TRACKER.proposed.csv (the tracker with only empty cells filled) and proposals.json
(per cell: item id, kind, page, quote of at most 200 characters; plus every conflict, never applied).
Installing the proposal is an attended custodian step. The live tracker, ledger and stores are never written.

Threat model: OUT's parent directory must be owned by this user and not group/world-writable, so no other
user can swap names in it; otherwise nothing is published. After that parent is opened, staging, the
checker read and the final rename all go through directory fds, and success is reported only when the
final name is a real directory with the staging directory's (dev, inode); anything else is moved aside.
"""
import argparse
import csv
import io
import json
import os
import re
import stat
import sys
from pathlib import Path

from . import tracker_proposal
from .followup_clock import RUN_ID, SettingsError, _read_bounded, _strict, read_register
from .ledger.store import checked_path
from .outcomes.register import COLUMNS, MAX_CSV_BYTES, MAX_ROWS, PACIFIC, _day, _instant

SCHEMA = "records-tracker-update-proposals-v1"
RECEIVED_SCHEMA = "records-received-item-v1"
MAX_RECEIVED = 64 * 1024 * 1024
CSV_NAME, JSON_NAME = "MASTER-REQUEST-TRACKER.proposed.csv", "proposals.json"
# register field -> item kinds allowed to propose it (promised_at has its own rule)
FIELD_KINDS = {"determination_at": ("denial", "records"), "production_at": ("records",), "extension": ("extension",)}
READ_KINDS = ("response", "extension", "denial", "fee", "records")
AFFIRMATIVE = ("yes", "true", "14", "14 days", "extended")


class ProposalError(ValueError):
    pass


def _json(path, limit, code):
    try:
        return _strict(_read_bounded(checked_path(path, existing=False), limit))
    except (OSError, ValueError, SettingsError):
        raise ProposalError(code) from None


def _read_tracker(path):
    try:
        raw = _read_bounded(checked_path(path, existing=False), MAX_CSV_BYTES)
        reader = csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
        names = next(reader, None)
        rows = [r for r in reader if r]
    except (OSError, ValueError, csv.Error, UnicodeError):
        raise ProposalError("tracker_unreadable") from None
    folded = [n.strip().casefold() for n in names or []]
    if not names or len(set(folded)) != len(folded) or len(rows) > MAX_ROWS or any(len(r) != len(names) for r in rows):
        raise ProposalError("tracker_invalid")
    return names, folded, rows


def _column(names, folded, field, extend):
    hits = [i for i, n in enumerate(folded) if n in COLUMNS[field]]
    if len(hits) > 1:
        raise ProposalError("tracker_column_ambiguous")
    if hits:
        return hits[0]
    if not extend:
        return None
    names.append(COLUMNS[field][0])  # canonical alias, only when a fill needs it
    folded.append(COLUMNS[field][0])
    return len(names) - 1


def _pacific_day(stamp):
    moment = _instant(stamp) if isinstance(stamp, str) else None
    return moment.astimezone(PACIFIC).date() if moment else None


def _source(item, kind, page, quote):
    return {"item_id": item["item_id"], "kind": kind, "page": page, "quote": quote[:200]}


def _candidates(item, row, start, today):
    """-> [(field, value, source)], conflicts for one matched item. Dates only from the item's own record."""
    out, conflicts = [], []
    basis, kind = item.get("kind_basis") or {}, item["kind"]
    page, quote = basis.get("page"), basis.get("quote")
    cited = type(page) is int and page > 0 and isinstance(quote, str) and quote.strip()
    for field, kinds in FIELD_KINDS.items():
        if kind not in kinds or not cited:
            continue
        if field == "extension":
            out.append((field, "yes", _source(item, kind, page, quote)))
            continue
        day = _pacific_day(item.get("received_at"))
        if day is None:
            conflicts.append({"code": "date_unavailable", "column": field, "item_ids": [item["item_id"]]})
        elif start is None or day < start:
            conflicts.append({"code": "date_before_request_start", "column": field, "item_ids": [item["item_id"]]})
        elif day > today:
            conflicts.append({"code": "date_after_as_of", "column": field, "item_ids": [item["item_id"]]})
        else:
            out.append((field, day.isoformat(), _source(item, kind, page, quote)))
    stated = {}
    for entry in item.get("deadlines") or []:
        iso = entry.get("date") if isinstance(entry, dict) else None
        if iso is None or entry.get("year_inferred") or entry.get("confidence") != "rule_match" or _day(iso) is None:
            continue
        if type(entry.get("page")) is int and isinstance(entry.get("quote"), str) and entry["quote"].strip():
            stated.setdefault(iso, _source(item, kind, entry["page"], entry["quote"]))
    for iso, source in sorted(stated.items()):
        if start is None or _day(iso) < start:
            conflicts.append({"code": "date_before_request_start", "column": "promised_at", "item_ids": [item["item_id"]]})
        else:
            out.append(("promised_at", iso, source))
    return out, conflicts


def derive(register, received, names, folded, rows):
    """-> (filled rows, proposals, conflicts). Collects every candidate before editing any cell."""
    today = _day(register["as_of"])
    eligible = {}
    for row in register["requests"]:
        arrival = row.get("answer_arrived") if isinstance(row, dict) else None
        if isinstance(arrival, dict) and arrival.get("state") == "arrived_unrecorded" and row.get("ledger_request_id"):
            eligible.setdefault((row["agency_key"], row["ledger_request_id"]), []).append((row, set(arrival.get("occurrence_ids") or ())))
    conflicts, cells = [], {}
    for item in received["items"]:
        if not isinstance(item, dict) or item.get("kind") not in READ_KINDS or not isinstance(item.get("item_id"), str):
            continue
        oids = set(item.get("occurrence_ids") or ())
        hit = [k for k, bound in sorted(eligible.items()) if any(oids & ids for _, ids in bound)]
        key = (item.get("agency_id"), item.get("request_id"))
        if key in hit and len(eligible[key]) > 1:  # two rows share one binding: never pick one by input order
            conflicts.append({"code": "tracker_binding_ambiguous", "item_ids": [item["item_id"]]})
            continue
        if key in hit:
            row, _ = eligible[key][0]
            sources = [s for s in row.get("sources") or [] if isinstance(s, dict) and "row" in s]
            number = sources[0]["row"] if len(sources) == 1 else None
            record = rows[number - 2] if type(number) is int and 2 <= number < len(rows) + 2 else None
            id_col = _column(names, folded, "request_id", False)
            agency_col = _column(names, folded, "agency", False)
            ok = (record is not None and id_col is not None and record[id_col].strip() == row["request_id"]
                  and agency_col is not None and record[agency_col].strip().casefold() in
                  (str(row["agency"]).casefold(), str(row["agency_key"]).casefold()))
            if not ok:
                conflicts.append({"code": "tracker_row_unresolved", "request_id": row["request_id"], "item_ids": [item["item_id"]]})
                continue
            found, bad = _candidates(item, row, _day(row.get("request_date")), today)
            conflicts += [dict(c, request_id=row["request_id"], row=number) for c in bad]
            for field, value, source in found:
                cells.setdefault((number, field), []).append((value, source))
        elif item.get("request_id") in (None, ""):
            conflicts.append({"code": "reply_without_request_id", "item_ids": [item["item_id"]]})
        else:
            conflicts.append({"code": "reply_unmatched", "item_ids": [item["item_id"]]})
    out_rows, proposals = [list(r) for r in rows], []
    for (number, field), found in sorted(cells.items()):
        request_id = rows[number - 2][_column(names, folded, "request_id", False)].strip()
        values = sorted({v for v, _ in found})
        ids = sorted({s["item_id"] for _, s in found})
        index = _column(names, folded, field, False)
        current = rows[number - 2][index] if index is not None and index < len(rows[number - 2]) else ""
        if len(values) > 1:
            conflicts.append({"code": "replies_differ", "row": number, "request_id": request_id, "column": field, "item_ids": ids})
        elif current == values[0] or (current and (field == "extension" and current.strip().casefold() in AFFIRMATIVE
                                                 or field != "extension" and _day(current) == _day(values[0]))):
            continue  # already recorded: no change
        elif current:
            conflicts.append({"code": "filled_cell_differs", "row": number, "request_id": request_id, "column": field, "item_ids": ids})
        else:
            index = _column(names, folded, field, True)
            for record in out_rows:
                record.extend([""] * (len(names) - len(record)))
            out_rows[number - 2][index] = values[0]
            proposals.append({"row": number, "request_id": request_id, "column": names[index], "value": values[0],
                              "sources": [s for _, s in found]})
    return out_rows, proposals, sorted(conflicts, key=lambda c: json.dumps(c, sort_keys=True))


def build(project, run_id):
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ProposalError("run_id")
    try:
        register = read_register(project, run_id)
    except (OSError, ValueError):
        raise ProposalError("register_unreadable") from None
    if register is None:
        raise ProposalError("register_missing")
    received = _json(os.path.join(project, "run-outputs", run_id, "outcomes", "received-items.json"), MAX_RECEIVED, "received_items_unreadable")
    if (not isinstance(received, dict) or received.get("schema") != RECEIVED_SCHEMA or received.get("run_id") != run_id
            or not isinstance(received.get("items"), list)):
        raise ProposalError("received_items_invalid")
    names, folded, rows = _read_tracker(os.path.join(project, "MASTER-REQUEST-TRACKER.csv"))
    filled, proposals, conflicts = derive(register, received, names, folded, rows)
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows([names, *filled])
    doc = {"schema": SCHEMA, "run_id": run_id, "private": True, "review_required": True, "applied": False,
           "proposals": proposals, "conflicts": conflicts}
    return buffer.getvalue().encode("utf-8"), (json.dumps(doc, sort_keys=True, indent=2) + "\n").encode("utf-8"), doc


def _identity(st):
    return (st.st_dev, st.st_ino)


def _publish(out, project, files):
    """New 0700 directory beside OUT, outside the project; checked before the single rename into place."""
    out = Path(os.path.abspath(out))
    parent = out.parent
    if os.path.lexists(out):
        raise ProposalError("output_exists")
    try:
        real, root = Path(os.path.realpath(parent)), Path(os.path.realpath(project))
        if real != parent or not parent.is_dir() or real == root or root in real.parents:
            raise ProposalError("output_location_refused")
        name = ".tmp-proposals-%d-%s" % (os.getpid(), re.sub(r"\W", "_", out.name))
        pfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise ProposalError("output_parent_unsafe") from None
    sfd, ident, written, published = None, None, [], False

    def same():
        """The staging name in the parent still is the directory we made (fail closed on replacement)."""
        try:
            now = os.stat(name, dir_fd=pfd, follow_symlinks=False)
            held = os.fstat(sfd)
        except OSError:
            return False
        return _identity(now) == _identity(held) == ident and stat.S_ISDIR(now.st_mode)

    try:
        try:
            held = os.fstat(pfd)
            if _identity(held) != _identity(os.stat(parent)):
                raise ProposalError("output_parent_unsafe")
            if held.st_uid != os.geteuid() or held.st_mode & 0o022:
                raise ProposalError("output_parent_not_private")  # owner-only parent: nobody else can swap names
            os.mkdir(name, 0o700, dir_fd=pfd)
            sfd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=pfd)
            ident = _identity(os.fstat(sfd))
        except OSError:
            raise ProposalError("output_parent_unsafe") from None
        if not same():
            raise ProposalError("output_staging_replaced")
        for fname, data in files:
            fd = os.open(fname, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=sfd)
            written.append(fname)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        # The checker reads the CSV through an fd opened inside the held staging fd: no pathname is resolved.
        cfd = os.open(CSV_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=sfd)
        try:
            if not stat.S_ISREG(os.fstat(cfd).st_mode):
                raise ProposalError("output_staging_replaced")
            anchored = "/proc/self/fd/%d" % cfd
            if not os.path.exists(anchored):
                raise ProposalError("output_fd_paths_unavailable")
            _, problems = tracker_proposal.check_proposal(anchored)
        finally:
            os.close(cfd)
        if problems:
            raise ProposalError("checker_problems:%d" % len(problems))
        if not same():
            raise ProposalError("output_staging_replaced")
        try:
            os.stat(out.name, dir_fd=pfd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ProposalError("output_exists")
        os.rename(name, out.name, src_dir_fd=pfd, dst_dir_fd=pfd)
        published = True
        try:
            final = os.stat(out.name, dir_fd=pfd, follow_symlinks=False)
            verified = stat.S_ISDIR(final.st_mode) and _identity(final) == ident == _identity(os.fstat(sfd))
        except OSError:
            verified = False
        if not verified:  # never report success for an object we did not make; move it aside, delete nothing
            aside = ".unverified-proposals-%d-%s" % (os.getpid(), re.sub(r"\W", "_", out.name))
            try:
                os.stat(aside, dir_fd=pfd, follow_symlinks=False)
            except FileNotFoundError:
                try:
                    os.rename(out.name, aside, src_dir_fd=pfd, dst_dir_fd=pfd)
                except OSError:
                    pass
                else:
                    raise ProposalError("output_unverified_quarantined") from None
            except OSError:
                pass
            raise ProposalError("output_unverified_left_in_place")
    except BaseException as exc:
        if sfd is not None and not published and same():
            for fname in written:
                try:
                    os.unlink(fname, dir_fd=sfd)
                except OSError:
                    pass
            try:
                os.rmdir(name, dir_fd=pfd)
            except OSError:
                pass
        if isinstance(exc, OSError):
            raise ProposalError("output_write_failed") from None
        raise
    finally:
        if sfd is not None:
            os.close(sfd)
        os.close(pfd)


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="records tracker-proposals", description=__doc__.splitlines()[0])
    for name in ("project", "run-id", "out"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    try:
        csv_bytes, json_bytes, doc = build(args.project, args.run_id)
        _publish(args.out, args.project, ((CSV_NAME, csv_bytes), (JSON_NAME, json_bytes)))
    except ProposalError as exc:
        print("tracker_proposals_" + str(exc), file=sys.stderr)
        return 1
    print("proposals=%d conflicts=%d | private, review-required; nothing applied" % (len(doc["proposals"]), len(doc["conflicts"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
