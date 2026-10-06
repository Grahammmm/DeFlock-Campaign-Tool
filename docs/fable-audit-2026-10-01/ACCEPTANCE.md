# Audit acceptance and evidence matrix

All rows are REQUIRED CHECKS, not claimed passes. Record exact source commit, fixture/config/dependency hashes, command, timestamp with timezone, test count, skips, result, evidence and limitations. Use synthetic/public code hashes only in public receipts.

| Area | Required adversarial and positive cases | Evidence that closes the check |
| --- | --- | --- |
| Release | Fresh clone/install; package manifest; dirty checkout; dependency drift; actual CLI dispatch | Executed code and runtime match the declared release; absent features reported |
| Ledger | Migration/replay; failed transaction; corrupt receipt; invalid subject; two writers; stale lease | Atomic acceptance, immutable history, recoverable leases and consistent counts |
| Mail | New attachment; replay; same bytes across folders; UIDVALIDITY reset; reordered MIME; partial failure | Correct occurrences and objects; checkpoint never passes an unpreserved message |
| Mail scope/health | Missing folder; stale successful export; failed current run; unknown transfer status | Exact coverage cutoff; no false zero-new-mail or inferred outage |
| Portal | Expired URL; login HTML with HTTP 200; denied redirect; unapproved host; changed bytes | No false original; item identity stable; permission failures blocked without bypass |
| Archives | Traversal; symlink; decompression bomb; encrypted member; DOCX member inheritance | Bounded extraction and explicit parent/child coverage; no execution |
| Formats | TXT, EML, MSG, mixed PDF, image/capture, CSV/TSV, XLSX and DOCX | Actual parser path, exact locators and denominator; unsupported paths stay gaps |
| OCR | Rotated/poor scan; mixed text/image pages; missing binary; timeout; new OCR version | Per-page derivative and method; honest confidence; visual-review holds |
| Catalog | Unknown agency/date; cross-agency agreement; occurrence added; stale snapshot; changed source | Card visible, typed/hinted/blocked joins distinct; authoritative board counts |
| Detection | All seven: positive/negative/unknown/redacted/date-boundary/duplicate/zero-hit | Eligible=evaluated+skipped+blocked under defined denominator; no partial run promoted |
| Queue | All PDFs already digested; non-PDF pending; privacy hold; starvation; expired lease | Deterministic explained first-unfinished-stage queue and timely handoff |
| Review | Self-review; alias identity; stale hash; changed source/public artifact; unresolved challenge | Stable independent identity and invalidation; actual decisive-source inspection |
| Law | Wrong event date; missing policy version; role/exception mismatch; production vs determination | Explicit applicability and counterevidence; draft law package not treated as reviewed |
| Privacy | Direct identifiers; indirect single-row inference; image metadata; private note/link leakage | Exact public artifact reviewed; machine scan alone cannot grant clearance |
| Publishing | Changed approved bytes; crash before/after remote success; correction/withdrawal/build failure | Durable idempotency and exact approved content, recorded release/rollback |
| Hosted security | Spoofed identity headers; bad JWT issuer/audience/expiry; path/asset bypass; CSRF/CORS | Unauthorized requests fail, including static/details/download paths |
| Backup | Restore originals/ledger/rules/receipts into isolated runtime; missing blob; tampering | Counts, hashes and links reconcile; no sends/deploys on restore |
| Scheduler | Overlap/reboot/DST/catch-up/failed run; competing old job | One owner, correct local times, preserved checkpoints, bounded recovery |
| Unattended | Controlled authorized arrival with no active agent session | Next scheduled run produces private board card and durable run receipt |
| Public repo | Real source hash/private path/credential-shaped data; narrow allowed public hashes | Scanner stays enabled; no broad allowlist; explicit exported-path review |

## Tiny vertical slice and fault boundaries

Start with actual preservation of a synthetic EML containing a TXT attachment, not a fixture that has already extracted/cataloged it. Verify NULL legacy type behaves like true intake. Route through the actual installed parser and validators; fixed startup configuration selects code. The initial card may state unknown metadata but cannot imply full digestion.

In a text-only slice, two preserved originals with only the attachment eligible may yield 2 preserve/1 extract/1 initial catalog, with the EML explicitly pending. This is a test expectation for that bounded slice, NOT a universal end-to-end success threshold. Review, comparison, privacy and publication stay false until individually evidenced.

Test identical fresh-process replay, changed original bytes, changed derivative, wrong subject/scope/root, symlink and conflicting provenance. Bind serialized identities canonically; raw receipt hash and accepted-stage receipt are different objects. Do not weaken strict support schemas.

Inject crashes after parser output, after immutable pointer, after enrollment, after extract acceptance, after card write, and after catalog acceptance before response. Retain orphan/failed evidence. A retry before durable commit may repeat computation but must not create ambiguous accepted identity or duplicate lifecycle/checkpoint effects. Test concurrent conflicting immutable writers.

## Launch gates versus backlog rollout

Infrastructure acceptance: merged reviewed code, pinned installed runtime, working bounded integrated path, protected board, backup/rollback, authenticated approval controls, exact schedule diff and owner-authorized unattended demonstration.

Backlog rollout: reconcile every historical original/occurrence and prior receipt, route every unsupported item, execute eligible detectors with denominators, advance high-value reviews and produce three independently checked privacy-cleared proposals. These are later operational measures, not prerequisites to writing a reliable runner.

Host activation and external-effect demonstrations require separate scope approval. Offline CI cannot close them. Never label an unrun row passed.
