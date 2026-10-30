"""Counts-only, local checks for a records-run-report-v1.

Exit 0 means accounting/baseline checks passed, NOT production acceptance.
P1-P17 describe report checks; two consecutive scheduled runs and substantive human
acceptance remain separate gates. V1 has no receipts, policy, rows, digest or
rollback evidence, so those requirements cannot receive a positive verdict.

Baseline needs_g entries use (stage, code) identities, like the report schema.
The same 116 adoption gaps occur in delta AND adoption: these are observations,
not 232 distinct gaps. Compare each identity independently (no offsetting a
widened stage with a decrease elsewhere). An absent baseline code allows zero.
"""
import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from campaign_tool.records.run_report import STAGE_ORDER, validate_run_report

CODE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
RUN_ID = re.compile(r"run_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}")
COUNT_KEYS = {"input", "output", "failed", "skipped"}
KNOWN_OUTCOMES = {"EXTRACTED", "AWAITING_OCR", "CORRUPT_DOCUMENT", "EXTRACT_INCOMPLETE",
                  "WORKBOOK_POLICY_REQUIRED", "PARSER_MISSING", "DECODER_NEEDED", "EXTRACT_TIMEOUT",
                  "EXTRACT_NOT_ACCEPTED", "EXTRACT_ERROR", "PRESERVE_PENDING", "DEFERRED_RUN_BUDGET"}
OPTIONAL_BASELINE = {"run_budget_seconds", "expected_outcomes", "expected_awaiting_ocr_images"}


def is_count(value):
    return type(value) is int and value >= 0


def code_counts(items, baseline=False):
    """Validate identities before using any input string in output."""
    if not isinstance(items, list):
        raise ValueError("invalid needs_g")
    counts = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("invalid needs_g")
        required = {"stage", "code", "count"}
        if baseline and set(item) != required:
            raise ValueError("invalid baseline entry")
        code, stage, count = item.get("code"), item.get("stage"), item.get("count")
        if (not isinstance(code, str) or not CODE.fullmatch(code)
                or stage not in STAGE_ORDER or not is_count(count)):
            raise ValueError("invalid needs_g")
        key = (stage, code)
        if key in counts:
            raise ValueError("duplicate needs_g")
        counts[key] = count
    return counts


def validate_inputs(report, baseline):
    if (not isinstance(baseline, dict)
            or not {"schema_version", "needs_g"} <= set(baseline)
            or set(baseline) - {"schema_version", "needs_g"} - OPTIONAL_BASELINE
            or type(baseline["schema_version"]) is not int
            or baseline["schema_version"] != 1):
        raise ValueError("invalid baseline")
    if "run_budget_seconds" in baseline:
        budget = baseline["run_budget_seconds"]
        if type(budget) is not int or budget <= 120:
            raise ValueError("invalid budget")
    if "expected_outcomes" in baseline:
        expected = baseline["expected_outcomes"]
        if (not isinstance(expected, dict)
                or any(k not in KNOWN_OUTCOMES or not is_count(v) for k, v in expected.items())):
            raise ValueError("invalid expected outcomes")
    if "expected_awaiting_ocr_images" in baseline and not is_count(baseline["expected_awaiting_ocr_images"]):
        raise ValueError("invalid image baseline")
    accepted = code_counts(baseline["needs_g"], baseline=True)
    if not isinstance(report, dict):
        raise ValueError("invalid report")
    validate_run_report(report)
    if not RUN_ID.fullmatch(report["run_id"]):
        raise ValueError("invalid run identity")
    current = code_counts(report["needs_g"])
    for stage in report["stages"]:
        counts = stage["counts"]
        if set(counts) != COUNT_KEYS or not all(is_count(v) for v in counts.values()):
            raise ValueError("invalid counts")
        for outcome in stage.get("outcomes", []):
            if not CODE.fullmatch(outcome["code"]) or not is_count(outcome["count"]):
                raise ValueError("invalid outcome")
    return current, accepted


def evaluate(report, baseline, prior=None, replay=False):
    """Return (exit code, safe lines); never include validation exception text."""
    try:
        current, accepted = validate_inputs(report, baseline)
        if replay and prior is None:
            raise ValueError("replay requires prior")
        if prior is not None:
            validate_inputs(prior, baseline)
    except (ValueError, TypeError, KeyError, OverflowError):
        return invalid_result()

    lines = [f"run_id={report['run_id']}",
             "Exit gate: accounting + baseline + notify health + run assertions; not production acceptance.",
             "Two consecutive scheduled runs: NA (one report supplied)."]
    balanced = True
    for stage in report["stages"]:
        c = stage["counts"]
        ok = c["input"] == c["output"] + c["failed"] + c["skipped"]
        balanced &= ok
        lines.append(f"CHECK {stage['name']} {'PASS' if ok else 'FAIL'} "
                     f"input={c['input']} output={c['output']} failed={c['failed']} skipped={c['skipped']}")
    widened = False
    for key in sorted(current.keys() | accepted.keys()):
        count, limit = current.get(key, 0), accepted.get(key, 0)
        ok = count <= limit
        widened |= not ok
        lines.append(f"GAP {key[0]} {key[1]} {'PASS' if ok else 'FAIL'} "
                     f"count={count} baseline={limit}")

    stages = {s["name"]: s for s in report["stages"]}
    extract = stages["extract"]
    outcomes = {o["code"]: o["count"] for o in extract.get("outcomes", [])}
    output, scope = extract["counts"]["output"], extract["counts"]["input"]
    # V1 extract.input is the pending originals considered in this run, not a
    # separately established all-originals inventory. Name this denominator.
    if output == 0:
        p1 = ("FAIL", "extract.output=0; positive extraction required")
    elif "outcomes" not in extract:
        p1 = ("NA", "PRESERVE_PENDING outcome coverage absent")
    else:
        pending = outcomes.get("PRESERVE_PENDING", 0)
        p1 = ("NA", f"extract.output={output} PRESERVE_PENDING={pending} "
              f"extract.input={scope}; full in-scope denominator absent")
    def count(code):
        return sum(n for (_, c), n in current.items() if c == code)

    dry = count("PORTAL_FETCH_DRY_RUN_ONLY")
    denied = count("PORTAL_HOST_NOT_ALLOWLISTED")
    if dry or denied:
        p2 = ("FAIL", f"PORTAL_FETCH_DRY_RUN_ONLY={dry} PORTAL_HOST_NOT_ALLOWLISTED={denied}; "
              "full receipt/hash/byte-cap/contact evidence absent")
    else:
        p2 = ("NA", "received-file counts, verified hashes, byte caps and contact audit absent; "
              "portal.output counts enrollment, not receipts")
    workbook = count("WORKBOOK_POLICY_REQUIRED") + count("EXTRACT_WORKBOOK_POLICY_REQUIRED")
    p4 = (("FAIL", f"WORKBOOK_POLICY_REQUIRED={workbook}; workbook scope/policy evidence absent")
          if workbook else ("NA", "workbook scope and installed-policy extraction evidence absent"))
    notify = stages["notify"]
    notify_ok = (not report["notify_failed"] and notify["status"] == "ok"
                 and notify["counts"] == {"input": 1, "output": 1, "failed": 0, "skipped": 0})
    p8 = (("NA", "notify health recorded; G-only alert delivery/timing evidence absent")
          if notify_ok else ("FAIL", "notify stage did not complete successfully"))
    criteria = [
        p1, p2,
        ("NA", "derived-text/preserve receipt bindings and per-document review status absent"),
        p4,
        ("NA", "per-agency structured rows, record types and citation spans absent"),
        ("NA", "per-agency detector manifests and zero-hit/no-context distinction absent"),
        ("NA", "private cited digest and no-send/no-publication evidence absent; outputs is only a snapshot"),
        p8,
        ("NA", "reviewed backup/rollback proof and receipt immutability evidence absent"),
    ]
    lines.append("Criterion Status Reason")
    lines.extend(f"P{i} {status} {reason}" for i, (status, reason) in enumerate(criteria, 1))
    extra = extra_criteria(report, baseline, prior, replay)
    lines.extend(f"P{i} {status} {reason}" for i, (status, reason) in enumerate(extra, 10))
    exit_code = int(not balanced or widened or not notify_ok or any(s == "FAIL" for s, _ in extra))
    lines.append(f"EXIT {exit_code}; machine checks only; review required")
    return exit_code, lines


def outcome_counts(report):
    counts = {}
    for stage in report["stages"]:
        for item in stage.get("outcomes", []):
            counts[item["code"]] = counts.get(item["code"], 0) + item["count"]
    return counts


def extra_criteria(report, baseline, prior, replay):
    def row(ok, reason, present=True):
        return ("PASS" if ok else "FAIL", reason) if present else ("NA", "optional input absent")

    outcomes = outcome_counts(report)
    ocr, refusals = report.get("ocr"), report.get("refusals")
    budget = baseline.get("run_budget_seconds")
    expected = baseline.get("expected_outcomes")
    images = baseline.get("expected_awaiting_ocr_images")
    prior_outcomes = outcome_counts(prior) if prior is not None else {}
    current_g = code_counts(report["needs_g"])
    prior_g = code_counts(prior["needs_g"]) if prior is not None else {}
    extract = report["stages"][4]
    refused = sum(o["count"] for o in extract.get("outcomes", []) if o["code"] == "EXTRACT_NOT_ACCEPTED")
    return [
        row(budget is not None and report["duration_seconds"] < budget - 120,
            "duration below budget minus 120 seconds", budget is not None),
        row(all(outcomes.get(k, 0) == v for k, v in (expected or {}).items()),
            "expected outcome counts equal", expected is not None),
        row(set(outcomes) <= KNOWN_OUTCOMES, "known outcome codes only"),
        row(ocr is not None and ocr["awaiting_ocr_images"] == images,
            "image backlog equals baseline", ocr is not None and images is not None),
        row(ocr is not None and report["review_required_count"] >= ocr["pages_ocr"] - ocr["pages_reviewed"],
            "review count covers unreviewed OCR pages", ocr is not None),
        row(outcomes.get("PRESERVE_PENDING", 0) <= prior_outcomes.get("PRESERVE_PENDING", 0),
            "preserve pending did not grow", prior is not None),
        row(report["new_data"]["documents"] == 0 and all(n <= prior_g.get(k, 0) for k, n in current_g.items()),
            "replay adds no documents or needs_g counts", replay),
        row(refusals is not None and refusals["not_accepted_coded"] == refused,
            f"refusal coverage; unlisted={refusals['unlisted']}" if refusals is not None else "",
            refusals is not None),
    ]


def invalid_result():
    return 1, ["CHECK INPUT FAIL invalid report or baseline; values suppressed",
               "Criterion Status Reason",
               *(f"P{i} NA invalid input prevents assessment" for i in range(1, 18)),
               "EXIT 1; machine checks only; review required"]


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("non-finite JSON value")


def load_json(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream, object_pairs_hook=unique_object, parse_constant=reject_constant)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", help="records-run-report-v1 JSON")
    parser.add_argument("--baseline", required=True, help="accepted stage/code counts JSON")
    parser.add_argument("--prior", help="prior synthetic or operator-local report")
    parser.add_argument("--replay", action="store_true", help="assert replay adds nothing; requires --prior")
    args = parser.parse_args(argv)
    try:
        prior = load_json(args.prior) if args.prior else None
        if args.prior and prior is None:
            raise ValueError("invalid prior")
        result = evaluate(load_json(args.report), load_json(args.baseline), prior, args.replay)
    except (OSError, ValueError, UnicodeError, RecursionError):
        result = invalid_result()
    exit_code, lines = result
    print("\n".join(lines))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
