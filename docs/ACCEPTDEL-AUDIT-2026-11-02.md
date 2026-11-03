# D1-ACCEPTDEL audit (task t_88a75ef3)

Rollback tag: `pre-acceptdel-t_88a75ef3` -> 877f0e11 (pushed). Approval: decisions/G-DECISIONS.log (line 1306 for 3-D1; line 1376 reconfirmation), RETRO-2026-10-31-3-FLOCK-HEALTH.md section 6.1. Line 1306 is NOT a consumer-scope waiver.

## Ruling applied (operator/G 2026-11-02, final)

Set A (delete) requires that a consumer search actually covered EVERY required scope (~/agent-work/bin, /etc/cron.d, /etc/cron.daily, /etc/crontab, /var/spool/cron/crontabs, real ~/.hermes/profiles/*/cron, searched without any extension allowlist) and found nothing. Set B (retain) is everything else.

## Coverage actually achieved (receipt: consumers2.json)

| Scope | Result |
|---|---|
| agent-work/bin (all text files, incl. extensionless) | searched, 0 hits |
| /etc/cron.d /etc/cron.daily /etc/cron.hourly /etc/cron.weekly /etc/crontab | searched, 0 hits |
| /etc/systemd/system (all text) | searched, 0 hits |
| user crontab dirs | NOT SEARCHABLE: /var/spool/cron/crontabs permission denied (mode drwx-wx--T root:crontab); not bypassed |
| ~/.config/systemd/user EXCLUDED (~/.config prohibited) | NOT SEARCHED (prohibited) |
| hermes cron | searched, 0 hits |
| hermes profile cron (26 dirs) | searched, 0 hits |
| agent-work top-level + ops dirs, extensionless included | searched; hits are historical evidence / audit snapshots only (hand-inspected) |

`~/.config` was never read. Off-box / other-machine consumers cannot be proven either way.

Because `/var/spool/cron/crontabs` could not be searched, a required scope is missing for every candidate. **Set A = 0 files; Set B = 131 files; nothing is deleted.** The earlier 114-file deletion has been reverted in full (tools/ and the eight test edits restored byte-identical to main).

## Counts vs report

- Measured: 131 `tools/*accept*` scripts (report: 131) - matches.
- 105 with no textual reference in any tracked file (report: 105) - matches; they total 52153 lines (report: ~52,047; difference 106 lines, consistent with the report being measured at an earlier commit; not otherwise explained).
- 25 referenced only by their own test + 1 referenced only by a docstring in another candidate (accept_fc4_row_page_state <- accept_fc7_retirement_rollback) = 26 (report: 26) - matches once the chain member is counted.
- Of the 26: 8 are driver-only (test mentions them in comments/text scan) and 17 are test-subject scripts (imported/path-loaded/subprocess-run by the owning test); 1 is the chain member. The 114 candidates = 105 + 1 chain + 8 driver-only.

Candidate lines total: 58184. Test-subject lines total: 9172. Lines removed by this PR: 0.

## Reference-check methods (in-repo, evidence unchanged)
- Whole-stem word grep over all git-tracked files (code, tests, workflows, Makefile/sh, docs, json); no dynamic glob/name loading found (tests only glob flock_*.py).
- Runnable-doc and CI/workflow mentions: none for candidates.
- External: consumers2.py searched the scopes above without extension allowlist; hits only in kanban evidence JSON and an audit snapshot copy of this repo.

## Set B-RETAINED: candidates (114) - unresolved because /var/spool/cron/crontabs could not be searched

| File | Lines | In-repo refs | External hits (searched scopes) | Required scope missing |
|---|---|---|---|---|
| tools/accept_bm1_correspondence_json.py | 757 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa01.py | 312 | tests/records/test_fa01_reconcile_kind.py | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa03.py | 307 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa100.py | 404 | tests/records/test_fa100.py | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa101.py | 420 | tests/records/test_fa101.py | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa102.py | 517 | tests/records/test_fa102.py | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa1309.py | 83 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa1401.py | 605 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa1404.py | 414 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa1412.py | 252 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa1601.py | 312 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa1604.py | 286 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa1605.py | 224 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa1609.py | 234 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa1702.py | 265 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa1704.py | 238 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa1705.py | 310 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa1706.py | 327 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa1709.py | 277 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa1710.py | 250 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa23_occurrence_uid.py | 330 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa31_safety_json.py | 251 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa34_ocr_page_unique.py | 232 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa43_collection_members.py | 587 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa45_unit_finite.py | 791 | tests/records/test_fa45_unit_finite.py | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa47_extraction_schema.py | 603 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa49_ocr_tsv.py | 1562 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa51_native_sha.py | 604 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa52_budget_types.py | 1082 | tests/records/test_fa52_budget_types.py | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa55_url_unicode.py | 778 | tests/records/test_fa55_url_unicode.py | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa56_collection_query.py | 524 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa68_refusal_clock.py | 391 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa70_stage_verdict.py | 276 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa91.py | 518 | tests/records/test_fa91.py | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa92.py | 746 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa94.py | 199 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa95.py | 295 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fa99.py | 256 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fc2_retained_scope.py | 1217 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fc3_agency_shapes.py | 1159 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fc3_bundle_json.py | 1114 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fc3_chunk_identity.py | 1191 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fc4_catalog_json.py | 754 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fc4_review_offsets.py | 853 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fc4_row_page_state.py | 1209 | tools/accept_fc7_retirement_rollback.py | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fc4_source_ordinal.py | 561 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fc7_board_unicode.py | 632 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fc7_catalog_join_json.py | 1153 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fc7_catalog_lease_clock.py | 970 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fc7_detector_run_clock.py | 701 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fc7_queue_scope.py | 798 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fc7_report_close.py | 1388 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fc7_retirement_rollback.py | 1340 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fc7_search_unicode.py | 464 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fc7_thread_cycles.py | 362 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fc7_workbook_sheet_shape.py | 462 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fc7_wp4_artifact_bound.py | 427 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fc_cli_replay.py | 436 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fc_detector_shape.py | 462 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fc_doc_coverage.py | 383 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fc_evidence_counts.py | 425 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fc_finding_identity.py | 664 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/accept_fc_kind_json.py | 422 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc10_digest_version_acceptance.py | 467 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc10_evidence_token_bounds_acceptance.py | 294 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc10_fact_event_key_acceptance.py | 624 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc10_law_jurisdiction_acceptance.py | 825 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc10_normalization_tail_acceptance.py | 302 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc10_partial_hit_suppression_acceptance.py | 359 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc10_persistence_fault_prefixes_acceptance.py | 279 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc10_preparation_revision_closure_acceptance.py | 347 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc10_purpose_rate_oracle_acceptance.py | 356 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc10_review_receipt_cap_acceptance.py | 254 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc10_volume_boundary_oracle_acceptance.py | 274 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc11_digest_identities_acceptance.py | 507 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc11_event_offset_acceptance.py | 843 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc11_model_payload_budget_acceptance.py | 544 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc12_audit_hearing_acceptance.py | 362 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc12_bundle_snapshot_acceptance.py | 526 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc12_digest_snapshot_acceptance.py | 559 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc12_duty_groups_acceptance.py | 390 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc12_reconcile_priority_acceptance.py | 367 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc13_attempt_binding_acceptance.py | 584 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc13_correspondence_priority_acceptance.py | 723 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc13_document_priority_acceptance.py | 733 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc13_mapping_consistency_acceptance.py | 481 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc13_receipt_positions_acceptance.py | 372 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc13_redaction_iterables_acceptance.py | 324 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc13_rule_qualification_acceptance.py | 392 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc13_structured_revisions_acceptance.py | 273 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc14_reconcile_snapshot_acceptance.py | 253 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc15_message_id_case_acceptance.py | 224 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc15_policy_empty_requirements_acceptance.py | 393 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc15_row_cross_source_dedup_acceptance.py | 301 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc15_row_header_cardinality_acceptance.py | 323 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc15_support_id_matrix_acceptance.py | 327 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc16_detector_manifest_balance_accept.py | 386 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc16_digest_duration_scales_accept.py | 712 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc16_document_locator_accept.py | 1044 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc16_document_ocr_kind_accept.py | 245 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc16_document_summary_segments_accept.py | 286 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc16_law_business_oracle_accept.py | 285 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc16_law_deadline_range_accept.py | 236 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc16_law_summary_snapshot_accept.py | 331 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc16_structured_support_types_accept.py | 225 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc9_audit_windows_acceptance.py | 616 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc9_candidate_clocks_acceptance.py | 527 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc9_digest_legal_downgrade_acceptance.py | 513 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc9_header_citations_acceptance.py | 705 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc9_policy_history_compare_acceptance.py | 327 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc9_request_identity_acceptance.py | 268 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc9_review_restart_lifecycle_acceptance.py | 268 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc9_rows_detector_handoff_acceptance.py | 334 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |
| tools/fc9_training_context_acceptance.py | 328 | none | agent-work top-level + ops dirs, extensionless included | /var/spool/cron/crontabs |

## Set B-RETAINED: test-subject scripts (17) - script is the subject under test; folding would worsen its test

| File | Lines | Owning refs |
|---|---|---|
| tools/accept_fa1301.py | 523 | tests/records/test_fa1301.py |
| tools/accept_fa1306.py | 144 | tests/records/test_fa1306.py |
| tools/accept_fa1310.py | 93 | tests/records/test_fa1310.py |
| tools/accept_fa1607.py | 498 | tests/records/test_fa1607_image_resource_matrix.py |
| tools/accept_fa21_report_finite.py | 461 | tests/test_fa21_report_finite.py |
| tools/accept_fa24_notice_href.py | 322 | tests/test_fa24_notice_href.py |
| tools/accept_fa41_pointer_json.py | 850 | tests/records/test_fa41_pointer_json.py |
| tools/accept_fa42_link_nonblock.py | 796 | tests/records/test_fa42_link_nonblock.py |
| tools/accept_fa50_ocr_state.py | 834 | tests/records/test_fa50_ocr_state.py |
| tools/accept_fa53_replay_preservation.py | 651 | tests/records/test_fa53_replay_preservation.py |
| tools/accept_fa97.py | 261 | tests/records/test_fa97.py |
| tools/accept_fc2_duration_tokens.py | 877 | tests/runner/test_fc2_duration_tokens.py |
| tools/accept_fc3_context_snapshot.py | 801 | tests/records/test_fc3_context_snapshot.py |
| tools/fc14_tracker_snapshot_acceptance.py | 328 | tests/records/test_fc14_tracker_snapshot.py |
| tools/fc15_claim_bounds_acceptance.py | 359 | tests/records/test_fc15_claim_bounds.py |
| tools/fc9_claim_context_replay_acceptance.py | 683 | tests/records/test_fc9_claim_context_replay.py |
| tools/fc9_document_candidate_counts_acceptance.py | 691 | tests/records/test_fc9_document_candidate_counts.py |
