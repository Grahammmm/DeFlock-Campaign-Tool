"""Private, read-only, cited register dates; arithmetic is not legal acceptance."""
import argparse
import hashlib
import json
import sys

from .. import law
from .followup_clock import read_register
from .outcomes.register import _day, _roll_weekend

RULE = "ca-gov-7922.535-determination-and-extension-2026"
CAVEAT = ("Provisional reminder date: receipt, holiday last-day rules and extension-notice "
          "requirements are not checked. Not a legal finding.")


def deadlines(register, package, package_sha256):
    """Label every request's existing dates, preserving issues and checking both clocks."""
    rule = next(r for r in package["rules"] if r["rule_id"] == RULE)
    provenance = {"citation": rule["citation"], "source_url": rule["sources"][0]["url"],
                  "package": "jurisdictions/us-ca/package.json sha256 " + package_sha256,
                  "caveat": CAVEAT}
    encoded = package["records_law"]
    compatible = (encoded["determination_days"], encoded["determination_extension_days"],
                  encoded["day_type"]) == (10, 14, "calendar")
    out = []
    for row in register["requests"]:
        clock = row.get("deadline") or {}
        # Current register exports the chosen normalized start, not the raw receipt/send fields.
        start = _day(row.get("request_date"))
        basis = clock.get("basis")
        issues = list(row.get("issues") or [])
        if not compatible:
            issues.append("package_register_mismatch")
        received = {"date": start.isoformat() if start else None,
                    "label": "UNKNOWN" if not start else "DATED" if basis == "recorded_receipt" else "INFERRED",
                    "note": "recorded receipt" if basis == "recorded_receipt" else
                    "send date used; receipt not recorded" if start else "receipt/send date not recorded"}
        dates = {}
        for name, key, extension in (("determination_due", "base_due", False),
                                     ("extension_limit", "extended_due", True)):
            value = clock.get(key)
            note = ("only if the agency sent a written extension notice; recorded: " +
                    ("yes" if clock.get("extension_recorded") else "no")) if extension else (
                    "10 calendar days from receipt; a Saturday or Sunday due date moves to Monday")
            label = "COMPUTED" if value else "UNKNOWN"
            if not compatible:
                label = "UNVERIFIED"
            elif start:
                expected = law.deadline(package, start, extension=extension)
                if value != _roll_weekend(expected).isoformat():
                    issues.append("computed_mismatch")
                    label = "UNVERIFIED"
                elif value != expected.isoformat():
                    note += "; weekend-rolled"
            elif value:
                issues.append("computed_mismatch")
                label = "UNVERIFIED"
            dates[name] = {"date": value, "label": label, "note": note, **provenance}
        promise = (row.get("promised") or {}).get("date")
        out.append({"agency": row["agency"], "agency_key": row["agency_key"], "request_id": row["request_id"],
                    "received": received, **dates,
                    "promised": {"date": promise, "label": "DATED" if promise else "UNKNOWN",
                                 "note": "agency-stated" if promise else "not recorded"},
                    "issues": sorted(set(issues)), "review_required": True})
    return out


def main():
    parser = argparse.ArgumentParser(prog="records deadlines", description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    try:
        register = read_register(args.project, args.run_id)
        if register is None:
            raise ValueError("register_missing")
        package = law.load_package("us-ca")
        digest = hashlib.sha256(law.package_path("us-ca").read_bytes()).hexdigest()
        rows = deadlines(register, package, digest)
    except (OSError, ValueError, KeyError, TypeError, StopIteration, OverflowError):
        print("deadlines_unavailable: check this run's register and law package", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print("Private; REVIEW REQUIRED. " + CAVEAT)
        print("Date\tLabel\tWhat\tAgency\tRequest\tNotes / source")
        for row in rows:
            for what in ("received", "determination_due", "extension_limit", "promised"):
                entry = row[what]
                cells = [entry["date"] or "unknown", entry["label"], what, row["agency"], row["request_id"],
                         "; ".join(str(entry[k]) for k in ("note", "citation", "source_url", "package", "caveat") if k in entry)]
                print("\t".join(" ".join(str(c).split()) for c in cells))
            if row["issues"]:
                print("Issues: " + ", ".join(row["issues"]))
    return 0
