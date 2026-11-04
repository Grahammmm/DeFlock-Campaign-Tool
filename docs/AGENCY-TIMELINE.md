# Agency timeline report (read-only)

`tools/agency_timeline.py` writes a private per-agency contact timeline and
request history from the canonical records ledger. It reports counts,
identifiers and dates only. It makes no schema change and no write, and it
does not touch the mailbox, network or models. Its output is REVIEW
REQUIRED: a machine-valid report is not semantic, legal, source or
publication acceptance.

## Run

```sh
python3 -B tools/agency_timeline.py \
  --ledger "$PRIVATE_LEDGER" \
  --output-dir "$NEW_PRIVATE_DIR"
```

The argv takes exactly these two options, and both must be absolute,
normalized paths. The output directory must not exist yet, and its parent
must be an existing, link-free directory. The tool reads and builds the
report first. Only then does it create the directory (mode 0700) and
exclusively create `agency-timeline.json` and `agency-timeline.txt` (mode
0600). A refusal creates nothing and prints a single JSON line with a stable
`error` code. Stdout never contains a path or any source content. The exit
code is 0 on success and 2 on refusal.

## Source interfaces (unchanged)

| Interface | Use |
| --- | --- |
| `campaign_tool/records/correspondence.py` `snapshot(path)` | The existing correspondence projection reader. It takes one read transaction over `agencies`, `requests`, `originals`, `occurrences`, `mail_messages`, `joins`, `portal_items` and `ledger_meta`, and enforces the row bound. |
| `campaign_tool/records/ledger/store.py` `ledger(path, readonly=True)` | Called by the reader: `mode=ro`, `PRAGMA query_only=ON`, owner-only path checks, migration identity check. This module never imports a writable constructor (`initialize`, `import_legacy`, `sqlite3.connect`). |
| `campaign_tool/records/ledger_catalog.py` `scope_excluded(row)` | Hides out-of-scope originals and their descendants. |

`snapshot_sha256` is the SHA-256 of the canonical JSON of that snapshot. This
is the same binding `correspondence.report` emits, so the two reports bind to
the same ledger state. It covers the selected canonical rows, not every
original byte or the database file.

## Report fields

Design item M-14 field list, copied verbatim from the approved
work breakdown (plans/fable-mail-20261024 WORK-BREAKDOWN.json, pin
7943e54f...; DESIGN.md pin b14b7ebc... section 5.4). The work breakdown's
`contracts` table has no M-14 entry, so this list is the frozen contract:
"per agency: first/last contact time, sent/received counts by month, request
rows with recorded status and dates, attachment occurrence count, portal item
count, unresolved join count, unknown-domain message count; totals;
snapshot_sha256 binding; JSON and a plain-text table; counts and dates only."

JSON (`agency-timeline-report-v1`):

- Envelope: `schema`, `review_required` (true), `semantic_assessment`
  (`NOT_ASSESSED`), `visibility` (`private`), `snapshot_sha256`, `agencies`,
  `totals`, `gaps`, `limits`.
- `agencies[]` has one row per agency id (sorted), then one row with
  `agency_id: null` and `bucket: "unknown"`, shown as `(UNKNOWN)` in the
  table. Each row carries `agency_id`, `bucket`, `first_contact_at`,
  `last_contact_at`, `messages_sent`, `messages_received`, `undated_messages`,
  `months[] {month, sent, received}`, `request_count`, `requests[]`,
  `attachment_occurrences`, `portal_items`, `portal_items_unretrieved`,
  `unresolved_joins`, `unknown_domain_messages` and
  `agency_conflict_messages`.
- `requests[]` carries `request_id`, `status`, `submitted_at`,
  `determination_due_at`, `determination_at`, `production_at`,
  `linked_messages` and `last_linked_message_at`.
- `totals` holds the sum of every row count, the merged `months`, the overall
  first/last contact, and `agency_rows`.
- `gaps` holds `mail_messages_total`, `visible_messages`,
  `scope_excluded_messages`, `scope_excluded_occurrences`,
  `scope_excluded_joins`, `ancestry_invalid_occurrences`,
  `ancestry_invalid_messages`, `ancestry_invalid_portal_items`,
  `distinct_attachment_originals` (not a row sum), `withheld_values` and
  `unparseable_dates`. Mail rows partition exactly:
  `visible_messages + scope_excluded_messages + ancestry_invalid_messages =
  mail_messages_total`.

The text table renders the same report in four parts: `CONTACTS` with a
`TOTAL` row, `MONTHS (UTC)`, `REQUESTS` and `GAPS`.

## Definitions

- Contact: a visible `mail_messages` row. Its time is `received_at`, and
  only a timezone-aware value counts. Months are UTC `YYYY-MM`. A missing,
  naive or invalid time is still counted as a message, but it is undated:
  it gets no month and does not count toward first or last contact. A
  value whose UTC normalization overflows the calendar (for example
  `0001-01-01T00:00:00+01:00`) is invalid in the same way, for messages and
  request dates alike. First and last contact, per row and in `totals`, are
  chosen by comparing instants, never ISO text.
- Ancestry (fail closed): each occurrence's parent chain is followed up to
  100 links. If any link met on the way, valid lineage or not, has a
  scope-excluded original, the occurrence and everything under it is
  excluded (counted in `scope_excluded_*`). Otherwise a missing parent, a
  cycle or a chain over the bound makes the lineage invalid: the occurrence
  is withheld from every row and counted in `ancestry_invalid_occurrences`.
  A mail row whose occurrence is absent or invalid is never attributed by
  its own original; it is counted in `ancestry_invalid_messages`. A portal
  item whose recorded notice occurrence is absent or invalid is counted in
  `ancestry_invalid_portal_items`, and one whose notice is excluded is
  dropped.
- Direction: derived from the folder name only. If the last hierarchy
  segment, split on `/` or `.` and case-folded, is `sent`, `sent mail`,
  `sent items` or `sent messages`, the message counts as sent. Everything
  else counts as received. Headers, addresses and bodies are never parsed.
- Agency attribution: comes only from recorded `typed` joins whose agency
  exists and whose named request, if any, belongs to that same agency. The
  typed agency set is intersected at each level of the occurrence ancestry,
  as the correspondence projection does. Exactly one agency means the item
  is attributed to it. No typed agency means `UNKNOWN`, counted as
  `unknown_domain_messages`. A conflict also goes to `UNKNOWN`, but is counted
  as `agency_conflict_messages`. Domain-to-agency hints are a custodian task
  (M-15). The tool counts them and never guesses.
- Unresolved joins: `hinted` or `blocked` joins, plus `typed` joins that
  name an unknown agency or a request belonging to another agency. Each is
  counted under its agency, or under `UNKNOWN` when the agency is null or
  unknown. Joins on scope-excluded originals are counted only in `gaps`.
- Attachment occurrences: every visible `attachment` occurrence. Shared
  bytes delivered twice count twice, once per delivery.
- Portal items: grouped by the recorded `portal_items.agency_id`. Its
  `request_id` is an external reference and never joins to a request.
  Unretrieved means no original is recorded yet.
- Requests: recorded `status` and dates are reproduced, never reassessed,
  and no deadline is computed. `linked_messages` counts visible messages
  whose typed join names the request.
- Withholding: identifiers that are not short tokens are replaced by
  `withheld-id-<16 hex>`. A status that is not a lowercase token becomes
  `status_not_token`. An unparseable date becomes null and is counted in
  `gaps`.

## Acceptance

`scripts/n3_agency_timeline_acceptance.py --mode MODE --output-root DIR`
takes one mode per invocation: `focused`, `compatibility`, `negative`,
`portability` or `hygiene`. Each run writes a fresh unique directory below
DIR containing `run.json`, `output.txt` and raw child streams. Exit codes:
0 pass, 1 fail, 2 refused, 3 INPUT_UNAVAILABLE. Fixtures under
`tests/fixtures/n3_agency_timeline` are synthetic and read-only. Every
ledger is built at test time in a temporary owner-only directory. Full
repository discovery is operator-run.

## Limits

This is one current snapshot. It is not historical state and does not
establish mailbox completeness. Typed joins are recorded facts and are not
reverified here. Folder-derived direction can be wrong for mailboxes with
unusual folder names. The report is private analysis even without message
content, because it exposes agency and request identifiers and dates.
When the existing pinned `ledger(path, readonly=True)` opens a quiet
WAL-mode ledger, SQLite can create its own `-shm` sidecar and an empty
`-wal` file next to the database. The main file bytes, inode and logical
content stay unchanged, and the tests assert this.

## Stated production date (kit requests)

`kit-response --kind determination --expected-on YYYY-MM-DD` (or a later step, such as `fee_estimate`, that
restates it) records the agency's own stated production date; a date before that step's date is refused.
`kit-status` shows the latest one as `stated_production_date`. `kit due` lists a request whose stated date has
passed with no `produced` step since (closed requests never) under `stated_passed` as `stated_date_passed`,
counts it and exits 3: stated date passed — the agency's own date; provisional, not a legal deadline.

`kit-response --kind produced --partial [--expected-on YYYY-MM-DD]` records a rolling production: the request stays
open (`producing: true`, `next_installment` in `kit-status`) until a final `produced` (no `--partial`), `denied` or
`closed` step; a partial after a final one is refused. `kit due` lists it as `producing` (due = the agency's next
installment date) or, once that date has passed, `installment_passed` (counted, exit 3); `today` shows it and
`kit-followup` drafts a reminder naming the date. Only a final `produced` step clears a stated production date.

## Download links that expire (kit requests)

`kit-response --kind produced --download-expires-on YYYY-MM-DD` records the date the agency says its download link
expires (produced steps only; not before the step's own `--date`). The production stays "download pending" until
records are linked to it: a later `produced --record PREFIX` step on the same request clears the latest pending one
(records on the same step mean it is not pending at all). `kit-status` lists `downloads_pending` with days left;
`kit-due` adds a `download` row due on the expiry date and, once it has passed, `download_expired` (counted, exit 3):
"link expired: ask the agency for a new one"; `today` shows the same rows. Only the date is kept: the link itself is
never stored or fetched, and nothing is downloaded for you. Files without the key read exactly as before.

## Clarification requested (kit requests)

`kit-response --kind clarification_requested` records the agency asking you to clarify or narrow a request (not after
`produced`, `denied` or `closed`; one open at a time); `--kind clarified` records your reply, refused with none open.
While one is open, `kit-status` shows `waiting_on_you_since`, `kit due` lists it as `waiting_on_you` (counted apart,
never past due, stated date passed or exit 3; the provisional date still shown) and `kit-followup` stops with exit 3,
writing nothing. The tool makes no claim that a clarification pauses any legal period.

## Agency reference number (kit requests)

`kit-sent`/`kit-response --reference REF` (1-40 letters, digits, `- _ # .`) stores the agency's own number once per
request; a different one exits 3 unless `--replace-reference`. `kit-status`/`kit due` rows show `reference`; the
followup and denial-reply drafts add `(Your reference REF)` to the title and open `Regarding your reference REF,`.

`kit-response --kind fee_paid [--amount USD]` or `--kind fee_waived` records that the latest `fee_estimate` on that
request was paid (amount optional, whole cents, 0-100000) or waived; refused with no earlier estimate or when that
estimate is already settled (a new estimate re-opens it). `kit-status`, its `--csv` and `kit-coverage` show `fee_state`
(`unpaid`/`paid`/`waived`) beside the estimate and `fee_paid`; `kit-status` names each unpaid estimate on stderr.
Nothing is paid by the tool; the fee-cap reminder is unchanged.

`records` are what the agency produced (`--kind produced --record PREFIX`); `correspondence` is the letters and
confirmations: `kit-sent --correspondence PREFIX` (the sent message or portal confirmation) and `kit-response
--correspondence PREFIX` on any kind, produced included. Both take >= 12 hex of an ingested hash, repeatable; a hash
linked once (either way) is refused. `kit-status` lists `correspondence_prefixes`; the `records` counts never include them.

## Withheld basis (kit requests)

`kit-response --kind denied|produced --withheld-basis TEXT` (repeatable, at most 5 per step) records the reason or code
section the agency itself gave for withholding, as written: 1-200 characters after stripping, no control characters, no
duplicate within a step, refused on any other kind. Quote the agency; never put record contents in it. It is stored on
the step as `withheld_basis`; `kit-status` lists each one (`on`, `kind`, `basis`) and `kit-denial-reply` quotes each
under "The agency stated these reasons, as written:" and asks only for any withheld record or category those reasons do
not cover. `kit-withheld --directory D [--json]` lists every recorded basis across agencies, grouped by each Gov. Code
section number found in the agency's own text (`Gov. Code § 7923.600`, `Government Code section 7927.705(a)`,
`GC 7922.000`; a basis naming two sections appears under both; none: `no section number stated`). It is read-only and
adds no legal reading. Older `kit/sent.json` files with no `withheld_basis` read unchanged.

## Per-scope answers (kit requests)

`kit-sent` records the law-package scope ids its draft covers as `scopes` (every package scope, in package order, when
the kit was built without `--scope`; exactly the `--scope` list otherwise), read from `kit/summary.json`. A missing,
symlinked or unreadable summary, or no law package, stores no `scopes` (never guessed); `--replace` re-reads it.
`kit-response --scope ID [--scope ID ...] --disposition produced|partly_produced|withheld|no_records_stated` records, on
that dated step, what the agency said about those scopes (`scope_answers`). Both flags or neither; every ID must be one
the request asked for; a duplicate ID is refused; a request with no recorded `scopes` refuses `--scope`; not allowed on
`acknowledged`, `extension`, `clarification_requested`, `clarified` or `fee_estimate` steps. A disposition is your record
of what the agency said, never a legal finding, and is independent of `--withheld-basis`.
`kit-status` rows carry `scopes`: `null` (request recorded before this existed: scopes not recorded) or, per asked scope,
`{scope_id, answer, on}` with the latest disposition and its step date, `answer: null` for no answer yet. `kit-coverage`
(table and `--csv`) gains a last `scopes` column: `not recorded` or `k/n answered`.

## Public asked items from the tracker (kit-asked)

`kit-asked --directory D [--apply]` proposes the public "What we asked" list in `content/agencies.json` from the private
`kit/sent.json`, for each request whose agency is in that file. A dry run (the default) prints one line per field
change, `content/agencies.json <agency>.asked[<i>].<field>: <old> -> <new>` or `+ new item ...`, exit 0, and writes
nothing. `--apply` builds the whole new document, validates it with the content loader (a refusal, or more than 20 items
for an agency, prints `Stopped:`, exits 3 and writes nothing) and then writes it once, atomically. Nothing is sent or
published; build as usual after reviewing it.

Each proposed item is `topic` (the request's recorded scope titles joined with `; `, cut at a title boundary within 120
characters; `Records request` when no scopes were recorded), `asked_on` (the sent date), `state` and `updated_on` (the
latest step date, left out when there are no steps), linked to its request by the new optional `request` key (the
request name, `main` for the original; 1-40 characters a-z, 0-9 or `-`). The key is never rendered on the site, in
`data/*.json`, feeds or newsletters, and older content without it reads unchanged. State comes from the latest step and
the per-scope answers: no steps `awaiting_response`; `acknowledged`, `extension`, `determination`, `fee_estimate`,
`clarification_requested`, `clarified`, `fee_paid` or `fee_waived` as the latest step with no `produced` step
`acknowledged`; `denied` with no `produced` step `declined_in_writing`; any `produced` step `answered` only when the
request recorded scopes, every scope's latest answer is `produced` or `no_records_stated` and the latest production is
not a rolling (`--partial`) one, otherwise `partly_answered`. A request `closed` without production is not proposed: the
dry run says `organizer to choose` and any existing item is left as it is; `withdrawn` is never proposed.

An existing item with the same `request` is updated field by field and keeps its own `note`; an item with no `request`
(typed by you) and its note are never touched. Notes, withheld bases, references and linked records of the tracker never
reach content. `content-check` names a linked item whose date (`updated_on`, else `asked_on`) is before its request's
latest step (`... older than its request's latest step (<kind> on <date>); run kit-asked`) and a link to a request that
`kit/sent.json` does not have; with no `kit/sent.json` it adds nothing.

## Private calendar (kit-calendar)

`kit-calendar --directory D --out FILE.ics [--as-of DATE]` writes a new private iCalendar file (mode 0600) to import
into your own calendar: an all-day event for each open request's provisional determination date (answered requests
keep only the agency's stated production date) and a timed event for each staged, not cancelled meeting's verified
written-comment deadline on or after `--as-of`. It holds ids, dates, agency or body names and the official source
link only. It is refused inside the campaign's `public/` or `content/` and never overwrites: re-run into a new file.

## Requests filed on MuckRock (kit requests)

`kit-sent --channel muckrock --request-url https://www.muckrock.com/foi/.../<name>-<id>` records a request you filed
there yourself; the URL is stored on the entry, never fetched. To track it, open the request in your browser and save
its JSON (the request's API page) to a file, then run `kit-muckrock-import --directory D --agency ID --from-json
FILE [--request NAME]`. It reads only `id`, `status`, `datetime_updated`, `datetime_done` and `price` from that file,
checks the `id` against the stored URL and prints the proposed `kit-response` step as a dry run; `--apply` records it
under the normal step rules. `processed` is `acknowledged`; `fix` is `clarification_requested`; `payment` is
`fee_estimate` from `price` (none: record it by hand); `rejected` is `denied`; `no_docs` and `abandoned` are `closed`
with a note; `partial` is `produced --partial`; `done` is `produced`. `started`, `submitted`, `ack`, `appealing` and
`lawsuit` propose no step, and any other status is refused by name. Nothing is fetched or sent.

## Requests filed on an agency portal (kit requests)

`kit-sent --channel portal --request-url https://<portal host>/...` keeps a link back to a request you filed on the
agency's NextRequest, GovQA or JustFOIA portal. The agency must already have its portal recorded by `kit-verify-agency
--portal`, and the URL's host (compared lower-case) must equal that portal's host; the URL must be `https` with no
login, port, query or fragment, at most 300 characters. It is refused with `--channel email` or `mail`, and for an agency
with no verified portal. The link is stored as `request_url`, never fetched: the tool opens no portal page and uses no
portal login. `kit-status` and `kit-due` show it on the request's row (`kit-status` also prints `Portal request: URL` on
stderr), `kit-status --csv` adds a `request_url` column, and the `kit-followup` and `kit-denial-reply` drafts quote
`Portal request: URL` after the reference line. `kit-sent --replace` keeps a stored link unless you give a new one.
Older `kit/sent.json` files, with no `request_url`, read and write as before.

## A transcribed timeline in one write (kit-response-import)

`kit-response-import --directory D --agency ID --csv FILE [--request NAME] [--apply]` records a portal or letter history
you copied into a CSV by hand instead of one `kit-response` call per event. The file is UTF-8 (a BOM is fine), at most
256 KiB and 1-200 data rows, a regular file (a symlink is refused), with the header exactly
`date,kind,note,amount,expected_on,partial`; an empty cell means not given and `partial` is empty, `yes` or `no`.
`--record`, `--correspondence` and `--reference` are not columns: link records with `kit-response --record`; the agency
reference is stored once. Each row is checked in file order by the same rules as `kit-response` (a dry run) against the
recorded steps plus the rows before it, so a closing step in row 2 refuses an extension in row 3. Any refused row is
named as `line <L>: <the kit-response message>` (the header is line 1; up to 20 are listed, then `and N more`), the
command exits 2 and nothing is written. A row equal to a step already recorded on that request is reported
`already recorded, skipped`; the same row twice in the file is refused on its second line. Without `--apply` the command
only checks; with `--apply` every new step is recorded in file order in one atomic write. Nothing is fetched or sent.

## Taking back the latest step (kit-response-undo)

`kit-response-undo --directory D --agency ID [--request NAME] --reason TEXT` removes exactly the last recorded step of a
hand-sent (`email`, `portal` or `mail`) request: the one at the end of the list, not the one with the latest date. Use
it for a mistyped date or kind, or a `closed` recorded by mistake, which otherwise locks the request. There is no index,
date selector, bulk switch or redo; re-record the corrected step with `kit-response`. `--reason` is required (1-200
characters after whitespace is collapsed; no `<`, `>` or control characters) and is never printed. A MuckRock request,
an unknown request, a missing `kit/sent.json` and a request with no recorded step are refused, and nothing is written.

Before anything is written the steps that remain are replayed, in order, through the same rules `kit-response` applies
(dates, order, `acknowledged` first, extension, closed, clarification, fee, partial and expected dates, withheld basis,
download expiry). Stored full record and correspondence hashes are checked as stored; no ledger lookup is made, so a
step whose original file is gone still passes. The removed step is checked for shape only, so a step that is itself
wrong can be taken back.

The removed step, all its full hashes and metadata, the reason, a UTC time and the hashes of the entry before and after
are appended to the private `kit/sent-undone.json` (`schema_version` 1, mode 0600, an `undos` list). Receipts are never
changed or removed, and no ledger object, source or public content is touched. A missing archive means no receipts; a
corrupt, wrong-schema, symlinked or non-regular archive, or a symlinked `kit/` folder, is refused before either write.
The reference number and the request's own correspondence stay on the entry.

The two files are written in a fixed order and are **not** atomic together: the archive is written and fsynced first,
then `kit/sent.json` is replaced with the shorter list and the receipt's `undo_id` is stored on the entry (`undo_ids`).
If the second write fails or the process stops in between, the archive holds a receipt whose id is not on the entry: an
attempted, never a counted, undo. Run the command again; the retry gets a new id, and only ids on the entry count.
`kit-status` shows `undone_steps` on a request row (only when above zero) and prints `<request>: N step(s) undone` on
stderr; `kit-sent --replace` keeps the count. Every status, due, fee and download figure is recomputed from the steps
that remain, so undoing a `closed` reopens the request, undoing a fee payment makes the estimate unpaid again, and
undoing the final production leaves it partial or download-pending. Once every step is undone, `kit-sent --replace`
can correct a wrong sent date. Nothing is fetched or sent.

## Response times (kit-timing)

`kit-timing --directory D [--as-of YYYY-MM-DD] [--csv FILE]` is a private, read-only report of how many **calendar days**
each recorded request took between its steps. Every figure is labelled `calendar days; provisional; not a legal
deadline`: it describes what the organizer recorded and judges nothing. It reads `kit/sent.json` (and the names in
`kit/agencies.json` when that file is a regular file; otherwise the agency id is shown), never repairs either file, and
sends and fetches nothing. A malformed step (unknown kind, bad or out-of-order date, a step after today, a partial that
is not a produced step, a clarification answered twice or never asked, a second extension, a step after closed) refuses
the whole report with `kit/sent.json unreadable: malformed timing steps`, even when the bad step is past `--as-of`.

Per request, counted from the date it was sent (the same day is 0; a missing endpoint is `-`, never 0):

- `first_step_days`: the first recorded step except `clarified` and `fee_paid`, which are the organizer's own actions
  (`fee_waived` and `clarification_requested` are the agency's). The agency's first response, not the first answer.
- `determination_days`: the first `determination` step only; a denial is not substituted for it.
- `first_production_days`: the first `produced` step, partial or final. `final_production_days`: the first `produced`
  step that is not partial; a later production does not move it.
- `open_age_days`: the as-of date minus the sent date, only while no final production, `denied` or `closed` step is
  recorded by the cutoff. A determination or a partial production alone leaves the request open.
- A final production that is only a link counts as production even while `kit-status` lists its download as pending.
  Fetching that download is a separate reminder, not the response time.

Waits are their own spans and are **never subtracted** from any number above. `extension_spans` run from each extension
to the first following determination, production, denial or closing step; `clarification_spans` run from each
`clarification_requested` to the next `clarified` or `closed`. Each span is `{start_on, end_on, days, open}`; a span
with no ending step runs to the as-of date and is `open`, a censored observation rather than a completed turnaround.
A stated production date or a download expiry ends no wait.

`--as-of` (default today, not in the future) is a date cutoff: requests sent after it, and steps dated after it, are
left out after every stored step has been checked. It is a projection of the history recorded today, not a
reconstruction of what was known on that date. Per agency the report gives the `count` and `median` of each of the five
figures and of `extension_days` and `clarification_days` (one observation per wait; open waits are counted and flagged
`open`). Nulls are left out, zeros are kept, an even number of observations can give a `.5` median, and no observations
give a null median. There is no pooled median, ranking or score.

`--csv FILE` writes the same report to a new private `.csv` (mode 0600, never overwritten, refused inside the
campaign's `public/` or `content/`; spreadsheet formula prefixes are neutralised). Header:
`row_type,agency_id,request,name,metric,start_on,end_on,days,open,count,median,note`; `request_metric` rows carry the
five figures, `request_span` rows each wait (`open` is `yes` or `no`), `agency_metric` rows the counts and medians (the
wait metrics put their number of open waits in `open`). The label is repeated on every row. Notes, references, hashes,
amounts and contact details are never exported. The table still prints, followed by the new file's summary.
