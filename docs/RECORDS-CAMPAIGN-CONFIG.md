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
