# X6 phase 2a — S9–S12 only, review required

This candidate extends the shared geometry probe from phase 1. It is NOT a
complete X6 fix, installed runtime qualification, live rollout, or semantic
acceptance. All existing review/fidelity/publication and source-owner holds
remain. The complete design is `X6-ELIGIBILITY-CONTRACT.md`, frozen SHA-256
`e1a08bf2e587f8f81d821566de0cfb63388329744de13a14970a47184f449113`
(operator-owned external design, not a newly authored spec).

Admission: t_a91c7999 operator comment1056 explicitly allows a 2a S9–S12 /
2b S13–S16 split when preflight exceeds 40 turns / 3 hours; comment1066 stages
base `7664ccde2c8bf0890719fc5763cfb3195adb9484`. The author recorded the split
before editing. Comment1058 is a worker's missing-source report, NOT split
authority. Follow-up t_585d9a02 reserves operator admission/dispatch of 2b;
it is not a second verifier or authority to execute 2b.

## Implemented behavior and limits

* `pypdf-painted-rects-2` walks nested Form XObjects with caller transforms,
  each Form's Matrix/BBox clipping and resource scope. Repeated placements
  count against one page-wide byte/operation/form/image budget; recursion
  cycles, depth and graphics-stack limits fail closed. Exact rational rectangle
  union prevents duplicate-area credit. Text boxes and uncovered-image area
  remain unavailable (null), never zero or a geometry-only image exemption.
* Evidence remains bound to the preserved source hash and original page number.
  It records nested XObject paths, placement operation indices, exact rectangles
  and their measured union. The rendering domain is unrotated MediaBox, matching
  the existing pdftoppm default without `-cropbox`. Rotation and UserUnit are
  recorded. Area is in squared PDF user coordinates, NOT physical points or
  pixels; the existing separate render-pixel bound still applies.
* Ordinary geometry-neutral ExtGState settings, Normal/Compatible blending,
  `/SMask /None`, finite alpha in [0,1], and device-space transparency Groups
  no longer force a sparse native-only page unresolved. Opacity/compositing
  never exempts a raster-bearing page. This is bounded support, not a claim
  that all Word/Chrome output is supported. Soft masks, Type3 fonts, optional
  content, non-axis image/Form transforms, unknown group/state semantics,
  annotations and unsupported clipping stay unknown. A proven prior image may
  justify bounded OCR without a complete union measurement, per design rule6.
* Existing direct OCR statuses, confidence threshold80, blocked reasons,
  immutable artifacts, indexes, sticky fidelity holds and restricted vision
  eligibility are unchanged. No new vision-fallback reason is introduced.
  Low confidence / no text / no confidence are the only existing eligible
  paths with a verified raster; success remains unreviewed and partial.

## Executable synthetic contract

`tests/records/test_x6_phase2a.py` parses generated PDF object graphs. It does
not invoke real OCR, a vision model, a network service, or a live store.

| Requirement | Assertions |
|---|---|
| S9 | Tiled/overlapping/repeated placements have exact union350, not bounding-rectangle450; nested translated/scaled Forms and caller clip have exact union4; Form BBox clips to union100; unused/off-page Forms and native-only Forms preserve native sufficiency; quarter-turn/reflection placements, rotation90/UserUnit2/CropBox-vs-MediaBox and original page2 of2 bind correct geometry/source/derivative/locators. Actual direct and native routes are exercised. |
| gs/Group carry | Sparse native positives remain complete/ok, unchanged text and zero OCR/vision calls with supported graphics states/Groups; the same settings with a scan still produce bound OCR body text, not a skip. |
| S10 | Malformed/missing/empty/nonfinite bounds, bad transforms, unsupported clipping/masks/states, text decoding exception, shared operation/stream/form/image bounds and a cyclic Form remain unresolved/non-ok/blocked with precise reasons; no fabricated zero union or native success. A prior positive image plus unknown clipping stays OCR-required with null full-union measurement. |
| S11 | A stamped scan's fidelity hold dominates, persists through repeat loading and never invokes tools/vision; excessive UserUnit render bounds prevent launch; simulated Tesseract timeout preserves local_ocr_timeout and only the actual artifact prefix. |
| S12 | Empty / missing confidence /79.99/80/96 confidence have exact blocked/queued/unreviewed states and original threshold; only eligible states call configured stub vision after verified raster; successful vision stays not_reviewed/not_checked/not_done and page partial; failed vision retains text/gap; skip/no-raster and tampered-raster controls never invoke vision. |
| Negative control | Disable only nested traversal in an in-process synthetic double: the required OCR assertion fails while the same PDF/source bytes and other gates remain valid. Restore the double: the original positive evidence is reproduced. |
| Compatibility | Existing `test_x6_eligibility.Contract` and pinned adversarial `TextLayerHeuristic` retain all14 tests, including sparse native positives, junk Unicode, conservative image cases and queue skip refusal. No fixture is weakened or rewritten. |

Author's separate named-suite launcher is retained in the task packet as
`run_phase2a.py`. Exact invocations use the frozen installed interpreter
`/home/user/agent-work/fl2-merge/venv/bin/python -B`, the exclusive candidate
cwd and either `phase2a` or `compatibility`. It inventories installed pypdf
source bytes before application imports, installs parent network/process audit
checks, fixes the imported checkout, redirects TMPDIR/tempfile before tests,
and admits only the source-defined bounded PDF parser child. It records every
actual child argv/cwd/interpreter/checkout closure, raw output and hashes in a
new per-run directory. The child is the existing source-owned bounded parser,
not an audit-hook hostile-code sandbox; no new installed guard is claimed.
Renderer/Tesseract/vision calls are deterministic injected doubles. Full records
discovery is reserved for exact-head CI under SD-12, never local discovery.
See the task handoff for exact final identity, results, counts and output hashes.

## Required phase 2b / deployment prerequisites (NOT accepted here)

S13: mixed-document denominator/order, unchanged native provenance, selective
replacement and AWAITING_OCR when OCR is disabled.

S14: independent installed PDF reparse agreement and exact skipped-receipt
consumer/validator refusal, including ocr_page_not_accepted:skipped_machine_text.

S15: policy/probe-versioned isolated output identities and readers, native
admission identity, route-budget agreement, new-policy reuse/restart/
reconciliation with old evidence retained read-only. This 2a probe-version
change alone is NOT cache migration: existing local-ocr-3 identities can reuse
old skip receipts, and the current reader binds only the active probe. Do not
run 2a over historical roots or claim old evidence remains readable by this
unmigrated active reader. Old roots must remain untouched; historical
verification/migration needs the separately admitted 2b implementation.

S16: stamped-scan skip rejection / honest fail-closed behavior without a
separately qualified selective backend; skip-range/page-number validation and
source/sidecar page-count agreement. Stub control flow does not qualify a real
OCRmyPDF selective mode. No real tool qualification is admitted by this packet.

No X1/X2/X4, `_publish`, canonical ledger, old OCR result, service, credential,
provider, tool permission or live configuration is changed. No source downloads,
replay, emails, publication, merge, deploy or hold release is included. The sole
independent reviewer is opus-coder, verified against actual Sol/OpenAI author
provenance, using REVIEW-RUBRIC v1.3. Local passing tests are REVIEW REQUIRED;
required exact-head CI and independent review must be obtained before any
standing-approved merge, and they cannot make this incomplete X6 deployable.
