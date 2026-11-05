# Kit undo archive: size bound

`kit/sent-undone.json` is the private, append-only audit archive written by `kit-response-undo` (see the undo
section of [AGENCY-TIMELINE.md](AGENCY-TIMELINE.md)). Everything here is offline.

- **Cap.** The archive may be at most 16 MiB (`ARCHIVE_MAX_BYTES`, inclusive) of serialized output.
- **Bounded reads.** An existing archive is opened once with `O_NOFOLLOW | O_NONBLOCK`, checked with `fstat` to be a
  regular file, and read at most cap + 1 bytes. A symlink (including a dangling one), directory, FIFO, unreadable
  or oversized file is refused as `kit/sent-undone.json unreadable: ...`. Only a missing path means no receipts.
- **Append preflight.** Before any write the new archive is built in memory and measured as the exact UTF-8 bytes
  `kit._write_json` writes (2-space indent, non-ASCII kept, final newline). Exactly 16 MiB is accepted; one byte more
  is refused with `kit/sent-undone.json would exceed 16 MiB; nothing was changed`.
- **Refusal changes nothing.** Neither the archive nor `kit/sent.json` is touched, no attempt receipt is added, and no
  receipt is pruned, compressed or reset to make room. Keep the state and ask for a separately reviewed archival change.
- **Retries.** Earlier attempt receipts (an archive write that was not followed by `sent.json`) stay in the archive and
  count toward the budget, so a retry at the boundary is refused unchanged.
- **Unchanged.** The two files are still written archive first, then `sent.json`, and are not atomic together.
