# Flock fact store (schema v2)

One CSV per agency in a private store outside the repo; `tools/flock_facts.py` imports and checks it.

| Column | Rule |
| --- | --- |
| `agency`, `request`, `fact_type`, `value` | `agency`, `fact_type`, `value` non-empty; `request` non-empty when kind is `record` |
| `source_sha256` | 64 lowercase hex |
| `page` | integer >= 1 |
| `verbatim_quote` | non-empty, unless `value` is `UNREADABLE` |
| `fact_id` | derived, see below |
| `kind` | `record`, `policy`, `law` or `public` |
| `kind` `policy` (`--check STORE`) | source must be the POLICY version it quotes: a `doc_sha256` in that agency's POLICY.csv |
| `kind` `law` (`--check STORE`) | source must be the LAW row it quotes: a `text_sha256` in `STORE/LAW.csv`; `--check FILE` skips both links |
| `effective_from`, `effective_to` | empty or YYYY-MM-DD, from <= to |
| `added_by`, `added_at` | importer name; YYYY-MM-DD |
| `status` | `unreviewed`, `checked` or `disputed` |

The v1 import header is the first seven columns. Any other header is an error. Files are read as
`utf-8-sig` (CRLF accepted) and written as UTF-8 with `\n` line ends.

- **Slug:** agency lowercased, each run of non-`[a-z0-9]` characters becomes `-`, ends stripped.
- **Layout:** `<store>/agencies/<slug>/FACTS.csv`; directories are mode 0700 and a row's slug must match its folder.
- **Dedupe key:** (slug, `source_sha256`, `page`, `fact_type`, `value`), each stripped.
- **fact_id:** `F-` plus the first 12 hex of sha256 over the key joined with `\x1f`; import is idempotent.
- **Privacy:** the store and real FACTS.csv are never committed. The CLI prints row numbers, field
  names and counts only, and refuses a store inside the repository checkout.
- A clean `--check` is machine validity, not review or acceptance.

**Stale facts** (`stale --store DIR --date YYYY-MM-DD [--agency SLUG] [--findings FINDINGS.csv]`, read-only, ids only):
a fact is *expired* when `effective_to` is before the date (the end day itself is still in force) and *not-yet* when
`effective_from` is after it. It is *superseded* when, in the same agency, another fact with the same `kind` and
`fact_type` has a later `effective_from` on or before the date, while this fact has an `effective_from` and no
`effective_to`; the latest such fact is named. `disputed` facts are listed too. With `--findings`, each finding citing an
expired, superseded or disputed fact is listed; exit 3 when any does (without it: any expired or superseded fact).

**Review log** (`review --store DIR --agency SLUG --ids ID[,ID...] --set checked|disputed --reviewer NAME [--date D] [--note T]`):
the only way to change `status` after import. A reviewer equal to the fact's `added_by` (case and spaces ignored), an unknown or
duplicate id, a date after today or before `added_at`, or any `--check` error refuses the whole call. Each change appends a
row to the private `<store>/agencies/<slug>/FACT-REVIEWS.csv` (v1: `fact_id,from_status,to_status,reviewer,reviewed_at,note`,
mode 0600); a fact already at the target is `unchanged` and not logged. `--check` validates the log and names a fact whose
FACTS status differs from its last logged `to_status` (a hand edit). Ids and counts only; the note is never printed.

**Unreadable pages** (`unreadable --store DIR --out FILE.csv [--agency SLUG]`): writes a new private 0600 CSV `agency,source_sha256,page,fact_rows` (distinct pages with an `UNREADABLE` row; header only when none) outside the repo and `DIR/agencies`; stdout counts agencies checked, pages, documents and rows only.

## Store check

`python3 tools/flock_store_check.py STORE [--backups DIR --max-backup-age-days N] [--today YYYY-MM-DD]` runs the facts,
law/policy, requests, contact, watch list and publish checkers in-process, then `slugs` (a ROSTER slug or WATCHLIST agency
with no agency folder is an error; a folder in no ROSTER is a note; linked folders are named, never followed) and `backup`
(the newest `DIR/*.tar` whose first member is a `flock-store` manifest, by `created_at`; hashes are not re-verified, use
`flock_backup.py verify`). One `<section> | ` block each, at most 20 error lines; exit 0 only with no errors. It writes nothing.

## Report text state

`tools/flock_report.py` reads an optional `review.text_state_file` (absolute path outside the repository to the extraction run's
`run-outputs/<run>/text-state.json`, schema `records-text-state-v1`). Each cited record that is not `text_reviewed` there is
labelled on its bullet, its Sources line (and so in `region`) and as `text_state` in `SOURCES.json`: `(OCR text, not
visually reviewed)`, `(text state: <words>)`, or `(text state not recorded)` when the file does not list it. A missing,
linked, oversize or invalid file is a `config:` error and nothing is written. The label never changes which findings are
included. Operators should point live report configs at the latest text-state file; without the key a report makes no
claim about text state.
