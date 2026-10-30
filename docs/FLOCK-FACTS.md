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
