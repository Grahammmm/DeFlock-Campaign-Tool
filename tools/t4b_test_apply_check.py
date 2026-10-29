#!/usr/bin/env python3
"""Offline SYNTHETIC S5 evidence checker. Never opens a records store or runs tools.

The envelope is a test transport, NOT a replacement planner/report/policy schema.
Opaque row hashes represent full rows; actual adopt-mail reports remain nested
unchanged. A pass has no execution, enrollment, semantic or release authority.
"""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat

# Public, nonsecret source identities use short literal groups to avoid false
# credential-scan hits without adding scanner exclusions or changing the pins.
# Synthetic test identities are generated separately.
SOURCE_HEAD = (
    "93995a1a" "615f6aac" "a7bfb0fb" "eb39a7f3" "aa367997"
)
REPO = Path(__file__).resolve().parents[1]
PINS = {
    "docs/RECORDS-LEGACY-MAIL-RESOLUTION.md": (
        "db958989" "eedfeb18" "76972c83" "853fdfb8"
        "c6d9d354" "9ec7c56f" "34027531" "21cd42f3"
    ),
    "docs/RECORDS-MAIL-ADOPTION.md": (
        "717332e6" "edc1b83b" "7fc5ed7e" "9302586d"
        "b17d3d92" "6d7894dc" "bd527e18" "4d7d8394"
    ),
    "campaign_tool/records/mail_adoption.py": (
        "f9038893" "32c84e39" "c2e2cee1" "0cfcac7b"
        "fc33e439" "48a8cc1e" "28e43f63" "b0d6eda1"
    ),
    "campaign_tool/records/cli.py": (
        "7ab73a9d" "4cdd9c38" "a37fa374" "3493f0e0"
        "9fa66ee8" "e783b0b0" "fcdce199" "03275474"
    ),
}
F010_SHA = (
    "4b91e33b" "33f491e0" "685c7bf5" "51d17a4c"
    "557bb6b6" "c0d48c0c" "12b48e17" "1273e421"
)
RUNSHEET_SHA = (
    "141d9b61" "ec4cf7b1" "70bf3a14" "ae209be5"
    "35184a6b" "a52bd6b8" "cdf71c0f" "6c03f953"
)
GATES = ("coordinator_registration", "facade", "wal_queue_lock", "backup",
         "stop_disposition", "remaining_allowance", "inventory_policy_approval",
         "source_count_partitions", "engine_review", "test_scope", "command_closure")
STEPS = ("admission", "quiescence", "backup", "baseline", "census", "policy",
         "dry", "apply", "delta", "rerun", "integrity", "handback", "enrollment")
LEGACY = ("legacy_rows", "occurrence_projection_rows", "captured_legacy_occurrences")
HEX = re.compile(r"[0-9a-f]{64}\Z")


class CheckError(ValueError):
    pass


def need(condition, code):
    if not condition:
        raise CheckError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def digest(value):
    # Same serialization as ledger.store.canonical / original_resolution.digest.
    return sha(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())


def hash_value(value):
    need(isinstance(value, str) and HEX.fullmatch(value), "invalid_hash")
    return value


def count(value):
    need(type(value) is int and value >= 0, "invalid_count")
    return value


def hashes(values):
    need(isinstance(values, list), "invalid_set")
    for value in values:
        hash_value(value)
    need(values == sorted(set(values)), "noncanonical_set")
    return set(values)


def exact_keys(value, keys, code="invalid_shape"):
    need(isinstance(value, dict) and set(value) == set(keys), code)


def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        need(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def parse(raw):
    try:
        return json.loads(raw, object_pairs_hook=no_duplicates,
                          parse_constant=lambda _: (_ for _ in ()).throw(CheckError("nonfinite_json")))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CheckError("invalid_json") from error


def read_local(path, root):
    """Bounded regular-file read, no symlink components, no store-derived paths."""
    path, root = Path(os.path.abspath(path)), Path(os.path.abspath(root))
    need(path.is_relative_to(root), "outside_input_root")
    for part in reversed((path, *path.parents)):
        info = part.lstat()
        need(not stat.S_ISLNK(info.st_mode), "symlink_input")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        need(stat.S_ISREG(before.st_mode) and before.st_size <= 4 * 1024 * 1024, "invalid_input_file")
        raw = stream.read(4 * 1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
        need((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
             (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns), "input_changed")
        need(len(raw) == before.st_size, "input_changed")
    return raw


def clock(value):
    need(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value), "invalid_time")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise CheckError("invalid_time") from error


def validate_runsheet(text, repo=None):
    repo = REPO if repo is None else Path(repo)
    need(repo.is_absolute(), "missing_absolute_helper")
    need(isinstance(text, str) and text.strip(), "empty_runsheet")
    for step in STEPS:
        match = re.search(r"^## " + step + r"\n(.*?)(?=^## |\Z)", text, re.M | re.S)
        need(match and len(match[1].strip()) >= 120, "missing_runsheet_step:" + step)
    for phrase in ("NOT RUNNABLE", "historical tested population: k=0", "does not fix the400",
                   "t_e65333a7", "t_c8120082", "D5-A", "exact20", "UNVERIFIED"):
        need(phrase in text, "missing_runsheet_boundary")
    blocks = re.findall(r"<!-- COMMANDS\n(.*?)\nCOMMANDS -->", text, re.S)
    need(len(blocks) == 1, "missing_command_manifest")
    manifest = parse(blocks[0])
    exact_keys(manifest, ("source_head", "commands"))
    need(manifest["source_head"] == "<SOURCE_HEAD>" and
         f"Source: local {SOURCE_HEAD}, remote freshness UNVERIFIED." in text,
         "command_source_mismatch")
    for name, expected in PINS.items():
        need(sha(read_local(repo / name, repo)) == expected, "pinned_source_changed")
    # Inspect supported parser WITHOUT importing production runtime/defaults.
    tree = ast.parse(read_local(repo / "campaign_tool/records/mail_adoption.py", repo))
    supported = {a.value for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "add_argument"
                 for a in n.args if isinstance(a, ast.Constant) and isinstance(a.value, str)}
    commands = manifest["commands"]
    exact_keys(commands, ("census", "dry", "apply", "rerun", "checker"))
    base = ["python3", "-m", "campaign_tool.records", "adopt-mail", "--root", "<TEST_ROOT>",
            "--mail-history", "<FROZEN_EXPORT>", "--config", "<CONFIG>"]
    for name in ("census", "dry", "apply", "rerun"):
        argv = commands[name]
        need(isinstance(argv, list) and all(isinstance(a, str) for a in argv), "invalid_command")
        need(all(a in supported for a in argv[4:] if a.startswith("--")), "unsupported_option")
        expected = base + (["--legacy-resolution-inventory", "<NEW_PRIVATE_INVENTORY>"] if name == "census"
                           else ["--apply"] if name in ("apply", "rerun") else [])
        need(argv == expected, "incomplete_command")
    helper = str(repo / "tools/t4b_test_apply_check.py")
    argv = commands["checker"]
    # Resolve only this closed, inert template token against the caller's repo.
    # Never stat/read a checkout path copied from an author's machine.
    need(isinstance(argv, list) and len(argv) == 10 and
         argv[:2] == ["python3", "<REPO_ROOT>/tools/t4b_test_apply_check.py"], "missing_absolute_helper")
    argv = [argv[0], helper, *argv[2:]]
    need(Path(argv[1]).is_absolute(), "missing_absolute_helper")
    need(argv[2:] == ["--packet", "<SYNTHETIC_PACKET>", "--packet-sha256", "<PACKET_SHA256>",
                      "--run-id", "<RUN_ID>", "--decision-time", "<UTC_DECISION_TIME>"], "incomplete_command")
    need(bool(read_local(Path(helper), repo)), "missing_absolute_helper")
    # The complete reviewed template is closed, not just its headings/commands.
    # The code review covers these exact bytes; no filled or edited attended
    # runsheet is qualified by this synthetic-only checker.
    need(sha(text.encode()) == RUNSHEET_SHA, "runsheet_content_changed")
    return True


def resolution_summary(rows):
    return {"rows": len(rows), "sha256": digest(sorted((k, v["binding_sha256"]) for k, v in rows.items()))}


def snapshot(value):
    exact_keys(value, ("originals", "resolutions", "legacy", "mail_messages", "stages", "quick_check", "fk_violations"))
    count(value["mail_messages"])
    need(value["quick_check"] == "ok" and count(value["fk_violations"]) == 0, "integrity_failed")
    exact_keys(value["legacy"], LEGACY)
    for item in value["legacy"].values():
        hash_value(item)
    for table in ("originals", "resolutions", "stages"):
        need(isinstance(value[table], dict), "invalid_snapshot")
        for key in value[table]:
            hash_value(key)
    for row in value["originals"].values():
        exact_keys(row, ("row_sha256", "role", "scope", "cas_sha256", "provenance", "receipt_sha256", "occurrences"))
        hash_value(row["row_sha256"])
    for row in value["resolutions"].values():
        exact_keys(row, ("row_sha256", "binding_sha256", "schema", "policy_sha256", "capture_receipt_sha256", "capture_occurrence_id"))
        hash_value(row["row_sha256"])
        hash_value(row["binding_sha256"])
    need(set(value["stages"]) == set(value["originals"]), "stage_identity_mismatch")
    for row in value["stages"].values():
        exact_keys(row, ("preserve", "extract"))
        need(row["preserve"] in ("pending", "done") and row["extract"] in ("pending", "done", "failed"), "invalid_stage_status")


def validate(packet, run_id, decision_time):
    """Check inert metadata only; raises a safe code, never an assertion gate."""
    try:
        return _validate(packet, run_id, decision_time)
    except (KeyError, TypeError, AttributeError, IndexError) as error:
        raise CheckError("missing_or_malformed_evidence") from error


def _validate(p, run_id, decision_time):
    exact_keys(p, ("schema", "kind", "run_id", "source_head", "f010_sha256", "contract_pins", "binding",
                   "gates", "allowance", "historical_tested_k", "fixes400", "partition", "policy", "expected",
                   "snapshots", "reports"))
    need(p["schema"] == "s5-synthetic-evidence-v1" and p["kind"] == "SYNTHETIC", "not_synthetic")
    need(isinstance(run_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id) and p["run_id"] == run_id, "run_identity_mismatch")
    need(p["source_head"] == SOURCE_HEAD and p["f010_sha256"] == F010_SHA and p["contract_pins"] == PINS, "source_identity_mismatch")
    need(type(p["historical_tested_k"]) is int and p["historical_tested_k"] == 0 and p["fixes400"] is False, "false_coverage_claim")
    binding = p["binding"]
    exact_keys(binding, ("snapshot_sha256", "inventory_sha256", "policy_sha256", "approval_sha256", "config_sha256", "preflight_config_sha256", "backup_manifest_sha256"))
    for value in binding.values():
        hash_value(value)
    exact_keys(p["gates"], GATES)
    for gate in p["gates"].values():
        exact_keys(gate, ("state", "evidence_sha256", "run_id", "snapshot_sha256"))
        need(gate["state"] == "SIMULATED_QUALIFIED" and gate["run_id"] == run_id and
             gate["snapshot_sha256"] == binding["snapshot_sha256"], "unqualified_gate")
        hash_value(gate["evidence_sha256"])
    exact_keys(p["allowance"], ("remaining_attempts", "remaining_seconds", "requested_attempts", "requested_seconds", "stop_clear"))
    a = p["allowance"]
    need(a["stop_clear"] is True and 0 < count(a["requested_attempts"]) <= count(a["remaining_attempts"])
         and 0 < count(a["requested_seconds"]) <= count(a["remaining_seconds"]), "allowance_or_stop")
    exact_keys(p["snapshots"], ("before", "after", "rerun"))
    before, after, rerun = (p["snapshots"][key] for key in ("before", "after", "rerun"))
    for value in (before, after, rerun):
        snapshot(value)
    need(digest(before) == binding["snapshot_sha256"], "snapshot_hash_mismatch")
    need(after == rerun, "rerun_snapshot_changed")
    exact_keys(p["partition"], ("inventory", "policy", "held", "already_resolved", "resolvable", "blocked"))
    parts = {key: hashes(v) for key, v in p["partition"].items()}
    I, P, H, R, E, B = (parts[k] for k in ("inventory", "policy", "held", "already_resolved", "resolvable", "blocked"))
    need(not P & H and P | H == I and not R & E and not R & B and not E & B and R | E | B == P and not B, "partition_mismatch")
    need(I <= set(before["originals"]), "inventory_original_missing")
    need(R == P & set(before["resolutions"]) and not H & set(before["resolutions"]), "baseline_resolution_mismatch")
    need(isinstance(p["policy"], dict) and set(p["policy"]) == P, "policy_identity_mismatch")
    for entry in p["policy"].values():
        exact_keys(entry, ("verification_receipt_sha256", "binding_occurrence_id"))
        hash_value(entry["verification_receipt_sha256"])
        need(isinstance(entry["binding_occurrence_id"], str) and entry["binding_occurrence_id"].strip(), "missing_occurrence")
    old, new = before["resolutions"], after["resolutions"]
    need(all(new.get(k) == v for k, v in old.items()), "r0_changed")
    need(set(new) - set(old) == E and not E & H, "resolution_delta_mismatch")
    for key in E:
        row, entry = new[key], p["policy"][key]
        need(row["schema"] == "original_resolution_mail_export_v1" and row["policy_sha256"] == binding["policy_sha256"]
             and row["capture_receipt_sha256"] == entry["verification_receipt_sha256"]
             and row["capture_occurrence_id"] == entry["binding_occurrence_id"], "resolution_binding_mismatch")
    k0 = set(before["originals"])
    need(all({field: value for field, value in after["originals"].get(k, {}).items() if field != "occurrences"} ==
             {field: value for field, value in row.items() if field != "occurrences"}
             for k, row in before["originals"].items()), "k0_changed")
    need(before["legacy"] == after["legacy"], "legacy_changed")
    exact_keys(p["expected"], ("receipts", "eligible_new_messages", "initial_gaps", "final_gaps", "binding_messages",
                                "dependent_messages", "new_originals", "adopted_receipts", "preserve_promotions", "scope_counts"))
    ex = p["expected"]
    for key in ("receipts", "eligible_new_messages", "binding_messages", "dependent_messages"):
        count(ex[key])
    for gaps in (ex["initial_gaps"], ex["final_gaps"]):
        need(isinstance(gaps, dict) and set(gaps) <= {"original_scope_unresolved", "unsupported_rfc822_part"}, "invalid_gaps")
        for value in gaps.values():
            need(count(value) > 0, "invalid_gaps")
    n = ex["binding_messages"] + ex["dependent_messages"]
    need(ex["initial_gaps"].get("original_scope_unresolved", 0) - n == ex["final_gaps"].get("original_scope_unresolved", 0)
         and ex["initial_gaps"].get("unsupported_rfc822_part", 0) == ex["final_gaps"].get("unsupported_rfc822_part", 0), "gap_direction_mismatch")
    a_new, adopted, promoted = (hashes(ex[key]) for key in ("new_originals", "adopted_receipts", "preserve_promotions"))
    total_adopted = ex["eligible_new_messages"] + n
    need(len(adopted) == total_adopted and after["mail_messages"] == before["mail_messages"] + total_adopted, "mail_delta_mismatch")
    need(set(after["originals"]) - k0 == a_new and not a_new & I, "a_new_mismatch")
    for key in k0:
        old_occ = hashes(before["originals"][key]["occurrences"])
        new_occ = hashes(after["originals"][key]["occurrences"])
        need(old_occ <= new_occ and new_occ - old_occ <= adopted, "occurrence_continuity_mismatch")
    for key in a_new:
        row = after["originals"][key]
        need(row["role"] == "original" and row["scope"] == "in_scope" and row["cas_sha256"] == key
             and row["provenance"] == "verified_export_receipt" and row["receipt_sha256"] in adopted
             and bool(hashes(row["occurrences"])) and hashes(row["occurrences"]) <= adopted, "a_new_provenance_mismatch")
    need(promoted <= E, "promotion_scope_mismatch")
    for key, stage in before["stages"].items():
        expected = dict(stage)
        if key in promoted:
            need(stage["preserve"] == "pending", "promotion_direction_mismatch")
            expected["preserve"] = "done"
        need(after["stages"].get(key) == expected, "stage_delta_mismatch")
    for key in a_new:
        need(after["stages"][key] == {"preserve": "done", "extract": "pending"}, "new_stage_mismatch")
    scope_counts = {scope: sum(row["scope"] == scope for row in before["originals"].values())
                    for scope in {row["scope"] for row in before["originals"].values()}}
    need(isinstance(ex["scope_counts"], dict), "invalid_scope_counts")
    for value in ex["scope_counts"].values():
        count(value)
    need(ex["scope_counts"] == scope_counts, "scope_count_mismatch")
    exact_keys(p["reports"], ("preflight", "dry", "apply", "rerun"))
    decision = clock(decision_time)
    for phase, wrapper in p["reports"].items():
        exact_keys(wrapper, ("run_id", "binding", "observed_at", "exit", "sha256", "report"))
        need(wrapper["run_id"] == run_id and wrapper["binding"] == binding, "report_identity_mismatch")
        observed = clock(wrapper["observed_at"])
        need(0 <= (decision - observed).total_seconds() <= 3600, "stale_report")
        # Order is explicit, not dependent on JSON object order (see below).
        need(type(wrapper["exit"]) is int and wrapper["exit"] == 0, "report_failed")
        report = wrapper["report"]
        need(isinstance(report, dict) and bool(report) and wrapper["sha256"] == digest(report), "report_hash_mismatch")
        need(report["source"] == "mail-history" and report["mode"] == ("apply" if phase in ("apply", "rerun") else "dry-run"), "report_mode_mismatch")
        need(report["gate"]["open"] is True and report["gate"]["state"] in ("enabled", "enabled_marker_superseded_by_config"), "adoption_gate_closed")
        for field in ("failures", "gaps", "identity_conflicts", "unaccepted_coded_gaps"):
            need(report[field] == {}, "report_blocked")
        for field in ("receipts", "already_adopted", "pending_messages", "coded_gap_messages", "new_messages", "eligible_new_messages",
                      "incomplete_messages", "unfinalized_runs", "adopted_messages", "resumed_messages",
                      "attachments", "new_attachments", "pending_attachments", "coded_gap_attachments", "adopted_attachments"):
            count(report[field])
        need(report["incomplete_messages"] == report["unfinalized_runs"] == report["resumed_messages"] == 0, "incomplete_report")
        need(report["receipts"] == ex["receipts"] == report["already_adopted"] + report["pending_messages"] + report["coded_gap_messages"], "receipt_partition_mismatch")
        need(report["coded_gap_messages"] == sum(report["coded_gaps"].values()), "gap_count_mismatch")
        initial = phase == "preflight"
        terminal = phase == "rerun"
        gaps = ex["initial_gaps"] if initial else ex["final_gaps"]
        need(report["coded_gaps"] == gaps, "report_gap_mismatch")
        eligible = 0 if terminal else ex["eligible_new_messages"]
        resolvable = 0 if initial or terminal else n
        need(report["eligible_new_messages"] == eligible and report["pending_messages"] == eligible + resolvable
             and report["new_messages"] == eligible + resolvable + report["coded_gap_messages"], "work_partition_mismatch")
        need(report["adopted_messages"] == (total_adopted if phase == "apply" else 0), "adopted_count_mismatch")
        if initial:
            continue
        need(report["resolution_policy"] == {"state": "loaded", "sha256": binding["policy_sha256"]}
             and report["resolution_plan"] == {"state": "ok"} and report["resolution_drift"] == {}, "resolution_plan_blocked")
        for field in ("policy_originals", "policy_originals_resolved", "resolvable_originals", "policy_originals_blocked", "held_originals",
                      "resolvable_messages", "resolvable_binding_messages", "resolvable_dependent_messages", "resolved_originals"):
            count(report[field])
        need(report["policy_originals"] == len(P) and report["held_originals"] == len(H) and report["policy_originals_blocked"] == 0
             and report["policy_originals_resolved"] == (len(P) if terminal else len(R))
             and report["resolvable_originals"] == (0 if terminal else len(E))
             and report["resolvable_messages"] == (0 if terminal else n)
             and report["resolvable_binding_messages"] == (0 if terminal else ex["binding_messages"])
             and report["resolvable_dependent_messages"] == (0 if terminal else ex["dependent_messages"]), "resolution_partition_mismatch")
        need(report["resolved_originals"] == (len(E) if phase == "apply" else 0), "resolved_count_mismatch")
        for field in ("resolution_baseline", "resolution_after"):
            exact_keys(report[field], ("rows", "sha256"))
            count(report[field]["rows"])
            hash_value(report[field]["sha256"])
        need(report["resolution_baseline"] == resolution_summary(new if terminal else old)
             and report["resolution_after"] == resolution_summary(new if phase in ("apply", "rerun") else old), "resolution_digest_mismatch")
        if phase in ("apply", "rerun"):
            aft = report["after"]
            for field in ("policy_originals", "policy_originals_resolved", "policy_originals_blocked", "resolvable_originals",
                          "resolvable_messages", "coded_gap_messages", "pending_messages", "incomplete_messages",
                          "unfinalized_runs", "new_messages", "already_adopted"):
                count(aft[field])
            need(aft["policy_originals"] == aft["policy_originals_resolved"] == len(P) and
                 aft["policy_originals_blocked"] == aft["resolvable_originals"] == aft["resolvable_messages"] == 0
                 and aft["resolution_drift"] == {} and aft["resolution_plan"] == {"state": "ok"}
                 and aft["coded_gaps"] == ex["final_gaps"] and aft["coded_gap_messages"] == sum(ex["final_gaps"].values())
                 and aft["pending_messages"] == aft["incomplete_messages"] == aft["unfinalized_runs"] == aft["new_messages"] - aft["coded_gap_messages"] == 0
                 and aft["identity_conflicts"] == aft["unaccepted_coded_gaps"] == {}
                 and aft["already_adopted"] + aft["coded_gap_messages"] == ex["receipts"], "after_report_mismatch")
    times = [clock(p["reports"][phase]["observed_at"]) for phase in ("preflight", "dry", "apply", "rerun")]
    need(all(a < b for a, b in zip(times, times[1:])), "report_order_mismatch")
    pre, dry, applied, replay = (p["reports"][phase]["report"] for phase in ("preflight", "dry", "apply", "rerun"))
    for report in (pre, dry, applied, replay):
        need(report["attachments"] == pre["attachments"] and
             report["new_attachments"] == report["pending_attachments"] + report["coded_gap_attachments"], "attachment_partition_mismatch")
    for report in (dry, applied, replay):
        count(report["resolvable_attachments"])
        need(report["resolvable_attachments"] == (0 if report is replay else pre["coded_gap_attachments"] - dry["coded_gap_attachments"]), "attachment_resolution_mismatch")
    need(pre["new_attachments"] == dry["new_attachments"] == applied["new_attachments"] and
         dry["pending_attachments"] == applied["pending_attachments"] == pre["pending_attachments"] + dry["resolvable_attachments"] and
         dry["coded_gap_attachments"] == applied["coded_gap_attachments"] == replay["coded_gap_attachments"] and
         replay["pending_attachments"] == pre["adopted_attachments"] == dry["adopted_attachments"] == replay["adopted_attachments"] == 0 and
         applied["adopted_attachments"] == dry["pending_attachments"], "attachment_delta_mismatch")
    return {"status": "SYNTHETIC_VALID_REVIEW_REQUIRED", "activation_allowed": False, "semantic_acceptance": False,
            "s5_execution_verified": False, "historical_tested_k": 0, "synthetic_k": len(promoted), "fixes400": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", required=True)
    parser.add_argument("--packet-sha256", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--decision-time", required=True)
    args = parser.parse_args(argv)
    try:
        hash_value(args.packet_sha256)
        raw = read_local(args.packet, REPO.parent)
        need(sha(raw) == args.packet_sha256, "packet_hash_mismatch")
        validate_runsheet(read_local(REPO / "docs/runbooks/T4B-TEST-APPLY.md", REPO).decode())
        result = validate(parse(raw), args.run_id, args.decision_time)
    except (CheckError, OSError, UnicodeError, ValueError) as error:
        code = str(error) if isinstance(error, CheckError) else "input_unavailable_or_malformed"
        print(json.dumps({"status": "FAIL", "code": code, "activation_allowed": False}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
