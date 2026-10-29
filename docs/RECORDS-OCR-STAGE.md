# OCR stage (awaiting_ocr queue, Phase 2 prep)

`campaign_tool.records.ocr_stage` drains the documents that Phase 1 native-text
extraction (T11k) queues as `awaiting_ocr`. It is prepared ahead of the
rootless flock-ocr container swap so that T6's live wiring check only has to
connect a real queue and confirm the tools. Nothing here runs on a timer, touches
the live container, or writes the canonical ledger.

Status: implemented and tested with fake backends and synthetic scanned PDFs.
Live wiring (T6) is not done. Until the pinned engine commit in
`container/flock-ocr/pins.json` is bumped in a reviewed PR, the image does not
carry this module.

## Pipeline

1. **OCRmyPDF first.** A single run per document:
   `/usr/bin/ocrmypdf --quiet --skip-text --jobs 1 --output-type pdf -l eng --sidecar sidecar.txt source.pdf ocr.pdf`.
   The sidecar becomes exactly one page per PDF page, with
   `engine=tesseract`, `method=ocrmypdf`. OCRmyPDF 14.0.1 (`merge_sidecars`)
   writes a form feed only *between* pages, never after the last, so a
   trailing form feed is a blank final page and is kept. Pages that already
   have a text layer are skipped by `--skip-text`, and each run of consecutive
   skipped pages comes back as ONE segment, `[OCR skipped on page(s) A-B]`
   (`... N]` for a single page). The stage expands every range into one
   `native_text` page per page number, so later pages keep their own numbers
   (and the vision fallback renders the right page). That text belongs to
   native extraction, not this stage. A marker whose page numbers do not
   continue the count fails the document closed with
   `ocrmypdf_sidecar_inconsistent`, before any vision call.
2. **Triage.** Page text that is empty, has fewer than `min_chars` (20)
   alphanumeric characters, or has an alphanumeric ratio below
   `min_alnum_ratio` (0.6) is `ocr_empty` / `ocr_low_quality`. This is a triage
   heuristic, not a calibrated confidence.
3. **Qwen vision fallback.** Only when a `vision` block is configured, each
   triaged page is rendered with `/usr/bin/pdftoppm -r 200 -png -singlefile`
   and sent to an OpenAI-compatible `/v1/chat/completions` endpoint (llama.cpp
   server with the Qwen vision projector) as a base64 PNG data URL. The
   temperature is 0, and a leading `<think>...</think>` block is dropped. When
   non-empty text comes back, the page becomes `engine=qwen`,
   `method=llama.cpp-vision`, and the original reason is kept as
   `tesseract_reason`. The endpoint must be `http://` on a loopback host
   (127.0.0.1, ::1 or localhost) with an explicit port, and no proxy is used.

Both tiers are pluggable objects (`OcrmypdfBackend.pages`,
`QwenVisionBackend.page_text`), and `OcrStage(out, primary, fallback, quality)`
accepts any object of the same shape.

## Interfaces

Queue, `awaiting-ocr-v1` (JSON Lines, one document per line; unknown keys are
rejected):

    {"document_id": "<[A-Za-z0-9][A-Za-z0-9._:-]{0,127}>", "sha256": "<64 hex>", "path": "<absolute path under --source-root>"}

Config, `ocr-stage-config-v1` (owner-only JSON, every key optional; the
defaults are the image tool paths with no vision):

    {"ocrmypdf": "/usr/bin/ocrmypdf", "pdftoppm": "/usr/bin/pdftoppm", "language": "eng",
     "ocrmypdf_timeout": 900, "min_chars": 20, "min_alnum_ratio": 0.6,
     "vision": {"endpoint": "http://127.0.0.1:8003/v1/chat/completions",
                "model": "qwen3.8:27b-thinkingcap-q6", "dpi": 200, "timeout": 300, "max_tokens": 4096}}

### Rootless Docker: reaching the host vision server

By default the vision endpoint must be on a loopback host (`127.0.0.1`,
`::1`, `localhost`). Inside a rootless-Docker container that is the container
itself. The owner-only config may name exactly one extra host with
`vision.host_gateway`: a literal private IPv4 address, normally the
slirp4netns gateway, and the endpoint host must equal it:

    "vision": {"endpoint": "http://10.0.2.2:8003/v1/chat/completions", "host_gateway": "10.0.2.2", ...}

Loopback, public, link-local, multicast, unspecified, reserved and IPv6
values, hostnames and non-canonical spellings are rejected
(`OCR_CONFIG_INVALID`; `transcribe-png` exits 1). The usual endpoint rules
still apply: `http`, an explicit port, no credentials, query or fragment, and
requests never use a proxy. A literal IP needs no DNS lookup. Nothing is
auto-detected or read from the environment. Making the gateway reach the host's
loopback (RootlessKit host-loopback) is an operator setting outside the engine.

CLI:

    python3 -m campaign_tool.records.ocr_stage --queue Q.jsonl --source-root DIR \
      --out PRIVATE_OCR_ROOT [--config C.json] [--max-documents 50] [--timeout 2700] [--report R.json]

It prints exactly one `records-stage-result-v1` line: counts, an enum status
and fixed codes, never paths or text. `input` counts every queue line,
`output` the documents published in this run, `skipped` the documents already
published plus the backlog over `--max-documents`, and `failed` the failed
documents (including damaged cache entries) plus invalid queue lines. Only
uncached work counts against `--max-documents`: cache hits are always reported
and never become backlog, so repeated capped runs drain a stable queue.
Every document published in this run is counted in `review_required`.
`needs_g` codes: `OCR_VISION_PAGES` (info, this run only),
`OCR_PAGES_UNRESOLVED` (all outstanding unresolved pages, including those of
partial documents published by earlier runs, so a cached partial document
keeps the run at `warn`), `OCR_BACKEND_TRANSIENT`, `OCR_DOCUMENT_FAILED`,
`OCR_SOURCE_INVALID`, `OCR_CACHE_DAMAGED`, `OCR_QUEUE_ITEM_INVALID` (action)
and `OCR_BACKLOG` (info). Stage-level failures are `OCR_CONFIG_INVALID`,
`OCR_BACKEND_UNAVAILABLE`, `OCR_OUT_ROOT_UNSAFE` and `OCR_QUEUE_UNREADABLE`;
`OCR_STAGE_BUSY` is a warning when another run holds the lock. The optional
`--report` is a private per-document list holding ids, statuses, codes and
engine counts.

## Outputs and safety

- The queued path must be a normalized absolute path under `--source-root`
  (no `.`, `..`, `//` or trailing slash). It is opened component by component
  from a descriptor of the root, each component with `O_NOFOLLOW`, so a
  symlinked file or ancestor directory (even one swapped in mid-run) is
  refused as `source_symlink` before any byte reaches a backend. The root
  itself is the operator's argument and is trusted as given. The same
  descriptor is size-bounded, hashed against the queued SHA-256, required to
  start with `%PDF-`, and copied into a private work directory. The tools only
  ever see that copy.
- Results go to `<out>/<sha256>/<config-signature[:16]>/`: `page-NNNN.txt` for
  pages with text, and `result.json` (schema `ocr-stage-v1`, per page:
  status, engine, method, chars, text sha256, reason/vision error;
  `source_bytes`; `review_state: not_reviewed`; no text). Publication is a
  no-replace directory rename. A rerun with the same configuration makes no
  tool or model call. A configuration change, or a change to the sidecar
  parser version (`SIDECAR_PARSER`, part of the signature), gets a new
  signature directory and never rewrites the old one.
- A cache hit is verified before use: the source is still reachable beneath
  the root with the recorded size, the result directories and files are
  owner-only and not symlinks (opened by descriptor with `O_NOFOLLOW`), the
  manifest is internally consistent (pages, statuses, engines, counts,
  signature, source hash), and every recorded page text exists with the
  recorded length and SHA-256. Anything else is `cache_invalid` /
  `cache_damaged` (`OCR_CACHE_DAMAGED`): never a hit, never reprocessed over,
  never deleted. The rest of the queue continues; the operator inspects or
  moves the damaged directory, after which the next run redoes the document.
- Outcomes that may clear on their own (tool timeout, vision server
  unreachable/timeout/HTTP error) and failed documents are not published and
  are retried on the next run.
- The output root must be owner-only. A per-root `flock` serializes runs. OCR
  children run in their own session with `RLIMIT_AS`/`RLIMIT_FSIZE` bounds and a
  minimal environment, and the whole session is killed on timeout or exit. The
  stage deadline is a `BaseException`, so no backend handler can swallow it.

## Images (D-E)

With OCR configured, native extraction wraps each single-frame image original in
a deterministic one-page PDF using Pillow (`pillow-pdf`, 72 dpi, fixed epoch PDF
dates). The bounded image parser retains its pixel/frame limits. No OCR runtime:
images remain `AWAITING_OCR`. Multi-frame GIF/TIFF images still wait and are
counted in `awaiting_ocr_images`; they are not placed in the PDF-only queue.

The wrapper is a **derived artifact, not a canonical child original**. Its
write-once, owner-only bytes live at
`<ocr_output_root>/derived-pdf-v1/blobs/<derived_sha256>` (0400). Extraction
metadata and the source receipt carry `derived`, never a `children` member:
`schema: image-pdf-wrapper-v1`, `parent_sha256`, `derived_sha256`,
`method: pillow-pdf`, `pillow_version`, and `frame: 1`. Existing `children`
remains empty. No new original/occurrence or schema is introduced.

Page 1 reuses the existing budgeted PDF page-OCR chain and confidence threshold
(80). The OCR receipt's source identity is the **derived PDF hash**; the units
and page state belong to the **parent image**. The extraction route is
`extract:image-pdf-wrapper`. Enrollment and the installed validator re-derive
the wrapper from canonical image bytes, bind exactly one page-1 OCR receipt,
and verify the units against its text. They refuse changed derivations,
wrong-source/extra/missing receipts, and attempts to declare canonical children.

All wrapped pages keep `needs_visual_review: True`. Machine-valid extraction
does not grant visual, legal, privacy or publication acceptance. No vision
adapter is enabled by the image route. This does not authorize a live run or
rehearsal: live OCR enablement stays with the attended T8 operator session.

## For the T6 live wiring check

- Done in T6 (engine code, synthetic tests only): WP4 binds vision receipts in
  the ledger and validator, and the extract stage accepts
  `--ocr --ocr-vision-config FILE` (see the "Local vision fallback" section in
  `docs/RECORDS-OCR.md`). Point the vision file's argv at the same Qwen client
  with
  `argv = ["/opt/campaign-tool/venv/bin/python", "-I", "-m", "campaign_tool.records.ocr_stage", "transcribe-png", "--config", "<owner-only config>"]`
  and engine `qwen`. The command prints only the transcription and exits
  non-zero with empty stdout on any failure, which WP4 records as a blocked
  vision receipt. In the stage, `--ocr-page-timeout` bounds each pdftoppm and
  Tesseract call, `--ocr-vision-timeout` each vision call, and
  `--document-timeout` is the per-document wall-clock budget (10-1400 s, at
  most the stage `--timeout` minus 15 s). The `live-ocr-vision` plan pins
  1200 / 180 / 300 s, which admits one uncached page with a 27B vision call
  (2 x 180 + 300 = 660 s); see "Phase 2 OCR plans" in
  `deploy/records-runtime/README.md`.
- The pinned image provides `/usr/bin/ocrmypdf` 14.0.1, `/usr/bin/pdftoppm`
  22.12.0 and `/opt/campaign-tool/venv/bin/python`. The 14.0.1 sidecar and
  `--skip-text` behaviour was checked in the container on synthetic PDFs
  (T6-check t_4913f5d3): skip ranges and the between-pages-only form feed
  are what the parser above handles. The local tests run real host OCRmyPDF
  15.2.0, whose `merge_sidecars` is the same.
- `127.0.0.1:8003` inside a bridge-networked container is the container
  itself, not the host's llama.cpp server. T6 must choose how the container
  reaches it (for example host networking or a loopback-only published port);
  that choice belongs to T6 or the operator and is not made here.
- T11k's queue export must emit `awaiting-ocr-v1` lines, or T6 must adapt it.
  Binding `result.json` and page texts into the ledger is T6 work.
- Machine-valid OCR output is review-required, never semantic acceptance.

Tests: `tests/records/test_ocr_stage.py` (fakes plus synthetic PDFs built
byte by byte in `tests/records/ocr_stage_fixtures.py`). The real-tool smoke
tests run only where `/usr/bin/ocrmypdf` and `/usr/bin/pdftoppm` exist; even
then the vision model is faked.
