# kit-response-import: replay identity and the extension dispatch date

`kit-response-import` records an organizer-transcribed agency timeline from one CSV in one checked write. It
fetches and sends nothing. This page covers only how a row is recognised as already recorded; the step rules
themselves are those of `kit-response`.

## The CSV

The header is exactly `date,kind,note,amount,expected_on,partial`. The `expected_on` column is overloaded,
and its meaning follows the row's `kind`:

| `kind` | what `expected_on` means | stored as |
| --- | --- | --- |
| `extension` | the dispatch date the agency's extension notice states for its determination | `extension_expected_on` |
| any other kind | a production date (for example the next installment of a partial production) | `expected_on` |

The two are different promises and are never interchanged.

```csv
date,kind,note,amount,expected_on,partial
2026-03-05,acknowledged,Portal receipt,,,
2026-03-10,extension,Notice received,,2026-03-30,
2026-03-12,produced,First batch,,2026-04-15,yes
```

## Replay identity

A row equals a recorded step of the selected request when all of these match: event day, kind, normalized note,
whole-cent amount, the kind-appropriate date above, and the partial flag. Other stored fields (`recorded_at`,
`extension_reason`, records, correspondence) are outside the CSV and are left untouched.

- An identical row is skipped: preview and `--apply` both report `added 0, skipped 1`, and the file is not
  rewritten (bytes, mode and mtime unchanged). This holds for an extension recorded by `kit-response` or by an
  earlier import, even when later steps follow it.
- A row whose dispatch date is blank where one is stored, present where none is stored, or different, is not a
  duplicate. It goes through the normal `kit-response` checks and a second extension is refused with
  `extension is allowed only once` on its physical CSV line (exit 2). Nothing is written for any row in that
  file, including valid rows elsewhere in it.
- A legacy extension recorded without a dispatch date matches a CSV row with a blank date. No date is guessed
  from production or sent dates.
- Matching is per request: the same row replayed with `--request second` is checked against that request only.

## Corrections

The importer never edits or replaces a recorded step. To correct a wrong extension date, use the existing manual
undo and re-record workflow (see `docs/KIT-UNDO-ARCHIVE.md`), then import or record the corrected row.
