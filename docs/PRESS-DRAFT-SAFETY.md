# Press draft safety

`press draft` writes private, never-sent drafts. This note explains how it keeps a volunteer's edits.

## What the hash records are

- `kit/press/.written.json` records the SHA-256 of each draft the tool itself generated into `kit/press/`.
- `kit/press.suggested/.written.json` records the same for generated files in `kit/press.suggested/`. The two records are independent.
- A matching hash is evidence that the tool wrote those exact bytes and nobody changed them. It is **not** human approval, review or a legal-readiness claim.

## What a rerun does

1. A draft in `kit/press/` is refreshed only if its bytes match its own record entry. Otherwise it is treated as your edited original and left alone; the fresh version goes to `kit/press.suggested/<same name>`.
2. A suggestion is refreshed only if its bytes match the suggestion record entry. If you edited the suggestion, or it exists with no entry, the command refuses, names the file and writes nothing. Your original and your suggestion are never overwritten or deleted, and no numbered alternative names are created.
3. A suggestion that was written before this record existed has no entry, so it is refused even if its text equals today's draft. This is deliberately conservative: content equality is not treated as proof.
4. A record that is malformed, a symlink, not a regular file, over 1 MiB or has duplicate keys, and any symlinked or non-folder output directory, is refused before any folder or file is created.

## Recovery

Move the refused suggestion aside (rename it or copy it elsewhere), then run `press draft` again. A new tracked suggestion is written. Merge wanted text by hand.

Nothing here sends, prints, posts or publishes. These checks cover static unsafe paths, not concurrent changes to the folders while the command runs; a failure after the draft is written but before its record is updated leaves a draft that the next run protects.
