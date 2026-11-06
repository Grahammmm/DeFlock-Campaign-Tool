# Records arrival summary (REC-AUTO-6)

After every arrival-triggered records run, the existing notify helper sends ONE short Telegram
message (at most 6 lines). This unit only composes the text; sending, fallback and the
non-fatal behaviour are unchanged. Nothing here sends mail, approves, publishes or clears holds.

## Sections

- `Came in`: NEW logical originals this run by kind (response, extension, denial, fee, records,
  other). Items, not messages: a mail body and each attachment count separately. "New" means the
  original's SHA-256 was not in `run-outputs/<run_id>/arrival-baseline.json`, captured (owner-only,
  first capture wins) at the start of sync. Duplicate delivery of identical bytes counts zero.
- `Done`: the seven fixed stage names with report status and counts or error code.
- `Needs G`: drafts pending in the approval queue at end of run (a backlog count, not newly
  drafted) and hold groups with issue occurrences (occurrences are not distinct records).

`unavailable` means the source was missing, corrupt or refused; it is never shown as 0. The two
counts fail independently. Run ids, enums, counts and codes are the only content: no record,
agency, draft id, filename, address, subject, hash, quote or `--label` text enters the message.
Preflight refusals say no work was attempted. Scheduled and manual runs keep their legacy text.

## Report field

Optional `arrival_summary` in run-report and stage-result v1:
`{received_by_kind: {six kinds}|null, pending_drafts: int|null}`; only valid on arrival runs and
taken only from the `outputs` stage.

## Offline tests

```sh
python3 -B -m unittest tests.records.test_arrival_summary tests.records.test_arrival_e2e
```

`test_arrival_summary` covers baseline privacy and first-capture (including symlinked ancestor
directories failing closed), new-versus-cumulative counts, pending projection, schema refusals and
the composed text (bounds, unavailable, failure, preflight, legacy).

`test_arrival_e2e` (harness: `arrival_e2e_harness.py`) runs, in temporary roots with no network:
baseline taken -> a new Caltrans-style notice enrolled with its extraction -> the real `outputs` stage
(classification, comparison, received items, draft queue, arrival summary) -> the real panel on
127.0.0.1 shows the pending draft -> a signed POST to `/approvals/approve` marks it approved (read
back from the queue, approver kept, no duplicate pending row) -> the arrival text is composed from the
stage's own summary and checked for identifiers. A replay under a new run adds no items or drafts and
leaves the approval intact; negative controls (no arrival document, non-arrival run, no baseline) fail
the proof as they should.

## Limits

The e2e starts from a synthetic ledger document with its extraction receipt: IMAP/EML intake and OCR
are covered by `test_arrival_runtime` and `test_attachment_extraction_arrival`, not re-run here. The
Telegram text is composed, never sent; the transport (`notify.main`) is covered in
`tests/test_records_runtime.py`. A rendered example is in
`reports/REC-AUTO-6-telegram-summary-proof.txt` on AgentOrange. No arrival-triggered run report exists
yet in records-ro or the runtime, so a real-arrival render is unavailable until the first live arrival run.
