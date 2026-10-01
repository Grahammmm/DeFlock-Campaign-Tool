# Stack A / Stack B reconciliation

Status 2026-09-30 (evening): Stack A's foundation (#30, #37, #31, #32) is merged to `main` @ f40227a.
Before merging, WP0, WP1, WP2 and WP6 were merged in simulation on top of it: no conflicts, and 815 tests
passed (24 skipped). The remaining Stack B PRs therefore need no reordering. They see one README conflict at
most. The CI fixes listed below have been applied to #33–#36, and all four are green locally.

Original base: `main` @ 15f110c. Stack A = #30→#37→#31→#32→#33→{#34,#35}→#36. Stack B = #17–#29 plus #14.

## Recommendation

Merge Stack B first as the records core: ledger (WP1), exact mail preservation (#18/WP2), extraction and OCR (WP4/#17), catalog (WP5/#19), detectors (WP6), queue (WP7), review bundles (WP8) and recovery and publication gating (WP9). Then rebase Stack A on top as the product shell (law/kit/discovery, wizard, Workers/D1, site/content, outbox, Brevo/newsletter, hosted runner). Stack B is stronger wherever the two overlap. It has 13 green CI runs with pinned cross-WP composition, fail-closed provenance (exact locators, hash bindings, trusted validators) and stage authority. Stack A's runner handlers should become thin shims over Stack B functions. Every Stack B module runs on Linux, so all of them run inside Stack A's runner container. They do need a **persistent** records root (ledger SQLite, blobs, outbox), and today the runner deletes its job workdir after every job. No Stack A file needs whole-file deletion. Replace three functions and rewire four handlers. Fix CI on #33–#36 before rebasing.

## Merge order (combined)

1. #20 WP0: packaging, `records` CLI, `public_scan` (changes `tools/check_public_tree.py`)
2. #22 WP1: ledger (the base for 3, 6–11)
3. #18 M1 mail-delta (WP2 has byte-identical copies of its 3 files, so step 8 is a no-op for them)
4. #19 M1 catalog-links
5. #17 M2 OCR (README conflict with #20)
6. #21 WP4 extraction (→WP1)
7. #29 WP5 catalog (→WP1, WP4, #19)
8. #27 WP2 runner (→#18, WP1, WP4, WP5)
9. #23 WP3 portal (→WP1)
10. #26 WP6 detectors (→WP1 migrations)
11. #24 WP7 queue (→WP1)
12. #25 WP8 review
13. #28 WP9 recovery (→WP8)
14. Stack A remainder: #33, #34, #35, #36 (merge `main` in; #30/#37/#31/#32 already on `main`)
15. #14 social-reels: cherry-pick its 4 commits (its history is unrelated to current main)

All B dependencies are lazy (`import_module`, or `from .ledger import` inside functions). Each branch passes alone because its integration tests skip when a sibling is missing (skips: WP7 24, WP6 33, WP2 24, WP9 19, WP5 75).

## Stack B per-PR summary

| PR | Implements / adds | Depends | Local tests | CI |
|---|---|---|---|---|
| #17 ocr-m2 | `records/extract/ocr.py`: page OCR receipts, retries, holds, reconcile; operating plan | none | 250 OK | green |
| #18 mail-delta | `records/intake/mail_delta.py`: exact-hash receipt importer, MIME locators, ledger promotion | none | 255 OK | green |
| #19 catalog-links | `records/catalog_links.py`: receipt-bound agency/request link candidates | none | 245 OK | green |
| #20 WP0 | pyproject/setup.py wheel, `records/cli.py`, `release_manifest`, `export_clearance`, `public_scan`; release workflow | none | 273 OK | green |
| #21 WP4 | `extraction_routes/ledger/validation.py`, OCRmyPDF derivative holds | WP1 | 254 OK (2 skip) | green |
| #22 WP1 | `records/ledger/`: store, migrations, occurrence/unit/receipt projections, stages (claim/promote) | none | 475 OK | green (PR is DRAFT) |
| #23 WP3 | `records/portal/`: queue, approval+egress gates, pinned HTTPS, WP1 adapter | WP1 | 309 OK (3 skip) | green |
| #24 WP7 | `queue.py`, `queue_runtime.py`: priority, top-20, ledger lease handoff | WP1 | 275 OK (24 skip) | green |
| #25 WP8 | `review_bundle.py`: immutable bundles, `Authority`, role receipts, owner decision | none (edits gate + secret-report hash) | 272 OK | green |
| #26 WP6 | `records/detectors/`: 7 detectors, persistence | WP1 | 332 OK (33 skip) | green |
| #27 WP2 | `records/runner/`: journal, lifecycle recovery, canonical mail, pipeline, systemd templates | #18, WP1, WP4, WP5 | 341 OK (24 skip) | green |
| #28 WP9 | `recovery/backup.py`, `publication.py`, `review_gate.py` | WP8 | 288 OK (19 skip) | green |
| #29 WP5 | `catalog_stage.py`, `ledger_catalog.py`, acceptance, board | WP1, WP4, #19 | 318 OK (75 skip) | green |
| #14 social | `campaign_tool/social` Reel generator | none | 175 OK locally | **red**: `numpy` missing in offline/intake |

**Consistency within Stack B.** Pairwise path overlaps: README.md (#17×#20, #17×#14, #20×#14), and #18×#27 on the 3 identical mail-delta files. There are no other shared paths. One semantic duplicate: OCR exists twice, as a whole-document OCRmyPDF derivative in WP4 `extraction_routes.extract(ocr_executable=)` and as per-page Tesseract in #17 `ocr.extract_image_only_pages`. The owner should pick the canonical path; suggested split is WP4 for the derivative and #17 for page retry and reconcile.

## Overlap and decision table

| Concern | Stack A | Stack B | Stronger / why | Decision |
|---|---|---|---|---|
| Mailbox intake / MIME | `workers/workspace/src/mail.ts` (raw .eml to R2), `runner/handlers/classify_mail.py` `split_message` | `intake/mail_delta.py`, `runner/canonical_mail.py` | B. A's `split_message` drops empty parts, uses an ad hoc 1-based index, has no part/size limits and no rfc822 rejection. B has zero-based walk index plus dotted locator, filename binding, duplicate occurrences and 26 tests | MERGE: Worker ingress and classification stay A; enumeration and locators use B |
| Runner loop / leasing | `runner/loop.py` (HTTP lease from D1), `workers` runner API | `runner/core.py` (local journal, writer lock, timer), `queue.claim_next`, `ledger.stages.claim` | Different layers: A is hosted job transport, B is records-stage leases | BOTH: A job lease outside, B stage claim/promote inside handlers |
| Extraction + OCR | `handlers/extract.py` `run_worker` (calls private `folder._worker`) | WP4 `extraction_routes.extract`, `ExtractionLedgerAdapter`, `ExtractionStageAdapter`; #17 `ocr.py` | B: page manifests, parser provenance, OCR holds, installed validators | KEEP_B; A handler becomes a shim |
| Ledger / receipts | D1 `original`, `receipt_occurrence`, `extraction`, `digest`; `cli.ingest` `private/ledger.sqlite` | WP1 `ledger/store`, `stages`, projections | B: immutable history, stage authority, replay guards | KEEP_B canonical; D1 rows become read mirrors |
| Catalog | none (site `sources.json`, `agencies.json` are public content) | WP5 + #19 | B only | KEEP_B; site content consumes `ledger_catalog.export` only via reviewed bundle |
| Detectors | `digest/detectors.py` (5 regex text leads bound to law-package `rule_id`) | WP6 7 detectors over structured units/joins | Different inputs. B is more rigorous but needs structured evidence that nothing produces yet | BOTH: A stays "digest leads"; `digest.build` also records `detectors.core.run_all` / `persistence.persist_evaluation` when structured inputs exist |
| Review / identity / approvals | `review.py` + `workers/shared/src/review.ts` parity; D1 `review_receipt`; `external_action` with Access `approved_by` | WP8 `review_bundle` (`Authority`, `record_review`, `record_owner_decision`, `assess_bundle`) | B for finding review: signed principals, exact artifacts, late-challenge invalidation | MERGE: Access JWT becomes the WP8 `auth_context`; the TS port stays a UI preview only. `external_action` approval for sends, newsletter and social stays A |
| Publication / withdrawal | `handlers/build_site.py`, `deploy_site` action, public-site Worker version pointer | WP9 `PublicationOutbox.stage/execute/rollback_target`, `WP8ReviewGate` | B gate; A has the only real site adapter | MERGE: register a Cloudflare adapter (`perform(action,key,content,job)`) in WP9's installed adapters and route `deploy_site` through `PublicationOutbox` |
| Recovery / backup | `campaign_tool/backup.py` (tar export/verify/restore, `export_hosted`) | `recovery/backup.py` (`backup`, `restore`, `snapshot_ledger`, `copy_exact`; 256 MiB/1 GiB caps) | B for the local private scope (fd/no-follow, manifest hash); A for hosted export | MERGE |
| Portal | seed `portal.vendor` only | WP3 | B only | KEEP_B; seed can feed the portal queue |
| Release / CLI packaging | `campaign_tool/cli.py` commands, `runner/Dockerfile` (copies tree and `tools/`) | WP0 wheel, `records` CLI, `public_scan` | B | MERGE: Dockerfile installs the WP0 wheel |
| Outbox, law, kit, discovery, site, newsletter, meetings, redact, Workers | all A | none | A only | KEEP_A |

## File-level actions for Stack A after the B merge

**Delete (functions, not files)**
- `runner/handlers/classify_mail.py::split_message`, replaced by `mail_delta.mime_candidates` and `mail_delta.safe_filename`. Write the raw message to the private workdir first, because `mime_candidates` takes a path. `source_id` becomes `<message-id>#<mime locator>`.
- `runner/handlers/extract.py::run_worker` and `row_fields`, replaced by `extraction_routes.extract(source, sha, out, form, timeout, ocr_executable)` plus `extraction_routes.page_manifest`.
- `campaign_tool/backup.py::_snapshot_ledger`, replaced by `recovery.backup.snapshot_ledger`.
- `runner/Dockerfile` line `COPY tools/check_public_tree.py`.

**Rewire**
- `runner/handlers/extract.py::run`: `extract` → `ExtractionLedgerAdapter` enrollment → `ExtractionStageAdapter` (WP1 `stages.claim`/`promote`). For image-only pages, call `ocr.extract_image_only_pages` under the `strict_local` tier only.
- `runner/handlers/classify_mail.py::run`: enroll the original and its occurrences through WP1 (`CanonicalMailBackend` pattern or `ledger_import`), then mirror to D1.
- `runner/handlers/build_site.py::scan_public`: call `campaign_tool.records.public_scan.violations`. `run`: call `PublicationOutbox.stage` with a WP8 bundle id; execution goes through `PublicationOutbox.execute`.
- `runner/handlers/digest.py` and `campaign_tool/digest/build.py::build_digest`: add the optional `detectors.core.run_all` and `persistence.persist_evaluation`.
- `campaign_tool/backup.py::export/restore`: use `recovery.backup.backup/restore` for the private scope; keep `export_hosted`.
- `runner/loop.py::Settings`: add a persistent `RECORDS_ROOT` outside the per-job workdir.
- `workers/schema/d1.sql`: document `original`, `receipt_occurrence`, `extraction`, `digest`, `review_receipt` and `publication` as mirrors; WP1/WP8/WP9 are authoritative. Update `docs/CONTRACTS.md` to match.

**Unchanged**: `law.py`, `kit.py`, `discovery.py`, `site.py`, `content.py`, `markdown_lite.py`, `meetings.py`, `newsletter/`, `outbox.py`, `digest/redact.py`, `digest/schema.py`, `digest/model.py`, `runner/client.py`, `handlers/draft_followup.py`, all `workers/*` except the schema comments, `jurisdictions/`, `data/`, `schemas/`.

## Textual conflicts (B merged onto `origin/codex/integration` in the order above)

| Step | Result |
|---|---|
| #20, #22, #18, #19 | clean |
| #17 | **README.md** (both add a section after the split-plan paragraph; keep both) |
| #21, #29, #27, #23, #26, #24, #25, #28 | clean |
| #14 | `merge` refused: unrelated histories. Cherry-picking df7e47d, 31bb321, 8d1d906, 9e27472 is clean |

Combined tree: 1381 tests, 1 error, 25 skipped. The error is `tests/records/test_ledger_catalog_acceptance.py::test_package_composition_without_host_path_discovery`, a `FileExistsError` at line 199. Its first `copytree` of `campaign_tool` already contains `records/ledger` once WP1 is in-tree. Fix: `dirs_exist_ok=True`, or skip the copy when it exists. This happens with any WP1+WP5 merge and is not caused by Stack A.

## CI fixes for Stack A

1. **`intake` on #33–#36**: `tools/check_secret_report.py` reports "Verified provenance hash hits: 18; potential secrets: 17/18/18/19".
   - Rebase #33 onto f01ea8f. #33–#36 were built on 16ec83e and lack the low-entropy fix, so they still flag `examples/fictional-campaign/content/findings/cedar-policy-posted.json:13`, `content/sources.json:3,13`, `tests/test_content.py:27` and `tests/test_site_build.py:96`.
   - `tests/fixtures/review-cases.json` lines 22, 61, 100, 148, 203, 395, 445, 491 and 643 hold real computed `content_sha256` digests, so they cannot be replaced with `'cafe'*16`. Use a placeholder such as `"@finding_digest"` and resolve it in `tests/test_review_fixture.py` and `workers/shared/test/review.test.ts`. Or make the synthetic findings produce low-entropy digests by construction.
   - Add `// pragma: allowlist secret` at `workers/shared/src/ids.ts:3` (the ULID alphabet) and `workers/wizard/test/plan.test.ts:26-27` (`api_key: "...-test-key"`). Add `# pragma: allowlist secret` at `tests/test_redact.py:13` (the letters string; #34 and #36).
   - Reword `docs/NEWSLETTER.md:13` so the word "secret" does not sit next to `BREVO_API_KEY`, e.g. "stored as the Worker binding `BREVO_API_KEY`" (#35 and #36).
2. **`shell` on #34/#36**: `python3 -m unittest discover -s tests` fails with "ModuleNotFoundError: No module named 'runner.test_build_site'" and 7 more like it. `tests/runner/` collides with the top-level `runner/` package. Fix `.github/workflows/offline.yml:24` to `python3 -m unittest discover -s tests -t . -v`; verified locally (330 OK).
3. **#14**: install `numpy` and Pillow in `offline.yml` and `records-intake.yml`, or skip the render tests when they are absent.

## Risks

- **Persistent storage on Cloudflare Containers.** Their disk is ephemeral, so WP1 ledger, blobs and outboxes need durable storage. Compose or self-hosted runners are fine.
- **Two local ledgers.** `cli.ingest` `private/ledger.sqlite` and the WP1 ledger coexist; a migration rule is needed.
- **Duplicated review logic.** WP8 bundles and `review.py`/`review.ts` can drift unless the TS side is preview-only.
- **Per-WP composition workflows** (`records-wp5.yml`, `records-pipeline-connection.yml`, `records-recovery.yml`, etc.) check out pinned sibling commits and verify module origins (`tools/run_wp2_pipeline_ci.py` raises on an origin mismatch). Once everything is in-tree they need collapsing into one in-tree integration workflow.
- **WP0 provenance.** `setup.py` requires a clean tree and an exact package inventory, so every Stack A `campaign_tool/*` addition changes the release manifest. `runner/` is not packaged.
- **Pending review.** #22, #23, #25, #26 and #28 are still drafts awaiting independent review. Merging them is an owner decision.
