# Records analysis lane: review-config bootstrap (DFGL-06B)

`records analysis-config --root R --slug S [--check-only]` turns one operator-written agency profile and the agency's synced record
text inputs into a complete `flock_review` CONFIG at `R/agencies/S/review-config.json`, then runs `flock_review --check` in process.
It prints one outcome code: `CONFIG_WRITTEN`, `CONFIG_UNCHANGED`, `PROFILE_MISSING`, `PROFILE_INVALID`, `CONFIG_INVALID <first config field>`
or `ROOT_REFUSED`; `--check-only` prints `CONFIG_PENDING` instead of writing. A failing check leaves the previous config in place.
The root must be outside the repository and must not look like a flock store (top-level `LAW.csv` or `agencies/*/FACTS.csv`).
`review.run_date` is kept from an existing config (a new date only when none exists), so new records re-run only the facts stages.

## Analysis-root layout (host-owned, mode 0700, outside the repo)
```
<root>/
  profiles/<slug>.json                    operator-written, records-review-profile-v1
  .analysis/LANE-ON, analysis-ledger.jsonl, needs-model.json, runs/<run_id>/, packet-outbox/
  agencies/<slug>/review-config.json      written by analysis-config
  agencies/<slug>/review/                 review.out
  agencies/<slug>/review/inputs/records/<original_sha256>.txt + MANIFEST.csv (file,orig_sha256)
  agencies/<slug>/review/inputs/SOURCES.csv
  agencies/<slug>/review/{facts,rules,compare}/
```

## Profile `records-review-profile-v1` (operator JSON, no secrets; unknown keys are `PROFILE_INVALID`)
`schema`, `records_agency_id`, `slug`, `fact_agency`, `agency_name`, `portal`; `review` {`method_dir`, `method_python`, `added_by`,
optional `roster_doc_id`, `ocr_log_doc_ids`, `training_join`, `region_config`, `verify_sample`}; `rules_intake` {`law_specs`,
`policy_specs`, `extra_specs`, `f2_law_map`, `sources`} (copied as-is); `compare_f3` {`applicability`, optional `golden`}; optional
`not_posted_analysis_only_ids`, `log_doc_ids`. See `tests/fixtures/records/analysis-profile.json`.
