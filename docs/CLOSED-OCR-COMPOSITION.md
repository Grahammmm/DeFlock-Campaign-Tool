# Closed public OCR evidence composition

The public extraction adapter now enrolls the receipt, manifest.json, page.pdf,
page.png, sidecar.txt and confidence.tsv as hash-addressed evidence. The new
public bridge uses the existing public local-ocr-3 verifier and the documented
asset namespace. It does not import, copy or admit private compatibility source.
Its identity is public-ocr-closure-v1; no private hidden-literal parity is claimed.

Validation reads all six assets from canonical evidence CAS and reconstructs only
a disposable private verification tree. The original OCR output tree is never
opened during acceptance or exact-receipt replay. Every asset and both JSON
records are checked; duplicate/nonfinite JSON, wrong source/page/receipt, unknown
asset names and missing/changed bytes fail closed. Per-file and per-page closure
bounds are 16 MiB and 64 MiB; aggregate enrollment/acceptance limits remain.

Enrollment/validator versions advance to extraction-ledger-v2 and
records-extract-complete-v2. New immutable import identities distinguish adapter revisions. The existing
source/receipt uniqueness also remains: replaying a v1 receipt under v2 is held
with adapter_revision_requires_fresh_extraction_receipt. A controlled fresh reader
attempt must issue a new receipt; old receipts are never patched. Old history is retained and rejected
by the current validator; it is not rewritten or relabeled. Before deployment
reuses a completed v1 extraction, trusted startup must perform controlled
extract/dependent invalidation and publication authority revocation, then enroll
and validate the new evidence. No migration or automatic approval is provided.

Both verifier and closure bridge code hashes participate in installed parser
identity and content envelopes. Trusted startup must still bind the exact
installed runtime and genuine non-test preservation prerequisites. Hashes of
self-declared OCR artifacts do not authenticate tool execution or establish that
a raster faithfully depicts the original. This repair preserves the existing
functional extraction boundary and does not add derivation attestation or
substantive acceptance. Derivation/provenance and visual-review holds must remain
where the installed qualification cannot establish them. Original CAS bytes/size,
reader page denominator/locators and runtime/tool identity remain caller duties.

OCR fallback, XLSX and Pillow acceptance are not expanded here. Fallback outcomes
remain held, and workbook native accounting remains separate from semantic or
visual qualification. Any supported workbook policy requires source-bound unit
closure, native denominator reconciliation, exact helper/parser/runtime pins and
reviewed installed authority. The private modules remain AgentOrange-only.

Synthetic closure tests use an injected OCR runner and explicit testing_runner;
these are test-only evidence, never genuine campaign or independent acceptance.

The separate optional native workbook policy is described in
NATIVE-WORKBOOK-COMPOSITION.md. It requires explicit trusted startup composition;
the default policy and Pillow/fallback holds are unchanged.
