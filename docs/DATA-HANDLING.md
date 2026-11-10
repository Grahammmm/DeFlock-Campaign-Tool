# Data handling

The open questions for G are in `docs/DATA-HANDLING-DECISIONS.md` in the repository
(not included in the portable skills bundle).

Use an external campaign directory and keep private originals, mail, subscriber
data, signed links, credentials and analysis out of source control. The CLI sets
a restrictive umask and private storage permissions, but does not encrypt disk:
use appropriate host encryption, access controls and backups.

A campaign copied by hand (`cp -r`, `rsync`, unzip) under a loose umask can leave
records and the ledger readable by other users. `status --check` names each such
`private/` entry as `loose_mode`; `status --fix-modes` sets exactly those to 0700
(directories) or 0600 (files), never follows symlinks, then re-runs the check.

For each receipt record stable provider/message/attachment identity, original name,
source agency/request, receipt time and SHA-256. The starter captures only the
minimal source ID, name, time and bytes; full provenance relationships are planned.

Hash-deduplicate bytes, not correspondence context. A portal notice is not a
received file. An extraction success is not proof of full reading. Track unsupported,
expired, denied, redacted and partially extracted items explicitly.

Avoid reporting a blank export cell as a proven missing native value. Obtain schema,
related tables, redaction explanations and actual duty scope. Keep counterevidence.

Public exports require an exact artifact allowlist, privacy review, redistribution
rights, accessible source locators and correction linkage. Do not publish originals
merely because an agency supplied them. Public records can still expose private people.

Backups and portable exports must preserve originals, hashes, receipt relationships,
reviews and corrections. A website-only backup is not a records backup.

A subscriber list is personal data we collect from members of the public about their
interest in police surveillance. The workspace stores each address once, in its
`subscriber` table, only to enforce double opt-in; it is never published, rendered,
reported or exported (the D1 export excludes it). Only counts are displayed, and events,
incidents and logs carry a hash of the address, never the address.

## Private personal-data register

`python3 -m campaign_tool personal-data --directory D [--json]` lists the private
files in `D/kit/` and `D/private/` that can hold a person's name, email, phone or
postal address. It is read-only: it creates, edits, moves and deletes nothing, needs
no network, and never prints a record value. Deleting anything is the organizer's
decision; the command only says what is there.

`--json` prints one document, `{"schema_version": 1, "files": [...], "notice": "..."}`.
Each row has exactly `kind`, `path`, `purpose`, `status`, `record_count`,
`oldest_record_date` and `newest_record_date`. The text output prints the same
columns, one row per line, then the notice. The nine kinds are `requester`
(`kit/request-settings.json`), `reviewer_roster`, `review_assignments`,
`comments_sent`, `correction_requests`, `contact_log` (the contacts inside
`kit/sent.json`), `volunteer_assignments`, `supporters_consent` and `speaker_plans`.

- `status` is `present`, `absent`, `unreadable` or `unknown_kind`. A store that is
  oversized, malformed, a symlink, a hard link or not a regular file is
  `unreadable` with a null count, and the command exits 1 after printing the full
  report. Everything else exits 0.
- Dates are only the dates recorded inside the file (for example `added_on`,
  `received_on`, `confirmed_on`, a contact's `on`). Request owners and speaker
  plans record no date, so their dates are null. File modification times are never used.
- Every other file under `kit/` or `private/` (originals, the ledger, drafts, letters,
  signers, temporary and hidden files) is listed as `unknown` / `unknown_kind` and is
  never opened. Unknown does not mean free of personal data, and the command makes
  no promise about free text inside any file, or about originals beyond this list.
- File names can themselves identify someone, so keep the output private too.
- A symlinked campaign folder, `kit/` or `private/` is refused, as is a folder that
  cannot be listed completely (`personal-data inventory incomplete; review private
  folders locally`). No partial report is printed.
- The same refusal applies, in both the register and `--find`, to a tree with more
  than 10,000 entries in total or with directories nested more than 64 levels below
  `kit/` or `private/` (those two folders are level 0). A tree over a limit gets no
  partial report and is left untouched; it needs a private local review. Do not
  delete or move anything just to make a report look complete.

### Finding one person's name or contact (`--find`)

`python3 -m campaign_tool personal-data --directory D --find TEXT [--json]` is for a
person who asks what the campaign holds about them, or a volunteer who leaves. It
searches only these fields, and prints where a match is, never what it is:

| Kind | Searched field (`field`) | `record_position` |
|---|---|---|
| `requester` | each line of the `requester` contact block (`requester`) | `1.LINE` |
| `reviewer_roster` | each reviewer's `name` (`name`) | the reviewer's place in the list |
| `review_assignments` | each assignment's `reviewer` (`reviewer`) | the assignment's place in the file |
| `volunteer_assignments` | each request owner (`assignee`) | the owner's place in the file |
| `speaker_plans` | each speaker `label` (`label`) | the speaker's place in the list |

`comments_sent`, `correction_requests`, `contact_log` and `supporters_consent` have
no name or contact field and are listed as `no_identity_fields`: a contact's `role`
is a job title, and summaries, notes, `how` text, ids, hashes and dates are never
searched. Names written in free text there, in originals, drafts or the ledger need a
private human review.

- Matching is a literal, case-insensitive substring inside one field (or one contact
  line): Unicode case folding and canonical composition, so `Straße` matches
  `STRASSE` and a composed or decomposed `é` match each other, but accents, spaces and
  punctuation count (`555-0100` does not match `5550100`). There is no regex and no
  match across lines or fields.
- TEXT is trimmed and must have at least 3 characters and no control or formatting
  characters; otherwise the command stops with exit 2 before opening anything. TEXT
  is never printed, stored or written anywhere by the tool, but your shell history
  and process list can still hold it: run it only on your own machine, never in a
  shared chat, issue or recorded session, and clear the history entry if needed.
- `--json` prints `{"schema_version": 1, "mode": "find", "files": [...],
  "matches": [...], "notice": "..."}`. A match has exactly `kind`, `path`,
  `file_position`, `record_position` and `field`; a file row has exactly `kind`,
  `path`, `file_position`, `status` and `searched_fields`. The text output prints the
  match table, a blank line, the file table, then the notice.
- `path` is the catalog path (with a literal `*.json` for review assignments and
  speaker plans), never a real file name, which can itself name someone.
  `file_position` is that file's place among the kind's files sorted by name, the
  same order the register above prints them in; it is only valid until a file is
  added, renamed or removed. Every never-opened file is one `unknown` row,
  `kit/<unknown>` or `private/<unknown>`, with status `not_searched`; its
  `file_position` is its place among that folder's unknown rows in the register's
  order (sorted by relative path, so `private/drafts.txt` precedes
  `private/drafts/note.txt`).
- `status` is `searched`, `no_identity_fields`, `absent`, `unreadable` or
  `not_searched`. An unreadable store (malformed, oversized, a link, not a regular
  file) gives no matches at all, even from records before the problem, and the
  command exits 1 after printing the rest. The register's refusals apply unchanged
  and print nothing. Otherwise it exits 0, with or without matches.
- No match is not evidence that the campaign holds nothing about a person: free
  text, unknown files, backups and copies elsewhere are outside this search. Keep the
  output private; it is read-only and deletes nothing.
