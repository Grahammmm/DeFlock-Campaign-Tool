# Legacy original adoption (T6)

This offline driver adopts **declared legacy originals**, not email deliveries,
legal findings, or semantically accepted content. It never runs from a timer.
Live rehearsal, installation pin changes, and live apply remain attended T8 work.
The engine CLI is deliberately unchanged: its frozen T4B identity stays pinned.

## Entry point and trust

```sh
python3 -B -m campaign_tool.records.legacy_adoption \
  --root /private/processing-v1 --config /private/legacy.json \
  --manifest /private/manifest.tsv --policy /private/approved-policy.json
```

Default is read-only dry-run. Add `--apply` only for an explicitly approved,
attended adoption. There is no network access, sending, extraction, or publishing.
Inputs and outputs must stay outside the software checkout.

The config is an owner-owned, mode **0600**, regular, non-symlink JSON file with
exactly these fields (no command-line trust-pin override):

- `schema`: `legacy-adoption-v1`
- `manifest_sha256`, `policy_sha256`: lowercase SHA-256 pins. `policy_sha256` may
  be JSON `null` **only** for the first-proposal bootstrap below; a missing key,
  empty string, boolean or malformed hash is never null.
- `export_root`, `supplement_root`: absolute source roots, without traversal
- `adoption`: `enabled` or `disabled`

Disabled permits dry-run, not apply. Config, manifest, and policy use secure,
bounded opens; no symlink component is followed. Both artifact hashes are checked
before parsing the manifest, opening the ledger, or reading a source. Apply
reloads this trust closure under the root writer lock before preflight.

The manifest is mode **0600** TSV with the literal header
`sha256<TAB>size<TAB>source_path`. At most 10,000 rows; hashes are lowercase 64-hex,
sizes are decimal nonnegative integers within the capture limit, and paths are
absolute, bounded, and free of control characters and traversal. Duplicate
`(sha256, source_path)` pairs and conflicting sizes for any hash refuse the run.

## Source classes and D-B eligibility

Every manifest path is checked **before any source open**, including paths on
rows later excluded for format. A `.hermes` component or an `agent-work/flock-mvp`
component pair is forbidden. Other paths must be beneath one configured root.
Export sources require a recognized first component. For duplicate hashes choose
`received` > `received-user-files` > `portal-intake` > `productions` > `supplement`;
within a class, lexical path order breaks ties deterministically. Only the chosen
source is streamed, once in preflight (capture streams again during apply).

Eligibility comes from `originals.legacy_format`, **never the filename**:

- pdf, png, jpg, jpeg, docx, xlsx, zip, txt, html, htm, md: eligible unresolved
  declarations with no storage binding;
- json/csv: `excluded_metadata_class`, left untouched, not marked out-of-scope;
- absent/other formats: `format_unknown` count, `legacy_format_unknown` row code;
- other existing bindings: `already_bound`; excluded scope refuses adoption.

Dry-run verifies eligible sources' SHA-256 and size without ledger/CAS writes.
Sources must be owner-only, owner-owned, single-link regular files with no
symlink component. Apply additionally requires the existing read-only-filesystem
capture guard and refuses source paths in a git checkout or intake output.

`--write-policy-proposal /private/proposal.json` is dry-run-only. On a successful
preflight it exclusively creates a 0600 `approved-local-capture-policy-v1`
proposal: evidence hashes canonical JSON `{sha256,size,source_path}` for each
chosen eligible row; inventory hashes the manifest; approval hashes the config.
An existing output is never overwritten. A proposal is not human approval.
The externally reviewed policy must be pinned in the owner config before apply.

## Apply, replays and receipts

Hash/size mismatch, manifest conflict, unknown class, and policy-membership gaps
refuse the whole apply before ledger or CAS writes. The shared root lock spans
preflight and one `start_run` / `finish_run` lifecycle. Each eligible hash, in
sorted order, goes through the reconciler's guarded lock-held enrollment and
functional `accept` callbacks. Public reconciler methods still reject lock
contention exactly as before; no global reentrant-lock bypass was introduced.
Only `accept` advances preserve; substantive stages remain pending.

Completed local resolutions count as `already_adopted`; apply checks the pinned
policy/storage binding and asks the installed preserver to validate replay.
No new occurrence or resolution is appended on a completed replay. Enrollment
interrupted before acceptance is resumed as eligible, not falsely called done.
Runtime failures after successful preflight are not a batch rollback: committed
prefix work remains, the run is finalized failed, and a rerun resumes it.

Apply writes `<root>/legacy-adoption/<run_id>.json`, mode 0600, schema
`legacy-adoption-receipt-v1`: config/manifest/policy hashes, counts, and rows of
`{original_sha256,source_class,result}` only. No paths, filenames, or content.
Dry-run creates no receipt directory. Stdout is one counts-only JSON object,
including fixed refusal-code keys; OS/parser exception details never escape.
Exit 0 is successful processing, 3 is dry-run hash/size mismatch, 2 is refusal.
Known refusals include `legacy_source_path_forbidden`,
`legacy_source_root_not_admitted`, `legacy_source_class_unknown`,
`legacy_manifest_conflict`, `legacy_manifest_duplicate`, `legacy_manifest_invalid`,
`legacy_input_mode`, `legacy_manifest_hash_mismatch`, `legacy_policy_hash_mismatch`,
`legacy_adoption_disabled`, `legacy_policy_required`, and `capture_not_in_approved_inventory`.
Other failures use `legacy_adoption_refused`; machine-valid is review-required.

## Bootstrap order (first policy proposal)

A config with `"policy_sha256": null` and no `--policy` may run only with
`--write-policy-proposal` and without `--apply`. It does the same manifest,
ledger and source preflight as any dry run and writes the proposal; it never
writes the ledger, CAS, run rows or receipts and grants no adoption authority.

```sh
python3 -B -m campaign_tool.records.legacy_adoption \
  --root /private/processing-v1 --config /private/bootstrap.json \
  --manifest /private/manifest.tsv --write-policy-proposal /private/proposal.json
```

Every other mode with a null pin (no proposal flag, `--apply`, or a supplied
`--policy`), and any pinned config without `--policy`, refuses with
`legacy_policy_required` (exit 2). Order: (1) pin the manifest in a disabled
config with `policy_sha256` null; (2) write the proposal; (3) G approves the exact
policy; (4) pin its digest in the config and supply the policy file; (5) enable
only after explicit approval and rehearsal. The proposal's `approval_sha256`
hashes the original bootstrap config bytes; pinning or enabling later changes
those bytes and is not itself proof of approval.

## Source diagnostics (`--report-sources`)

`--report-sources` (with the usual `--root`, `--config`, `--manifest`) checks
**every physical manifest row**, in file order, against the apply-time source
rules and writes nothing: no ledger is opened (the root is not touched), no lock,
receipt, proposal or CAS write. It cannot be combined with `--apply` or
`--write-policy-proposal` (`legacy_config_invalid`, exit 2). A config with
`policy_sha256` null and no `--policy` may report; a pinned config still needs its
real `--policy`; ordinary null dry-run and every null apply still refuse with
`legacy_policy_required`. The whole manifest is validated first; a malformed one
refuses with the usual fixed code (exit 2) and no per-row output.

Output is one JSON line per row, `{"code", "row", "sha256_prefix"}` (1-based data
row number, first 12 hex of the manifest hash, never a path or content), then
`{"totals": {code: count}}` with positive counts only. Exit 0 when every row is
`ok`, 1 otherwise. Duplicate-hash rows are all reported; this is not the ranked
adoption selection. First failing rule wins, in this order:

1. `source_missing` - a component or file is absent or unavailable to this process
   (also open, read or filesystem-status failures; no error text is printed);
2. `source_symlink_component` - any component or the file is a symlink;
3. `source_not_regular` - a non-directory intermediate, or a FIFO/directory/device;
4. `source_not_owner`, 5. `source_multi_link`, 6. `source_mode_bits` (any group or
   other bit), 7. `source_not_readonly_fs` (filesystem not read-only);
8. `source_size_mismatch`, 9. `source_sha256_mismatch` (also unstable identity).

Example: `{"code": "source_not_readonly_fs", "row": 2, "sha256_prefix": "0123456789ab"}`.
The report is a **current observation**, not approval, policy eligibility, or proof
that a later apply will pass; apply keeps its own refusal codes.

## Attended T8 order (operator only; not executed by T6)

1. Build the real manifest from the admitted inventory, never archive hashing.
2. Freeze source mounts read-only; independently review manifest and source roots.
3. Pin the manifest in a private disabled config (null policy for the first
   proposal, see Bootstrap order); dry-run and inspect counts/proposal. Resolve
   discrepancies before any apply authority.
4. Review/approve the exact policy and enabled config; pin the installed engine.
5. Rehearse against an approved scratch restore, compare counts/receipts and
   preserve evidence, and obtain any retained live approval.
6. Only then perform the attended live apply; retain hashes and counts. No
   scope-writer, publication, substantive acceptance, or timer change is implied.
