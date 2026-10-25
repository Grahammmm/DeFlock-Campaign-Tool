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
  `distinct_attachment_originals` (not a row sum), `withheld_values` and
  `unparseable_dates`.

The text table renders the same report in four parts: `CONTACTS` with a
`TOTAL` row, `MONTHS (UTC)`, `REQUESTS` and `GAPS`.

## Definitions

- Contact: a visible `mail_messages` row. Its time is `received_at`, and
  only a timezone-aware value counts. Months are UTC `YYYY-MM`. A missing,
  naive or invalid time is still counted as a message, but it is undated:
  it gets no month and does not count toward first or last contact.
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
