# Records follow-up engine

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
