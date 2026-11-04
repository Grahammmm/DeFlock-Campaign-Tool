"""kit-fees: one private, read-only fee report over every recorded request (nothing is paid, sent or published).

Rows come from ``kit_track.status`` (the fee rules ``kit-status`` uses); cents come from the stored ``amount_cents``
of the latest estimate and of its settlement, summed as integers and printed through ``kit_track._dollars``."""
from . import kit_export, kit_track

SCHEMA = "kit-fees-v1"
NOTE = "Amounts as recorded by the organizer; not an invoice, not legal advice. Nothing was sent."
NONE_NOTE = " No fee estimates recorded."
ORDER = {"unpaid": 0, "paid": 1, "waived": 2}
HEADER = ("agency_id", "request", "name", "reference", "fee_estimate", "fee_state", "fee_paid", "over_fee_cap")


def fees(root, today=None, law_base=None):
    """The fee report: requests with a recorded estimate, and exact-cents campaign totals."""
    report = kit_track.status(root, today, law_base)
    sent = kit_track.read_sent(root)
    rows, quoted, paid, unpaid = [], 0, 0, 0
    for row in report["requests"]:
        if row["fee_state"] is None:
            continue
        key = row["agency_id"] if row["request"] == "main" else f"{row['agency_id']}#{row['request']}"
        steps = [s for s in sent[key]["steps"] if isinstance(s, dict)]
        last, settled = kit_track._fee(steps)
        quoted += steps[last]["amount_cents"]
        if row["fee_state"] == "unpaid":
            unpaid += steps[last]["amount_cents"]
        if settled and "amount_cents" in settled:
            paid += settled["amount_cents"]
        rows.append({k: row[k] for k in HEADER})
    rows.sort(key=lambda r: (ORDER[r["fee_state"]], r["agency_id"], r["request"]))
    count = lambda state: sum(r["fee_state"] == state for r in rows)  # noqa: E731
    counts = {"requests_with_fee": len(rows), "unpaid": count("unpaid"), "paid": count("paid"),
              "waived": count("waived"), "over_cap": sum(r["over_fee_cap"] is True for r in rows)}
    totals = {"quoted": kit_track._dollars(quoted), "paid": kit_track._dollars(paid),
              "unpaid": kit_track._dollars(unpaid), "counts": counts}  # cents totals and counts share names: nested
    return {"schema": SCHEMA, "requests": rows, "totals": totals, "note": NOTE + ("" if rows else NONE_NOTE)}


def write_csv(root, out, report):
    """Write the report's rows and one TOTAL row to a new private file (kit-status --csv rules); return a summary."""
    target = kit_export._target(root, out)
    totals = report["totals"]
    rows = [[kit_export._cell(r[k]) for k in HEADER] for r in report["requests"]]
    rows.append(["TOTAL", "", f"unpaid {totals['unpaid']}", "", totals["quoted"], "", totals["paid"], ""])
    kit_export.write_rows(target, out, HEADER, rows)
    return {"written": str(target), "rows": len(report["requests"]), "totals": totals, "note": report["note"]}
