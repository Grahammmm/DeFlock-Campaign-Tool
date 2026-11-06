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
python3 -B -m unittest tests.records.test_arrival_summary
```

Covers baseline privacy and first-capture, new-versus-cumulative counts, pending projection,
schema refusals and the composed text (bounds, unavailable, failure, preflight, legacy).

## Limits

The full fresh-email to panel-approval end-to-end harness (real panel POST with a signed
approval) is NOT part of this change; the existing arrival, approval queue and admin panel tests
cover those parts separately. A rendered sample message (not sent) is in
`reports/REC-AUTO-6-telegram-summary-proof.txt` on AgentOrange. No arrival-triggered run report
exists yet in records-ro, so that sample is built from the synthetic example report.
