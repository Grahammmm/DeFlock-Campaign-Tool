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
