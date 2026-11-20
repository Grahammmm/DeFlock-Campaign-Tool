# Several campaigns from one install

An organizer covering three counties should not run three copies and keep them in step by hand. The campaign directory
is already external to the repository; this adds a name for each one and one status across all of them.
Local only. Nothing is published, sent, copied or uploaded.

```sh
python3 -m campaign_tool campaigns add slo --directory ~/campaigns/slo
python3 -m campaign_tool campaigns add humboldt --directory ~/campaigns/humboldt
python3 -m campaign_tool campaigns list
python3 -m campaign_tool status --campaign slo          # the same as: status --directory ~/campaigns/slo
python3 -m campaign_tool campaigns status
python3 -m campaign_tool campaigns today               # one private to-do list across every campaign
python3 -m campaign_tool campaigns today --as-of 2026-03-15 --json
python3 -m campaign_tool campaigns remove humboldt      # unregisters; the directory is not touched
```

## The register

- One file, `$XDG_CONFIG_HOME/deflock-campaign/campaigns.json` (default `~/.config/...`; `DEFLOCK_CAMPAIGN_REGISTER`
  names another), mode 0600, never inside the repository. It holds `name`, `directory` and `added_at`, plus optionally one `records` binding (below), and nothing else:
  no agency, request, document or record text.
- `add` refuses a duplicate name, a missing or non-directory path, a symlinked path, a path inside (or around) the
  repository, a directory with no readable `campaign.json`, and a directory that is the same as, inside or around one
  already registered. A name is 1-40 letters, digits, `.`, `_` or `-`; names are case-sensitive.
- `remove` only unregisters. There is no purge, move or delete.
- The register is written to a same-directory temporary file and swapped in with `os.replace`. Its read-modify-write is
  serialized by an advisory lock on the register file itself, so two concurrent `add`s both land. This is the one lock in
  the feature: it is taken only for that transaction, never inside a campaign directory, and never while a campaign
  command runs or a campaign is read for `campaigns status`. There is no cache, index or database across campaigns.
- The register has a ceiling of 1 MiB (1,048,576 raw bytes, whitespace and the final newline included). A larger
  register is refused whole, with no truncation, partial listing or automatic repair, before any campaign is read. A
  compact register that is readable can still be refused by `add` or `remove` when the pretty-printed candidate it
  would write is above the ceiling; the register is then left as it was. This is an offline resource guard only.

## `--campaign NAME`

Every command that takes `--directory D` also takes `--campaign NAME`; giving both is a usage error, and an unknown name
lists the registered names. The command then behaves exactly as with `--directory <that path>`.

Isolation is enforced rather than assumed. A command given `--campaign A` cannot open, list, write or rename a path under
any other registered campaign, whether a symlink, a `..` or a path inside `campaign.json` points there; the attempt is
refused with `a command for one campaign may not reach another registered campaign`. `--directory` is unchanged.

## `campaigns status`

One private line per campaign, read from that campaign's own recorded files (`kit/sent.json`, `kit/requests/`,
findings and the law package), each campaign confined to its own directory while it is read:

```
campaign | NAME | requests=N open=N waiting-send=N waiting-publish=N law=STATUS | private; review-required
```

`waiting-send` is a drafted request not yet recorded as sent; `waiting-publish` is a finding with every review gate met
that has not been published. The view shows the gate and acts on none. A campaign whose state cannot be read prints
`NOT CURRENT: <ErrorType>` and the others still print; the command exits 3 then. No record text, name, address, email,
phone or plate appears. Review receipts stay bound to the campaign that issued them, so an approval recorded in one
campaign never releases anything in another.

## `campaigns today`

`today` for every registered campaign at once, for the organizer covering several counties. Unlike `campaigns status`
(counts only), this list holds **private work details**: agency and meeting names, finding ids, draft file paths and
comment instructions. Keep it off screens and pages you share.

```
campaign | NAME | N. [KIND] WHAT — COMMAND | private; review-required
campaign | NAME | Nothing due on YYYY-MM-DD. | private; review-required
campaign | NAME | NOT CURRENT: <ErrorType> | private; review-required
```

- Campaigns are listed by name in the register's case-sensitive order (`Alpha` before `alpha`). Inside one campaign
  the items are exactly that campaign's `today --directory D` list in its own ranking, numbered from 1; there is no
  urgency sort across campaigns and no renumbering.
- Each campaign is read alone, confined exactly as for `campaigns status`, and already before its `campaign.json` is
  read; the confinement is released after every campaign, whatever happened.
- Every command that names the campaign uses `--campaign NAME` instead of `--directory PATH`. Path operands stay paths
  (a finding draft file, `--into`), and human instructions (submit a written comment, check an official source, fix a
  file) are unchanged. Nothing listed is run, fixed, sent or published. The newsletter module
  (`python3 -m campaign_tool.newsletter`) takes only `--directory`, so its hint keeps `--directory PATH` and runs as printed.
- A campaign that cannot be read (missing directory or `campaign.json`, a refused path, a source that `today` would
  list as `unreadable`) prints one `NOT CURRENT: <ErrorType>` line instead of any of its items, never the error text;
  the other campaigns still print, and the command exits 3. Run `today --campaign NAME` to see what is wrong. A source
  `today` reports as unreadable shows as `NOT CURRENT: ValueError`, whatever the original error was.
- `--json` prints one flat array. Each item is the campaign's own `today` item (`rank`, `kind`, `what`, `due`,
  `command` and any recorded-receipt fields) plus `campaign`; a campaign that cannot be read is
  `{"campaign": NAME, "rank": null, "kind": "not_current", "what": "NOT CURRENT: <ErrorType>", "due": null,
  "command": null}`. A campaign with nothing due adds no JSON item.
- No register, or an empty one: `campaign | (none registered) | private; review-required` (JSON `[]`), exit 0; no
  register or folder is created. A bad `--as-of` stops with exit 1 before anything is read. `--as-of` plans one day
  from the files as they are now; it is not a historical view. Without it, today's date is taken once for every
  campaign.
- Exit codes: 0 every campaign read, 3 at least one `NOT CURRENT` (or a degraded records summary, below), 1 a bad date
  or an unreadable register.
- Read-only: no lock, cache, index or ledger, and the register and every campaign keep their bytes, paths and modes.
  `today --directory D` and `today --campaign NAME` for one campaign are unchanged.

### Records work per campaign (optional binding)

A campaign can be bound, by editing the private register, to the console export of its records project. This is an
explicit operator edit: there is no command, no automatic registration and no scan; `campaigns add` still creates an
unbound entry. Add one `records` object to that entry (placeholder values; take the identity from `source_project_id`
in your imported export's `EXPORT.json`):

```json
{"name": "example", "directory": "/path/to/campaign", "added_at": "2026-01-01T00:00:00Z",
 "records": {"export_root": "/path/to/console-exports/example", "source_project_id": "0123456789abcdef"}}
```

`export_root` is the host directory that holds `exports/` and `current` (an absolute, normalised path with no symlink
in it; it may sit inside its own campaign but never inside, around or equal to another campaign's directory, another
binding's export root or the repository). `source_project_id` is exactly 16 lowercase hex characters, and two
campaigns may not share one. Any other key, shape or overlap refuses the whole register without echoing the values.
Unbound entries and their output are unchanged.

For each bound, readable campaign `today` appends one `records_summary` row right after that campaign's site-kit rows
(`rank` null, `due` null, `command` null, `what` "Records work (on our records; not a legal finding)", plus a
`records` object: `state` (`ok`, `no_export` or `unavailable`), `code`, `exported_at`, `age_hours`, `stale`, the four
counts and `unavailable`). A bound campaign with nothing due shows this row instead of "Nothing due". The counts come
from `console-model/campaign-summary.json`, written by `records console-export` and sealed by the normal export
manifest, and are as of the export, not today's live state:

- `drafts_waiting`: pending drafts (suggested next steps included) except message ties.
- `messages_to_tie`: pending message-to-request ties only, not every unjoined message.
- `requests_overdue`: the console Home's "overdue" waiting-request count, i.e. an overdue mark is recorded; it is a
  computed date on our records, not a legal finding.
- `holds_needing_decision`: Home decisions that are public status lines or update packets only (a subset of Home's
  "need your decision" count).

A count that could not be loaded is `null`/`unknown`, never zero; the row lists it in `unavailable` and `today` exits
3. Only `EXPORT.json` and that one summary file of the campaign's `current` export are opened (no originals, mail,
journals, live project or older export), and the summary is bound to its manifest by a hash, which is not a signature
or an approval. `age_hours` is whole hours from the export time to the moment `today` runs (not `--as-of`); 24 hours
or more is `stale` and printed as `stale (N hours old)`; this is a display threshold, not a records or legal policy. A
missing, unreadable or future export time gives code `RECORDS_EXPORT_TIME_UNAVAILABLE` and exit 3. No export prints
`records: no records export` (exit 0). An export made before this feature says `records summary not in this export;
refresh the console export` (exit 3), and a damaged one gives `RECORDS_EXPORT_UNAVAILABLE` (exit 3); neighbouring
campaigns and the campaign's own site-kit rows are still shown. Re-run `records console-export` and `console-import` to
refresh. Nothing here sends, publishes, approves or deploys anything.

## Records campaign readiness

```sh
python3 -m campaign_tool.records campaign-readiness --project P [--json] [--analysis-root R] [--portal-approval-file F]
```

Read-only inventory of what still keeps a campaign's records pipeline from running on its own. Per agency in the
campaign's `content/agencies.json` it reports follow-up autonomy (`off`/`draft`/`invalid`), signature present/missing,
the asks and follow-up coverage tables (`present`, `proposed` when only `ID.proposed.tsv` exists, `missing`,
`invalid`), analysis profile and method, and the retention row; plus the seven `campaign.json` v1 fields, the law
package status (`draft`/`reviewed`) and whether a portal approval file exists. Each gap lists a `command` to run or a
`human` step, using the literal placeholders `P`, `R`, `S`, `ID`; nothing is executed.

- The report also has an additive `starter_files` map (`followup_settings`, `retention_file`, `asks_directory`,
  `coverage_directory`; each `present`/`missing`/`invalid`) and a matching `starter_files:` text line. It is physical
  presence only (no-follow type check): it is not validity, not a confirmed table and not permission to draft. A
  project made by `project-init` has all four present while every agency still shows autonomy off, signature missing
  and asks, coverage and retention rows missing. The schema string is unchanged.
- The analysis root and portal approval file live outside the project, so they are not guessed: without
  `--analysis-root` / `--portal-approval-file` they read `not_configured`, never `missing`.
- The approval file is checked by presence only (no-follow `stat`); it is never opened, so presence is not approval.
- Output holds agency ids, field names, states and fixed `READINESS_*` codes only: no record text, names, addresses,
  signature text, URLs or input paths. Nothing is written, sent, published or approved.
- Exit codes: 0 every input readable (autonomy off, missing files and a draft law are still 0); 1 an input was
  unreadable or malformed (`READINESS_CONFIG_INVALID`, `_SETTINGS_INVALID`, `_ASKS_UNAVAILABLE`,
  `_COVERAGE_UNAVAILABLE`, `_PROFILE_INVALID`, `_METHOD_UNAVAILABLE`, `_RETENTION_INVALID`, `_LAW_UNAVAILABLE`,
  `_PORTAL_APPROVAL_UNAVAILABLE`), the other rows are still printed; 2 the project, its campaign pointer or the agency
  list cannot be read (`READINESS_PROJECT_UNAVAILABLE`, `_CAMPAIGN_ROOT_UNAVAILABLE`, `_AGENCIES_INVALID`) with nothing on
  stdout. No exit code means ready to send.

- Run it as the records runner user before a switch. It also checks, for the user running it, whether `run-outputs/` and
  `approvals/` inside the project can be used or created, and prints one state each in a `writable` block: `present_writable`,
  `present_not_writable`, `present_unsafe` (a symlink, not a directory, another owner, or group or other mode bits),
  `missing_creatable` or `missing_not_creatable`. Anything but `present_writable` or `missing_creatable` adds the degraded
  `READINESS_OUTPUTS_UNWRITABLE` (exit 1). Nothing is created, written or changed; the repair is an operator creating the
  directory owned by the runner user with mode 0700.

## Open question: the hosted workspace

The hosted workspace is one campaign per deployment (`CAMPAIGN_ID` in `workers/workspace/src/env.ts`). Hosting several
campaigns means either several deployments or a schema change. Either costs money, so it is the owner's decision, with
the cost shown first. Nothing is provisioned or implemented here.
