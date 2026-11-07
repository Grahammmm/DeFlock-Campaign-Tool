# Records console: Board and Agency timeline (REC-CON-1)

Private, read-only pages served by `records panel` (stdlib, loopback only):

- `GET /board[?filter=overdue|waiting|ours|closed]` — Screen A. One row per request across all agencies: tier-1 (logs / audits) status, stage, last contact, statutory due date and basis, promised date, labelled overdue mark, next action, owner. Sorted needs-action-first (overdue, then ball in our court, then waiting on agency, then closed). Counters: requests, overdue, drafts pending, release links expiring within 14 days.
- `GET /agency?agency=KEY` — Screen B. Per request: clocks with basis, overdue marks, the "extension total exceeds 14 days on our records" flag, the stage ladder, next action and owner, asked items with tier and state, and the events oldest first with their source (message id, tracker row, approval journal, manual entry). Links to Documents (Screen C) and Drafts.

Data: `run-outputs/<run>/outcomes/timeline.json` (REC-TL-1), plus `release-links.json` and `response-items.json`. Each source fails alone and is named unavailable. Nothing is sent, fetched, approved or published; overdue marks are arithmetic on our records, not a legal finding. Agency keys are data and are matched exactly against the timeline.

## Home (CON-HOME-1)

`GET /` is the read-only Home decision queue, with counts by kind. Requests,
follow-ups, unsent approvals (when sending is configured), status lines and
proposed packets share one queue: most overdue first, then closing windows,
then oldest. Overdue is arithmetic on our records, not a legal finding.
Open links lead to the existing action pages; Home adds no approval or send.
Agency pages, redactions and findings show their not-connected source units.
The unchanged pipeline dashboard is now at `GET /dashboard`.

## Manual events (REC-CON-4)

Each request links to `GET /manual-event?agency=KEY&request=REF`. Choose call,
portal check or hand note, enter the event's ISO date and a one-line summary,
then append. Cancel merely returns to the agency; it writes nothing. Attribution
comes from the panel's startup `--actor`, not an editable form field; this is
local single-operator attribution, not authenticated identity.

`POST /manual-event` uses the same Host, Origin, body and CSRF checks as the
approval forms. A signed, one-hour form ticket binds the run, request, event id
and register version. Expired forms (410) and stale register versions (409)
require reopening the form. Retrying the same event is idempotent; reusing its
id with changed content refuses with 409.

Events append to private `<project>/manual-events.jsonl` (0600) under the
existing private journal lock. Nothing rewrites old lines or writes the
coordinator ledger. To correct a fact, submit a new event with the earlier
manual event id in **Corrects event id**; it must belong to the same request.
Both remain on the timeline, with the earlier entry marked superseded.
Hand notes do not advance the contact date or stage.

After a successful append the derived REC-TL-1 timeline is refreshed and the
browser returns to the agency page, showing summary and actor. If refreshing
fails (503), the event may already be durable: retry the unchanged form rather
than opening a new one. Malformed, incomplete, linked or non-private journals
refuse further writes. The form does not send, approve or publish anything.

## Documents agency aliases (CDATA-ALIAS-1)

An operator may hand-stage `<project>/agency-aliases.tsv` (UTF-8), header
`agency_key<TAB>alias`, at most 500 rows. Use public agency identifiers only.
Keys fullmatch `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`; aliases are nonempty,
printable, at most 128 characters, without tabs/newlines. Each alias maps to
one key and cannot itself be a key. Mapping is UNVERIFIED until REC-PILOT-1.
Documents joins items, release links, proposals and dispositions to those keys.
Absent file preserves exact matching; an invalid or symlinked file shows
`aliases: unavailable (aliases_invalid)` and applies no aliases, even valid rows.
No file is generated and no source records or publication gates are changed.

## Calendar (CON-CAL-1)

`GET /calendar[?month=YYYY-MM]` shows a month grid and an agenda (overdue and today first): statutory due and 14-day extension dates, agency-stated dates, planned follow-up drafts, status-line holds and packet expiries, release-link expiries and overdue marks. Each entry is DATED (recorded or agency-stated) or INFERRED (arithmetic or projection) and shows its source. Follow-up dates come only from `followup_clock.plan` run on copies of register rows; they are drafts that send only after your approval. Nothing is queued, sent or published. An unreadable source is named unavailable and the rest still list.

`GET /calendar.ics` and `records calendar --project P --run-id R --ics OUT` write an RFC 5545 file of all-day events. Each event holds only UID (32 hex plus `@calendar.deflock.invalid`), DTSTAMP, DTSTART, SUMMARY (kind, agency name, request ref, DATED/INFERRED) and DESCRIPTION (the source label). No bodies, URLs, release links, addresses, staff names, hashes or paths. Hosting the feed for phone subscription needs exposure beyond this loopback box: that is G's decision and is not built.
