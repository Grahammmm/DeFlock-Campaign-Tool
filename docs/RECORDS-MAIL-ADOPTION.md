# Mail-history adoption (`records adopt-mail`)

`campaign_tool/records/mail_adoption.py` adopts historical mail-export receipts
into the canonical records ledger, tagging each run `source=mail-history`. It
reuses the existing adapters instead of writing ledger rows itself:

- `intake.mail_delta` loads each receipt and checks its bytes and MIME
  (`load_receipt`, `source_path`).
- `runner.canonical_mail.CanonicalMailBackend` performs the writes:
  `start_run`, `preserve` per receipt, then `finish_run`. Canonical mail and
  occurrence identity checks, CAS storage and verification receipts all stay
  inside that backend.

The command only reads the export directory. It never connects to a mailbox,
runs an exporter, sends mail, or deletes or moves any file.

## Invocation

    python3 -m campaign_tool.records adopt-mail \
        --root <private processing root> \
        --mail-history <frozen export root containing receipts/> \
        [--config <owner-only adoption config>] [--apply]

- With no `--apply` the command does a dry run, which is the default. `--config`
  is optional for a dry run; when given, the report shows the gate decision that
  an apply would get.
- `--apply` requires a config that opens the gate (see below).
- The export root must not be inside the records root, and neither path may be
  a symlink.

Python API, used by the tests: `adopt(root, mail_root, *, config_path=None,
apply=False, backend_factory=None)` returns the report dict. A refusal raises
`AdoptionError(code)`.

## Adoption gate (configuration, never file deletion)

Config file (owner-only, at most 64 KiB, no extra keys allowed):

    {"schema": "mail-history-adoption-v1",
     "source": "mail-history",
     "adoption": "enabled" | "disabled",
     "disable_marker_sha256": null | "<64 lowercase hex>"}

The legacy disable marker `<root>/.adoption-disabled-v1.json` is only read and
hashed. It is never removed. `report.gate.state` is one of:

| state | meaning | apply |
|---|---|---|
| `disabled_no_config` | no `--config` given | refused |
| `disabled_by_config` | `adoption` is `disabled` | refused |
| `disabled_by_marker` | marker present but not pinned, or pinned to different bytes | refused |
| `marker_binding_mismatch` | config pins a marker that is no longer present (drift fails closed) | refused |
| `disable_marker_unreadable` | marker exists but cannot be read safely | refused |
| `enabled` | enabled, and no marker present or pinned | allowed |
| `enabled_marker_superseded_by_config` | enabled, and the pin matches the current marker bytes exactly | allowed |

Apply checks the gate a second time under the records-root writer lock. If the
gate has changed by then, apply is refused with `adoption_gate_changed`. A
refused apply writes nothing.

## Dry run: no persistent side effects

The dry run reads the ledger through a private snapshot reader instead of
`store.ledger(readonly=True)`, because a plain `mode=ro` open creates fresh
`-wal`/`-shm` sidecars next to a checkpointed WAL-mode ledger. If the WAL is
missing or empty, the ledger is opened with `immutable=1`. If the WAL is
non-empty (a live or crashed writer), it is opened with `mode=ro` so committed
frames are honoured; in that case the sidecars already exist. In both cases the
ledger and WAL identity (inode, size, mtime) must be unchanged after the read,
or the read fails with `ledger_changed_during_read`.

`test_dry_run_leaves_records_root_tree_identical` checks that every path, mode,
size, mtime and sha256 under the fixture base is unchanged by a dry run, both
before and after an apply.

## Idempotency

A receipt counts as adopted when a `mail_messages` row already exists for its
`(account, folder, uidvalidity, uid)` identity, which is the canonical mail
identity the backend enforces. Only pending receipts are passed to `preserve`.
If nothing is pending under the lock, apply returns without starting a run: no
`runs` row is written and the ledger bytes stay the same. Duplicate receipt
identities inside the export are reported as the gap
`duplicate_receipt_identity` and are not adopted twice.

Run rows use `run_id` `mail-history-<uuid4 hex>`, and
`summary.runner_summary.source` is `mail-history`. The runner outcome is
`completed` for a clean run, `completed_with_gaps` if any receipt failed, and
`failed` on an unexpected abort.

## Report counters (counts and hashes only; no paths, accounts or headers)

| key | meaning |
|---|---|
| `mode` | `dry-run` or `apply` |
| `source` | always `mail-history` |
| `receipts` / `attachments` | valid receipts found, and their attachment total (message parts minus the EML) |
| `already_adopted` | receipts whose identity already has a `mail_messages` row |
| `pending_messages` / `pending_attachments` | receipts and attachments not yet adopted |
| `predicted_binding_conflicts` | pending receipts that the backend will refuse because of an existing ledger original declaration (`original_scope_unresolved`, `original_scope_not_in_scope`, `original_bytes_conflict`, `original_storage_elsewhere`) |
| `gaps` | receipts that could not be loaded, by safe code |
| `gate` | gate decision: `state`, `open`, `marker_present`, `marker_sha256` |
| `adopted_messages` / `adopted_attachments` | written by this apply (always 0 in a dry run) |
| `failures` | per-receipt refusals during apply, by safe code |
| `run_id` | present only when apply started a run |
| `after` | apply only: `already_adopted` and `pending_messages` re-read under the lock after the run |

CLI exit codes: `0` clean; `2` refused (prints `{"status": "refused", "code": ...}`);
`3` the report has `failures` or `gaps`.

## Known limit

`preserve` refuses a receipt whose EML or attachment is already declared in the
ledger as an unresolved legacy original (`canonical_original_binding_conflict`).
This command does not resolve such declarations. They are reported in advance as
`predicted_binding_conflicts` and counted in `failures` on apply. Resolving them
needs the separately reviewed original-resolution contract
(`ledger/original_resolution.py`) and owner approval.

## Tests (synthetic only)

    python3 -B -m unittest tests.records.test_mail_adoption tests.records.test_mail_adoption_regression
