# Bounded format extraction

This adapter reuses the existing intake worker in an isolated child process. An original is checked against its expected hash, copied into a private run directory and parsed there. Input bytes are unchanged. The returned receipt describes extraction and never marks a document reviewed.

```sh
python -m campaign_tool.records.extraction_routes --source /private/original --sha256 EXPECTED_HASH --output-root /private/derived --format pdf
```

The output root must already be private, owned by the current actor, and free of symlink aliases. Each run retains a receipt, units and derivative files. The CLI summary on stdout omits units and absolute private paths (`run_path`, `ocr_output_root`); it reports `run_id`, the run directory name under the output root. The retained receipt keeps the full paths privately. Source, output size, child memory, time and unit count are bounded. Errors become explicit blocked results. No parser output or raw record can choose an executable.

Text, email, CSV/TSV, Word, spreadsheets and archives use the existing parser. Spreadsheet formulas and macros are never executed. Image parsing records dimensions and requires visual review. MSG decoding preserves ordinary attachment bytes by hash and records attachment locators. Embedded message objects that do not expose original bytes remain explicit decoder gaps. Optional parser versions are pinned in requirements-records-parsers.txt for an isolated runtime; this package does not install them or change a host image.

PDF receipts enumerate every expected page. Blank, failed and missing pages remain partial or blocked. Confidence stays unknown unless a real OCR measurement is available; successful text extraction never certifies visual fidelity. OCR runs only through the per-page helper in `docs/RECORDS-OCR.md`; there is no whole-document OCR path and no `ocr.pdf` derivative. OCR is configured by passing `ocr_output_root` (an existing private receipt root), `ocr_tools` (the doctor-resolved tool map from `extract.ocr.dependency_status()`) and `tool_signature` together; the CLI takes `--ocr-output-root` and `--ocr-tool-signature` and runs the doctor itself. The configuration is validated before any run directory is created, so misconfiguration is a clear `ocr_configuration_*` error rather than a parser failure. Without configuration, PDFs with non-ok pages report `ocr_runtime_unconfigured`. OCR activation and full runtime acceptance are separate deployment steps.

`inventory()` supplies a route and owner for each unfinished original, including unknown formats. Low-value roles remain visible as proposed classifications. The canonical ledger/runner adapter consumes these receipts in its own transaction; this format wrapper never edits canonical stages itself.

Tests use synthetic files and do not contact external services. Linux is the target for the child resource limits and secure path traversal. A missing optional parser is an explicit runtime gap, never silently treated as successful extraction.


Only pages whose native status is not `ok` are sent to per-page OCR; native-text pages keep their original units and page rows unchanged. For each OCR page the row gets `method` `local-ocr-3`, the Tesseract version label as `method_version`, the receipt's confidence, `ocr_receipt_id` and `ocr_receipt_sha256`, status `partial` and reason `ocr_visual_review_required`. The page's unit text is the receipt's `sidecar.txt`, used only when the receipt status is `ocr_text_unreviewed` or `visual_check_queued`. A blocked receipt leaves the native unit in place and makes the row `blocked` with the receipt's `blocked_reason`. Page locators come from the receipts, so the page denominator never changes. The merged units are written to `ocr-derived/` beside the untouched `derived/` output, and the receipt records `ocr_receipts` (page, receipt id, receipt hash, status) plus `pre_ocr` counts. A bounded OCR batch failure is reported as `ocr_failed` and the native results stay `partial`. Every OCR page stays on a visual-review hold. Images with multiple frames receive a separate item locator for each decoded frame. The PDF and spreadsheet child records its actual installed parser dependency version.

Runtime limits are per process/per output file and bounded parent waits. The timeout path kills the worker process group. These are not a container disk quota or network sandbox; deployment must provide those controls. Full OCR engine and descendant cleanup acceptance require the permitted production-equivalent runtime. Unit tests alone do not establish that runtime acceptance.

## Search

`tools/records_search.py` searches the extracted text this adapter already wrote; it never extracts or OCRs again.

```sh
python3 tools/records_search.py index --store /private/store --out /private/search-index
python3 tools/records_search.py query --index /private/search-index '"retention period" AND camera'
python3 tools/records_search.py stat --index /private/search-index
```

The store lists documents in `agencies/<slug>/INVENTORY.csv` (`request`, `doc_id`, `sha256`) and reads each original's `agencies/<slug>/extracted/<sha256>/units.jsonl`. Every document is recorded as `text`, `no-text` (units absent, or present with no text) or `unreadable`. Unchanged extraction bytes are a no-op on re-index; changed bytes replace only that document's rows; the new index is built in a temporary sibling and swapped in, so a crash keeps the previous index.

A hit prints `document_id | page | offset | request | agency | sha256` (offset is the UTF-8 byte offset of the first matched term on that page). No document text, snippet or highlight is ever printed: open the cited document to read the context, so a result set can be pasted into notes without carrying record content. Every query and `stat` ends with `coverage: indexed=N no-text=N unreadable=N; extracted text is unreviewed`, and a zero-hit query over a corpus with un-extracted documents says `no hits in indexed text; N documents have no extracted text`, never a bare "no results".

Queries are literal words and quoted phrases joined by `AND` / `OR`; regular expressions, wildcards and over-long queries are usage errors (exit 2).

The index holds record text, so three independent mechanisms keep it private: `index` refuses an index folder inside any repository or under a public output folder (`public`, `site`, `dist`, `build`, ...); the index file name and `.rsidx` suffix are in `campaign_tool/records/public_scan.py`'s denied set, so `tools/check_public_tree.py` fails on a stray index and names it; and `query` prints only ids, pages, offsets, slugs and hashes. The folder is 0700 and the file 0600. There is no export, copy or upload path. A hit is a pointer, not a finding, and marks nothing reviewed.
