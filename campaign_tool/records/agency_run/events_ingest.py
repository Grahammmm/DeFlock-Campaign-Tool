"""Turn mail-store receipts into Pacific-dated agency events (REC-TL-1a); read-only, sends nothing."""
import argparse
import csv
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import re
import stat
import sys
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")
COLUMNS = ("event_id", "date", "time_local", "agency", "request_ref", "event_type", "what_happened", "source",
           "who", "basis", "clock_note")
ORDER = {"request_sent": 0, "follow_up_sent": 1, "clock_start": 2, "clock_due": 3}
MANUAL_TYPE = {"request_sent": "sent", "follow_up_sent": "our follow-up sent"}
WHAT = {"request_sent": "Message sent from the DeFlock SLO mailbox to the agency",
        "follow_up_sent": "Follow-up message sent from the DeFlock SLO mailbox to the agency"}
CLOCK_NOTE = ("Gov. Code 7922.535 determination within 10 days of receipt (VERIFIED-IN-PACKAGE, "
              "needs_attorney_review); after-hours send dated next day; weekends and holidays not rolled; "
              "one extension of at most 14 days not applied")
REF = re.compile(r"[0-9]{2}-[0-9]{2,5}")
MAX_ALIASES, MAX_RECEIPT = 64 * 1024, 1024 * 1024


class Refusal(Exception):
    pass


def _pairs(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _clean(value):
    return re.sub(r"[\t\r\n<>]", " ", str(value)).strip()


def load_aliases(path):
    try:
        if os.path.getsize(path) > MAX_ALIASES:
            raise ValueError("size")
        data = json.loads(Path(path).read_text("utf-8"), object_pairs_hook=_pairs)
        aliases = data["aliases"]
        ok = (isinstance(data["agency"], str) and data["agency"].strip() and isinstance(data["agency_id"], str)
              and re.fullmatch(r"[a-z0-9][a-z0-9-]*", data["agency_id"])
              and isinstance(data["event_prefix"], str) and re.fullmatch(r"[A-Z]{2,6}", data["event_prefix"])
              and isinstance(aliases, list) and aliases
              and all(isinstance(a, str) and 3 <= len(a) <= 80 and len(_norm(a)) >= 3 for a in aliases)
              and isinstance(data["requests"], dict)
              and all(REF.fullmatch(k) and isinstance(v, str) and v for k, v in data["requests"].items()))
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        ok = False
    if not ok:
        raise Refusal("aliases_invalid")
    return data, [_norm(a) for a in aliases]


def _headers(receipt, name):
    value = (receipt.get("headers") or {}).get(name, [])
    value = value if isinstance(value, list) else [value]
    return [v for v in value if isinstance(v, str)]


def _when(receipt, indexed):
    """(utc datetime, 'recorded'|'proxy') or None."""
    stamp = receipt.get("internaldate") if isinstance(receipt.get("internaldate"), str) else indexed
    for text, basis in ((stamp, "recorded"), ((_headers(receipt, "Date") or [None])[0], "proxy")):
        try:
            parsed = datetime.strptime(text, "%d-%b-%Y %H:%M:%S %z") if basis == "recorded" \
                else parsedate_to_datetime(text)
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc), basis
        except (TypeError, ValueError):
            continue
    return None


def read_store(root):
    root = Path(root)
    try:
        status = json.loads((root / "SYNC-STATUS.json").read_text("utf-8"))
        healthy = isinstance(status, dict) and status.get("failures") == []
    except (OSError, ValueError):
        healthy = False
    if not healthy:
        raise Refusal("sync_status_unhealthy")
    index = {}
    try:
        with open(root / "MESSAGE-INDEX.csv", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                index[(row.get("folder"), row.get("uid"))] = row.get("internaldate")
    except (OSError, UnicodeError, csv.Error):
        pass
    receipts, bad = [], []
    for path in sorted((root / "receipts").glob("*.json")):
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECEIPT:
                raise ValueError("unsafe")
            data = json.loads(path.read_bytes())
            if not isinstance(data, dict) or not isinstance(data.get("headers", {}), dict):
                raise ValueError("shape")
            if not all(isinstance(data.get(k, ""), (str, int)) for k in ("folder", "uid")):
                raise ValueError("shape")
            receipts.append(data)
        except (OSError, ValueError):
            bad.append(("receipts/" + json.dumps(path.name, ensure_ascii=True), "", "", "bad_receipt"))
    return receipts, index, bad


def _fmt_time(local):
    return local.strftime("%H:%M ") + local.tzname()


def build(receipts, index, data, keys):
    refs = data["requests"]
    sent, unclassified, unmapped = [], [], set()
    for r in receipts:
        folder, uid = _clean(r.get("folder", "")), _clean(r.get("uid", ""))
        when = _when(r, index.get((r.get("folder"), str(r.get("uid")))))
        day = when[0].astimezone(PACIFIC).date().isoformat() if when else ""
        text = [_norm(v) for h in ("From", "To", "Cc", "Subject") for v in _headers(r, h)]
        match = REF.search(" ".join(_headers(r, "Subject")))
        found = next((m for m in REF.findall(" ".join(_headers(r, "Subject"))) if m in refs), None)
        reason = ("no_agency_match" if not any(k in t for k in keys for t in text)
                  else "no_request_ref" if not match
                  else "request_unmapped" if not found
                  else "no_time" if not when
                  else "inbound_needs_review" if not folder.lower().startswith("sent") else None)
        if reason == "request_unmapped":
            unmapped.add(match.group(0))
        if reason:
            unclassified.append((folder, uid, day, reason))
        else:
            mid = (_headers(r, "Message-ID") or [""])[0]
            sent.append((when[0], found, f"{folder}:{uid} Message-ID {_clean(mid)}", when[1]))
    rows, seen = [], set()
    for utc, ref, source, basis in sorted(sent):
        local = utc.astimezone(PACIFIC)
        kind = "follow_up_sent" if ref in seen else "request_sent"
        note = f"receipt basis: {basis}"
        rows.append((local.date().isoformat(), local.strftime("%H:%M"), _fmt_time(local), ref, kind, source, "DATED", note))
        if kind == "request_sent":
            start = local.date() + timedelta(days=1 if local.hour >= 17 else 0)
            for name, day in (("clock_start", start), ("clock_due", start + timedelta(days=10))):
                rows.append((day.isoformat(), "24:00", "", ref, name, "computed", "INFERRED", CLOCK_NOTE))
        seen.add(ref)
    rows.sort(key=lambda x: (x[0], x[1], ORDER[x[4]]))
    return rows, sorted(unclassified), sorted(unmapped)


def _write(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def write_outputs(out, rows, unclassified, data):
    tsv = ["\t".join(COLUMNS)]
    manual = []
    for n, (day, _, local, ref, kind, source, basis, note) in enumerate(rows, 1):
        eid = f"{data['event_prefix']}-{n:03d}"
        who = "DeFlock SLO" if kind in WHAT else "computed"
        what = WHAT.get(kind, "Section 7922.535 clock " + ("start" if kind == "clock_start" else "due date"))
        tsv.append("\t".join((eid, day, local, _clean(data["agency"]), ref, kind, what, source, who, basis, note)))
        if kind in MANUAL_TYPE:
            manual.append(json.dumps({"event_id": eid, "agency_id": data["agency_id"], "request_id": data["requests"][ref],
                                      "type": MANUAL_TYPE[kind], "date": day, "actor": "DeFlock SLO", "summary": what},
                                     sort_keys=True, ensure_ascii=False))
    _write(out / "events.tsv", "\n".join(tsv) + "\n")
    _write(out / "manual-events.jsonl", "".join(m + "\n" for m in manual))
    _write(out / "unclassified.tsv", "".join("\t".join(r) + "\n" for r in [("folder", "uid", "date_pacific", "reason")] + unclassified))
    return len(manual)


def run(receipts_dir, aliases_file, out_dir):
    data, keys = load_aliases(aliases_file)
    out, root = Path(out_dir).resolve(), Path(receipts_dir).resolve()
    if out == root or root in out.parents:
        raise Refusal("out_inside_receipts")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise Refusal("out_not_empty")
    receipts, index, bad = read_store(root)
    rows, unclassified, unmapped = build(receipts, index, data, keys)
    unclassified = sorted(unclassified + bad)
    out.mkdir(parents=True, exist_ok=True)
    manual = write_outputs(out, rows, unclassified, data)
    return {"ok": True, "events": len(rows), "manual_events": manual, "unclassified": len(unclassified),
            "bad_receipts": len(bad), "unmapped_refs": unmapped}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records agency-events", description=__doc__)
    for name in ("receipts", "agency-aliases", "out"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    try:
        report = run(args.receipts, args.agency_aliases, args.out)
    except Refusal as err:
        print(f"agency-events: {err}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
