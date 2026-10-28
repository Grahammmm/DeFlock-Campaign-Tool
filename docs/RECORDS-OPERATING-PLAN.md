# Reliable records pipeline: build and operating plan

This document describes the reusable records engine and the work required to
make it dependable for a campaign. It is an implementation plan, not a claim
that the planned commands or production service already exist. Campaign data,
accounts, paths, agency policies and model endpoints belong in private
configuration outside this repository.

## What has prevented effective progress

| Observed problem | Consequence | Required correction | Acceptance evidence |
| --- | --- | --- | --- |
| Stored objects, received originals, archive children and generated support files were reported together | A large catalog count looked like a reviewed-document count; generated files dominated the backlog | Keep separate denominators and reversible role classifications; show source lineage and unresolved classifications | Same snapshot reconciles distinct hashes, occurrences, received originals, children, support artifacts and exclusions |
| A catalog import preserved prior author declarations but did not authenticate independent acceptance | Existing full/partial labels were mistaken for final review | Import old work with its source hash and original label; advance a stage only with its required receipt | A prior full digest without independent evidence stays prior work; changed bytes invalidate dependent approval |
| Optional parsers existed in a private bundle but some commands did not inherit its environment | Tests and subprocesses failed on missing imports | One pinned runtime for CLI, workers and tests, with an environment check before a run | Parent and worker report the same parser versions; absent modules produce explicit readiness failures |
| Mail-export receipts and MIME extraction used different part numbering and filename conventions | One legitimate delivery failed despite preserved matching payloads | Validate zero-based MIME walk index, original filename, deterministic safe stored filename, content type, bytes and hash separately | Nested mail, Unicode/spaced filenames, duplicates and adversarial paths have positive and negative tests |
| Local OCR tools were absent and the OCR draft did not securely persist every page outcome | Image-only pages remained blocked; draft receipts could not support reliable reuse | Approved local OCR installation, bounded page work, immutable derivatives, verified receipt reuse and a durable visual-check index | Every selected page has text or a specific reason; reruns preserve source hashes and reject altered receipts |
| Agency hints, parent joins and portal-link relationships were unresolved | Records could be attributed to the wrong agency or receipt | Store candidate joins separately from verified joins, with exact evidence and an accountable resolver | No automatic attribution on conflicting candidates; every unresolved join remains visible |
| The detectors were incomplete and had no corpus execution receipt | No reproducible cross-record triage results | Implement all seven specified detectors, version their inputs/rules and persist exact locators | Every applicable input has a completed, skipped-with-reason or blocked disposition |
| Raw-record model permissions and a functioning local model were unsettled | Automatic semantic cataloging and digestion could not proceed as specified | Verify a private local model endpoint and allowed data boundary; keep cloud input limited to reviewed redacted excerpts until authorized otherwise | Endpoint test succeeds locally; model requests and outputs are schema-validated and raw material cannot cross the configured boundary |
| The main records CLI exposed gates but no connected run command | Several individually useful scripts required an active operator | A resumable orchestrator with a single writer, stage checkpoints, failure isolation and one board snapshot | A fresh original reaches a catalog card through one invocation, without manually running separate scripts |
| Mail-export freshness and the requested schedule were disconnected | Fresh records might wait unnoticed | Inventory the existing scheduler; prepare one replacement or absorbed job, approved before activation | A controlled attachment appears after the next scheduled run; stale exports and failed folders are reported |
| Routine implementation fixes reopened already-settled approval questions | Work paused unnecessarily while unrelated stages could proceed | Keep an explicit authorization ledger and continue authorized reversible fixes/tests; reserve approval for the actual installation, schedule, egress, sending and publication boundaries | Each held action names the applicable restriction and owner; a pending decision does not stop independent permitted work |
| Engineering progress and document progress were mixed in reports | It was difficult to tell whether the backlog was shrinking | Report code delivery, preservation, extraction, digestion, review and publication separately | Each report identifies new stage transitions, its exact window and the remaining denominator |

The remedy is a pipeline that resumes safely and exposes failures. Review
attention should follow new productions, high-value records and detector hits;
full human digestion of generated artifacts is not a prerequisite for progress.
Proposed low-value dispositions remain proposals until the organizer approves
that policy. An ambiguous evidence classification must not silently close a file.

## Current implementation and acceptance boundaries

The merged engine contains the portable intake implementation, review gates,
synthetic tests, CI, catalog import/board generation and agency-reconciliation
candidates. The top-level records CLI currently dispatches the four review gate
commands. Separate modules provide intake and catalog operations. The integrated
`run`, mail scheduler, complete detectors, semantic local-model catalog and
publication flow are still work to complete.

A merged pull request establishes code delivery. A host execution receipt and
its milestone checks establish operational completion. A catalog card or a
successful text extractor does not establish original-level understanding.

| Milestone | Build and operational exit conditions |
| --- | --- |
| M0: portable foundations | Existing intake and gates retained with their tests; synthetic regression coverage, credential scan and Python-version CI pass |
| M1: inventory and private board | Import existing ledger/snapshot without redoing completed work; one card per hash; evidence and artifact counts reconcile; agency/type/date/value fields carry verified values or explicit unknown reasons; open requests and gaps appear; owner-only board access is tested; requests show sourced dates, extensions, productions, status and next actions or explicit unknowns |
| M2: extraction recovery | Local OCR tools approved and available; every image-only page has a receipt; low-confidence/fidelity pages have queue entries; document/image/mail/capture support is explicit; recoverable failures rerun without rewriting originals |
| M3: complete detectors | Seven detectors, verified rule versions and private policy overlays tested and run on applicable corpus inputs; all dispositions and exact source locators persisted; high-severity hits checked against originals |
| M4: triage and proposals | Ranked queue; three factual drafts with hash-bound independent source checks, privacy scans and limits; full finding gate for legal claims; draft files await tier and owner approval |
| M5: one scheduled job | One approved job absorbs existing exporter/checkpoints; runs at 07:00, 13:00 and 20:30 Pacific; controlled mail attachment reaches the private board unattended; restore and incident tests pass |

Complete missing M1 acceptance checks before marking that milestone complete.
M2 code can be prepared alongside those checks; passing synthetic OCR tests
alone does not satisfy the host/corpus portion of M2.

## Data contracts and inventory

Use a content-addressed store and one transactional ledger. An original SHA-256
is its byte identity. A separate occurrence identifies each delivery, path or
request context. Preserve both identical deliveries and changed versions.

Required entities:

- Object: hash, bytes, detected format, immutable private location and preservation verification.
- Occurrence: source adapter, stable source identity, receipt time, original filename, request, parent and verification evidence.
- Mail occurrence: account identity, folder, UIDVALIDITY, UID, Message-ID, EML hash and exact MIME part relationships.
- Portal item: provider, request/item identity, notice occurrence and retrieval disposition. Private signed URLs stay outside public output.
- Catalog: role, verified agency, request IDs, document type, date range, parties, summary, value score/version, explicit unknowns and stage.
- Request: stable agency/request identity, sent/received dates, scope, sourced determination due date and rule basis, extensions, determination date, productions, current status, next follow-up, and unresolved date/applicability reasons. Never substitute a portal assignment date for a statutory deadline.
- Unit: original hash and physical page, sheet/cell/row, MIME part, image or archive member locator; extractor/version and unit hash.
- Extraction attempt: environment, selected scope, successful/failed/skipped units, artifacts and page confidence/fidelity reasons.
- Review: author, coverage intervals, facts, counterevidence, source locators, local-model version and limitations; original work retained on correction.
- Rule: official source, preserved version/hash, event dates, role, jurisdiction and private policy overlay applicability.
- Hit: detector/version, input hashes, rule hashes, exact locators, severity and interpretation limits.
- Item: proposed text, tier, evidence, independent review, privacy scan, supersedes and owner decision.
- Run: engine commit, config digest, source cutoff, stage checkpoints, budgets, metrics, failures and output snapshot.

Do not count internal DOCX XML members as separate fully digested documents, or
assume a parent digest covers its children. Preserve children and their lineage;
calculate review obligations from the original's format and content structure.
Generated support files remain in the inventory but have a separate obligation
class. Classification corrections are appended and invalidate affected queues.

Use this deterministic denominator contract:

- Stored objects are every distinct inventoried hash, including support artifacts and recorded exclusions.
- Agency evidence originals are the union by hash of verified agency-delivered originals and qualifying independently usable attachments/archive documents. A qualifying child has verified delivery provenance and represents a document in its own right; office-package XML/relationships, generated renders and internal extraction fragments do not qualify merely because they have bytes.
- Inline logos, transmittals, invoices and auto-replies retain provenance and a proposed low-value disposition until the organizer decides the closing policy. Qualification and public value are separate decisions.
- External primary sources are preserved official policies, statutes or other sources obtained outside agency productions; report them separately even when they support analysis.
- An object with disputed role, unverifiable delivery or uncertain child scope remains unresolved and outside any claim of completed evidence coverage. Never remove it from the inventory.
- If the same hash qualifies in multiple occurrences, count it once in the evidence-original union while retaining all delivery and request contexts. Record the classification rule version and supporting receipt; preserve superseded decisions.

Every count must name its snapshot and population. Report stored objects,
verified originals, missing bytes, extraction coverage and review acceptance
separately. A new snapshot must explain changes from its predecessor.

## Required runtime and access

| Requirement | Used for | Provisioning and proof |
| --- | --- | --- |
| Python 3.11+ and pinned parser environment | PDF/workbook/mail/archive extraction and tests | Reuse approved installed or vendored parsers; verify imports in both parent and bounded child workers |
| Tesseract and Poppler | Local per-page OCR of image-only PDF pages (the only OCR path), page rendering and visual recovery | Organizer approves system installation; probe versions and language data, then run a known synthetic scanned page |
| Private local model endpoint | Semantic catalog and original-level draft digestion | Configure privately; local-only smoke test, bounded requests, schema validation and prompt-injection isolation |
| Records-mail read access | All-folder export and stable delta intake | Reuse approved credential configuration and UID checkpoints; verify folder-level coverage without displaying credentials |
| Authorized portal access | Retrieve items mentioned in productions | Respect host approval and egress policy; expired/denied items remain blocked with owner and reason |
| Private persistent storage | Originals, ledger, derivatives and backups | Space/budget checks, restrictive permissions and restoration exercise |
| Owner-only board hosting | Queue and review access | Verify anonymous rejection and owner access for all assets/routes |
| Repository contributor access | Engine branches, PRs and CI | Push branches through an authorized CLI or connector; never put campaign history/data into Git |
| Reviewed event-date legal sources | Rule comparisons | Preserve official text and applicable policy versions, including counterevidence and unresolved versions |

On a compatible Debian-based host the proposed system provisioning command is:

```sh
apt-get update && apt-get install -y --no-install-recommends tesseract-ocr poppler-utils
```

This is an administrator action requiring explicit organizer approval. Test
binaries and language data afterwards. The processing account remains non-root.
This command does not provision a local model. Select and document that runtime
separately, including model identity, license, hardware needs and data boundary.
Do not install global parser packages to work around a missing worker environment.

## Processing path

1. Validate private configuration, dependencies, read-only source scope, disk budget and lock ownership.
2. Run the existing exporter under the single approved job, retaining UIDVALIDITY/UID and every folder's cutoff.
3. Preserve EML and attachment bytes before advancing the delivery checkpoint; failed imports retain their original receipts.
4. Inventory permitted portal notices and drop-folder inputs. Retrieve only through authorized paths; never mark a link as acquired bytes.
5. Detect actual file type and extract bounded units. Run local OCR only where needed, and only per page: WP4 extraction sends PDF pages that are not natively `ok` to the per-page OCR helper (`docs/RECORDS-OCR.md`), the single OCR path. There is no whole-document OCR derivative (`ocr.pdf`); page-level receipts keep exact page locators, per-page confidence and per-page errors, and native-text pages keep their original units. Preserve selected page scope, source hash, confidence and errors.
6. Create/update catalog cards with deterministic validations and locally generated drafts. Reuse unchanged earlier work with provenance.
7. Run applicable detectors on normalized records and applicable rule versions. Persist zero-hit results and skipped/blocked coverage as well as hits.
8. Rank new productions, important records, hits and unresolved gaps. Timebox an individual document or join to about 45 minutes and assign an owner for further work.
9. Draft factual updates from decisive original locators. Independently reproduce numbers/quotes, scan the exact public text and retain limits.
10. Generate one consistent private board/report snapshot. Resume unfinished stage work on the next invocation.
11. Only an explicitly approved publication item may be exported to the separate campaign site as a reviewable content PR.

A missing OCR binary should block relevant OCR pages while native-text
extraction and cataloging continue. A failed portal item should not stop mail.
A model outage should preserve deterministic catalog metadata and queue semantic
review. A failed privacy check should hold its proposed text without discarding
its evidence. Unsupported items receive a reason and recovery route.

## Format and recovery support matrix

| Format | Processing and exact locator | Recovery or limitation |
| --- | --- | --- |
| Native PDF | Every physical page, text and relevant table/annotation structures | Decisive rows, redactions and signatures need visual comparison |
| Image-only PDF | Local page rendering, OCR text and word-confidence probe; page/artifact hashes | Low confidence, absent text and render fidelity get separate queue entries |
| PNG/JPEG/TIFF | Frame/image hash, dimensions, OCR where appropriate and item-level review | Multiple frames, orientation and truncated captures need explicit coverage |
| XLSX | All sheets, hidden sheets, rows/cells and formula text | Never execute macros, evaluate formulas or fetch external links |
| CSV/TSV | Stable encoding/dialect, header and physical/logical row locators | Ambiguous schemas remain unmapped; printed row labels need native-ID verification |
| EML | MIME walk, headers/body/attachments and exact parent-child joins | Embedded mail and filename normalization are distinct validation cases |
| MSG | Outlook compound-file body, attachments and message structure | Unsupported parser parts stay visible; a transmittal is not the referenced attachment |
| DOCX | Paragraph/table relationships and embedded media with original context | Internal ZIP/XML children do not substitute for document digestion |
| ZIP and other archives | Bounded member/depth/size extraction and parent lineage | Path traversal, symlinks and resource-limit failures are retained as dispositions |
| HTML/capture | Preserved content, capture time, canonical source and item/section locator | Scripts never run as evidence; capture coverage and source authenticity need review |
| Unknown/encrypted/malformed | Preserve bytes, detect type, report exact failure | Obtain permitted password/reissue/parser capability; never guess successful coverage |

Version this support matrix with the engine. A format is supported only when
its executable path and tests exist; the table specifies the target behavior.

## Original-level digestion and cross-record comparison

Enumeration comes before summarization. For each evidence original, enumerate
all expected pages, worksheets (including hidden ones), meaningful message
parts, tables and capture frames. Chunk large material at stable page/row
boundaries and retain overlap/continuation markers. Each chunk's receipt records
source hash, unit hashes, exact ranges, extraction limits and model identity.
Never infer full coverage from a short summary or one sampled page.

The local draft captures actors, event dates, agency assertions, systems, data
flows, operational settings, training, sharing, retention, audit practices,
contract duties, omissions and cross-record links. Facts carry exact source
locators; unknowns and counterevidence remain explicit. Validate the output
schema and every cited range before accepting it into the catalog or digest.
Store prompts and full responses privately for reproduction. Source text cannot
change model instructions, invoke tools or authorize network/action requests.

Use code to reproduce counts and compare normalized dates/IDs. Keep original
values and normalization mappings private and versioned. A same-named person,
organization or printed row does not establish a verified join. If an identifier
or date is missing/redacted, record the unresolved join and request needed data
rather than treating a failed join as proof of a violation.

Compare a supported fact with the applicable official event-date rule and
adopted local policy, including role, exceptions and contrary evidence. Preserve
rule versions and provenance. Distinguish the date of conduct, disclosure
response and retention period; a current policy may not govern an older event.
A research lead is not a reviewed governing rule.

The independent checker receives the original locators before the desired
conclusion. Within the authorized data boundary it reproduces decisive numbers,
quotes, signatures and dates and challenges plausible alternatives. Coverage
acceptance and claim acceptance are separate receipts. A legal reviewer then
checks proposed rule comparisons and a privacy reviewer checks the exact public
artifact. Keep challenges and corrections; never manufacture a pass from a broad
review recommendation. Local-model absence blocks semantic automation but must
not prevent preservation, deterministic extraction or catalog metadata updates.

A document with missing required units remains partial/blocked. A completed
coverage receipt does not guarantee the truth of agency assertions. Tier-A items
can use a carefully verified bounded fact before the rest of the corpus is done;
the published claim must describe only its reviewed scope.

## All seven detector contracts

Implement the detector IDs: `search-before-training`, `purpose-quality`,
`external-sharing`, `retention-over-policy`, `audit-gap`, `cpra-deadline` and
`volume-anomaly`. Inputs use normalized private records with original locators.
Each detector must record its applicability decision, required joins and rule
versions, and return deterministic hits or a specific coverage disposition.

Test positive/negative cases, redaction, policy boundaries, duplicate IDs,
conflicting joins and missing inputs. Deduplicate by a scoped native event ID
only when that ID's meaning is established. Apply an explicit campaign timezone
for calendar-day comparisons. An enabled permission is distinct from a data
transfer; a missing produced audit is distinct from an audit that never occurred;
a blank export field is distinct from an absent native value. Volume anomalies
are triage signals with no standalone legal conclusion.

## Review and public outputs

Pending organizer decisions keep tier-A items awaiting tier approval, raw text
local-only and low-value records proposed rather than closed.

For a factual update, bind every sentence to source hashes/locators and preserve
receipt dates. The independent checker inspects decisive source material before
receiving the author's proposed conclusion. Store checked scope and verdict.
Scan the exact public artifact for personal details and private locators; require
the organizer's approval before publication.

For a legal finding, add event-date law, applicable local policy, regulated role,
exceptions, counterevidence and independent factual/legal/privacy review. Run
the existing finding gate against exact artifacts. A structural gate is not a
merits decision. Changed evidence or text invalidates its dependent reviews.

Start with three factual updates from the highest-value verified evidence.
Unresolved legal questions should not block a carefully bounded factual update,
but a source-verification gap must remain visible and prevent readiness.

## Scheduling, failures and recovery

The final intake schedule is 07:00, 13:00 and 20:30 America/Los_Angeles. It is
not the old every-15-minute skill description. Arrival-to-next-run wait and
run-start-to-catalog latency are separate measures; this schedule cannot promise
that every arrival is cataloged within one hour.

Before enabling anything, present the actual existing unit/timer, actor,
checkpoint location and export invocation, plus the exact proposed replacement
and rollback. One lock-protected job absorbs the exporter; never leave two jobs
advancing the same mailbox state. Schedule changes require explicit approval.
The 21:00 report reads the same ledger and does not introduce another intake job.

Use bounded retries/backoff for transient errors. Record permanent failures with
reasons, affected hashes and owners. Alert on three consecutive failures and on
stale mail or missing folders; deduplicate alerts until the condition changes.
A process timeout does not establish that the process stopped: inspect its live
handle before restarting. Cap pages, archive expansion, model tokens, runtime
and concurrency through campaign configuration.

Keep backups of originals, SQLite and migration receipts. Validate restoration
into a separate private directory and rebuild the board from that restored
ledger. Never mutate the source corpus to repair a derived artifact. Publish
board snapshots atomically and retain the previous successful snapshot on failure.

## Tests and release checks

Before production acceptance, prove these journeys with synthetic fixtures:

- A fresh mail attachment reaches a card; unchanged rerun creates no new object or occurrence.
- An identical attachment delivered through another folder/request adds provenance without duplicating bytes.
- Changed UIDVALIDITY is reconciled and a mid-run crash preserves the last successful checkpoint.
- Nested messages and stored filenames match exact payloads, with no path traversal.
- Scanned/rotated/mixed PDF pages produce exact page receipts; one bad page leaves other pages processing.
- Tampered derivatives/receipts and nested symlinks fail closed; interrupted writes cannot promote incomplete output.
- Prior full/partial work imports without gaining independent approval; changed bytes invalidate affected reviews.
- Every detector has all specified positive/negative/boundary tests and complete execution coverage.
- Redaction/export gaps never become automatic violations; unavailable required joins remain blocked.
- A challenged draft is held; a correction preserves history; altered public text loses its previous approval.
- Anonymous access to the private board and its assets fails; authorized owner access succeeds.
- No campaign originals, model prompts, credentials, signed links or private configuration enter Git or CI.
- Restore plus rerun recreates the same inventory, stage dispositions and deterministic detector output.
- After schedule approval, the organizer's controlled attachment reaches the board on the next unattended run.

Public CI runs synthetic tests and offline scanners on supported Python versions.
Keep actual OCR integration tests separate from injected command tests; record
which executable versions were exercised. A passing scanner is one check,
not a comprehensive privacy clearance.

## Work order and progress reporting

1. Finish M1 inventory obligations, ambiguous role/agency joins, request metadata and private-board access.
2. Deliver M2 OCR fixes and tests; provision approved tools; recover supported evidence and document every remaining blocked page.
3. Deliver all M3 detectors and versioned rule inputs, then run them over the entire applicable corpus.
4. Deliver M4 triage/review/public-text tooling and three checked, privacy-scanned factual drafts.
5. Deliver M5 orchestrator and exact schedule replacement; enable only after approval and verify the unattended attachment journey.
6. Stabilize with bounded batch measurements, restoration, recurring backlog work and incident handling; publish a new-organizer quickstart only for paths that actually work.

Each session reports new transitions and remaining counts for preservation,
extraction, semantic digestion, independent challenge, legal/privacy review and
publication readiness. Name the snapshot, mail cutoff, blocked owners and next
acceptance step. Code commits and catalog imports have their own counts. Track
backlog age and time spent per stage so preparation cannot indefinitely replace
source processing.
