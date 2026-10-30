# Private OCR human-review queue (D-D)

Vision remains off. OCR confidence at least 80 permits **read-with-flag** and
places the page on a human spot-check queue. Below 80, or with no confidence,
the page is held until a human confirms it. Rejection disallows use at any
confidence. G chooses the reviewer and sample size; software does not.

Only **G or a person G names** may record a real review decision. No model,
builder or automated reviewer may mark real pages. A role label is not proof
of authorization, and machine-valid output is not semantic acceptance.

## Commands (authorized private project only)

```sh
python3 -m campaign_tool.records.ocr_review export --project /private/project --out /private/ocr-queue.json
python3 -m campaign_tool.records.ocr_review mark --project /private/project --original-sha256 SHA256 --page 3 --decision confirmed --reviewer-label human-reviewer
```

Replace `SHA256` with the original's full lowercase SHA-256. Decisions are
`confirmed` or `rejected`. Labels match `^[a-z][a-z0-9_-]{0,31}$`: use a role,
not a person's name. A mark must name an exact pending page. Repeating it is
refused without appending. A changed derivative hash or confidence invalidates
the earlier review and requeues the page.

Export creates a new file (0600); it refuses to overwrite any existing file or
write inside `processing-v1` or `ocr-review-v1`. Its `ocr-review-queue-v1` JSON
contains pending rows sorted by original hash and page number, each with only
`original_sha256`, `page_no` and `bucket` (`spot_check` or `hold`). Counts are
`pending_spot_check`, `pending_hold`, `reviewed_confirmed`, `reviewed_rejected`,
for current review-required page bindings. No text, filename, path or confidence
value appears in the export. Keep it private; this is not a publication command.

Marks append canonical JSON lines to `ocr-review-v1/reviews.jsonl` (0700 directory,
0600 file), locked across cooperating writers, with `O_APPEND` and fsync. Each
record binds original hash, page number, confidence and derivative hash, plus
schema, decision, reviewer role, UTC timestamp and previous-line SHA-256.
**The review file contains no OCR text, document text, paths or filenames.**
Do not include a name in a reviewer label. Existing nonprivate review directories
or unsafe review files are refused, not repaired.

Every read checks the canonical hash chain. Middle-line editing or deletion
refuses export, marking, use-policy checks and extraction counting with
`ocr_review_chain_broken`. This chain is not an authenticated signature or an
external audit anchor: it cannot detect a complete rewrite or suffix truncation.
Preserve private backups under the existing operational policy.

## Read-only boundary and limitations

All ledger reads use the existing read-only snapshot. Neither command writes
`processing-v1/*`, `page_state`, the canonical ledger, or OCR/vision receipts.
After extraction, `review_required` counts pending page bindings; busy or
unreadable snapshots leave that count absent without changing the extraction
outcome. A broken review chain instead refuses the count visibly.

`page_use_allowed` is a pure policy helper, **not yet wired into downstream
consumers**. Until follow-up work does that, under-80 text is gated only by this
queue and the existing `needs_visual_review` flag. This unit does not enable
vision, perform OCR, choose human samples, authorize live runs, or send/publish
anything. Development and acceptance use synthetic ledgers only; existing
live-run, acquisition, canonical-store, queue and publication holds remain.
