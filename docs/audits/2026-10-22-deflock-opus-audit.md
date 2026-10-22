# Records pipeline audit — 2026-10-22

**Scope:** `campaign_tool/records/**` at `main` @ `75494fa`, plus `docs/ROADMAP.md`, `AGENTS.md` and `tests/records`.
**Method:** We read the code and checked existing test coverage. Every finding below was reproduced with synthetic data in a temp directory. No real records, credentials or network services were used.
**Status:** This is an audit of the code only. It is not a legal, privacy or capacity review, and nothing in it means a finding or package has been reviewed.

## Summary

- **Critical (1):** IMAP login does not check the server's TLS certificate, so the mailbox password can be captured.
- **High (2):**
  - Scope quarantine spreads through shared files into unrelated evidence.
  - Any non-blank vision-model reply, including a refusal, is accepted as the page's OCR text.
- **Medium (10)**, **Low (8)**, and a list of areas that were checked and found sound.
- **No path was found that sends email or publishes anything outward by accident.**

Six findings have failing tests in `tests/audit_repro/test_audit_repro.py`. They are skipped by default so the normal suite stays green. To run them:

```
AUDIT_REPRO=1 python3 -B -m unittest tests.audit_repro.test_audit_repro -v
```

On `75494fa` they give `Ran 6 tests … FAILED (failures=5, errors=1)`. Each one will pass once its fix lands.

## Findings

### Critical

**C1. IMAP over TLS does not verify certificates or hostnames.**
- **Where:** `campaign_tool/records/intake/imap_intake.py:222`
- **Impact:** `imaplib.IMAP4_SSL(...)` is called without `ssl_context`. Python's imaplib then falls back to `ssl._create_stdlib_context`, which is the *unverified* context (`ssl.py:805`). Anyone who can intercept the connection receives the real username and password at LOGIN. They can also feed the pipeline fake "original" mail.
  - A live repro used a self-signed `CN=attacker.invalid` server. Login succeeded, and the server captured `LOGIN synthetic-user "synthetic-secret"`.
  - The portal transport (`portal/https_transport.py`) does this correctly.
- **Fix:** Pass `ssl_context=ssl.create_default_context()`. Consider refusing `ssl: false` unless it is overridden explicitly.
- **Test:** `ImapTlsVerification`
- **Confidence:** High

### High

**H1. Scope quarantine walks the edge graph in both directions without limit.**
- **Where:** `intake/scope_quarantine.py:24-27`, then the moves and deletes at `:39-48`
- **Impact:**
  - The loop adds every parent *and* every child of each hash, then repeats until nothing new is added.
  - Walking up to the container that directly holds the personal record looks intended. But one identical shared child (a logo, a signature image, an empty file) then links in every other container that holds the same bytes, and all of their members.
  - In the repro, quarantining `personal.zip` also quarantined an unrelated `records.zip`:
    - its blob was moved out of the active store;
    - it was added to `scope_exclusions` for good;
    - later inventories drop it.
  - The run still reports that no originals were modified. This is a silent loss of coverage.
- **Fix:**
  - Walk downward only, plus the direct containers of the named paths.
  - Quarantine a shared child only when *all* of its parents are quarantined.
  - Never move a hash that still has a parent outside the quarantine set.
- **Test:** `QuarantineOverreach`
- **Confidence:** High

**H2. Model output is accepted as resolved page text with no quality gate.**
- **Where:**
  - `ocr_stage.py:853` (`if vision.strip():`)
  - `ocr_stage.py:402` (`THINK_BLOCK` only strips a *closed* `<think>…</think>`)
  - the same pattern in `extract/vision.py:276`
- **Impact:**
  - A refusal ("I'm sorry, I can't read this image"), a reply cut off mid-reasoning (an unclosed `<think>`), or `"."` is stored as the page text.
  - The document is marked `ocr_complete` with 0 unresolved pages, and the result is cached.
  - Tesseract output with the same text *is* flagged `ocr_low_quality`.
  - This is the main place where a model's output can be treated as fact.
- **Fix:**
  - Run `Quality.check` on vision text too.
  - Reject an unclosed `<think>` and any `finish_reason` other than `stop`.
  - Map refusal or empty replies to `vision_empty` (unresolved).
- **Tests:** `VisionOutputAcceptedAsText` (2 cases)
- **Confidence:** High

### Medium

**M1. One symlinked `.eml` in the inbox fails the whole manual run.**
- **Where:** `run.py:203` and `:210-211`
- **Impact:**
  - The file list uses `is_file()`, which follows symlinks.
  - `secure_open` then refuses the symlink with `ELOOP`. But it sits *outside* the per-message `try`, so `ingest_inbox` raises and the run is marked `failed/run_failed`.
  - This repeats on every run until someone removes the file. The comment at `:222` says this should not happen ("one bad message never stops the run").
- **Fix:** Move the open and read inside the `try`, or skip `is_symlink()` entries and record them as failures.
- **Test:** `InboxSymlinkAbortsRun`
- **Confidence:** High

**M2. The `digest_gates` stage drops `in_progress` slots from its count and reports `ok`.**
- **Where:** `run_stages.py:860-868`
- **Impact:**
  - The input count is pending + blocked + done only. With 15 gated rows, 5 of them `in_progress`, the stage reports `input: 10` and `ok`, exit 0.
  - A related gap: `ledger/stages.py:445` `recover_abandoned_leases` only resets leases that have not expired (`expires_at>now`). Slots whose leases expired after a crash can therefore stay `in_progress`. We traced this in the code but did not run it.
- **Fix:** Count `in_progress` and any unknown status, and return `warn` with `GATE_IN_PROGRESS`.
- **Test:** `DigestGatesDropsInProgress`
- **Confidence:** High for the miscount

**M3. The scheduled health check is skipped exactly when a run fails.**
- **Where:** `schedule.py:226-227`
- **Impact:**
  - `health --record` runs as `ExecStartPost=` on a `Type=oneshot` unit, and systemd runs `ExecStartPost` only when `ExecStart` succeeds.
  - Unattended runs exit 2 when failed or held and 3 when there are gaps (`unattended.py:493`).
  - So failure and gap alerts are never recorded. `test_schedule_health.py:180` only checks that the line is present.
- **Fix:** Use `ExecStopPost=`. Alternatively, add `SuccessExitStatus=2 3` and make sure the health check reads the exit status.
- **Confidence:** High. This rests on documented systemd behaviour; it was not run under systemd.

**M4. The `mail-wire-receipt-v2` format probe follows symlinks, accepts any path and reads whole files.**
- **Where:** `intake/eml_export.py:203` and `:209` (`head_of(item, secure=False)` → `(root / item["path"]).read_bytes()[:16384]`)
- **Impact:**
  - Symlinks are followed, and absolute or `..` paths in the receipt are honoured.
  - The whole file is read before it is cut to 16 KiB. A symlink to `/dev/zero` raised `MemoryError` in the repro.
  - The format is classified from bytes that are never checked against the recorded hash.
  - This path is reached from `unattended.py:183` and `run.py:484`.
- **Fix:** Use the secure branch (`folder.secure_open` + `read(16384)`) together with `mail_delta.source_path`.
- **Confidence:** High

**M5. Retained-units reader and writers disagree on the CAS layout.**
- **Where:**
  - The reader expects `evidence/cas/<sha[:2]>/<sha>`: `retained_units.py:147`, `:157-160`
  - Every writer stores `evidence/<sha>`: `extraction_ledger.py:142`, `:157`, `:363`; `extraction_validation.py:494`; `catalog_wp4_support.py:112`
- **Impact:**
  - `read_current_units` refuses every real enrolled import with `unit_provenance_mismatch`. That means `grounded_evidence` and the search index can never read real data.
  - It fails closed, but the feature does not work. No test runs enroll and then read.
- **Fix:** Use one layout, and add an end-to-end enroll-then-read test.
- **Confidence:** High

**M6. `backup_v2` refuses WAL ledgers when run as root.**
- **Where:** `recovery/backup_v2.py:279-282` → `recovery/backup.py:135` (read-only open) → `fingerprint()` at `backup.py:37-38`, which includes ctime
- **Impact:**
  - As root, SQLite changes the ownership of the source `-wal` file during the read-only snapshot, which bumps its ctime.
  - The check at `backup_v2.py:359` then raises `scope_changed_during_backup` every time.
  - Running the existing suite as root shows this: 3 failures and 2 errors in the WAL tests of `tests/test_records_backup_v2.py`.
- **Fix:** Leave ctime out of the comparison for sidecars, snapshot from a private copy, or refuse root up front with a clear reason code.
- **Confidence:** High

**M7. Rollback re-stages content that is no longer approved or privacy-cleared.**
- **Where:** `publish.py:473-495`
- **Impact:**
  - Rollback restores an old release with no owner identity and no check that its approval or privacy receipt is still current.
  - If v1 was corrected *because of* a privacy problem, rollback puts the v1 bytes back in staging.
  - The impact is limited to staging, since publishing still re-checks. Note that `tests/records/test_publish.py:122` asserts this behaviour, so it is a design gap rather than an accident.
- **Fix:** Require an owner identity and a bound authority that is still current, or allow only a withdrawal.
- **Confidence:** High

**M8. Policy reconciliation never checks fact values against their citations.**
- **Where:** `understanding/reconcile.py:39` checks that the cited spans exist; `:69` then classifies on the free-form `value`
- **Impact:**
  - Any model-supplied value plus any valid but unrelated span produces `potential_conflict` or `no_conflict_observed`.
  - The output is still labelled as needing review, but it presents ungrounded values as if they were evidenced.
- **Fix:** Require the value to appear in a cited quote. Otherwise classify as `not_assessed` with `fact_value_unbound`.
- **Confidence:** High

**M9. The OCR page count comes from the tool's sidecar, not the PDF.**
- **Where:** `ocr_stage.py:830` onward (`page_count = len(texts)`)
- **Impact:** A short sidecar makes the missing pages disappear from the count: a 3-page PDF with a 1-page sidecar is reported `ocr_complete` with `page_count 1`.
- **Fix:** Count pages independently with pypdf and fail closed on a mismatch.
- **Confidence:** High that the check is missing. This was shown only with a stub tool.

**M10. The vision adapter's timeout leaves its child processes running.**
- **Where:** `extract/vision.py:255` uses `subprocess.run(timeout=…)`
- **Impact:** A wrapper adapter's grandchild survives after the receipt says `vision_timeout`. The other runners already use `killpg`.
- **Fix:** Use `start_new_session=True` + `killpg`, or reuse `ocr_stage.run_group`.
- **Confidence:** High

### Low

- **L1. IMAP loads a whole message into memory before the size check.**
  - **Where:** `intake/imap_intake.py:301-307`
  - **Impact:** A 300 MiB message added about 600 MiB of peak memory before it was rejected.
  - **Fix:** Check `RFC822.SIZE` first.
- **L2. The legacy EML export trusts existing part files without comparing bytes.**
  - **Where:** `intake/eml_export.py:133`, `:144`
  - **Impact:** A tampered file gets a "complete" receipt. It is caught later as a hash mismatch, but the message is then stuck for good.
  - **Fix:** Use `_ensure_private`.
- **L3. A cached OCR result is served when the source changes but keeps the same size.**
  - **Where:** `ocr_stage.py:801` compares only `st_size`
  - **Impact:** A swapped original reports `cached`/`ok`.
  - **Fix:** Re-hash the source on a cache hit.
- **L4. The public scan is easy to bypass.**
  - **Where:** `public_scan.py:11`, `:24`, `:31`
  - **Impact:** These all scan clean:
    - path names in other letter case: `Private/`, `Originals/`, `.ENV`
    - key formats: `ENCRYPTED`, `DSA` and `PGP` private keys
    - Brevo `xsmtpsib-` keys and unquoted `password = …`
    - fragments split across a line break
  - **Fix:** Match paths case-insensitively, use `BEGIN [A-Z ]*PRIVATE KEY`, and normalise whitespace before matching.
- **L5. `gates/batch_report` always exits 0.**
  - **Where:** `gates/batch_report.py:92`
  - **Impact:** Missing agency packages or unreadable findings are listed in the output but do not fail the run.
- **L6. The unattended inbox report overstates `deferred` after a fault that doesn't stop the run.**
  - **Where:** `unattended.py:247-254`
  - **Impact:** 12 files are reported as 11 preserved + 1 failed + 1 deferred.
- **L7. The assessor's duplicate check for historical paths never fires.**
  - **Where:** `recovery/assessor.py:113`
  - **Impact:** The check tests a `Path` against keys stored as `str` at `:119`. The assessor is diagnostic only.
  - **Fix:** `str(old) not in historical_paths`
- **L8. Read-only opens of a cleanly closed WAL ledger create `-wal`/`-shm` files.**
  - **Where:** `recovery/backup.py:135`, `ledger_catalog.py:297`, `ledger/store.py:115`
  - **Impact:** These leftover files can make a later `backup_v2` inventory refuse with `unknown_or_missing_scope_store`. That consequence is inferred from the code.
  - **Fix:** Use `immutable=1` under the quiescence lock.

### Traced in the code but not demonstrated

These are not counted as findings.

- The directory is not fsynced after `os.link`:
  - `ledger/store.py:198`
  - `extraction_ledger.store_blob` (`:143`), when the blob already exists
- The fixed-name search index can be deleted by a second builder: `retained_units.py:209-216`. The index is disposable and checked against its hash.
- Stage stdout spools to an uncapped `TemporaryFile` (`run_orchestrator.py:273`). Only the stage timeout limits it.
- `challenge.py:135` skips the quote check when `digest.model_id` is set. A review challenge is still always raised.
- `extraction_validation` gives accepted pages that are only queued for a visual check full-text coverage. Combined with H2, this lets model text reach an accepted stage.
- `imap_intake.fetch` does not check that the returned UID matches the one requested.

## Checked and found sound

- **Outward actions:**
  - Unattended mode forbids model configuration.
  - The portal stage is inventory-only (`apply=False`).
  - IMAP is contacted only with an explicit `--mail-config`, opens folders read-only (`select(readonly=True)`), fetches with `BODY.PEEK[]`, and never stores or expunges.
  - Nothing in `records/` sends email or publishes to a live site.
  - The portal requires explicit approval, an egress allowlist, pinned public IPs, re-authorised redirects and verified TLS.
- **Archives:**
  - `folder.worker` checks member paths, symlink members, encryption, size and ratio, a 1 GiB total, member count and nesting depth.
  - DOCX and XLSX get a central-directory check.
  - `document_drop` and `portal.engine` never extract.
- **Paths:**
  - `secure_open` refuses a symlink at any path component.
  - `PrivateDir`, `mail_delta.source_path`, `native_receipt` and `local_capture` reject `..`, escapes outside the root, and swapped files.
- **Hashing and preservation:**
  - Content-addressed blobs are written to a temp file, fsynced and linked without replacing anything.
  - Hashes are computed on exactly the bytes stored.
  - `backup`/`backup_v2` verify and restore check every member against a trusted manifest digest, and restore targets must be fresh.
  - `ledger/store.initialize` and `import_legacy` are transactional.
  - `stages.py` writes under `BEGIN IMMEDIATE` with rollback.
  - `extraction_ledger.enroll` fsyncs evidence before commit, and history is immutable via triggers.
- **Queue and runner:**
  - WP1 `_check_claim` fences every promote on claim, owner, run, revision, inputs, expiry and the current lease.
  - `queue_runtime` re-checks the claim before staging and reading.
  - flock locks are released when the descriptor closes.
  - Subprocesses use argv lists (no `shell=True`), with timeouts, process-group kill and bounded output.
  - `runner/core.py` caps retries.
- **Gates and evidence:**
  - `validate_findings` fails closed on empty evidence, self-review and stale digests.
  - `grounded_evidence` uses exact byte, span and quote equality.
  - `semantic_coverage` treats zero units as not complete, and its denominator cannot shrink.
  - `publish.publish`/`approve` check the approved hash, privacy currency and retirement.
  - `export_clearance` rejects empty exports.
  - `review_bundle` uses HMAC envelopes.
- **Error handling:** We checked all `except Exception` sites. The ones on the stage paths re-raise or record a failure. None of them turns a failure into a success.

## Test runs

- `python3 -B -m unittest discover -s tests/records -t .`: see the result recorded below.
- `AUDIT_REPRO=1 python3 -B -m unittest tests.audit_repro.test_audit_repro -v`: 6 tests, 5 failures and 1 error, all as expected on `75494fa`.
- Without `AUDIT_REPRO`: 6 skipped, OK.
