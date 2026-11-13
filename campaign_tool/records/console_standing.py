"""Console Home section 'Where each agency stands': one read-only row per agency (CON-HOME-2).

Folds the run's timeline requests by agency. An optional hand-staged ``<project>/agency-roster.tsv``
(header ``agency_key<TAB>agency``, public names only) adds agencies with no request in this run. Overdue is
arithmetic on our records, never a legal finding. Nothing here fetches, sends, approves or publishes.
"""
import os
import stat
import urllib.parse
from pathlib import Path

from . import console_board as cb, console_layout
from .outcomes import timeline as _tl
from .outcomes.reading import esc

ROSTER = "agency-roster.tsv"
MAX_ROWS, MAX_FIELD, MAX_BYTES = 200, 120, 64 * 1024
NOTE = "on our records; not a legal finding"
RANK = {"overdue": 0, "ours": 1, "waiting": 2, "closed": 3, "empty": 4}


def _invalid(code):
    return {"state": "unavailable", "code": code}


def load_roster(project):
    """{'state': 'absent'} | {'state': 'ok', 'rows': [(key, name)]} | {'state': 'unavailable', 'code': 'roster_invalid'}."""
    path = Path(project) / ROSTER
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            return _invalid("roster_invalid")
        text = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return {"state": "absent"}
    except (OSError, UnicodeDecodeError):
        return _invalid("roster_invalid")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or lines[0].rstrip("\r") != "agency_key\tagency" or len(lines) - 1 > MAX_ROWS:
        return _invalid("roster_invalid")
    rows, seen = [], set()
    for line in lines[1:]:
        parts = line.rstrip("\r").split("\t")
        if len(parts) != 2 or not all(0 < len(p) <= MAX_FIELD and p.isprintable() for p in parts) or parts[0] in seen:
            return _invalid("roster_invalid")
        seen.add(parts[0])
        rows.append(tuple(parts))
    return {"state": "ok", "rows": rows}


def _basis(mark):
    if mark.get("kind") == "promised_date_passed" or mark.get("basis") == "recorded_receipt":
        return "DATED"
    return "INFERRED" if mark.get("basis") == "submission_proxy" else "UNKNOWN"


def _contact(row, today):
    days = [cb._day(e.get("date")) for e in cb._live(row)
            if (e.get("source") or {}).get("kind") in cb.CONTACT_SOURCES and e.get("type") not in cb.NOT_CONTACT]
    days = [d for d in days if d and d <= today]
    return max(days) if days else None


def _urgent(model, row):
    marks = [m for m in row["overdue_marks"] if m.get("kind") != "unknown"]
    return (not marks, row["next_action"].get("owner") not in cb.OURS, (row.get("clocks") or {}).get("due") or "9999")


def _fold(model, today, agency, requests):
    opened = [r for r in requests if not r.get("closed")]
    contact = [d for d in (_contact(r, today) for r in requests) if d]
    out = {"key": agency, "agency": next((r["agency"] for r in requests if r.get("agency")), agency),
           "requests": len(requests), "open": len(opened),
           "last": max(contact) if contact else None, "overdue": None, "basis": None, "clock_unknown": False}
    out["days"] = (today - out["last"]).days if out["last"] else None
    if not opened:
        return out | {"stage": "closed", "action": "nothing: recorded closed", "owner": "none", "tier1": "", "rank": "closed"}
    marks = [m for r in opened for m in r["overdue_marks"] if m.get("kind") != "unknown"]
    if marks:
        pick = sorted(marks, key=lambda m: (_basis(m) != "DATED", str(m.get("date"))))[0]
        out["overdue"], out["basis"] = pick, _basis(pick)
    out["clock_unknown"] = any(m.get("kind") == "unknown" for r in opened for m in r["overdue_marks"])
    top = sorted(opened, key=lambda r: _urgent(model, r))[0]
    stages = [_tl.STAGES.index(r["stage"]) for r in opened if r.get("stage") in _tl.STAGES]
    rank = "overdue" if marks else "ours" if top["next_action"].get("owner") in cb.OURS else "waiting"
    return out | {"stage": _tl.STAGES[max(stages)] if stages else "unknown", "rank": rank,
                  "action": top["next_action"].get("action"), "owner": top["next_action"].get("owner"),
                  "tier1": "; ".join("%s: %s" % (r.get("request_id"), cb._tier1(model, r)) for r in opened)}


def agency_rows(model, today, roster):
    groups = {}
    for r in cb._requests(model):
        groups.setdefault(r.get("agency_key") or "(no key)", []).append(r)
    rows = [_fold(model, today, key, reqs) | {"flag": ""} for key, reqs in groups.items()]
    if roster.get("state") == "ok":
        names = dict(roster["rows"])
        for row in rows:
            if row["key"] not in names:
                row["flag"] = "not in roster"
        rows += [{"key": k, "agency": n, "requests": 0, "open": 0, "last": None, "days": None, "overdue": None,
                  "basis": None, "clock_unknown": False, "stage": "-", "action": "", "owner": "", "tier1": "",
                  "rank": "empty", "flag": "no request on record in this run"} for k, n in roster["rows"] if k not in groups]
    return sorted(rows, key=lambda x: (RANK[x["rank"]], x["agency"] or x["key"]))


def render_standing(model, today):
    out = ['<section id="standing"><h2>Where each agency stands</h2>']
    if model["timeline"]["state"] != "ok":
        return out[0] + '<p role="status">%s No agency rows can be shown.</p></section>' % esc(
            console_layout.unavailable_note("The request timeline", model["timeline"].get("code")))
    roster = load_roster(model["project"])
    if roster["state"] == "absent":
        out.append("<p>roster not staged; showing register agencies only</p>")
    elif roster["state"] != "ok":
        out.append('<p role="status">roster: unavailable (%s).</p>' % esc(roster["code"]))
    rows = agency_rows(model, today, roster)
    out.append("<table><thead><tr>%s</tr></thead><tbody>" % "".join('<th scope="col">%s</th>' % h for h in (
        "Agency", "Stage", "Last contact", "Days since", "Overdue (basis)", "Next action", "Owner", "Tier 1")))
    for r in rows:
        if r["overdue"]:
            over = "overdue %s (%s), %s" % (r["overdue"].get("date") or "", r["basis"], NOTE)
        elif r["clock_unknown"]:
            over = "clock unknown (UNKNOWN), %s" % NOTE
        else:
            over = ""
        link = '<a href="/agency?agency=%s">%s</a>' % (esc(urllib.parse.quote(r["key"], safe="")), esc(r["agency"]))
        name = link if r["requests"] else esc(r["agency"])
        if r["flag"]:
            name += " (%s)" % esc(r["flag"])
        cells = [name, esc(r["stage"]), esc(r["last"].isoformat() if r["last"] else "no contact recorded"),
                 esc("" if r["days"] is None else r["days"]), esc(over), esc(r["action"]), esc(r["owner"]), esc(r["tier1"])]
        out.append("<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in cells))
    return "\n".join(out + ["</tbody></table></section>"])
