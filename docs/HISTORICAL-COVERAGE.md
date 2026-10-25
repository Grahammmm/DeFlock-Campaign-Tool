# Historical mail coverage (L-B1) checker

Design item M-26 (checker half), card t_177ff79b. Status: REVIEW REQUIRED.
A machine-valid report is not a semantic, legal, source or publication acceptance.
The checker lifts no hold.

## Why a diff, not the current mailbox

If the current mail server matches the current export, that does not prove every
past email is still on file. Only receipt diffs against frozen historical roots
can show that. The accepted mailbox packet already has the inventory and diff
tool. This file adds the verdict rule and a small offline checker that applies it.

## Read-only sources

- Accepted mailbox packet attachment (read-only, never copied into this
  repository):
  /home/user/.hermes/kanban/boards/deflock/attachments/t_64ecd488/mailbox-rebuild-design_2.zip
  (sha256 prefix 33e783e2; full value on the card). Its member receipt_inventory.py
  (sha256 prefix 03290a10) defines identity normalisation. Its `diff`
  subcommand (member lines 130 to 137) writes the offline-mail-diff-v1 shape below.
  The member is read for its shape only. It is never edited.
- Approved mail design plan `plans/fable-mail-20261024/WORK-BREAKDOWN.json`
  (sha256 prefix 7943e54f) and `DESIGN.md` (sha256 prefix b14b7ebc). These are
  read-only plan pins. The attestation field list below is copied unchanged from
  their contracts table.

Full digests are kept on the card and not committed here, so that the
repository credential scan stays clean.

## Inputs

### offline-mail-diff-v1 (from the accepted shape, produced by the operator)

    {"schema": "offline-mail-diff-v1", "review_required": true,
     "added":   [record, ...],     # identity in current only
     "removed": [record, ...],     # identity in the historical root only
     "changed": [{"identity": [...], "before": record, "after": record}, ...],
     "counts":  {"added": int, "removed": int, "changed": int, "unchanged": int},
     "limits":  str}
    record = {"identity": [account_id, folder, uidvalidity, uid],
              "original": {"sha256": hex64, "bytes": int},
              "attachments": [{"part": str, "sha256": hex64, "bytes": int}, ...]}

The checker refuses the file, with a typed code, in any of these cases:

- the schema string is not exactly `offline-mail-diff-v1`;
- a key is missing or an extra key is present (an added `version` key counts);
- `review_required` is not `true`;
- `counts.added`, `counts.removed` or `counts.changed` differs from the length of its list;
- `counts.unchanged` is not a non-negative integer;
- a record has the wrong shape (identity is four strings, with uidvalidity and uid as positive decimal text, as the packet normalises them);
- an identity appears in more than one list;
- a `changed` entry whose before and after records are equal;
- the JSON has duplicate keys or non-finite numbers.

The `limits` text is never copied into the output.

### historical-root-attestation-v1 (custodian only; HELD / UNSUPPLIED)

The field list is copied from the WORK-BREAKDOWN contracts table:

    "schema": "historical-root-attestation",
    "version": 1,
    "root_label": "str (^[a-z0-9][a-z0-9-]{0,63}$)",
    "layer": "L-B1|L-B2",
    "diff_sha256": "hex (offline-mail-diff-v1 for L-B1; phase1-analysis-diff-v1 for L-B2)",
    "inventory_sha256": "hex|null (L-B1 historical inventory)",
    "root_manifest_sha256": "hex|null (L-B1: manifest of the frozen root decoded by M-07)",
    "capture_time_utc": "ISO-8601 with UTC offset",
    "count_unit": "receipts|occurrences|unique_messages",
    "producer_statement_sha256": "hex",
    "attested_by_role": "custodian",
    "status": "ATTESTED"

The plan says the custodian must accept, amend or replace this shape (M-23 item 10)
before any real routing run. A model may not produce or fill it. The checker
never writes an attestation.

This is an L-B1 checker, so it accepts only `layer: "L-B1"`. An L-B2
attestation belongs to M-29 and is refused here. `version` must be the integer
1; a boolean or string is refused. `capture_time_utc` must be ISO-8601 with an
explicit offset. Any explicit offset is accepted, because the contract wording
says "with UTC offset". Whether only +00:00 should be allowed is left for the
custodian to decide when accepting the shape. Every field is type-checked
before its value is compared, so a list, object, null or other non-string where
a string is expected (for example `count_unit: []`) is the same typed refusal
(exit 2) as a wrong string. Any other field value outside the
list above is a typed refusal.

## Verdict rule

Each `--diff` file is one historical root compared against current. An
attestation is bound to a diff when its `diff_sha256` equals the sha256 of that
diff file's exact bytes.

Per-root counts, as printed:

| printed key              | diff source                      | meaning                                                                             |
|--------------------------|----------------------------------|-------------------------------------------------------------------------------------|
| `added`                  | `added`                          | current only; not a historical occurrence                                           |
| `historical_only`        | `removed`                        | HISTORICAL_ONLY: still on file in the frozen root and retained in the denominator   |
| `content_conflict`       | `changed`                        | CONTENT_CONFLICT; the custodian decides                                             |
| `unchanged`              | `counts.unchanged`               | the same in historical and current                                                  |
| `historical_denominator` | removed + changed + unchanged    | every historical occurrence; nothing is dropped                                     |

Per-root verdicts, checked in this order:

1. UNSUPPLIED: no valid attestation is bound to this diff by diff_sha256. This
   includes a missing attestation and one whose diff_sha256 does not match.
2. NOT_COVERED_CONFLICT: the root has at least one CONTENT_CONFLICT.
3. COVERED: every historical occurrence is either unchanged in current or listed
   HISTORICAL_ONLY, with the source root label and capture time taken from the
   bound attestation.

Overall verdict:

- UNSUPPLIED when any root is UNSUPPLIED, or when a supplied attestation is
  bound to none of the supplied diffs (an attested root whose diff is missing).
- Otherwise NOT_COVERED_CONFLICT when any root has a conflict.
- Otherwise COVERED.

The verdict is never COVERED while any root is UNSUPPLIED. The checker never
claims the inventory is "complete". It never suggests taking anything off file:
a HISTORICAL_ONLY occurrence is evidence to keep, and a moved or renamed
message, or a UIDVALIDITY change, also shows up as added plus historical-only.

These cases are refused before any report exists:

- two diff files with identical bytes;
- two attestations bound to the same diff;
- two attestations with the same root label.

## Command

    python3 tools/historical_coverage_check.py --diff /abs/diff-<label>.json [--diff ...] \
        [--attestation /abs/attestation-<label>.json ...] --output /abs/new-report.json

- Argv must be exactly as shown. Abbreviations, unknown options and relative paths are refused.
- Input and output paths must be absolute and canonical. A symlinked input is refused.
- `--output` must not exist yet. It is created with O_EXCL | O_NOFOLLOW and mode 0600.
- The report (`historical-coverage-verdict-v1`, `review_required: true`) is written to `--output` and printed on stdout. It names diffs by file basename and sha256 only.
- Exit codes:
  - 0: overall COVERED.
  - 1: report written with another overall verdict.
  - 2: typed refusal. A JSON line `{"status": "refused", "code": ...}` goes to stderr. Nothing goes to stdout and no output file is created.
- Stdlib only. No network, no subprocess, no environment or HOME reads.

## Operator steps that this card does not cover

The operator runs the attended inventory and diff over the real historical
roots (M-26 goal; inputs HELD on custodian provenance). Custodian attestations
stay HELD / UNSUPPLIED until the custodian supplies them. Until then every real
run reports UNSUPPLIED, which is the honest result.

## Acceptance (card t_177ff79b)

    python3 scripts/n3_historical_check_acceptance.py --mode focused|negative|portability|hygiene --output-root <abs out root>

- focused: runs the card test suite in process (at least 8 tests).
- negative: a mutant seam drops HISTORICAL_ONLY from `historical_denominator`. The retention test must fail for the marker RETENTION_DENOMINATOR. The seam is then restored and the positive suite must pass.
- portability: runs the suite from a differently named relocated root with a synthetic HOME. It also scans the card files for box-only paths (only the attachment path above is allowed) and checks that output allocation is unique.
- hygiene: static checks for no network and no stray writes. It then runs detect-secrets, `tools/check_secret_report.py` and `tools/check_public_tree.py` through an exact-argv subprocess allowlist.

Every mode writes a receipt in a fresh unique directory below the output root.
Before importing candidate code (and after installing the guards) the runner
puts the root it resolved from its own file path, or the relocated root in
portability mode, first on `sys.path`; no environment variable is used. In
portability mode the staged checkout entry is taken off `sys.path`. The receipt
records this as `import_path_setup`, and the focused, negative and portability
criteria fail if the root is not first on `sys.path` before and after the
import (hygiene imports no candidate code).
Fixtures are synthetic. `tests/fixtures/n3_historical_check/scenarios.json` lists
the populations only. Diffs and attestations are generated at test time in
scratch space, never inside the fixture directory.
