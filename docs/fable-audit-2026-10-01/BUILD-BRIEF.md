# Records Pipeline Build Brief (sanitized historical edition)

> **Historical design document, not current deployment evidence.** This is a sanitized adaptation of the owner-supplied September 30 build brief. All twelve sections and WP0-WP9 requirements are retained; private host paths, account identity, source hashes, source-specific examples, image identities and operational identifiers are removed or generalized. The raw original is NOT published. Later instructions in [DECISIONS-AND-BOUNDARIES.md](DECISIONS-AND-BOUNDARIES.md) and the infrastructure-first scope govern current work. Historical counts and status labels below are not today's accepted totals. Proposed commands are design targets, not a claim that those interfaces exist. The scheduler remains gated.

Source date: September 30, 2026

## 1. How to use this brief

Codex is being asked to turn the existing DeFlock SLO records components into one pipeline that takes a new email or portal production through preservation, extraction, catalog, detectors, review, legal comparison, privacy review and an owner-approved website update - with a single ledger that can answer, for every original, what is done and what exact step is next. The Sept 30 audit found every part of that path exists in some form; none of it is joined. This brief is the join.

Scope. Ten work packages (WP0-WP9, section 7) built in the order in section 8. Each has files, a CLI surface, synthetic tests and an acceptance check. Nothing outside those packages is in scope unless it blocks one of them.

Operating rules for Codex while building:

Work in the public engine repo Grahammmm/DeFlock-Campaign-Tool (campaign_tool/records/) for all reusable code, and only in the private records root on <PRIVATE_RUNTIME> for campaign data, receipts and config. Never the other way round (section 9).

One PR per work package, small enough to review in one sitting. Open the PR, run CI, report the result, and stop. Do not merge; the owner merges. Do not treat an earlier PR's approval as approval for a later one.

Ship the artifact before enriching it. The audit deliverable itself was late because investigation kept extending (finding F17). Every WP has a 45-minute timebox rule for any single document, parser or rule issue: record the blocker in the ledger with a reason and owner, move on.

Every claim of completion carries a receipt: a run ID, an input hash, an output hash, a test run ID, or a CI run ID. A chat message saying it works is not a receipt.

Counts are stage-specific. Never report an extracted document as processed, a digest as a review, or a gate pass as publication readiness. Use the vocabulary in section 5.

When a step needs something only the owner can do (a credential, a merge, a host change, a portal permission, a publish), write the exact service, host, control and proposed change into the owner-decisions log (section 10) and continue with unrelated permitted work. Do not repeat the ask in chat.

Report in the format in section 12: stage counts from the ledger, what changed, what is blocked and by whom, what the owner must decide. No tool-call counts, no narrative of effort.

Source of truth for this brief: the private audit DeFlock-pipeline-audit-2026-09-30.html (evidence cutoff 9:07 AM Pacific, Sept 30, 2026). Where this brief and that audit disagree on a fact, the audit wins; where they disagree on design, this brief wins.

## 2. What exists today

The merged engine (0.1.0.dev0, commit <HISTORICAL_ID_REDACTED>) contains M0 gates and intake plus M1 catalog, board and agency-candidate reconciliation. Everything else is a candidate branch, a spec, or a manually-run host script. Codex builds on what is listed here rather than rewriting it.

| Component | Where it lives | Status Sept 30 | Keep / replace |
| --- | --- | --- | --- |
| Mail exporter | <PRIVATE_PATH>, state in <PRIVATE_PATH>, lock sync.lock | Working. Manual run 08:45 PT: 6 folders, 0 new, 0 failures. Scanned 118 msgs / 60 attachments vs index rows 69 / 46 - unreconciled | Keep the IMAP/receipt logic; wrap it as a runner stage (WP2); fix indexes |
| Mail-delta importer | branch codex/records-mail-delta, 26 synthetic tests, staged not merged | Candidate | Merge into WP2 after review |
| Portal intake | <PRIVATE_PATH> + root-owned approval.json (4 NextRequest hosts + 2 S3 hosts, expires 2026-12-31) | Inventory-only works. 105 links "already receipted", 0 downloads since 09-24. private production A: 18 notices, 0 bytes | Keep; add retrieval queue + item identity (WP3) |
| Intake worker / extraction | campaign_tool/records/ intake; private runtime via skills/flock-records-analysis/scripts/run_intake_isolated.sh | Merged. Unit locators for PDF, EML, CSV/TSV, ZIP, XLSX, DOCX, text | Keep; add per-page state, MSG, image paths (WP4) |
| OCR | Candidate image sha256:<HISTORICAL_ID_REDACTED>... (OCRmyPDF 14.0.1, Tesseract 5.3.0, Poppler 22.12.0); apt install failed exit 100 on read-only root | Built and tested on synthetic input; not activated; authoritative container launcher not located | Activate via launcher diff (WP4, owner gate) |
| Catalog + board | M1 code merged; latest candidate snapshot <HISTORICAL_ID_REDACTED>... (2026-09-30 02:29 UTC): 1,647 cards, 2,537 occurrences | Candidate. No accepted snapshot pointer. Board not verified behind Cloudflare Access | Rebuild from ledger (WP5) |
| PR19 catalog links | head <HISTORICAL_ID_REDACTED>..., 16 focused + 245 full tests, 3 green CI runs | Ready, unmerged | Owner merge decision |
| Review-stage overlay | <PRIVATE_REVIEW_OVERLAY> (<HISTORICAL_ID_REDACTED>...) | 4 hashes, 13 pages, 2 privacy passes; all 4 agency/request joins blocked | Import as candidate receipts (WP1) |
| Detectors (7) | flock-campaign-operations/references/detectors.md | Spec only. Zero implementations in merged tree, zero corpus runs | Build (WP6) |
| Triage / queue | Spec weights in pipeline-spec.md | Ad-hoc; selection excluded any PDF with a digest -> zero eligible items | Build first-unfinished-stage queue (WP7) |
| Review contract | flock-records-analysis review contract: author + 2 reviewers, receipts with finding digest | In use manually; ~7 bounded examples with receipts (sample-agency, Morro MOU, AG Policy 461, DOJ, Policy 435, private sample A) | Keep contract; make receipts ledger rows (WP8) |
| Legal comparison | Research baseline research-2026-09-20/KB-README.md; 12-value classification vocabulary | Manual | Keep vocabulary; add rule registry (WP8) |
| Privacy gate | validate_findings.py --check-files --require-ready | Works for structural checks; private sample A held; sample-agency drafts mixed public/private text | Keep; enforce typed public/private split (WP8) |
| Publishing adapter | - | Absent from inspected tree. Private drafts exist; owner_approval: false | Build outbox + site PR adapter (WP9) |
| Scheduler | legacy-mail.timer (user systemd, UID 1000) -> daily.py at 00:00 PT | Runs daily. Requested 07:00 / 13:00 / 20:30 PT replacement not enabled | Replace with one runner timer (WP2 + owner gate) |
| Website | <PRIVATE_PATH> Cloudflare Workers/D1, Brevo, Turnstile, Umami | Deployed commit and route unverified | Out of scope except the publish handoff |

Corpus numbers to carry forward unchanged (from snapshot <HISTORICAL_ID_REDACTED>...): 1,647 cards; extraction complete 1,128 / partial 42 / pending 239 / unsupported 236 / out of scope 2; 385 analyst digest entries over 247 unique hashes (242 in intake, 5 outside); 1,405 cards with no matched digest; 159 author-declared full, 74 partial, 6 conflicting. These describe the extraction and digest stages only. There is no accepted count of originals that have completed every stage.

## 3. Why progress stalled

The work was not wasted; it was unjoined. Six causes account for almost every audit finding, and each maps to one design rule below that the rest of this brief enforces.

| Cause (audit findings) | What actually happened | Design rule that removes it |
| --- | --- | --- |
| No single ledger (F01, F02, F12) | Snapshots, overlays, receipts and chat messages each held part of the state. Nobody could say what was next for a given hash. Directory timestamps were used as workflow cues | One SQLite ledger is the only source of truth. Board, reports and queue are views of it. Every other file is either an immutable original or a receipt that the ledger references by hash |
| Stage confusion (F11, count reconciliation) | "Extracted" was reported as "processed"; a digest's existence closed an item even though independent, legal and privacy stages were open; author-declared coverage was read as acceptance | Seven named stages, each with its own receipt type and denominator. A count is always "N originals at stage S". No stage is inferred from another |
| Wrong queue predicate (F09, F10) | Selection looked for PDFs with no digest, found none, and spent the session searching for a short new document. Handoffs waited for the next hourly wake-up | The queue returns the first unfinished required stage for the highest-priority item, with a lease and a timebox. Handoff is immediate inside the run |
| Identity drift (F03, F06, F14) | 118 scanned vs 69 indexed messages; attachment index lacks account and UIDVALIDITY; 105 portal links "already receipted" with no validated join to bytes | Identity rules in section 5 are fixed. Indexes are derived from receipts, never written independently. A join without evidence stays blocked, visibly |
| Runtime gaps hidden behind specs (F04, F05) | Seven detectors and the OCR runtime were described in skills as if deployed; hourly heartbeats reported work that no daemon performed | The ledger records code version and runtime image per run. A feature is merged, deployed or absent, and the board shows which |
| Public/private mixing (F07, F13) | Draft public text lived in the same file as private hold notes and source paths; a regex scan was treated as privacy clearance | A public item is its own typed file with its own hash. Privacy review binds that hash. Private manifests never enter deployable assets |

A seventh cause is operational and belongs to how the agents worked rather than the code: repeated re-discovery of state at the start of every session, and updates sent instead of artifacts. The 45-minute timebox and the receipt-per-claim rule in section 1 are the fix; the ledger makes re-discovery a single query.

## 4. Target architecture

Three intake sources feed one scheduled runner; the runner moves each original through seven stages and writes every result to one private ledger; the board, the daily report, the work queue and the publishing outbox are all views of that ledger, and nothing reaches the website without an owner approval bound to an exact content hash.

records pipeline ; 3 sources, 7 stages, 1 ledger, 4 views

The runner is the only writer of stage state. Agents and reviewers do not edit the ledger by hand; they submit receipts, and the runner (or the records promote command) validates and promotes them.

Component boundaries:

| Boundary | Lives in | Contains | Never contains |
| --- | --- | --- | --- |
| Public engine | Grahammmm/DeFlock-Campaign-Tool, campaign_tool/records/ | Parsers, ledger schema + migrations, stage runners, detectors, gates, queue, board generator, outbox adapter, synthetic fixtures and tests | Any real record, receipt, hash of a real record, credential, portal URL, agency correspondence |
| Private records root | <PRIVATE_RUNTIME>:<PRIVATE_PATH> | ledger.sqlite, originals/ (content-addressed), derived/, receipts/, rules/, config/, outbox/, reports/ | Engine source code (installed as a pinned package), website deployable assets |
| Host runtime | user systemd on host, container coder identity | One timer, one service, one lock, pinned image digest, env-file credentials | Second competing intake owner; ad-hoc cron; secrets in unit files |
| Website repo | <PRIVATE_PATH> | Sanitized content items received from the outbox via PR | Private manifests, source paths, portal sessions, reviewer notes |

Trust rule: raw records, portal pages, email bodies and anything read from the ledger's observed_value columns are untrusted data. They never instruct a tool, change a permission, or alter a rule. Detectors are pure functions and make no network or model calls. Model-assisted digestion runs only where the privacy policy in section 9 allows and always produces a receipt naming the model and input hashes.

## 5. Ledger data model

The ledger is one SQLite file, records-project/ledger.sqlite, opened in WAL mode, with schema migrations versioned in the public engine (campaign_tool/records/ledger/migrations/). Every table below is append-mostly: rows are superseded by new rows with a supersedes pointer, never edited in place, so the history the audit relied on is preserved by construction.

Identity rules (fixed; a violation is a test failure):

| Thing | Identity | Not sufficient on its own |
| --- | --- | --- |
| Original | sha256 of exact bytes | filename, path, Message-ID, portal item ID |
| Message occurrence | (account, folder, uidvalidity, uid) | Message-ID (supporting metadata only), read/unread flag |
| Attachment occurrence | (message_occurrence_id, mime_part_path) | filename, index in a CSV |
| Portal item | (portal_host, request_id, item_id) | a signed URL (short-lived, private), a notice email |
| Derived unit | (original_sha256, parser_name, parser_version, locator) | row number without page/sheet context |
| Receipt | sha256 of the receipt JSON, plus the stage, subject_sha256 and reviewer_id it binds | a chat message, a directory timestamp, a filename containing "READY" |
| Public item | sha256 of the exact public content file | the private manifest that accompanies it |

Core tables (column lists are the minimum; Codex may add columns, never remove or rename these):

-- what we hold
originals(sha256 PK, bytes, mime_detected, first_seen_at, role, scope, storage_path, preservation_status)
occurrences(id PK, original_sha256 FK, kind {mail,attachment,portal,archive,local}, source_ref JSON,
            parent_occurrence_id, acquired_at, acquisition_method, evidence JSON)
-- mail-specific identity, one row per (account,folder,uidvalidity,uid)
mail_messages(id PK, account, folder, uidvalidity, uid, message_id, eml_sha256, headers JSON, received_at,
              UNIQUE(account,folder,uidvalidity,uid))
portal_items(id PK, portal_host, request_id, item_id, agency_id, title, notice_occurrence_id,
             retrieval_status, last_attempt_at, attempts, last_error, original_sha256, UNIQUE(portal_host,request_id,item_id))
-- what we derived
units(id PK, original_sha256 FK, parser, parser_version, locator JSON, unit_type, text_sha256, derived_path, status)
page_state(original_sha256, page_no, method, method_version, derivative_sha256, confidence REAL NULL,
           needs_visual_review BOOL, status {ok,partial,blocked,inapplicable}, reason, PRIMARY KEY(original_sha256,page_no))
-- who and what it is about
agencies(id PK, name, jurisdiction, portal_host)
requests(id PK, agency_id FK, external_ref, submitted_at, determination_due_at, determination_at, production_at, status)
joins(id PK, original_sha256 FK, agency_id NULL, request_id NULL, join_type, evidence JSON,
      status {typed,hinted,blocked}, blocked_reason)
-- stage machine
stage_state(original_sha256, stage, status {pending,in_progress,done,blocked,inapplicable},
            receipt_sha256 NULL, owner, updated_at, run_id, PRIMARY KEY(original_sha256,stage))
receipts(sha256 PK, stage, subject_sha256, reviewer_id, role, verdict, coverage JSON, locators JSON,
         rationale, model_or_tool, input_hashes JSON, created_at_tz, supersedes NULL, path)
-- analysis outputs
digests(id PK, original_sha256, author_id, coverage_declared, denominator JSON, path, sha256, supersedes NULL)
detector_runs(run_id PK, detector, detector_version, rules_version, started_at, eligible, evaluated, skipped, blocked, manifest_sha256)
detector_hits(id PK, run_id FK, original_sha256, locators JSON, severity INT, observed_value_private JSON, applicability JSON, dedupe_key UNIQUE)
comparisons(id PK, original_sha256, rule_id, rule_version, observation, expected, classification, evidence JSON, counterevidence JSON, next_action, reviewer_id)
-- publishing
proposals(id PK, tier {A,B}, agency_id, public_content_sha256, manifest_sha256, privacy_receipt_sha256,
          independent_receipt_sha256, owner_approval {none,approved,rejected}, approved_content_sha256, supersedes NULL)
publications(id PK, proposal_id FK, site_pr_ref, deployed_version, published_at, rollback_ref)
-- operations
runs(run_id PK, kind, started_at, ended_at, engine_version, image_digest, config_sha256, coverage_cutoff, status, summary JSON)
work_leases(item_key PK, stage, owner, leased_at, expires_at, attempts, last_error, next_eligible_at)
rules(id PK, kind {statute,policy,contract}, authority, citation, effective_from, effective_to, source_sha256, clause, applicability)
alerts(id PK, key UNIQUE, first_seen, last_seen, count, owner, state)

Derived files, never hand-edited: MESSAGE-INDEX.csv and ATTACHMENT-INDEX.csv are regenerated from mail_messages and occurrences on every run with a schema_version header row (fixes F14). The board JSON and catalog.json are generated from stage_state joined to originals. If a generated file and the ledger disagree, the ledger is right and the generator has a bug.

Import of existing evidence (WP1) maps into these tables, not the reverse: v26 and the 1,647-card candidate populate originals, occurrences, units; the 385 analyst digest entries populate digests with coverage_declared kept as declared; existing review receipts populate receipts with stage set from their role; overlay joins with unresolved evidence populate joins with status='blocked'. Nothing is upgraded on import.

## 6. Stage state machine

Every original has exactly one row in stage_state per stage, and a stage is done only when a receipt of the right kind is bound to it. The seven stages run in the order below for every item; a stage can be inapplicable (with a reason) but never skipped silently.

| # | Stage | Enters when | done requires (the receipt) | blocked examples | Invalidated by |
| --- | --- | --- | --- | --- | --- |
| 1 | preserve | Occurrence observed (mail, portal, archive, local) | Bytes stored at content-addressed path; sha256, byte length, mime, occurrence rows with evidence; for portal: HTTP outcome + content-type + redirect chain | Expired link; login HTML returned; host outside approval; archive over resource limits | Never (bytes are immutable; a changed file is a new original with a version link) |
| 2 | extract | preserve done | Every unit or page has a page_state row of ok, partial, blocked or inapplicable; parser name+version; derivative hashes; needs_visual_review set from real OCR confidence or unknown | Unsupported format with no decoder; encrypted archive member; MSG without parser | New parser/OCR version applied to this original |
| 3 | catalog | extract done or blocked (catalog still needs a card) | Card with document type, title, date range, denominator (pages/rows/sheets), role, value score + basis; joins rows to agency/request with status typed or an explicit blocked_reason; occurrence count reconciled | Agency unknown; cross-agency agreement without evidence of parties | Corrected join evidence; new occurrence of the same bytes |
| 4 | detect | catalog done, and original is of an eligible type | One detector_runs manifest naming this original as evaluated, skipped or blocked for each of the 7 detectors at current rules_version | Missing training production; redacted native fields (tracked, not scored) | Rule version change; detector version change; extraction redo |
| 5 | review | catalog done; queue selects by priority | (a) digest with denominator and actual coverage, exact locators, contradictions, unresolved links; (b) independent receipt from a second reviewer who reopened decisive pages before seeing the digest; conflicting declarations resolved or recorded | Reviewer is the author; coverage denominator unknown | Superseding digest; changed source join |
| 6 | compare | review done | One comparisons row per relevant rule with classification from the 12-value vocabulary, evidence and counterevidence, and a rule row with official source + effective interval | Policy version unresolved (VERSION_OR_APPLICABILITY_UNRESOLVED); LEGAL_REVIEW_REQUIRED | Rule revision; new counterevidence; superseding digest |
| 7 | privacy | A proposal exists whose public content file is final | Receipt bound to public_content_sha256, covering text, attachments, image metadata, map points, links, and reidentification of single rows; validate_findings.py --check-files --require-ready pass | Single-row inference unresolved; mixed public/private file; contact-status wording | Any byte change in the public content (new hash = new privacy stage) |

After stage 7 the item is owner_ready. That is a proposal state, not a stage: the owner approves or rejects the exact public_content_sha256. Approval creates approved_content_sha256; the outbox adapter refuses any file whose hash differs.

Transition rules the runner enforces:

A stage moves pending -> in_progress only by taking a lease (work_leases) with an owner and expiry; expired leases return to pending with attempts + 1.

A stage moves to done only through records promote <sha256> <stage> --receipt <path>, which validates the receipt schema, the subject hash, the reviewer identity, the coverage denominator and (for stage 7) the file check. A failed validation leaves the stage unchanged and records the failure reason.

Invalidation cascades forward only: a new digest reopens compare and privacy; a rule change reopens detect and compare for the originals it applies to. It never reopens preserve.

blocked carries blocked_reason, owner and next_eligible_at. Permission and authentication failures are blocked immediately, with no retry. Transient failures retry with backoff up to attempts = 5, then blocked.

The board shows, per stage, four counts: done, in_progress, pending, blocked. "Complete" on the board means all seven stages done or inapplicable; the audit's missing end-to-end count is this query.

Review vocabulary is fixed and imported unchanged from the existing review contract: classifications NOT_ASSESSED, NOT_APPLICABLE, VERSION_OR_APPLICABILITY_UNRESOLVED, NO_CONFLICT_OBSERVED, APPARENT_RULE_CONFLICT, CONFIRMED_RULE_CONFLICT, CONFIRMED_DOCUMENT_CONTRADICTION, DOCUMENTATION_GAP, PRODUCTION_SCOPE_GAP, REDACTION_OR_EXPORT_LIMIT, AGENCY_ASSERTION_UNCORROBORATED, LEGAL_REVIEW_REQUIRED. Legacy aliases map to these on import without upgrading meaning. Each legal conclusion additionally carries the owner's confidence label: verified, likely or needs attorney review.

## 7. Work packages

Ten packages, each one PR in the public engine plus (where marked) one private config or host change. Every package ends with the same three artifacts: a green CI run ID, a synthetic acceptance test named test_wp<N>_acceptance, and a one-paragraph entry in records-project/reports/WP<N>-DONE.md that states what is merged, what is deployed, and what is still absent. All CLI commands live under one entry point, records, in campaign_tool/records/cli.py, extending the four existing gate commands.

### WP0 - Pin the engine and prove the baseline

Scope: make main a reproducible release. Tag 0.1.0 at the current merged tree (<HISTORICAL_ID_REDACTED> or its reviewed successor). Add campaign_tool/records/release_manifest.py that emits commit, package version, schema version, dependency lock hash and enabled features. Install the pinned package on <PRIVATE_RUNTIME> in the coder environment; records version --json on the host must match the tag.

Also: run the full test suite on main fresh (the 245-test figure is a historical PR19 result, not current main), and report the CI run ID.

Acceptance: records version --json on host equals the manifest in the tag; CI run ID recorded; no private data in the repo (records scan-public --strict passes, see WP8).

### WP1 - Ledger, import and reconciliation

Scope: create the SQLite schema (section 5) with migrations; write records import that loads existing evidence into it without upgrading any declared status; write records reconcile that explains every count difference.

Import sources, in order: (1) catalog snapshot <HISTORICAL_ID_REDACTED>... -> originals, occurrences, units, extraction dispositions; (2) COVERAGE-RECONCILIATION.json -> digests (385 entries, 247 hashes; 5 outside intake go to originals with scope='outside-intake'); (3) all review-receipts/independent/** and synchronized-ledger/private/**/REVIEW-RECEIPT*.json -> receipts, with stage derived from role; (4) the corrected overlay <HISTORICAL_ID_REDACTED>... -> joins with status='blocked' for all four; (5) v26 and the Sept 29 candidate for provenance only (recorded as prior snapshots, not merged into counts).

Reconciliations to produce as reports (records-project/reports/reconcile-<date>/): mail runner 118 msgs / 60 attachments vs index 69 / 46, classified per row as duplicate bytes, excluded content, stale index, unavailable original, incomplete receipt or parser error; portal 105 links vs receipts, each classified as bytes-present-and-joined, bytes-present-unjoined, or receipt-without-bytes; the 6 conflicting analyst declarations; the 5 outside-intake hashes.

CLI: records ledger init|migrate|status, records import <kind> <path>, records reconcile mail|portal|digests --report <dir>, records counts (prints the four counts per stage and the end-to-end complete count).

Tests: synthetic fixtures for each import kind; a fixture where index rows and receipts disagree must produce a classified reconciliation, not an exception.

Acceptance (audit Step 1): every included original has one card; every occurrence points to stored bytes or an explicit acquisition failure; records counts reproduces 1,647 / 1,128 / 42 / 239 / 236 / 2 from the ledger; every difference from earlier snapshots has a written explanation. The audit's missing number - originals complete through all seven stages - is now printed (expected to be 0 or near 0; that is fine).

### WP2 - Mail stage, runner skeleton and scheduler

Scope: wrap the existing exporter as the first runner stage rather than rewriting it. Build the runner: records run --profile <name> acquires the lock, mints run_id, records engine version + image digest + config hash in runs, executes stages in order, promotes state atomically per item, and writes runs.summary.

Mail fixes: durable per-folder (uidvalidity, highest_uid) checkpoints in the ledger (the audit could not verify these exist); on UIDVALIDITY change, re-enumerate the folder and reconcile by eml_sha256 before writing new occurrences; the checkpoint advances only past messages with a complete receipt; read/unread flags are never state; attachment occurrences carry account, uidvalidity and MIME part path. Merge the codex/records-mail-delta branch (26 tests) into this package after review, or record why not.

Config-to-mailbox inventory: records mail folders --verify lists every folder the account exposes and marks which are configured; unconfigured folders are an alert, not a silent gap. Credentials stay in mail.json (mode 0600, owner coder) loaded by the runner; the runner never logs env values and sets PYTHONTRACEBACK-style crash output to redact them.

Scheduler (owner gate): produce records-project/ops/timer-diff.md with the exact records-pipeline.timer / .service units (OnCalendar=America/Los_Angeles 07:00,13:00,20:30, Persistent=true), the change that retires legacy-mail.timer, how its last_success checkpoint is carried into the ledger, and the rollback. Do not enable. After the owner's go, enable, then run the unattended acceptance in WP9.

Tests: synthetic IMAP server fixture (e.g. aiosmtpd/greenmail-style or a recorded transcript) covering new attachment, repeat delivery, same bytes in two folders, UIDVALIDITY reset, connection drop mid-folder.

Acceptance: after a simulated drop, successful originals are retained, failed messages are visible with reasons, and replay creates zero duplicate occurrences.

### WP3 - Portal retrieval queue

Scope: extend portal_intake.py (or port it into the engine as campaign_tool/records/portal/) from inventory-only to a durable retrieval queue driven from portal_items.

Behaviour: notice email -> parse request/item IDs -> portal_items row (retrieval_status='pending'); retrieval only for hosts in approval.json and only when the host egress test passes; every attempt writes host, redirect chain, HTTP status, content-type, byte count; a text/html body or a body containing a login form is blocked: login_page, never cataloged; bounded size/timeout/retries; temp bytes promoted to originals idempotently; a refreshed signed URL attaches to the existing item when request+item IDs match. Signed URLs are stored only in portal_items.last_url_private, redacted from every report.

First real targets must be selected from the owner's current private portal inventory after synthetic tests, with explicit permitted access. A previously deferred production stays separate unless the owner changes that instruction.

CLI: records portal inventory, records portal fetch --apply (refuses without approval file + egress), records portal status.

Acceptance: expired link, login HTML, denied redirect, duplicate item and changed bytes each produce the right row state; no false document receipts; unrelated permitted items continue when one is blocked.

### WP4 - Extraction by format and OCR activation

Scope: per-page/per-unit state (page_state), a format inventory of the 236 unsupported and 239 pending items, and the OCR runtime.

Build: records extract inventory groups every non-complete original by detected mime + role and assigns a route: image-direct-review, ocr, decoder-needed:<format>, low-value-proposed, blocked:<reason>. Add an MSG parser (extract-msg or olefile) with tests; add image originals as units with dimensions and hash; keep DOCX ZIP-member extraction from marking the document reviewed; keep formula evaluation and macro execution disabled.

OCR (owner gate): locate the authoritative container launcher (search systemd units, docker-compose*, podman quadlets, and host scripts under <PRIVATE_PATH> report the path). Write records-project/ops/ocr-activation.md with the image reference change to sha256:<HISTORICAL_ID_REDACTED>..., the preserved mounts (coder home, SSH, workspace, read-only archive, writable Flock bind), read-only root, network-disabled flag, and rollback to sha256:<HISTORICAL_ID_REDACTED>.... Do not restart shared services. After owner go: activate, run OCR over image-only pages with per-page method, confidence (only if Tesseract reports one), needs_visual_review, derivative hash.

Historical page-fidelity hold: 19 pages in a preserved PDF need individual rendering and visual receipts. Keep needs_visual_review true until a reviewer clears each; private source identity is excluded.

Acceptance (audit Step 2): every unit is ok, partial with named gaps, inapplicable with reason, or blocked with a recovery owner; no whole-document completion inferred from partial page recovery; a rotated page, a mixed text/image PDF and a poor scan each yield correct page states in tests.

### WP5 - Catalog, typed joins and the private board

Scope: regenerate cards and the board from the ledger; one accepted snapshot pointer; typed agency/request joins.

Build: records catalog build writes catalog.json + board/ from stage_state x originals x joins; records snapshot accept <id> moves the pointer transactionally after validating canonical paths (no ../), occurrence joins, reviewer identities and denominators; candidate overlays render on the board labelled candidate. Populate agencies and requests from the existing request table (build it if missing: every known request with agency, external ref, dates, status). A filename hint creates joins.status='hinted', never typed.

Board: per stage the four counts; new arrivals since last run; open requests with determination_due_at; blocked items grouped by owner; proposals by state. Verify the deployed board sits behind Cloudflare Access including static assets and detail routes; record the check in reports/board-access-check.md.

Acceptance: unknown agency, cross-agency agreement, stale snapshot and path alias are handled; records counts and the board agree byte-for-byte on every number; PR19 either merged (owner decision) or its join logic reimplemented here with its 16 focused tests ported.

### WP6 - Seven detectors

Scope: implement search-before-training, purpose-quality, external-sharing, retention-over-policy, audit-gap, cpra-deadline, volume-anomaly as pure functions in campaign_tool/records/detectors/, each detect(units, joins, rules, config) -> hits, no I/O.

Rules: a rules registry file per campaign (records-project/rules/rules.yaml) with authority, citation, effective interval, source hash, clause; detectors take rules_version as input and record it on every hit. Thresholds from the spec (purpose-quality severity 3 above 25%, severity 2 above 5%; federal/out-of-state recipient high severity) apply to qualified observations only; redacted or blank fields are counted separately in applicability and never raise severity on their own. cpra-deadline uses a California holiday table with a source hash and keeps determination and production dates distinct.

Run: records detect run --all writes one detector_runs manifest per detector with eligible / evaluated / skipped / blocked counts and a manifest_sha256; hits are deduplicated by dedupe_key = (detector, original_sha256, locator, rules_version); observed values are stored in observed_value_private and never exported to public artifacts.

Tests: for every detector, five synthetic fixtures - positive, negative, redacted/unknown, policy-boundary, duplicate-event - plus an idempotence test (a repeat run with unchanged inputs produces identical manifests and zero new hits).

Acceptance (audit Step 3): a full-corpus run manifest exists for all seven; every severity-3 hit has an independent spot-check receipt before it can influence a proposal; no detector output is worded as a legal conclusion.

### WP7 - Work queue and agent packets

Scope: replace ad-hoc selection with records queue next --stage-any --owner <id>.

Predicate: for each original not complete, find its first stage whose status is pending (or blocked with next_eligible_at passed); score with the spec weights (+40 new production for an open request, +30 policy/log/audit/sharing/contract/denial type, +20 per severity-3 hit capped +40, +10 agency in live findings, -50 proposed low-value), clamp 0-100, record config_version and reasons on the packet. Non-PDF originals and privacy-held proposals are eligible. Items are leased for 45 minutes; an unreturned lease expires and the item returns with attempts + 1.

Packet format (records-project/queue/<item>-<stage>.json): original hash, stage, denominator, paths to derived text and page images, prior receipts, the exact receipt template to fill, and the timebox. An agent that cannot finish inside the timebox returns the packet with blocked_reason; the runner records it and issues the next packet in the same run - no waiting for the next hourly wake-up.

Acceptance: with the current corpus the queue returns a non-empty, deterministic, explained top 20; a blocked portal item does not block preserved local records; the "zero eligible" failure mode from the audit cannot recur (test: corpus with every PDF digested still yields review/compare/privacy work).

### WP8 - Review, comparison and privacy tooling

Scope: make receipts first-class, split public from private artifacts, and wire the privacy gate to the content hash.

Build: JSON schema for receipts and digests (campaign_tool/records/schemas/); records promote <sha256> <stage> --receipt <path> (section 6); records review blind-open <sha256> that hands an independent reviewer page images and units without the author's digest, and records the order of exposure; records compare new <sha256> --rule <id> scaffolds a comparisons row with the 12-value vocabulary and the verified / likely / needs attorney review label; records followup draft produces an unsent targeted request (existing request ID, missing category, custodian, period, useful alternative) into outbox/followups/ with send=false.

Public/private split: a proposal is two files - public/<id>.md (exact public content, no editorial prefixes, no source paths) and private/<id>.manifest.json (sources, locators, reviewer notes, hold reasons). validate_findings.py gains --public-file and refuses any public file containing a private path pattern, a portal host, or a hash of a private artifact. records scan-public --strict runs in CI over the whole repo.

Reprocess a bounded, owner-authorized sample of preserved correspondence, sharing reports, agreements, policies and privacy-held access logs through this tooling. Private source identities and evidence locators are intentionally omitted. Preserve the source-first and privacy checks for each.

Acceptance (audit Step 4): three tier-A candidates each have sentence-level sources, an independent source-first receipt, a privacy receipt bound to the public file hash, a limitations block, and owner_approval='none' awaiting the owner; an author reviewing their own claim is rejected by promote; a changed public byte reopens stage 7.

### WP9 - Outbox, site adapter, reports, alerts, recovery

Scope: the last mile and the operational shell.

Outbox and adapter: records publish stage <proposal_id> copies only public/<id>.md whose hash equals approved_content_sha256 into outbox/site/, then opens a PR against <PRIVATE_SITE_REPOSITORY> containing the content item (ID, tier, agency, date, headline, body, public source URLs or null, limitations, supersedes) and nothing else; records site_pr_ref in publications. Deploy and rollback use the site's existing release process; the adapter records deployed_version and rollback_ref. Map points follow the same path with their own versioned dataset and precision radius.

Daily report: records report daily reads the ledger only: new mail, new unique originals, new subscriber opt-ins (count only), stage completions, blocked by owner, proposals awaiting the owner, publications. Written to reports/daily/<date>.md; the Telegram/heartbeat message is a copy of this file, never a separate narrative.

Alerts: keyed and deduplicated in alerts: stale mailbox coverage (>26 h since last successful sweep), unmatched attachments, denied portal access, stalled leases, repeated parser failure on the same hash, unconfigured mail folder. One message per key; a changed error or a recovery is a new event.

Recovery: records restore --into <isolated-dir> rehearsal restoring originals, ledger, rules and receipts from backup with hash reconciliation and no sends or deploys; crash test that kills the runner mid-stage and verifies the next run resumes without duplicate promotions.

Acceptance (audit Steps 5 and 6): one synthetic item traced from original -> review -> PR -> deployed content with a tested rollback; a test attachment placed in the mailbox appears on the board after the next scheduled run with no interactive session; replay causes no duplicates; the old daily exporter is no longer a second intake owner.

## 8. Build order

### WP1 (the ledger) is the first real deliverable and everything else reads from it; the scheduler is enabled last, after a full unattended run is demonstrated, so the pipeline is never "live" while its state is still ambiguous.

build order ; 5 phases, 10 packages, 4 owner gates

Dependencies and what each phase unblocks:

| Phase | Packages | Hard prerequisite | Unblocks | Exit gate (owner) |
| --- | --- | --- | --- | --- |
| 1 Truth | WP0, WP1 | none | every later package reads the ledger | records counts reproduces the audit's numbers and prints the end-to-end count |
| 2 Intake | WP2, WP3 | WP1 | live mail and portal bytes flow into the ledger; timer diff ready | owner reviews timer-diff.md and says go (enable happens in phase 5) |
| 3 Content | WP4, WP5, WP6 (parallel) | WP1; WP6 also needs WP5 joins for agency-scoped rules | detectors and OCR produce stage 2-4 completions at corpus scale | OCR activation approved and executed; PR19 merged or superseded; one full detector run manifest |
| 4 Work | WP7, WP8 | WP5 (cards), WP6 (hits for priority) | review throughput becomes measurable; three real proposals reach owner_ready | tier A/B policy confirmed; three tier-A bundles presented |
| 5 Ship | WP9 | WP8 (approved hash), WP2 (timer diff) | publication path and unattended operation | unattended arrival-to-card demo passed; timer enabled; old daily exporter retired |

Two things Codex can start on day one without any owner input: WP0 and WP1 (all inputs are already-preserved private evidence and public code). Two things Codex must not start until the gate is passed: enabling the timer (phase 5) and activating the OCR image (phase 3), because both change the host runtime.

Order inside a package: schema and fixtures first, then the CLI, then the real-data run, then the WP<N>-DONE.md entry. A package whose synthetic tests pass but whose real-data run is blocked is reported as merged, not deployed, which is a valid and honest state.

## 9. Non-negotiable rules

These apply to every package, every agent profile and every run; a violation is a stop-and-report, not a judgment call. They restate the owner's existing rules and the audit's boundaries in one place so Codex does not have to rediscover them.

Privacy and model routing

Raw public-records responses and agency correspondence may be sent to cloud models. Content carrying third-party personal data - plate reads, private names, home addresses, officer personnel details, badge IDs, signatures, case numbers tied to people - is redacted locally first or processed by local models only. The redaction step writes a receipt naming what was removed by category, never the removed values.

Credentials, subscriber data, and anything from the action/privacy mailbox workflows never enter the ledger's exportable columns, a model prompt, a report, or the public repo.

Every model-assisted step records model name, provider, prompt template hash and input hashes in its receipt. Detectors use no model at all.

Legal analysis cites current statute, regulation, policy or case text verified from a primary source with a source hash and effective interval, and labels each conclusion verified, likely or needs attorney review. Nothing is published as a legal conclusion.

Public / private separation

The public engine repo receives only generic code and synthetic fixtures. records scan-public --strict runs in CI and fails on: any SHA-256 that matches a private original, any <PRIVATE_PATH>, <PRIVATE_PATH> or <PRIVATE_PATH> path, any portal host or signed-URL pattern, any agency correspondence text, any credential-shaped string.

Public content is a separate typed file with its own hash. Privacy review binds that hash. A change of one byte reopens the privacy stage.

Private manifests, source paths, reviewer notes and portal sessions never enter website deployable assets or the site repo PR.

Authority and side effects

Codex does not merge PRs, enable or change systemd units, restart shared services, change container images, change approval.json, send any email or agency message, publish website content, or delete originals. Each of these is an owner action; Codex prepares the exact change and the rollback and logs it in section 10's register.

Approval is per item and per PR. An approval for PR16 does not cover PR19; an approval for one publication does not cover the next.

A root-owned approval file is an authorization record, not proof of network permission. Portal retrieval checks both before any attempt.

Evidence and honesty

Counts are stage-specific and denominators are stated. "1,128 extracted" is never reported as "1,128 processed".

A receipt certifies the operation it records and its observed coverage, not document authenticity, native-database completeness or executed status. Produced images and template fields do not prove execution, adoption dates or signatures.

Missing production is a DOCUMENTATION_GAP unless stronger evidence proves nonperformance.

Directory timestamps, filenames and stale status labels are never workflow state.

Originals are immutable and never deleted; low-value items are proposed low-value until the owner rules otherwise.

Engineering hygiene

One PR per work package, synthetic fixtures only, CI green before the PR is reported. No dirty or staged checkout is ever called a release.

Every scheduled or batch run records engine version, image digest and config hash. A feature is merged, deployed or absent on the board - never assumed from a skill document.

Load and reliability tests never exercise third-party mail or portal services in bulk.

Timebox any single document, parser or rule problem to 45 minutes; record the blocker and move on.

## 10. Owner decisions

Eight decisions are yours and block specific gates; Codex records each in records-project/ops/owner-decisions.md as a row (date, decision, options presented, choice, evidence link) and never re-asks a decided one. Options are ranked; the first is the recommendation.

| # | Decision | Blocks | Options, ranked (1 = recommended) | Why the recommendation |
| --- | --- | --- | --- | --- |
| D1 | Tier A / Tier B policy | WP8 acceptance, phase 4 gate | 1. Adopt as specified: tier A = documentary ("agency produced X", "policy says Y") needs factual + privacy review; tier B = rule-conflict claim needs full factual + legal + privacy review. 2. Single tier, full review for everything. 3. Defer | Tier A lets three well-supported drafts move now without waiting on legal questions; the split is already what the receipts distinguish |
| D2 | Closing proposed low-value items at catalog | WP5, WP7 queue weights | 1. Allow closing at catalog for two roles only - inline branding/signature images and exact-duplicate deliveries - with originals preserved and a reopen path. 2. Keep everything open, rely on the -50 weight. 3. Close broadly by value score | Narrow closing removes real noise (the branding image already has a 1/1 visual receipt) without a score deciding what is worthless |
| D3 | Replacement schedule | Phase 5 gate | 1. Enable the 07:00 / 13:00 / 20:30 PT timer after reviewing timer-diff.md, and accept "cataloged by the next scheduled run" as the service promise. 2. Same plus a fourth run at 23:30. 3. Keep the midnight daily timer | Three runs is your stated preference; a one-hour arrival target would need an event trigger or hourly polling, which is a separate decision |
| D4 | PR19 catalog links (<HISTORICAL_ID_REDACTED>...) | WP5 | 1. Merge after Codex re-runs its tests against current main and reports the CI ID. 2. Close it and let WP5 reimplement with the 16 tests ported. 3. Leave open | It is the only ready join code; re-verification costs one CI run |
| D5 | OCR image activation (sha256:<HISTORICAL_ID_REDACTED>...) | Phase 3 gate, WP4 | 1. Approve once ocr-activation.md names the authoritative launcher, preserved mounts and rollback. 2. Approve a fresh image rebuild instead. 3. Continue manual Poppler rendering only | The image is already built and tested; what is missing is the safe switch, not the software |
| D6 | codex/records-mail-delta merge | WP2 | 1. Merge into WP2's PR after review. 2. Cherry-pick tests only. 3. Discard | 26 synthetic tests exist; discarding them re-creates the work |
| D7 | Portal egress beyond the four NextRequest hosts | WP3 (Caltrans GovQA, MuckRock) | 1. Extend approval.json and host egress per portal, one at a time, starting with whichever holds an open production. 2. Leave scope as is; manual download for others. 3. Broad allowlist | Per-host extension keeps the authorization record honest and the audit trail narrow |
| D8 | Publication approval mechanics | WP9 | 1. Approve by writing the public_content_sha256 into owner-decisions.md (or replying with it in Telegram); adapter matches the hash. 2. Approve by merging the site PR yourself. 3. Verbal approval in chat | A hash-bound approval is what stops a later edit from slipping through as approved |

Things Codex must surface but that need no decision: the mail index discrepancy explanation (WP1), the 236-item format inventory (WP4), the board access check (WP5), and the first detector run manifest (WP6). These arrive as reports.

What you should never be asked for: passwords in chat, manual downloads of every attachment, or routine parser choices. If a package would need any of those, the package is designed wrong and Codex should say so.

## 11. Acceptance test matrix

Each row is a synthetic test in the public engine (tests/acceptance/test_<area>.py) and, where marked, a controlled real-data check on the host recorded in reports/acceptance/<date>.md. A row passes only when the expected evidence is produced by the ledger or a receipt, not by a log line.

| Area | Case | Expected evidence | Real-data check |
| --- | --- | --- | --- |
| Mail | New attachment; repeat delivery; same bytes in another folder; UIDVALIDITY change | Correct occurrence identities; one originals row per hash; zero skipped messages; zero false duplicate receipts | Test attachment sent to the records mailbox appears on the board after the next scheduled run |
| Mail failure | Connection drop after some messages | Successful originals retained; failed messages visible with reasons; checkpoint not advanced past them; replay creates no duplicates | Kill the runner mid-folder; rerun |
| Portal | Expired link; login HTML; denied redirect; duplicate item; changed bytes | No false document receipt; blocked_reason and owner; new original with version link when bytes differ | private production A items after D7 |
| Archive | Path traversal; decompression bomb; encrypted member; DOCX container | Bounded handling; parent-child provenance; no inherited parent completion | - |
| OCR | Mixed text/image PDF; rotated page; poor scan; ambiguous date | Per-page status and derivative hashes; ambiguous content flagged needs_visual_review | sample-agency 19 pages |
| Spreadsheet | Hidden sheet; formula vs cached value; repeated row; missing native event ID | Exact sheet/cell coverage; no formula execution; event-count limit stated | private sample B workbook |
| Catalog | Unknown agency; cross-agency agreement; stale snapshot; path alias | Typed joins with evidence; blocked unknowns; canonical paths; records counts equals board | Four overlay joins resolved or still blocked with reason |
| Detectors | Positive; negative; redacted; rule boundary; duplicate event; repeat run | Deterministic hits with locators and applicability; identical manifest on repeat; zero duplicate hits | Full-corpus manifest for all seven |
| Queue | Every PDF digested but downstream stages open; blocked portal item; expired lease | Non-empty explained queue; unrelated items proceed; lease returns with attempts+1 | Top-20 from the live ledger |
| Review | Author reviews own claim; stale digest hash; changed public attachment | promote rejects; independent re-review required | Three tier-A bundles |
| Privacy | Direct identifier; single-row inference; private note mixed with public text | Publication held until the exact candidate is corrected and re-reviewed; hash-bound receipt | private sample A resolution |
| Scheduler | Overlap; reboot; DST change; stale lease; repeated failure | Single run ownership; correct Pacific times across DST; recovery; one deduplicated alert per key | One week of runs rows with no gaps |
| Publishing | Approved hash vs changed content; withdrawn finding; build failure | Only the approved artifact accepted; correction history; rollback receipt | One synthetic item end to end |
| Backup | Restore originals, ledger, rules, receipts into isolation | Hash reconciliation; consistent references; no sends or deploys during restore | Quarterly rehearsal, first one during WP9 |
| Public repo | Private hash, path, host or credential in a commit | records scan-public --strict fails CI | Every PR |

CI uses synthetic data only and never contacts agency or subscriber systems. A green suite proves the tested behaviour, not full real-record coverage; the real-data column is what closes each gate.

## 12. Definition of done and report format

The pipeline is done when a pinned engine release can take an authorized new attachment through the next scheduled run into the reconciled private catalog; extract or explicitly account for every unit; evaluate every applicable detector with coverage receipts; select work by its first unfinished stage; hold at least three independently checked, privacy-reviewed factual updates awaiting the owner; and move approved content into the website through a traceable, reversible release - with every historical original visible in the same inventory, its prior work preserved and its remaining gaps named.

Done checklist (all must be true, each with a receipt):

[ ] records version --json on the host matches a tagged release; CI run ID recorded

[ ] records counts prints per-stage counts and the end-to-end complete count from the ledger, and the board shows the same numbers

[ ] Mail index discrepancy (118/60 vs 69/46) and portal 105-link reconciliation each have a classified report with zero unexplained rows

[ ] Every one of the 236 unsupported and 239 pending originals has a route or a blocked reason with an owner

[ ] OCR image active on the host with rollback recorded, or D5 explicitly declined

[ ] All seven detectors have a full-corpus run manifest; every severity-3 hit has an independent spot-check receipt

[ ] Queue returns a deterministic, explained top-20 from the live ledger

[ ] Three tier-A proposals at owner_ready with hash-bound privacy receipts

[ ] One item traced original -> PR -> deployed -> rolled back (synthetic or owner-approved real)

[ ] Test attachment reached the board via the enabled 07:00 / 13:00 / 20:30 timer with no interactive session; old daily exporter retired

[ ] Restore rehearsal completed into isolation with hash reconciliation

[ ] owner-decisions.md has a row for D1-D8

Report format Codex uses for every status message (Telegram, PR description, WP<N>-DONE.md, daily report). Five headings, in this order, nothing else:

## Ledger counts (run <run_id>, <timestamp PT>)
preserve 1647/0/0/0 ; extract 1128/0/281/236 ; catalog ... ; detect ... ; review ... ; compare ... ; privacy ...   (done/in_progress/pending/blocked)
end-to-end complete: N

## Changed since last report
- <stage> <count> items, receipts: <path or hash>

## Blocked
- <item or class> - <blocked_reason> - owner: <who> - next eligible: <when>

## Needs owner decision
- D<n>: <one line> - see owner-decisions.md

## State of the build
merged: WP0, WP1 ; deployed: WP0 ; absent: WP6, WP9   (CI run <id>)

No tool-call counts, no narrative of effort, no restating of blockers already in the register. If a heading has nothing under it, write none. A message that cannot be produced from the ledger is a sign the ledger is missing a column, and that is the bug to fix first.

## Public handoff note

The unredacted original and its exact private evidence locators remain with the owner. This sanitized edition is authorized for the public audit package. Do not reconstruct private paths or source identities from placeholders. Read CURRENT-STATE.md and DECISIONS-AND-BOUNDARIES.md for later instructions and verified GitHub state.
