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

## Store check

`python3 tools/flock_store_check.py STORE [--backups DIR --max-backup-age-days N] [--today YYYY-MM-DD]` runs the facts,
law/policy, requests, contact, watch list and publish checkers in-process, then `slugs` (a ROSTER slug or WATCHLIST agency
with no agency folder is an error; a folder in no ROSTER is a note; linked folders are named, never followed) and `backup`
(the newest `DIR/*.tar` whose first member is a `flock-store` manifest, by `created_at`; hashes are not re-verified, use
`flock_backup.py verify`). One `<section> | ` block each, at most 20 error lines; exit 0 only with no errors. It writes nothing.
