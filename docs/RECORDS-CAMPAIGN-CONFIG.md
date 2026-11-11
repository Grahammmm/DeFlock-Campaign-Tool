# Records campaign clock

The private refusal log reads the optional `timezone` key in exactly
`<records-project>/campaign.json`. For example, a fictional campaign can use:

```json
{"schema_version": 1, "name": "Cedar Civic Records", "timezone": "UTC"}
```

A missing file or absent key preserves `America/Los_Angeles` (Pacific).
A present file must be a JSON object with integer `schema_version: 1`;
other keys are ignored. The timezone must be a nonempty IANA zone name accepted
by Python's `ZoneInfo`, no surrounding whitespace, at most 128 characters.
Null, invalid zones, duplicate keys, malformed or oversized JSON, unreadable
files, symlinks and non-regular files are refused, not replaced with Pacific.
The file limit is 64 KiB. Invalid configuration makes the refusal log unavailable
with `denial_log_invalid_source` (the loader raises `campaign_timezone_invalid`).

Only this records project is consulted: no ancestor, sibling campaign register,
or inferred site directory. Reads do not create or modify configuration,
snapshots, approvals or caches. The project must satisfy the existing records
safe-directory boundary (no symlink directory components).

This setting currently affects only refusal-log receipt dates and their date
basis labels. Aware timestamps convert to the campaign day; recorded date-only
values stay unchanged, missing dates stay UNKNOWN, and naive timestamps remain
invalid. Pacific wording, stable row identities and review warnings are retained.
Other clocks are not migrated. A timezone is not a law package and enables no
new state's legal rules; sending and publication approvals remain unchanged.

## Campaign names and law package

`campaign_config.load(project)` returns the validated v1 fields below. `load_timezone` is unchanged and still ignores
unknown keys; `load` refuses them (`campaign_config_invalid`), as it does any invalid field. A missing file gives the
neutral defaults (display name "Records project", `us-ca`, no SLO text).

| key | rule | default |
| --- | --- | --- |
| `campaign_id` | `^[a-z0-9-]{2,40}$` | none |
| `display_name` | 1-80 printable chars, trimmed | `Records project` |
| `law_package` | `^[a-z]{2}-[a-z]{2}$` | `us-ca` |
| `public_base_url` | https, host only (no path, query, userinfo) | none |
| `records_mailbox_label` | 1-80 printable chars, trimmed | none |
| `timezone` | as above | `America/Los_Angeles` |

```json
{"schema_version": 1, "campaign_id": "cedar-civic", "display_name": "Cedar Civic Records",
 "law_package": "us-ca", "public_base_url": "https://records.cedar.example",
 "records_mailbox_label": "Cedar records inbox", "timezone": "UTC"}
```

The board shows the campaign line only when `campaign_id` is set; a package other than `us-ca` is named by its id in the
computed-dates legend. `write_change` adds a `campaign` block to the site-change file only when `campaign_id` is set, and
refuses (`campaign_config_invalid`) when the file is invalid. Unconfigured output is byte-identical to before.
