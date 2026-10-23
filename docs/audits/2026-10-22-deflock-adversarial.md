# Records pipeline: adversarial test report, 2026-10-22

**Scope:** `campaign_tool/records/ocr_stage.py`, `extract/ocr.py`, `extract/vision.py`, `native_extract.py` and the RFC822 intake inventory, at `main` @ `75494fa`.
**Builds on:** `docs/audits/2026-10-22-deflock-opus-audit.md` (branch `claude/audit-deflock`). Nothing it already lists (C1, H1, H2, M1-M10, L1-L8) is repeated here.
**Method:** New fuzz and adversarial tests with synthetic data only. No real records, no credentials, no network. Two tests use a loopback socket on `127.0.0.1`. No OCR tool or model was run; stub runners stand in for them. No source file was changed.
**Status:** A code-and-test review only. It is not a legal, privacy or capacity review, and nothing here means any finding or package has been reviewed.

## How to run

```
ADVERSARIAL_REPRO=1 python3 -B -m unittest discover -s tests/adversarial_repro -t . -v
```

- Without `ADVERSARIAL_REPRO=1` all 45 tests are skipped, so the normal suite stays green.
- Each test asserts the **correct** behaviour. A failing test is the proof of a defect and will pass once the fix lands.
- On `75494fa`: **45 tests: 26 fail, 19 pass.** The runner reports `failures=25, errors=7` because it counts each sub-case (4 language codes, 4 junk text layers) on its own.

## Summary

- **High (4):** X1, X2, X4, X6.
- **Medium (5):** X3, X5, X7, X8, X10.
- **Low (6):** X9, X11, X12, X13, X14, X15.
- **Held up under attack (19 tests):** page order, duplicate inputs, timeouts, resume, page selection, mutated mail. See the last section.

## Findings

### High

**X1. One unencodable model reply stops the whole OCR run, every run.**
- **Where:** `ocr_stage.py:880` (`text.encode("utf-8")` in `_publish`); `:1042-1050` (`run` only catches `BackendError` and `OSError`); `:1090` (`main` turns it into `STAGE_INTERNAL_ERROR`).
- **Impact:**
  - JSON allows `"\ud800"`. `json.loads` accepts it and `parse` returns it.
  - Publishing then raises `UnicodeEncodeError`. Nothing catches it, so the run aborts.
  - The reply is deterministic (temperature 0), so the same document fails on every run.
  - Every document behind it in the queue is never processed.
  - The half-built staging folder (holding page text) is left behind.
- **Fix:**
  - In `parse`, try `content.encode("utf-8")` and raise `BackendError("vision_response_invalid")` on failure.
  - Wrap each document in `run` so an unexpected exception becomes an `ocr_failed` outcome with a fixed code. One document should never stop the queue.
- **Tests:** `PoisonedVisionTextAbortsStage` (2). **Confidence:** High.

**X2. A badly-formed reply from the vision server also stops the whole run.**
- **Where:** `ocr_stage.py:320-331` (`urllib_transport` does not catch `http.client.HTTPException`); `:395-398` (`parse` does not catch `RecursionError`).
- **Impact:**
  - A non-HTTP reply (`BadStatusLine`), a chunked reply cut short (`IncompleteRead`), or a deeply nested JSON body leaks a raw exception.
  - The run ends as `STAGE_INTERNAL_ERROR`. A server restart or a hostile loopback listener can do this.
  - A plain connection reset is handled; these are not.
- **Fix:** Catch `http.client.HTTPException` and map it to `vision_unavailable`. Catch `RecursionError` and map it to `vision_response_invalid`. Add the per-document guard from X1.
- **Tests:** `MalformedVisionHttp` (3), `VisionReplyParsing.test_deeply_nested_json_reply_is_an_invalid_response`. **Confidence:** High.

**X4. A quote mark in the OCR output makes the confidence score wrong, or kills the batch.**
- **Where:** `extract/ocr.py:179-190` (`_confidence` uses `csv.DictReader` with the default quoting); caller at `:659`; handlers at `:669-678`.
- **Impact:**
  - Tesseract TSV is not CSV. A word that starts with `"` opens a quoted field that swallows every row after it.
  - A page of mostly garbage words (mean 28 over 51 rows) scored 94.5 once the rows after one `"x` word were dropped.
  - With a score over 80 it becomes `ocr_text_unreviewed` instead of `visual_check_queued`. Bad text skips the visual check.
  - If the swallowed part exceeds 131072 characters, `csv.Error` is raised. No handler catches it, so **the whole batch aborts**. The pages after it get no receipt, the private raster in `.attempt-*` is left behind, and every re-run hits the same page.
- **Fix:** Parse with `csv.reader(..., quoting=csv.QUOTE_NONE)` (or split on tabs). Catch `csv.Error` and record the page as blocked.
- **Tests:** `ConfidenceParsing` (3). **Confidence:** High.

**X6. A scan with a small text layer is never OCR'd.**
- **Where:** `extract/ocr.py:623-628` (`if machine_text.strip(): status = "skipped_machine_text"`).
- **Impact:**
  - Any non-blank text layer makes the whole page "machine text" and OCR is skipped.
  - A scan with only a Bates stamp (`BATES-000123`) is skipped. Productions are often stamped this way.
  - Text that is only `U+FFFD`, NUL, zero-width space or a BOM also counts, because `str.strip()` does not remove those.
  - The receipt records no text for the page. This is silent loss of coverage on image-only pages.
- **Fix:**
  - Count only letters and digits. Use a floor such as 20 characters.
  - Also compare against image coverage: a page with a full-page image and almost no text should be OCR'd.
  - Reject pages whose "text" is only control, format or replacement characters.
- **Tests:** `TextLayerHeuristic` (1 + 4 sub-cases). **Confidence:** High for the code path. Medium for severity: the real downstream use of `skipped_machine_text` pages was not traced.

### Medium

**X3. A document that was not published is still counted as new output to review.**
- **Where:** `ocr_stage.py:868-871` (transient result is returned unpublished, status stays `ocr_partial`); `:903` (`processed` counts `ocr_partial`); `:915`, `:928-929` (`output`, `new_data`, `review_required`).
- **Impact:** After a vision timeout nothing is written, but the result line says 1 document was output and needs review. The consumer is told there is data that does not exist. This is a timeout shown as progress.
- **Fix:** Count a result as output only when it was published. Give transient outcomes their own status (for example `ocr_deferred`) and leave them out of `new_data` and `review_required`.
- **Test:** `TransientCountedAsOutput`. **Confidence:** High.

**X5. A PDF with no pages is a silent success.**
- **Where:** `extract/ocr.py:575-576` (`count = 0`, `selected = ()`); `:829-834`, with `blocked == 0` returning exit 0.
- **Impact:** `extract_image_only_pages` returns `[]` and the CLI prints `pages: 0` and exits 0. A document that cannot be read looks finished. (Truncated or garbled PDFs are refused correctly.)
- **Fix:** Raise `ValueError("pdf_has_no_pages")` when `count == 0`.
- **Tests:** `MalformedPdfs` (2). **Confidence:** High.

**X7. An interrupted OCR attempt leaves a private page image on disk.**
- **Where:** `extract/ocr.py:607` (`mkdtemp`), cleaned only in the `finally` at `:722-727`.
- **Impact:** `KeyboardInterrupt` or `SystemExit` between the two `try` blocks skips the cleanup. `.attempt-*` keeps the page PNG and OCR text. Nothing sweeps it later.
- **Fix:** Put the `mkdtemp` and the whole attempt in one `try/finally`.
- **Tests:** `PageSelectionAndResume.test_interrupt_during_ocr_leaves_no_attempt_directory` (and the leak assertion in `ConfidenceParsing`). **Confidence:** High.

**X8. A disk error on one document loses the rest of the queue and is reported as "queue unreadable".**
- **Where:** `ocr_stage.py:876-895` (`_publish` has no cleanup on error); `:1049-1050` (`except OSError` maps to `OCR_QUEUE_UNREADABLE`).
- **Impact:** With a full disk, the first write failure ends the run. Outcomes already gathered are dropped. The error code blames the queue file. The staging folder stays.
- **Fix:** `try/finally` around `_publish` to remove staging. Handle `OSError` per document as `ocr_failed/publish_failed`.
- **Tests:** `WorkDirectoryHygiene.test_disk_error_on_one_document_does_not_lose_the_others`, `PoisonedVisionTextAbortsStage.test_failed_publish_leaves_no_staging_directory`. **Confidence:** High.

**X10. The cache key leaves out settings that change the stored text.**
- **Where:** `ocr_stage.py:361-363` (`identity()` omits `max_tokens`); `:683-686`.
- **Impact:**
  - A run with a small `max_tokens` stores truncated page text. Raising the limit later reuses the same cache entry, so the truncated text is served as `ocr_complete`.
  - The prompt text, `THINK_BLOCK` and sidecar-parser changes are code constants that only count if someone remembers to bump `SIDECAR_PARSER`. The vision prompt has no version.
  - Model, endpoint and dpi **are** keyed correctly.
- **Fix:** Add `max_tokens` and a hash of `VISION_PROMPT` to `identity()`. Bump a vision-parser version the same way as `SIDECAR_PARSER`.
- **Test:** `CacheKeyCompleteness.test_max_tokens_is_part_of_the_cache_key`. **Confidence:** High.

### Low

**X9. Work folders from a killed run are never cleaned.**
- **Where:** `ocr_stage.py:806-811`; `.work/doc-*` is removed only by the run that created it.
- **Impact:** After a SIGKILL or OOM, `source.pdf` (a private copy of an original, up to 512 MiB) stays in `.work` forever.
- **Fix:** While holding the stage lock, remove all `.work` children at the start of `run`.
- **Test:** `WorkDirectoryHygiene.test_stale_document_copies_from_a_killed_run_are_cleared`.

**X11. Queue validation accepts a trailing newline in `document_id` and `sha256`.**
- **Where:** `ocr_stage.py:94-95` (`^...$` matches before a final `\n`).
- **Impact:** An id such as `"doc\n"` or a 65-character "hash" passes. The hash check later rejects the file, but ids with a newline reach the result files.
- **Fix:** Use `re.fullmatch` or `\Z`.
- **Tests:** `QueueValidation` (2).

**X12. A relative `--source-root` crashes the stage.**
- **Where:** `ocr_stage.py:458` (`os.path.commonpath` raises `ValueError` when mixing relative and absolute paths); not caught in `read_queue` or `run`.
- **Impact:** Every queue line crashes the run as `STAGE_INTERNAL_ERROR` instead of a clear config error.
- **Fix:** `os.path.abspath` the root in `run` (or reject it with `OCR_CONFIG_INVALID`).
- **Tests:** `QueueValidation` (2).

**X13. Only a leading reasoning block is removed from vision text.**
- **Where:** `ocr_stage.py:105` and `:402` (`^\s*<think>.*?</think>\s*`, `count=1`).
- **Impact:** A second block, or a block after a short preamble, stays in the stored page text.
- **Fix:** Strip every closed block. Treat any leftover `<think>` tag as `vision_response_invalid`.
- **Tests:** `VisionReplyParsing` (2). **Confidence:** Medium (it depends on how the model formats replies).

**X14. The stage cannot be set to OCR Chinese, Serbian (Latin) or similar.**
- **Where:** `ocr_stage.py:239` (`[a-z]{3}(\+[a-z]{3}){0,3}`). `extract/ocr.py:556` accepts `[a-zA-Z0-9_+]`.
- **Impact:** Tesseract codes such as `chi_sim`, `chi_tra` and `srp_latn` are refused with `OCR_CONFIG_INVALID`. The two OCR paths disagree.
- **Fix:** Allow an optional `_xxx` part, for example `[a-z]{3}(_[a-z]{3,4})?`.
- **Test:** `LanguageCodes` (4 sub-cases).

**X15. Two run ids can share one outcome file.**
- **Where:** `native_extract.py:288` (the id is filtered to `[A-Za-z0-9-_.]`).
- **Impact:** `2026-10-22/run-1` and `2026-10-22run-1` both become `extract-2026-10-22run-1.json`. The later run overwrites the earlier run's outcome list.
- **Fix:** Reject unsafe ids, or add a short hash of the original id to the name.
- **Test:** `OutcomeFileNames`.

## Held up under attack (tests that pass)

| Area | What was tried | Result |
|---|---|---|
| Page order | 500 random sidecars (skip runs, blanks, accents, CJK) | Page numbers and skip-range expansion always correct |
| Sidecar fuzz | 2000 garbage sidecars (bad markers, huge numbers, NUL) | Only `BackendError`, markers always match their page |
| Citation offsets | Blank pages 2 and 4 of 4 | Vision asked for page 4 only; text is saved as `page-0002` and `page-0004`, none for page 1 |
| Page selection | `0`, `-1`, past the end, duplicates, `True`, `1.0`, `"1"` | All refused; selected pages keep their true numbers |
| Duplicates | Same bytes under two ids; one extra byte | One tool run for identical bytes; a one-byte change is a new document |
| Timeouts | OCR tool and Tesseract timeouts | Reported as failure or blocked receipt, never cached, never text |
| Resume | Second run over finished pages | Same receipt ids, tools not called |
| Huge page | 200000 x 200000 pt MediaBox | Blocked as `page_raster_bound`; `pdftoppm` not run |
| Damaged PDFs | Truncated mid-file; random bytes after `%PDF-` | Refused with an error |
| Queue lines | 2000 mutated lines | Only `BackendError` |
| Queue handoff | 11 odd file names (spaces, quotes, NFC/NFD, newline, bidi) | Every line from `native_extract` is accepted by `ocr_stage` |
| Mail | 1500 mutated messages; 6 odd-encoding cases | Only fixed failure codes; odd encodings refused, not guessed |

Also checked by hand (not in the suite): Hindi, Thai and Vietnamese text in NFC and NFD forms pass `Quality.check` the same way, and a PDF truncated at seven different points was always refused.

## Traced but not demonstrated

- **Unbounded capture:** `extract/vision.py:255` and `extract/ocr.py:241` use `capture_output=True`. The output is held in memory before the size check at `vision.py:266`. The child's memory limit does not cover the parent. A very chatty adapter could use a lot of memory. Not run, to avoid a memory spike.
- **Durability:** `ocr_stage._publish_noreplace` (`:664-672`) does not fsync the parent folder after the rename. After a power loss a result folder could be empty and then report `cache_damaged` until an operator removes it. Not reproducible without a crash test.
- **M9 amplification (known root cause):** a sidecar with N form feeds produces N pages, and each blank page costs one vision call, up to 100000. The fix for M9 (count pages from the PDF) closes this too.
- **Strict drop folder:** `intake/document_drop.py:22-27` refuses the whole folder if one file is empty, has another extension or is a sub-folder. This is fail-closed and may be intended, but one stray file blocks all documents.

## Run record

- Python 3.13, run as root, `pypdf` 6.x. No `ocrmypdf` or `tesseract` installed.
- Command: `ADVERSARIAL_REPRO=1 python3 -B -m unittest discover -s tests/adversarial_repro -t . -v`
- Result: `Ran 45 tests ... FAILED (failures=25, errors=7)` (26 test methods fail, 19 pass).
- Default run (no env var): `Ran 45 tests ... OK (skipped=45)`.
