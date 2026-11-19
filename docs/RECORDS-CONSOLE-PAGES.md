# Console pages: a short phone guide

One entry per page of the records console, in the order of its menu (the menu is read from the running console, not from this text). Links are console paths: open them on the console, not on GitHub. Checked against main `81097c708038` (it includes the Home "System" line, PR #1771, and the Backups section of System health, PR #1770). It names no record, person, address or private path.

## How to read any page

- **unknown**: a figure or time could not be worked out. It is never shown as `0`, "ok" or "fresh". Home counts, the Home System line and Data age use it.
- **unavailable**: the page tried to read its source and could not (missing, wrong format, unreadable). It says which source and gives a short code. An unavailable list is never an empty list.
- **not configured on this console**: the console was started without the option this page needs, so nothing was read. The data is not wrong; the operator must start the console with the option. Some pages say "needs configuration" or "not given to the console" instead.
- **Buttons only record.** The hosted phone console (`--export-root`) sends no mail, publishes nothing and runs nothing. Every decision button (Approve, Reject, Save edit, Confirm, Veto and the like) writes the decision to the decision journal (`--decisions-dir`); the next records run applies it. Two things are not decision buttons: the Timeline "Show" button only filters the view and writes nothing, and the agency page's manual-event form (below) appends to its own journal. Until a decision is applied the row says "pending apply", and the Decisions page shows the outcome. Approve always needs G's pasted detached signature; signing in alone never approves. Without `--decisions-dir` the decision buttons are refused (a hosted page can still show a button or form, but saving it fails with "read_only_export"). The only hosted write that works without `--decisions-dir` is the manual event, and only when `--manual-events` is given. A separate local console (`--project`) writes the approval queue directly and is not covered here; sending exists only there, with `--send-config`, switched off by default.
- **Data age** (top of every page): when the export was made, with "STALE" if it is older than `--stale-hours` (default 13) or a newer run has finished. Each export page footer names the run and export.
- **Sources**: the *export* is the pinned copy served by `--export-root` (run outputs, ledger copy, queue, packets, public updates). The *run report* is `--run-report`, a path ending in `run-report.json`; the `reports` and `alerts` folders beside it are read too. The *analysis root* is `--analysis-root`. Timer state is read live from the host unless `--no-system`.

## The menu

### [Home](/)
Shows the approval-anchor status, a System line (last run, health, drafts waiting, data age; each can say "unknown" alone), what needs a decision, the next 7 days, requests waiting on agencies, a timeline and next steps. Source: export, plus the run report for the System line.
Buttons: none, only Review and Open links. Needs: `--run-report` for last run and health (else "unknown"); `--analysis-root` for the Finding review line; `--stale-hours` sets when data is stale. The System items link to `/run?id=`, `/health` and `/drafts?state=pending`.

### [Approvals](/#decide)
The "Needs your decision" part of Home: drafts, follow-ups, update packets and public status lines waiting for G, oldest first, with an overdue note (on our records, not a legal finding). Source: export queue, packets and public-update files.
Buttons: none here; Review opens the draft. If a source is unavailable the count says "unknown", never 0.

### [Agencies](/board)
Every request for every agency, with filters, then one page per agency ([agency page](/agency?agency=AGENCY_ID)) with refusals, stated retention and message ties. Source: export timeline, received items and retention table.
Button: "This tie is wrong", on an agency page only when decisions are on; it queues a replacement for review and changes nothing until signed and applied at the next run. Link: "Add manual event", on every agency request; it opens a form (Type, Date, Summary, optional Corrects event id) with an "Append event" button. It needs `--manual-events DIR` (a private directory) and does not need `--decisions-dir`: saving appends at once to that separate manual-event journal. Nothing is sent or published, and the event shows on the agency timeline only after the next data refresh. Without `--manual-events` the hosted console refuses the save.

### [Calendar](/calendar)
One month grid and an agenda of due dates, marked DATED (recorded or stated) or INFERRED (arithmetic), with a Download .ics link. Source: export; `--meetings-dir` adds meetings.
Buttons: none (month arrows are links). Dates are not legal deadlines.

### [Timeline](/timeline)
Every agency's events, newest first, 100 per page, with overdue marks. Source: the pinned run's timeline in the export.
Button: "Show" only applies the Agency, Kind and Month filters (a read). Needs nothing extra.

### [Library](/documents)
Received documents and finding proposals per agency, all "review required, not publishable" ([one agency](/documents?agency=AGENCY_ID)). A document opens a detail page by the first 12 characters of its hash ([document](/document?id=DOCUMENT_PREFIX)): pages, OCR state, asked items, link expiry; citing findings need `--analysis-root`. Source: export. Buttons: none.

### [Settings](/settings)
Read-only: whether sending is set up and on, stop files, the Drafts mailbox, reminder settings and the public-line and packet switches, with the commands G runs elsewhere. Source: the project or export. Buttons: none. On the hosted console it says nothing can be sent because sending is not set up (`--send-config` and `--mail-drafts-config` are local only).

## More

### [Dashboard](/dashboard)
The pipeline in six parts: mail, documents, stage status, findings, deadlines and drafts. Source: export and run report; `--kit-root` adds deadlines and `--as-of` fixes the day.
Buttons (only with decisions on): Reject, Save edit, and Approve, which marks the draft approved-to-send only. A Review link opens the draft.

### [Oversight](/oversight)
Now, Stuck and Needs G: recent runs, arrivals waiting, overdue and idle requests, links about to expire, pending approvals and the switch. Source: run report and export. Buttons: none.

### [Public updates](/public-updates)
The auto-post switch and each status line (proposed, holding, posted, retracted, refused). Source: export public-update file.
Buttons: Turn auto-post OFF, Kill switch, Undo or Veto a line, Record line approval (G's signature). Each is recorded and takes effect at the next records run; turning it ON is only by G's signature elsewhere.

### [Packets](/packets)
Update packets awaiting G, with the exact text to sign. Source: export packets file.
Buttons: Approve (pasted signature) and Reject (reason). Recorded only; nothing is sent, published or written to the site.

### [OCR pages](/ocr-review)
Scanned pages whose text a person must compare with the original; one page at `/ocr-review?id=`. Source: export OCR file.
Buttons: Confirm this page (after ticking that you compared it) and Reject this page; applied at the next records run. Needs `--export-root` plus `--decisions-dir`, else read-only.

### [Finding review](/findings/review)
Findings held for a human review, with cited facts and the exact text to sign; the model check shown is not a human review. Source: analysis root.
Buttons: Record approval (signature) and Record rejection (reason), applied at the next analysis run. Needs `--analysis-root` (else "needs configuration") and, for the buttons, `--export-root` plus `--decisions-dir`.

### [Automation](/automation)
Stages, open alarms and gaps, timers and companion services; substeps the report does not list stay "unknown". Source: run report and host timers. Buttons: none. Needs `--run-report`; `--timer-unit` adds timers; `--no-system` leaves unit state unknown.

### [Jobs](/jobs)
Every scheduled job and what the host reports (installed, enabled, last result, next run). A GitHub Actions part says "not configured" unless a read-only setting is given. Buttons: none; nothing starts or stops. `--timer-unit` adds jobs.

### [Drafts and next steps](/next-steps)
Suggested social posts, site updates and agency follow-ups with the reason and exact wording. Source: export queue.
Buttons: Approve (pasted signature) and Deny (reason). Approving records the decision only; nothing is posted, published, edited or sent.

### [Run history](/runs)
The newest 30 finished runs: start (Pacific time), trigger, state, duration, stage chips and error codes. Source: the `reports` folder beside `--run-report`. Buttons: none, only Details links to [one run](/run?id=RUN_ID), which explains each stage in plain words. Without the option: "not configured on this console".

### [System health](/health)
A verdict (worst of stop, warn, unknown, ok), the pipeline checks, open alerts, a Backups section, free disk and whether the export matches the newest run. Source: run report, its `alerts` folder, the export and disk; Backups reads the backup-check alerts folder. Buttons: none. Needs `--run-report`; `--analysis-root` adds the analysis check; `--backup-alerts-dir` fills Backups (without it: "not configured on this console"; an unreadable folder: "unavailable"; no open alarm says so but is not proof that a backup exists or was verified); `--no-system` makes disk readings "unavailable".

### [Draft history](/drafts)
Every approval-queue draft in every state, times in Pacific; filter by state, agency or kind, for instance [pending](/drafts?state=pending). No body, no controls. Source: export queue. A draft opens in the [review page](/review?id=DRAFT_ID) with its full text and Save edit, Approve and Reject with reason (recorded only; read-only without decisions).

### [Decisions](/decisions)
Every decision this console recorded and its outcome: "Waiting for the next records run", Applied, Refused, or "Outcomes unavailable". Source: the decision journal and the export's apply results. Buttons: none. Without `--decisions-dir`: "not configured on this console".

### [Findings](/findings)
Every finding in every review state, with counts by state and hold, never marked publishable; waiting ones link to Finding review. Source: analysis root. Buttons: none. Without `--analysis-root`: "not configured on this console".

## Which option each page needs

| Option | Pages |
| --- | --- |
| `--export-root` | all hosted pages (the pinned export) |
| `--decisions-dir` | every decision button (not the manual-event form); Decisions |
| `--run-report` | Run history, System health, Automation, Oversight, Home System line |
| `--analysis-root` | Findings, Finding review, Library citing findings, Home line |
| `--public-origin`, `--access-config`, `--access-keys` | hosted decisions behind Access |
| `--meetings-dir`, `--kit-root` | Calendar meetings, Dashboard deadlines |
| `--manual-events` | the agency page's "Append event" button (works without `--decisions-dir`) |
| `--timer-unit`, `--no-system` | Jobs, Automation, Health disk |
| `--backup-alerts-dir` | System health Backups section |
