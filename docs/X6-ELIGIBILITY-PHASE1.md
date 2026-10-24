X6 eligibility v1 — phase 1 only (S1–S8)

Status: implementation candidate; independent review required. NOT deployable,
NOT full approved-design acceptance. Design SHA256:
e1a08bf2e587f8f81d821566de0cfb63388329744de13a14970a47184f449113.
Base: 75494fa0a3f525e378aac7b9cefbf4c3f7ec58b7.

One shared policy in records/pdf_eligibility.py drives native admission, direct
per-page OCR eligibility and queue-stage skip-marker refusal. It has no text
length or page-area cutoff. Sparse native-only text stays unchanged and avoids
OCR. Whitespace/control/format/replacement-only strings are not usable text;
punctuation, symbols, combining sequences and non-Latin content are usable.

The probe reads actual content-stream painting operators, not resource presence.
Its recorded MediaBox domain is the default pdftoppm box. Measurements are in
unrotated PDF user space; rotation and UserUnit are recorded rather than silently
mixed with raster pixels. Only exact axis-aligned transforms and rectangular
clipping are currently measurable. Image rectangles are intersected with the
page and clipping domain. Their union is measured without double-counting.
Unused resources and proven fully clipped/off-page placements do not count.
Placement/intersection/union arithmetic uses exact rationals of pypdf-decoded
numbers, with bounded numerator/denominator size. Exact rational rectangles are
recorded alongside binary64 display coordinates/areas; those display values do
not decide whether a sliver exists. Original PDF numeric decoding remains
pypdf's pinned implementation, not an independent renderer qualification.

Text-box and uncovered-image areas are explicitly null/unavailable, not zero.
Any proven painted raster requires OCR under v1 rule 5, so missing text-box
measurements do not create a completeness exemption. A full invisible overlay,
long native header or small embedded scanned table cannot cancel image evidence.
No text/image ratio or geometry-only overlap exemption is implemented.

S8 intentionally OCRs a painted white background, small logo/photo and an
already-OCRed scan. These are conservative false positives, not a claim of zero
redundant OCR. Pixel-content discrimination is out of scope.

The bounded phase-1 probe refuses unsupported clipping/transforms/fonts, Form
XObjects, masks/transparency, annotations, unknown operators and exhausted
bounds as unresolved unless an already-proven image suffices. It never calls a
model to resolve geometry. Native admission remains non-ok with a coded issue;
direct invocation yields blocked/page_eligibility_unresolved. Existing fidelity
holds and text-probe failures still dominate. Successful OCR remains unreviewed
and partial downstream, with all original artifact/confidence semantics.

The separate queue-stage backend still uses --skip-text. A marker is accepted
as native_text only after a positive native_sufficient decision on the preserved
source page. Otherwise the document fails closed as
ocr_failed/page_eligibility_unresolved and publishes no success. No selective
force/redo OCR mode is claimed qualified; no document-wide --force-ocr is added.

Remaining mandatory phase 2, card t_a91c7999 (separate admission after phase-1
review):
  S9: tiled/nested/repeated geometry, rotation/UserUnit/page-box qualification.
  S10: comprehensive malformed geometry/resource/failure semantics.
  S11: full hold/render-bound/timeout integration.
  S12: all confidence/fallback/failure permutations.
  S13: mixed-page denominator/order and AWAITING_OCR integration.
  S14: installed reparse, skipped-receipt rejection and downstream bindings.
  S15: policy-bound cache identities, old-result isolation/migration and restart
       reconciliation, including route-side budget identity checks.
  S16: full queue denominator/skip refusal and selective-backend qualification.

IMPORTANT: phase 1 retains old cache identities and the local-ocr-3 reader for
compatibility; old results are still historical/unreassessed and can be reused.
Do not deploy phase 1 over an existing result root or claim this closes cached
X6 results. Optional new eligibility evidence is bound by the receipt/manifest
hashes; legacy receipts remain readable. No blind VERSION bump, old-result
rewrite, bulk replay or live-root migration occurs in this phase.

Synthetic tests:
  tests.records.test_x6_adversarial copies only X6 definitions verbatim from
  audit d0264abadb0a3e1b30455855749a182309a5e36a, with the opt-in decorator
  removed so both tests always run. Original file SHA256:
  9f1f93895068e2650fd33f98f3fedbfd0cb5dad7c1a25f29d93d92040b3b359f.
  Tests are evidence from the audit, not independent approval of the new code.
  tests.records.test_x6_eligibility generates actual PDFs, including ToUnicode
  decoding, and asserts native preservation, full-page OCR text replacement,
  source hashes/page locators, unreviewed outputs and queue refusal.
  Existing queue tests now use actual native-only pages where they previously
  mislabeled image-only fixtures as native by supplying skip markers. Existing
  assertions remain enabled; a separate stamped-scan refusal regression covers
  the formerly accepted false skip.

No live OCR/vision, real records, canonical writes, new dependencies, service or
credential changes, merge, deployment or publication are authorized here.
