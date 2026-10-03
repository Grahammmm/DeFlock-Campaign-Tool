# Unattended run safety (candidate, not activation)

`records run --unattended --root /srv/example/records --mail-config /srv/example/mail.json --max-originals-per-run 200 --json`

This is an opt-in generic engine mode. It never invokes configured models or direct image analysis, activates services, clears holds, decodes embedded RFC822, or marks deferred stages reviewed. Manual mode preserves its interfaces but now reports technical stage failures and intake gaps with nonzero exits. Explicit `advance_all([])` means no subjects.

## Fixed run outcomes

| Outcome | Ledger status | CLI exit | Slot health |
| --- | --- | --- | --- |
| Clean selected slice, including an empty slice | completed | 0 | ok |
| Under-threshold technical gaps or expected human review | completed_with_gaps | 3 | gaps |
| Unsupported/ambiguous input needing an adapter | held | 2 | held |
| Safety stop or unexpected technical exception | failed | 2 | failed |
| Interrupted invocation | interrupted | nonzero interruption | failed |
| Unknown terminal status | unchanged | not claimed successful | failed |

Only `completed` is healthy. `partial` is a gap, never healthy. Health and alerts also account for gaps and holds. A bounded clean slice can be completed while reporting explicitly deferred work; this is not full backlog completion.

## Budgets and stops

`RunSafetyPolicy(max_originals=200, max_fetches=200, seconds=2700)` accepts exact positive integers, not booleans, with upper bounds of 200, 200, and 2700 respectively. Fetch attempts are charged before fetching, globally across folders and local/IMAP inputs. Failures and replay fetches consume the budget. Advancement charges each distinct selected original once, before any stage work, with a separate maximum of 200. Excess attachments remain preserved backlog, not fictitiously completed work.

The failure ratio is separate for intake and advancement, global within each phase: stop when `failed * 10 > attempted`. Exactly 10% does not stop; a first-attempt technical failure does. The same trusted code on three distinct original hashes stops independently of ratio. Replays/copies/retries of the same hash do not inflate this threshold. The distinct-hash counters persist across runs. Fetch failures with no bytes have no invented original identity.

Unsupported formats are explicit adapter holds, not technical faults. They preserve bounded bytes before parsing and leave the failing UID checkpoint unchanged. A complete exporter receipt is never invented for a rejected message. Canonically preserved attachments with an unsupported format likewise prevent the containing UID's checkpoint advancement. No subsequent fetch or original advancement begins after a hold. Folder visibility continues without further fetching after a fetch-count stop, unless the time budget has expired.

Model configuration, privacy/redaction-stage faults hard-stop immediately. Expected ordinary review holds are reported as gaps, not parser faults. Privacy-stage rejection is conservatively a hard stop. Durable guard state is stored under a namespaced ledger metadata key. A crash leaving an active attempt creates an interruption hold. There is deliberately no automatic or CLI hold-clearing operation; operator repair and explicit owner acknowledgement require a separate reviewed procedure. Existing privacy stage holds are never cleared.

A monotonic budget is checked before each attempt and between stages. IMAP socket/extraction timeouts are bounded by remaining time. Checks are cooperative, not a claim that arbitrary in-process parser/preservation code is preemptible. Rendered (not installed) systemd services supervise the whole command with `TimeoutStartSec=2760` and `TimeoutStopSec=60`. They use unattended mode and the explicit 200-original bound. Deployment of those units is a separate owner gate.

## Reports and safe codes

Run reports retain `records-run-report-v1`, `intake`, `mailbox`, `subjects`, and stage counts, and add `status`, `exit_code`, `safety`, and `originals_deferred`. Unattended subject results contain fixed statuses/codes, never exception narratives. IMAP reports retain folder visibility plus preserved/failure counts, and expose `attempted`, `deferred`, `limit_reached`, `failure_codes`, and `stop_code`; each folder reports attempted/deferred work. Pending failed UIDs are distinguished from later deferred UIDs. Checkpoints are written only after retention, canonical preservation, and supported-format preflight.

Exact `mail_delta.Rejected` and `IntakeError` types may expose only explicitly reviewed fixed codes. Subclasses, unknown codes, arbitrary exceptions, and narrative strings map to `fetch_or_preserve_failed`. Known examples include `export_scope_mismatch`, `unsupported_rfc822_part`, `ambiguous_inline_body_part`, and bounded parser failures. New producer codes require reviewed consumer allowlist updates and joint rejection-code tests; an unknown code stays fail-closed and non-narrative until then.

## Composition and acceptance gates

This branch starts from main without merging or modifying the frozen credential-source PR. It supplies a global 200 fetch cap on that base. Nondefault `max_messages_per_run` from JSON requires the separately accepted credential-source configuration interface; that bound is consumed when present. Credential-source configuration itself is not duplicated here. The candidate has been reconciled onto main with that accepted interface.

Composition with MIME safety changes must test every newly approved stable rejection code end-to-end through intake, guard report, checkpoint behavior, CLI status, and health. Unknown producer codes stay generic technical faults, not silently approved adapter holds. This contract is an integration gate, not certification of an unmerged producer version.

The first-implementation failed receipts remain preserved. Six explicitly approved repairs cover recovery/run initialization, module import, checkpoint attempt ordering, deferred-input completion reporting, duplicate test inheritance, and the legacy unconfigured-folder report key. Subsequent interface, MSG, creation-path, and receipt-finalization repairs are explicitly approved. Their implementation and synthetic validation are in progress; acceptance is not claimed. Explicitly configured empty folders select no folders: archive visibility does not imply intake, fetching, or checkpoint advancement. Only a null folder selection means all available folders. Producer/consumer code integration is coordinated with the MIME author before shared-file writes.

## Local OCR and worker lifecycle boundary

`--ocr` is supported when the existing local OCR doctor resolves provisioned `tesseract` and `pdftoppm` tools and their required parser/runtime dependencies. This mode uses the existing bounded local PDF OCR path; unprovisioned direct image originals remain visual-review holds; provisioned image work is delegated to the existing extraction adapter without model calls or invented reviewed evidence. Missing prerequisites produce a durable `ocr_runtime_unavailable` hold before intake. No dependencies are installed by this command.

The 2700-second engine budget is container-local and monotonic, but cooperative. Remaining time bounds parser and initial socket timeouts; arbitrary in-process preservation work is not certified preemptible. The engine does not provide a worker cancellation API or certify that killing a Docker client terminates the container worker. Host integration must supervise inside the container, target the worker process group, enforce bounded termination/cleanup, and independently confirm worker exit. An interrupted active attempt becomes an owner-held interruption on the next invocation. Setup failures expose fixed-code JSON only; durable host launch/preflight failure receipts and post-run health/board adapters are outside this patch. Missing final receipts are not success evidence.

## Final receipt and parser contracts

Backend preservation uses the canonical account/folder/UID binding; exporter calls use its keyword-only mail-root/mailbox interface. Checkpoint helpers retain the accepted base connection and commit behavior. Raw retention and final report creation use explicit creation-path validation, exclusive no-follow file creation, owner-only output, and durability syncs; existing input/CAS validation is not weakened.

A final report is written and synced before the run's terminal ledger update. The terminal summary binds its exact report SHA-256. A missing or partial report produces fixed-code failure and never a completed ledger row. Consumers must require the terminal ledger status and report hash to agree, plus a successful worker-exit proof; a standalone JSON file written before that commit is not acceptance evidence. Persistent run holds are never automatically cleared.

MSG is eligible only when the existing extract-msg decoder imports successfully. Missing decoder readiness creates an adapter hold before checkpoint advancement, with raw/canonical preservation intact. This is not embedded RFC822 decoding. The provisioned image path likewise delegates to existing extraction/validation rather than claiming visual review or introducing a model.

## Fair finite advancement sweeps and mechanical counts

Default advancement walks a finite `(first_seen_at, sha256)` sweep. An owner-only,
atomically replaced `reports/unattended-advance-resume.json` stores its cursor and
fixed frontier. New arrivals wait for the next sweep rather than preempting its
remaining originals. An attempt, including a retriable failure, consumes one of
at most 200 advancement slots. No deferred original is marked complete, and no
IMAP checkpoint is changed by the cursor. Explicit subject lists retain their
caller-selected ordering; an empty list selects nothing.

Unchanged receipt-bound ordinary review holds are acknowledged without an
advancement attempt only after the ledger validator checks transition authority,
revision and prerequisite bindings. Their receipts and stage statuses remain
unchanged. Pending work and privacy holds do not receive this exemption. Each
invocation inspects at most 400 originals, advances the sweep across acknowledged
holds, and reports `review_holds_retained` separately from `originals_deferred`.
Malformed resume metadata fails closed; persistent safety holds are never cleared.

`end_to_end_complete` remains the integer mechanical seven-stage completion count
from the ledger in both manual and unattended reports, including mixed batches.
It is not a substantive model review or acceptance claim. Gap/hold/failure status,
nonzero exit, deferred counters and health remain independent and authoritative;
a completed email plus a held scan reports a count of one and unhealthy gaps.

## No-paid operational progress and substantive gaps

The default operational path needs no model/provider configuration or paid
service. Intake, extraction, catalog/detector work and mechanical receipts may
progress with models off. Both nominal and unattended reports conservatively
expose `substantive_complete: false` and `substantive_review_status: queued` for
nonempty corpora (`not_applicable` for an empty corpus). Operational completion,
mechanical integer counts, technical gap/hold status and substantive review are
separate contracts; `completed` does not certify source-level substantive review.

The label is not a newly implemented assignment queue or independent review.
Manual/independent source review remains queued through the existing private
workflow, with no automatic hold clearing. Later reviewed composition must retain
nested `analysis` metadata, including detector-only mode and its own
`substantive_complete: false`, as well as review-content/receipt provenance. This
safety branch does not configure a provider or edit the analysis implementation.
