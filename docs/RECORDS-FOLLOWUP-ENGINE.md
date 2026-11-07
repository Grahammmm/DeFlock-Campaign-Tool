# Records follow-up engine

## Agency events import

This is a private, review-required timeline input, not a legal finding or publication.
An operator stages an EVENTS-SCHEMA TSV at `PROJECT/agency-events/AGENCY_KEY.tsv`
and a CSV at `PROJECT/agency-events/request-map.csv` with exactly these columns:
`request_ref,agency_key,request_id`. Map each reference to its timeline request id.
No operator-workspace files are discovered or copied automatically.

Preview with `python3 -m campaign_tool.records events-import --project PROJECT --agency-key AGENCY_KEY`.
Add `--apply` to append mapped events to `manual-events.jsonl` (0600).
The default is dry run: no journal or lock is created.
The JSON report lists mapped counts by timeline type, skipped count, unmapped and
refused event ids, added count and already-present count. Unmapped rows block all
writes and exit 1 unless `--allow-unmapped` explicitly permits only mapped rows.
Malformed input or refused rows always block writes, even with that option.
Row text is never printed; invalid event ids are reported by row number.

Computed clocks, fetched-link/system/contract events, request-received events,
context references and references ending `(third party)` are skipped.
Fixed type mappings include clarification/reconsideration sent as our follow-up
and clarification requested as its own type (no stage change).
Promised dates retain their row date as `new_date`. Dates are taken as written
(YYYY-MM-DD, Pacific per EVENTS-SCHEMA); summaries are clipped to 200 characters.
Every field must be free of angle brackets. Unknown types and missing mappings
are listed as unmapped, never guessed.

Ids are `tsv-AGENCY_KEY-EVENT_ID`. Re-import skips ids already in the journal,
even if the staged text changed; it never rewrites earlier entries. The shared
console append helper retains its private lock, bounded journal and append-only
behavior. Console forms still enforce tickets, freshness and conflicting retries.
Refresh the derived run timeline through the normal records output stage to see
imports. Ledger events may also appear: distinct sources remain visible.
Nothing sends, fetches, approves, publishes or writes the coordinator ledger.

## Clocks and the due rule

`records followups clocks --project P --run-id R` prints a read-only JSON table
for every request in that run, including closed requests. It shows statutory
base/extended/due dates and state, the agency's promise, follow-up due dates,
formal-notice due date, extension-limit flag (null without a matching timeline),
autonomy and issues. Rows sort by earliest date on or after the run's `as_of`,
then agency and request; rows without upcoming dates sort last.

`silence_after_days` is an integer from 0 through 30 in the settings `default`
or per-agency entry (the agency overrides the default). Its default is 0:
`follow-up due = statutory due or unsatisfied promised date + 1 + silence_after_days`
calendar days. Thus 0 retains the first-day-after behaviour; 5 waits until D+6.
Closed requests and satisfied clocks have no follow-up due. Issues or autonomy
`off` still prevent drafts. Invalid settings fail closed to all off.

Formal-notice timing remains the approved reminder's `updated_at` day plus
`notice_after_days`; stale-date reminders never escalate a replacement clock.
These dates are **on our records; not a legal finding**. Drafting never sends;
all drafts remain subject to human review. `rows` keeps its existing display.
