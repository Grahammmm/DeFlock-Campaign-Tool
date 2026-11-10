# Checking the saved evidence store

`python3 -m campaign_tool status --check --directory CAMPAIGN` re-hashes every object in
`private/objects/` against `private/ledger.sqlite`. It works offline and never changes a
byte or a permission of the store (reading may update access times; `--fix-modes` is the separate, explicit
permission repair). Code: `campaign_tool/store_check.py`.

## Results

| Exit | Meaning |
| --- | --- |
| 0 | Stable store, no problems. The counts and a `check` block are printed. |
| 3 | The check finished and found problems (`missing_file`, `not_a_regular_file`, `size_mismatch`, `hash_mismatch`, `no_ledger_row`, `leftover_temp`, `unexpected_entry`, `loose_mode`, `invalid_ledger_row`). Nothing is repaired. |
| 1 | The check could not give a trustworthy answer and printed one `Stopped:` line, with no counts and no `check` block. |

Two fixed refusal lines stop the check (exit 1). They carry no path, error text, file
name, source id or file content:

- `Stopped: Stored object changed during check; retry with the store idle` - a stored
  object was seen changing while it was read (its size, timestamps or file identity moved,
  it ended early or grew, or the file was replaced between the last two looks). No clean
  result and no `hash_mismatch` is reported for a file seen changing. Stop whatever is
  writing to the store, then run the check again.
- `Stopped: Stored object could not be read; check permissions and retry` - an object could
  not be examined, opened or read (for example a permission error or an I/O error). Fix the
  permissions or the disk problem, then run the check again.

`status --fix-modes` may already have tightened permissions before a later refusal; the
refusal does not undo it.

## How an object is read

Each object is looked at without following links. If it is still a regular file of the
ledger's size, it is opened once with no-follow, non-blocking flags, and that same open
file is used for the size check and the hash. A link, a pipe, a socket, a directory or a
device put in place of the object is reported as `not_a_regular_file` and is never read, so
a replacement can neither point the check outside the store nor make it wait for a writer.
Reads come in chunks of at most 1 MiB and stop one byte past the ledger's size, so a file
that grows during the check cannot make it read without end. The path is looked at again
before a clean result is accepted.

## What this guarantees, and what it does not

It covers substitution of the object file itself (the last path component) and a change to
that file that the check can observe while it runs. It is not an atomic snapshot of the
store, a defence against replacing a parent directory (`private/` and `private/objects/`
must be trusted and stay in place), proof against a deliberately malicious writer, a read
timeout on a stalled disk, full hardening of the ledger, or a repair of races in
`status --fix-modes`.

## Byte counts in the ledger

Each ledger row's byte count must be a nonnegative integer (zero is a valid empty object). A
row with a valid hash and any other count (text, binary, a fractional or negative number, a
boolean, an empty value) is reported as `invalid_ledger_row` with only its hash. The bad
value is never shown, converted or repaired, and that object file is not looked at. The
other rows are still checked, and the object is not additionally reported as `no_ledger_row`.
This validates one field only; it is not a check of the ledger schema, receipts or database.

