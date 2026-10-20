# Mail-history adoption (`records adopt-mail`)

`campaign_tool/records/mail_adoption.py` adopts historical mail-export receipts
into the canonical records ledger, tagging each run `source=mail-history`. It
reuses the existing adapters instead of writing ledger rows itself:

- `intake.mail_delta` loads each receipt and checks its bytes and MIME
  (`load_receipt`, `source_path`).
- `runner.canonical_mail.CanonicalMailBackend` performs the writes:
  `start_run`, `preserve` per receipt, then `finish_run` (and `recover_run` for
  a dead prior adoption run). Canonical mail and occurrence identity checks, CAS
  storage and verification receipts all stay inside that backend.

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
`AdoptionError(code)`. `blocking(report)` is the CLI's exit-3 predicate.

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

## Receipt classification (read-only)

Each valid receipt is classified against the ledger:

| state | condition | apply action |
|---|---|---|
| `new` | no `mail_messages` row for the `(account, folder, uidvalidity, uid)` identity | enrolled through `preserve` |
| `complete` | identity enrolled, bindings match exactly (below), and every declared original (EML and attachments) has preserve stage `done` | none (`already_adopted`) |
| `incomplete` | identity enrolled with matching bindings, but at least one declared original is not preserve `done` | resumed through the same idempotent `preserve` call |
| `identity_*` conflict | identity enrolled but the receipt declares different bytes or evidence | refused; nothing written; earlier evidence untouched |

Binding validation for an enrolled identity is the read-only equivalent of what
`CanonicalMailBackend.preserve` (re-enrollment) and its `_validator`
(preservation replay) require. The expected occurrence set is recomputed from
the receipt and the intake catalog (`intake.sqlite`, MIME locators) exactly as
the backend derives it: occurrence ids, kinds, CAS hashes, `source_ref`
(including `mime_path`), `parent_occurrence_id`, acquisition method and
evidence. The verification receipt is opened at its content-addressed path
`<root>/intake/verification-receipts/<receipt_sha256>` (derived from the
digest, never taken from stored evidence, no symlinks) and re-hashed. Once all
declarations agree, every canonical original bound to the identity (the EML or
native original, each attachment, each native item) is opened read-only at
`<root>/intake/blobs/<sha256>` (path derived from the digest, never from
evidence; no symlink on any component), stream-hashed and length-checked
against its declaration, and its file identity must stay stable across the
read. Nothing is repaired, re-sealed or re-chmod-ed. This applies to
`incomplete` identities too, so a damaged one is never resumed. First
failure wins:

| code | meaning |
|---|---|
| `identity_message_id_mismatch` | the identity's row does not carry the canonical message id, the id and identity resolve to different rows, or the row's identity columns differ |
| `identity_eml_changed` | `mail_messages.eml_sha256` differs from the receipt's EML hash |
| `identity_attachment_set_changed` | the identity's attachment occurrence hashes (as a multiset) differ from the declared attachments plus native items: added, removed or duplicated attachment |
| `identity_receipt_changed` | an occurrence's evidence `receipt_sha256` differs from the current receipt bytes |
| `identity_evidence_unreadable` | stored evidence cannot be parsed |
| `identity_intake_missing` / `identity_intake_occurrence_mismatch` | the intake catalog lacks, duplicates or disagrees with an occurrence the receipt declares |
| `identity_occurrence_duplicate` | the receipt would derive the same occurrence id twice |
| `identity_occurrence_mismatch` | an expected occurrence id is missing, an unexpected one is present (changed id, duplicate mail occurrence), or kind / CAS hash / acquisition method differs |
| `identity_locator_changed` | `source_ref` (account, folder, uidvalidity, uid, `mime_path`) differs |
| `identity_parent_changed` | `parent_occurrence_id` differs |
| `identity_verification_receipt_missing` / `identity_verification_receipt_changed` | the content-addressed verification receipt is absent, not a regular file (symlink) or its bytes no longer hash to the receipt digest |
| `identity_evidence_mismatch` | evidence keys differ from the backend's exact set, or `cas_sha256`, `bytes` or `native_item` disagree |
| `identity_verification_path_changed` | evidence `verification_receipt_path` is not the canonical content-addressed path |
| `identity_original_missing` / `identity_original_*` | an original has no effective declaration, or one the backend would refuse |
| `identity_original_blob_missing` | a bound canonical original is absent from the CAS |
| `identity_original_blob_unsafe` | the CAS entry is a symlink, not a regular file, or unreadable |
| `identity_original_blob_changed` | the stored bytes no longer hash to the digest or their length differs from the declared `bytes` (same-length corruption included) |
| `identity_original_blob_unstable` | the CAS file changed identity, size or timestamps while it was being verified |
| `identity_original_blob_invalid` | the digest or declared length is not well-formed (never opened) |
| `identity_mail_row_changed` / `identity_mail_row_unverifiable` | the `mail_messages` row differs from the projection the backend would write (headers, Message-ID, received date, provenance), or that projection cannot be recomputed |

Only `export_receipt_path` may differ (it must still be a string): the backend
itself accepts a superseded export path for the same receipt bytes.

`identity_receipt_changed` is deliberately strict: the backend binds occurrences
to the exact receipt bytes, so even a semantically equal but re-serialised
receipt is refused rather than silently accepted. An identically re-written
receipt (same bytes, different path) stays `complete`.

## Failure after enrollment (durable incomplete state)

`CanonicalMailBackend.preserve` commits `mail_messages` and occurrences in one
transaction and only then runs installed preservation. An exception or crash
between the two leaves an enrolled identity with pending or in-progress preserve
state. That state is never hidden:

- every later dry run and apply reports it as `incomplete_messages` (and inside
  `pending_messages`), and the CLI exits `3`;
- apply resumes it: the backend re-verifies the receipt, matches the existing
  rows exactly, and runs only the unfinished preservation;
- a prior adoption run left `running` by a dead process is reported as
  `unfinalized_runs`; apply finalizes it as `interrupted` through
  `CanonicalMailBackend.recover_run` (reported in `recovered_runs`). This is
  safe because only this command creates `mail-history-` runs and it holds the
  exclusive records-root lock for a run's whole life;
- if the dead process held a live preserve lease (crash after claim), the stage
  engine refuses the resume until the lease expires (default 300 s). Apply
  reports a failure and `after.incomplete_messages`, exit `3`; a later apply
  after expiry completes it.

## Dry run: no persistent side effects

The dry run never uses `store.ledger(readonly=True)`: a `mode=ro` open creates
`-shm` (and rebuilds it from a crashed writer's WAL). Instead:

- WAL absent or empty, no rollback journal: the ledger is read with
  `immutable=1`, which touches no file;
- WAL non-empty (live writer, or a crash not yet recovered), or a rollback
  journal present: refused with `ledger_wal_not_checkpointed` before anything is
  opened (exit `2`);
- the identity (inode, size, mtime, mode) of the ledger and its `-wal`, `-shm`
  and `-journal` must be unchanged afterwards, otherwise
  `ledger_changed_during_read`.

The intake catalog `<root>/intake/intake.sqlite` (read only when an enrolled
identity must be validated) follows the same rules with the codes
`intake_wal_not_checkpointed`, `intake_changed_during_read` and
`intake_not_owner_only`.

Apply holds the records-root lock and, if the WAL is non-empty, first opens and
closes the ledger read-write so SQLite recovers committed frames and checkpoints
(`ledger_wal_recovered: true`). If the WAL is still non-empty afterwards, a
writer outside the lock is live and apply refuses with `ledger_active_writer`
(`intake_active_writer` for the intake catalog).

## Idempotency

Only eligible `new` and `incomplete` receipts are passed to `preserve`; coded
gaps (below) never are. If nothing is
pending under the lock and no dead run needs finalizing, apply returns without
starting a run: no `runs` row is written and the records-root tree stays
byte-identical. Duplicate receipt identities inside the export are reported as
the gap `duplicate_receipt_identity` and are not adopted twice.

Run rows use `run_id` `mail-history-<uuid4 hex>`, and
`summary.runner_summary.source` is `mail-history`. The runner outcome is
`completed` for a clean run, `completed_with_gaps` if any receipt failed or any
coded gap was reported, and `failed` on an unexpected abort.
`runner_summary` carries `coded_gap_messages` and `coded_gaps` in every case.

## Coded gaps (G decision 2026-10-19, T4 option c)

A `new` receipt that the canonical backend cannot adopt under its current
contract is a coded gap. It is classified read-only in the backend's own order:

1. legacy receipts (no `schema` key): the export EML is opened without
   following symlinks, bounded by and hashed against its declared size and
   digest, and parsed with the same `mail_delta.mime_candidates` check the
   backend runs on the staged copy. A rejection gives its own code (for example
   `unsupported_rfc822_part`); an unreadable or changed source gives
   `source_unreadable`, `source_size_mismatch` or `source_hash_mismatch`;
2. the original-binding precondition for every original of the receipt:
   `original_scope_unresolved`, `original_scope_not_in_scope`,
   `original_bytes_conflict` or `original_storage_elsewhere`. These are the
   read-only prediction of the backend's `canonical_original_binding_conflict`.

Coded gaps are never passed to `preserve`, so they cause no CAS blob, intake
catalog row, verification receipt or ledger write. They are classified afresh
on every run, counted in `coded_gaps` by code in every dry-run, apply, `after`
and run summary, and never dropped: `new_messages = eligible_new_messages +
coded_gap_messages` always holds.

`ACCEPTED_GAP_CODES` holds the classes G accepted as reported gaps:
`original_scope_unresolved` (an EML already declared as an unresolved legacy
original) and `unsupported_rfc822_part`. They do not block (exit `0`). Any
other code appears in `unaccepted_coded_gaps` as well and blocks (exit `3`).
Resolving the accepted classes needs a separately reviewed contract
(follow-up T4B); this command never rewrites a legacy declaration.

## Report counters (counts and hashes only; no paths, accounts or headers)

Invariant: `receipts == already_adopted + pending_messages + coded_gap_messages + sum(identity_conflicts)`.

| key | meaning |
|---|---|
| `mode` | `dry-run` or `apply` |
| `source` | always `mail-history` |
| `receipts` / `attachments` | INVENTORY: every valid receipt in the export and its attachment total, whatever its ledger state |
| `already_adopted` | `complete` receipts |
| `new_messages` / `new_attachments` | `new` receipts (not yet enrolled) = eligible + coded gaps |
| `eligible_new_messages` | `new` receipts with no coded gap |
| `coded_gap_messages` / `coded_gap_attachments` | `new` receipts that are coded gaps |
| `coded_gaps` | coded gaps by stable code |
| `unaccepted_coded_gaps` | the subset of `coded_gaps` outside `ACCEPTED_GAP_CODES` (blocking) |
| `incomplete_messages` | `incomplete` receipts (enrolled, preservation unfinished) |
| `pending_messages` / `pending_attachments` | adoptable work remaining: eligible `new` + `incomplete` |
| `identity_conflicts` | refused enrolled identities, by `identity_*` code |
| `unfinalized_runs` | `mail-history-` runs still `running` (a dead prior apply) |
| `gaps` | receipts that could not be loaded, by safe code |
| `gate` | gate decision: `state`, `open`, `marker_present`, `marker_sha256` |
| `adopted_messages` / `adopted_attachments` | `new` receipts enrolled and preserved by this apply (0 in a dry run) |
| `resumed_messages` | `incomplete` receipts completed by this apply |
| `failures` | per-receipt refusals during apply, by safe code |
| `run_id` | present only when apply started a run |
| `recovered_runs` | apply only, when dead runs existed: `{finalized|already_finalized|recovery_refused: n}` |
| `ledger_wal_recovered` | apply only, when a non-empty WAL was recovered under the lock |
| `after` | apply only: `already_adopted`, `new_messages`, `incomplete_messages`, `pending_messages`, `coded_gap_messages`, `coded_gaps`, `unaccepted_coded_gaps`, `identity_conflicts`, `unfinalized_runs` re-read under the lock after the run |

CLI exit codes: `0` clean (accepted coded gaps may be present and are
reported); `2` refused (prints `{"status": "refused", "code": ...}`);
`3` blocking: `failures`, `gaps`, `identity_conflicts` or
`unaccepted_coded_gaps` present; a dry run with
`incomplete_messages` or `unfinalized_runs`; an apply whose `after` still has
pending, incomplete, conflicting, unaccepted-gap or unfinalized items; or
`recovery_refused`. Zero adopted messages alone never means success.

## Known limit

`preserve` refuses a receipt whose EML or attachment is already declared in the
ledger as an unresolved legacy original (`canonical_original_binding_conflict`),
and a legacy receipt whose EML contains a `message/rfc822` part
(`unsupported_rfc822_part`). This command does not resolve either. Per G's
decision they are accepted coded gaps (above), reported and never adopted.
Resolving them needs a separately reviewed contract (T4B, or the
original-resolution contract in `ledger/original_resolution.py`) and owner (G)
approval.

## TEST acceptance runbook

This is the exact procedure for the local-only acceptance worker. It may only
be executed against an exact head that has passed an independent
different-model review. G decided on 2026-10-19 (T4 option c): adopt everything
adoptable; the receipts whose EMLs are already declared as unresolved legacy
originals and the `message/rfc822` receipt are accepted coded gaps (115
expected on TEST); TEST is restored from the pre-apply backup before the run.
The final gate/config state still comes only from G's written decision (step
6). B1 limits are absent from the supplied plan; none are invented here. Rule:
at most two unsuccessful attempts in total after the first failure, then stop
and escalate through the prescribed route. Never hand-edit the ledger or intake
catalog, never delete the marker, never follow or write live-root storage
paths. Keep all detailed output local; report aggregates only.

Acceptance target: inventory `receipts=154`; every receipt is either adopted
or an accepted coded gap; exactly `GAPS` coded gaps (115 per G's decision),
all in `ACCEPTED_GAP_CODES`; the TEST ledger ends with exactly
`TOTAL - GAPS` `mail_messages` rows (39 on the restored TEST), all of them
identities from the frozen export. Only a written G decision changes `TOTAL`
or `GAPS`; without one, any other count is a stop, not a pass.

Variables (set locally; all paths must resolve inside the TEST copy):

    TEST=<approved TEST copy root, resolved locally by the acceptance worker>   # verify actual layout first
    ROOT="$TEST/processing-v1"
    EXPORT="$TEST/mail-history"
    TEST_CONFIG=<owner-only mail-history-adoption-v1 config, approved locally>
    LEDGER="$ROOT/ledger.sqlite"
    INTAKE="$ROOT/intake/intake.sqlite"
    EV=<private local evidence dir outside $TEST, mode 0700>
    TOTAL=154                     # change ONLY on a written G decision; record its reference in $EV
    GAPS=115                      # accepted coded gaps per G's 2026-10-19 decision (option c)
    G_GATE_STATE=<final gate.state from G's written decision>
    G_CONFIG_SHA256=<sha256 of the final config G approved, or 'none' if G decided no config>
    Q() { sqlite3 "file:$LEDGER?mode=ro&immutable=1" "$1"; }   # only valid when WAL is empty
    J() { python3 -B -c 'import json,sys
    v=json.load(open(sys.argv[1]))
    for k in sys.argv[2].split("."): v=v[k]
    print(json.dumps(v, sort_keys=True))' "$1" "$2"; }          # print one report key
    # No sqlite3 CLI? Use python3's sqlite3 module with the same URI (uri=True), read-only.

If `G_GATE_STATE` and `G_CONFIG_SHA256` are not available from a written G
decision, stop before step 0: step 6 cannot be evaluated.

Step 0 -- preconditions (any failure: stop, no retry, escalate):

    test "$(stat -c %u "$LEDGER")" = "$(id -u)" && test "$(stat -c %a "$LEDGER")" = 600
    test ! -s "$LEDGER-wal" && test ! -e "$LEDGER-journal"     # quiesced, checkpointed
    test ! -s "$INTAKE-wal" && test ! -e "$INTAKE-journal"
    ! fuser "$LEDGER" "$INTAKE" 2>/dev/null                     # no live writer
    git -C <engine checkout> rev-parse HEAD                     # must equal the reviewed head
    grep -rIl -- <live root prefix> "$ROOT" "$EXPORT" | wc -l    # expect 0 live-root references (G-approved snapshot only)
    Q "PRAGMA quick_check"                                      # expect: ok
    Q "PRAGMA foreign_key_check" | wc -l                        # expect 0

Step 1 -- baseline (record locally):

    M0=$(Q "SELECT count(*) FROM mail_messages")
    R0=$(Q "SELECT count(*) FROM runs")
    Q "SELECT status,count(*) FROM runs WHERE run_id LIKE 'mail-history-%' GROUP BY status" > "$EV/runs0"
    sha256sum "$ROOT/.adoption-disabled-v1.json" > "$EV/marker0" 2>/dev/null || echo absent > "$EV/marker0"
    manifest() { (cd "$1" && find . -printf '%p %y %m %s %T@\n' | LC_ALL=C sort; \
                  find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum); }
    manifest "$TEST" > "$EV/tree0"; manifest "$EXPORT" > "$EV/export0"

Step 2 -- dry run (exit must be 0):

    python3 -B -m campaign_tool.records adopt-mail --root "$ROOT" --mail-history "$EXPORT" \
        --config "$TEST_CONFIG" > "$EV/dry.json"; echo "exit=$?"
    manifest "$TEST" > "$EV/tree1"; cmp "$EV/tree0" "$EV/tree1"       # full-tree non-mutation

Required dry-run values:

- INVENTORY (every valid receipt, whatever its state): `receipts=$TOTAL` (154),
  `attachments=82`.
- `gaps={}`, `identity_conflicts={}`, `incomplete_messages=0`,
  `unfinalized_runs=0`, `unaccepted_coded_gaps={}`.
- `coded_gap_messages = $GAPS` (115) and the values of `coded_gaps` sum to it;
  every key is in `ACCEPTED_GAP_CODES` (`original_scope_unresolved`,
  `unsupported_rfc822_part`). Record `coded_gaps` locally.
- `gate.open=true`, and `gate.marker_sha256` matches `$EV/marker0`.
- NEW/ELIGIBLE counts, distinct from inventory: record `A0=already_adopted`
  and `N0=new_messages`; require `A0 + N0 = $TOTAL`,
  `eligible_new_messages = N0 - $GAPS` and `pending_messages = N0 - $GAPS`.
  Call `E0 = N0 - $GAPS`.
- Require `M0 = A0`: every existing `mail_messages` row is an export identity
  whose canonical bindings were fully validated. `M0 > A0` means the ledger
  holds rows unrelated to this export; stop and escalate, never accept them
  silently. Do not assume `N0 = 154`: the original freeze already contained
  adopted identities (three per the upstream handoff; after the restore expect
  `A0=3`, `N0=151`, `E0=36`).

Any deviation: stop and escalate; do not apply.

Step 3 -- apply (exit must be 0):

    python3 -B -m campaign_tool.records adopt-mail --root "$ROOT" --mail-history "$EXPORT" \
        --config "$TEST_CONFIG" --apply > "$EV/apply1.json"; echo "exit=$?"
    manifest "$TEST" > "$EV/tree2"

Required in both branches: `failures={}`, `resumed_messages=0`,
`adopted_messages + coded_gap_messages = N0` with `coded_gap_messages = $GAPS`,
`after = {already_adopted: $TOTAL - $GAPS, new_messages: $GAPS,
incomplete_messages: 0, pending_messages: 0, coded_gap_messages: $GAPS,
coded_gaps: <as in step 2>, unaccepted_coded_gaps: {}, identity_conflicts: {},
unfinalized_runs: 0}`, and no `recovered_runs` or `ledger_wal_recovered` key.

Branch on `E0`:

- `E0 > 0`: `adopted_messages = E0` and a `run_id` key. Expect `R1 = R0 + 1`,
  and the `mail-history-` status counts equal `runs0` plus exactly one
  `completed_with_gaps` (or `completed` when `GAPS = 0`). Its
  `runner_summary.coded_gaps` equals the step-2 `coded_gaps`.
- `E0 = 0`: the command does not start a run. Require `adopted_messages = 0`,
  no `run_id` key, `R1 = R0`, `runs1` identical to `runs0`, and
  `cmp "$EV/tree1" "$EV/tree2"` succeeds (nothing written).

SQL (both branches):

    test ! -s "$LEDGER-wal" && test ! -s "$INTAKE-wal"
    Q "SELECT count(*) FROM mail_messages"            # = $TOTAL - $GAPS exactly (39), and = M0 + E0
    R1=$(Q "SELECT count(*) FROM runs")               # = R0 + 1 if E0 > 0; = R0 if E0 = 0
    Q "SELECT status,count(*) FROM runs WHERE run_id LIKE 'mail-history-%' GROUP BY status" > "$EV/runs1"
    Q "SELECT count(*) FROM runs WHERE run_id LIKE 'mail-history-%' AND (status='running' OR ended_at IS NULL)"   # 0
    Q "SELECT count(*) FROM stage_state s WHERE s.stage='preserve' AND s.status!='done' AND s.original_sha256 IN
       (SELECT original_sha256 FROM occurrences WHERE acquisition_method='verified_mail_export')"   # 0
    Q "PRAGMA quick_check"; Q "PRAGMA foreign_key_check" | wc -l   # ok; 0

Step 4 -- zero-pending rerun (exit must be 0):

    python3 -B -m campaign_tool.records adopt-mail --root "$ROOT" --mail-history "$EXPORT" \
        --config "$TEST_CONFIG" --apply > "$EV/apply2.json"; echo "exit=$?"
    manifest "$TEST" > "$EV/tree3"; cmp "$EV/tree2" "$EV/tree3"       # nothing written

Required: no `run_id`, `adopted_messages=0`, `resumed_messages=0`,
`pending_messages=0`, `incomplete_messages=0`, `already_adopted=$TOTAL - $GAPS`,
`coded_gap_messages=$GAPS` (still reported, same `coded_gaps`),
`unaccepted_coded_gaps={}`, `identity_conflicts={}`, `failures={}`, `gaps={}`;
SQL counts identical to step 3 (`mail_messages = $TOTAL - $GAPS`, `runs = R1`).
Zero adopted alone does not pass: every step-2/3 condition must also hold.

Step 5 -- source integrity:

    manifest "$EXPORT" > "$EV/export1"; cmp "$EV/export0" "$EV/export1"
    sha256sum "$ROOT/.adoption-disabled-v1.json" | cmp - "$EV/marker0"   # or still absent

Step 6 -- final configuration and gate state (G's written decision only):

Put the final config in place only as G's decision states (for example
re-disabling adoption by replacing the owner-only config file). Never touch the
marker. Then verify with a dry run, which must not mutate anything:

    python3 -B -m campaign_tool.records adopt-mail --root "$ROOT" --mail-history "$EXPORT" \
        ${G_FINAL_CONFIG:+--config "$G_FINAL_CONFIG"} > "$EV/final.json"; echo "exit=$?"   # 0
    manifest "$TEST" > "$EV/tree4"; diff "$EV/tree3" "$EV/tree4"   # empty, except the config file's own lines if G placed it inside $TEST
    J "$EV/final.json" gate.state                                  # = "$G_GATE_STATE"
    J "$EV/final.json" already_adopted; J "$EV/final.json" pending_messages   # $TOTAL - $GAPS; 0
    J "$EV/final.json" coded_gap_messages                          # $GAPS
    test "$(stat -c %a "$G_FINAL_CONFIG")" = 600 && sha256sum "$G_FINAL_CONFIG"   # = $G_CONFIG_SHA256 (skip if 'none')
    sha256sum "$ROOT/.adoption-disabled-v1.json" | cmp - "$EV/marker0"   # marker unchanged (or absent)

`G_FINAL_CONFIG` is the config path G's decision names (unset when G decided no
config). A gate state or config hash other than G's decision is a failure; the
acceptance worker never chooses one.

Report back aggregates only (counts, exit codes, cmp results, the final gate
state, sha256 of each manifest file and of the final config), never paths,
receipts, headers or ledger rows. If apply exits `3`, do not "fix" anything:
rerun at most as permitted above only when the report shows a transient code
(e.g. a live lease after a crash); otherwise stop and escalate with the safe
codes.

## Scripted TEST verification (`scripts/verify_adoption_test.py`)

The local-only acceptance worker can run steps 0 to 5 above as one standard-library
script inside the Flock container. It decides no policy, never writes or
changes the config, and never runs on anything except a `*-TEST` copy:

    python3 -B <engine>/scripts/verify_adoption_test.py \
        --test-root "$TEST" --config "$TEST_CONFIG" --expect-coded-gaps "$GAPS" \
        --receipt <new file outside $TEST> [--dry-run-only]

- `--test-root` must name a directory ending in `-TEST`. `processing-v1/` and
  `mail-history/` are fixed children of it and cannot be overridden.
- The effective uid must own the ledger, and the config must already exist and
  be owner-only.
- The engine is the checkout that contains the script. `--engine` overrides it.
- The expected totals default to 154 messages and 82 attachments, and
  `--expect-coded-gaps` defaults to 0; on TEST pass `115` per G's decision.
  Unaccepted coded gaps always fail the dry-run checks.
- The script stops before apply if any step-2 precondition fails, and stops
  before the rerun if any step-3 check fails.

Exit codes: `0` every check passed and the receipt was written, `1` a check
failed or an operational error occurred (the receipt shows `stopped_at` and
`failed_checks`), `2` refused before running anything (this includes a receipt
directory that is not writable), `3` every check passed but the receipt file
could not be written.

Filesystem, SQLite and subprocess failures are caught at the CLI boundary. The
receipt then has `status: error`, the phase in `stopped_at` and a fixed
`error.code` such as `os_error:eacces` or `sqlite_error:sqlite_notadb`. No
exception message, path or traceback is printed, because a file name in the
export can itself be record text. A failure before the apply phase never runs
apply. An unreadable directory fails the tree digest instead of being skipped.
If the receipt itself cannot be written, the same sanitized JSON goes to stdout
with `receipt_write_error`.

The receipt contains only integers, booleans and fixed codes: check names, the
gate state and safe error codes. It holds the counts from the dry run, the
apply and the rerun, the SQL counts (`mail_messages`, `runs`, mail-history run
statuses, open runs, unfinished preserve stages, quick_check, foreign keys),
`new_on_rerun`, and non-mutation checks for the dry run, the zero-new branch,
the rerun, the export and the marker. It contains no paths, headers or record
text. Step 6 (the final config and gate state) remains a manual G decision.
Synthetic tests: `tests.records.test_verify_adoption_test_script`.

## Tests (synthetic only)

    python3 -B -m unittest tests.records.test_mail_adoption \
        tests.records.test_mail_adoption_regression tests.records.test_mail_adoption_recovery \
        tests.records.test_mail_adoption_bindings tests.records.test_mail_adoption_coded_gaps \
        tests.records.test_verify_adoption_test_script
