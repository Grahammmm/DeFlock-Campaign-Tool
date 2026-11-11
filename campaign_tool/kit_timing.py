"""kit-timing: private, read-only response-time report from kit/sent.json (recorded history only).

Every number is a whole count of calendar days between two recorded dates. Waits (extensions, clarification requests)
are listed as their own spans and never subtracted from another number. Every recorded request history is replayed
through the shared kit-response rules first; one the replay rejects refuses the whole report. Nothing is sent,
fetched or repaired here."""
import os
import statistics
from datetime import date
from pathlib import Path

from . import kit, kit_export, kit_track

NOTE = "calendar days; provisional; not a legal deadline"
PROJECTION = ("This is an as-of projection of the history recorded today, not what was known on that date. "
              "Waits are shown as their own spans and are never subtracted from any other number.")
SCALARS = ("first_step_days", "determination_days", "first_production_days", "final_production_days", "open_age_days")
WAITS = ("extension_days", "clarification_days")
HEADER = ("row_type", "agency_id", "request", "name", "metric", "start_on", "end_on", "days", "open", "count",
          "median", "note")
ORGANIZER = ("clarified", "fee_paid", "withdrawn")  # the organizer's own actions: never the agency's first response
UNREADABLE = "kit/sent.json unreadable: "
MALFORMED = UNREADABLE + "malformed timing steps"


def _today(today):
    return today or date.today()


def _as_of(as_of, today):
    if as_of is None:
        return today
    day = as_of if isinstance(as_of, date) else kit_track._day(as_of)
    if day is None:
        raise ValueError("--as-of must be a real YYYY-MM-DD date")
    if day > today:
        raise ValueError("--as-of must not be in the future")
    return day


def _names(root):
    """Names from a regular kit/agencies.json, or {} when absent (show IDs).

    Exact agency IDs must be unique; repetitions receive a fixed, value-free refusal.
    """
    kit_dir = Path(root) / "kit"
    if kit_dir.is_symlink():
        raise ValueError("kit/ must not be a symlink")
    path = kit_dir / "agencies.json"
    if not os.path.lexists(path):
        return {}
    if path.is_symlink() or not path.is_file():
        raise ValueError("kit/agencies.json unreadable: not a regular file")
    try:
        agencies = kit._load_existing(path)
    except (OSError, ValueError, RecursionError):  # JSON/encoding errors; unprocessable nesting is unreadable, not absent
        raise ValueError("kit/agencies.json unreadable: not a JSON list of agencies with names") from None
    names = {}
    for entry in agencies:
        if entry["agency_id"] in names:
            raise ValueError("kit/agencies.json unreadable: duplicate agency_id") from None
        names[entry["agency_id"]] = entry["name"]
    return names


def _parse(entry, today):
    """[(kind, date, partial)] of one entry's steps. The whole recorded history is first replayed through the shared
    kit-response rules (``kit_track.validate_steps``, as of the effective day ``today``, never the as-of cutoff);
    ValueError, always the same generic message, for any history that replay rejects."""
    if date.fromisoformat(entry["sent_on"]) > today:
        raise ValueError(UNREADABLE + "a request was sent after today")
    try:
        kit_track.validate_steps(entry, "recorded request", today=today)
    except ValueError:  # the diagnostic can name private values: report only that the history is malformed
        raise ValueError(MALFORMED) from None
    return [(step["kind"], kit_track._day(step["on"]), step.get("partial") is True) for step in entry["steps"]]


def _span(start, end, cutoff):
    return {"start_on": start.isoformat(), "end_on": (end or cutoff).isoformat(),
            "days": ((end or cutoff) - start).days, "open": end is None}


def _row(agency_id, request, name, entry, steps, cutoff):
    sent = date.fromisoformat(entry["sent_on"])
    days = lambda found: None if found is None else (found - sent).days  # noqa: E731
    first = lambda test: next((on for kind, on, partial in steps if test(kind, partial)), None)  # noqa: E731
    final = first(lambda kind, partial: kind == "produced" and not partial)
    ends = ("determination", "produced", "denied", "closed", "withdrawn")
    stopped = any(kind in ("denied", "closed", "withdrawn") for kind, _, _ in steps)
    extensions = [_span(on, next((o for k, o, _ in steps[i + 1:] if k in ends), None), cutoff)
                  for i, (kind, on, _) in enumerate(steps) if kind == "extension"]
    clarifications = [_span(on, next((o for k, o, _ in steps[i + 1:] if k in ("clarified", "closed", "withdrawn")), None),
                            cutoff)
                      for i, (kind, on, _) in enumerate(steps) if kind == "clarification_requested"]
    return {"agency_id": agency_id, "request": request, "name": name, "sent_on": entry["sent_on"],
            "first_step_days": days(first(lambda kind, partial: kind not in ORGANIZER)),
            "determination_days": days(first(lambda kind, partial: kind == "determination")),
            "first_production_days": days(first(lambda kind, partial: kind == "produced")),
            "final_production_days": days(final),
            "open_age_days": None if final is not None or stopped else (cutoff - sent).days,
            "extension_spans": extensions, "clarification_spans": clarifications}


def _median(values):
    if not values:
        return None
    middle = statistics.median(values)
    return int(middle) if middle == int(middle) else middle


def _summary(rows):
    out = []
    for agency_id in sorted({row["agency_id"] for row in rows}):
        mine = [row for row in rows if row["agency_id"] == agency_id]
        metrics = {}
        for metric in SCALARS:
            found = [row[metric] for row in mine if row[metric] is not None]
            metrics[metric] = {"count": len(found), "median": _median(found)}
        for metric, key in zip(WAITS, ("extension_spans", "clarification_spans")):
            spans = [span for row in mine for span in row[key]]
            metrics[metric] = {"count": len(spans), "median": _median([span["days"] for span in spans]),
                               "open": sum(span["open"] for span in spans)}
        out.append({"agency_id": agency_id, "name": mine[0]["name"], "request_count": len(mine), "metrics": metrics})
    return out


def report(root, as_of=None, *, today=None):
    """The response-time report as of ``as_of`` (default today). Every stored entry is validated before anything is cut."""
    today = _today(today)
    cutoff = _as_of(as_of, today)
    names = _names(root)
    parsed = [(*kit_track._split(key), entry, _parse(entry, today))
              for key, entry in kit_track.read_sent(root).items()]
    rows = []
    for agency_id, request, entry, steps in parsed:
        if date.fromisoformat(entry["sent_on"]) > cutoff:
            continue
        kept = [step for step in steps if step[1] <= cutoff]
        rows.append(_row(agency_id, request, names.get(agency_id, agency_id), entry, kept, cutoff))
    rows.sort(key=lambda row: (row["agency_id"], row["request"]))
    return {"as_of": cutoff.isoformat(), "note": NOTE, "requests": rows, "agencies": _summary(rows)}


def _text(value):  # a table cell: no control character can reach the terminal
    return "".join(c if c.isprintable() else " " for c in str(value))


def _days(value):
    return "-" if value is None else str(value)


def _span_text(kind, span):
    end = f"open as of {span['end_on']}" if span["open"] else span["end_on"]
    return f"    {kind} {span['start_on']} to {end}: {span['days']} days" + (" (open)" if span["open"] else "")


def format_report(report):
    """The report as a plain-text table (read-only text)."""
    lines = [f"Response times ({report['note']})", f"As of {report['as_of']}. {PROJECTION}",
             "A dash means the step is not recorded. Open ages and open waits run to the as-of date and are "
             "censored observations, not completed turnaround.", ""]
    if not report["requests"]:
        return "\n".join([*lines, "No recorded requests."])
    lines.append("agency / request | name | sent | first step | determination | first production | "
                 "final production | open age")
    for row in report["requests"]:
        lines.append(" | ".join([_text(kit_track._label(row["agency_id"], row["request"])), _text(row["name"]),
                                 row["sent_on"], *(_days(row[metric]) for metric in SCALARS)]))
        lines += [_span_text("extension", s) for s in row["extension_spans"]]
        lines += [_span_text("clarification", s) for s in row["clarification_spans"]]
    lines += ["", f"Per agency: count and median ({report['note']})"]
    for agency in report["agencies"]:
        lines.append(f"{_text(agency['agency_id'])} ({_text(agency['name'])}), {agency['request_count']} request(s)")
        for metric, found in agency["metrics"].items():
            extra = f"; {found['open']} open (censored)" if found.get("open") else ""
            lines.append(f"    {metric}: count {found['count']}, median {_days(found['median'])}{extra}")
    return "\n".join(lines)


def _csv_rows(report):
    note, rows = report["note"], []
    for row in report["requests"]:
        who = (row["agency_id"], row["request"], row["name"])
        rows += [["request_metric", *who, metric, "", "", row[metric], "", "", "", note] for metric in SCALARS]
        for metric, key in zip(WAITS, ("extension_spans", "clarification_spans")):
            rows += [["request_span", *who, metric, s["start_on"], s["end_on"], s["days"], s["open"], "", "", note]
                     for s in row[key]]
    for agency in report["agencies"]:
        for metric, found in agency["metrics"].items():
            rows.append(["agency_metric", agency["agency_id"], "", agency["name"], metric, "", "", "",
                         "" if "open" not in found else found["open"], found["count"], found["median"], note])
    return [[kit_export._cell(cell) for cell in row] for row in rows]


def check_csv(root, out):
    """Refuse a bad --csv target before anything is printed or created."""
    kit_export._target(root, out)


def write_csv(root, out, report):
    """Write the report to a new private .csv (kit-status --csv rules); return a summary."""
    target = kit_export._target(root, out)
    rows = _csv_rows(report)
    kit_export.write_rows(target, out, HEADER, rows)
    return {"written": str(target), "rows": len(rows), "note": report["note"]}
