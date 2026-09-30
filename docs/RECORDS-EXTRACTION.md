# Bounded format extraction

This adapter reuses the existing intake worker in an isolated child process. An original is checked against its expected hash, copied into a private run directory and parsed there. Input bytes are unchanged. The returned receipt describes extraction and never marks a document reviewed.

```sh
python -m campaign_tool.records.extraction_routes --source /private/original --sha256 EXPECTED_HASH --output-root /private/derived --format pdf
```

The output root must already be private, owned by the current actor, and free of symlink aliases. Each run retains a receipt, units and derivative files. Source, output size, child memory, time and unit count are bounded. Errors become explicit blocked results. No parser output or raw record can choose an executable.

Text, email, CSV/TSV, Word, spreadsheets and archives use the existing parser. Spreadsheet formulas and macros are never executed. Image parsing records dimensions and requires visual review. MSG decoding preserves ordinary attachment bytes by hash and records attachment locators. Embedded message objects that do not expose original bytes remain explicit decoder gaps. Optional parser versions are pinned in requirements-records-parsers.txt for an isolated runtime; this package does not install them or change a host image.

PDF receipts enumerate every expected page. Blank, failed and missing pages remain partial or blocked. Confidence stays unknown unless a real OCR measurement is available; successful text extraction never certifies visual fidelity. With an explicitly configured OCRmyPDF executable, the adapter preserves an OCR derivative and its exact hash. OCR output remains subject to extraction and visual-review checks. OCR activation and full runtime acceptance are separate deployment steps.

`inventory()` supplies a route and owner for each unfinished original, including unknown formats. Low-value roles remain visible as proposed classifications. The canonical ledger/runner adapter consumes these receipts in its own transaction; this format wrapper never edits canonical stages itself.

Tests use synthetic files and do not contact external services. Linux is the target for the child resource limits and secure path traversal. A missing optional parser is an explicit runtime gap, never silently treated as successful extraction.


OCR derivatives are re-extracted in a second bounded worker. The original and initial extraction remain preserved. Changed page counts are rejected; derivative text retains original page numbers plus the derivative hash, and remains on a visual-review hold. Missing OCR tool version evidence is reported as unverified. Images with multiple frames receive a separate item locator for each decoded frame. The PDF and spreadsheet child records its actual installed parser dependency version.

Runtime limits are per process/per output file and bounded parent waits. The timeout path kills the worker process group. These are not a container disk quota or network sandbox; deployment must provide those controls. Full OCR engine and descendant cleanup acceptance require the permitted production-equivalent runtime. Unit tests alone do not establish that runtime acceptance.
