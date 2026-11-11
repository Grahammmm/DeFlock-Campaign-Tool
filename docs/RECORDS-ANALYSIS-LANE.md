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

## Export (DFGL-06A, container side)

`records analysis-export` is a read-only command that runs inside the records container. It exports, per
settled run, the DFGL-05 analysis-ready manifest and a page-marked text copy of each ready record, so the
review lane (DFGL-06/06B/07, host side) can work from hash-bound text without touching the project. It never
copies originals, mail, approvals or credentials, never writes inside the project, and starts nothing. Text
derivatives are review-required; exporting them is not a review.

```
records analysis-export --project P (--run RUN | --after RUN|- [--max-runs N]) (--out DIR | --stdout)
```

- **Runs.** Only ids matching `run_<YYYYMMDDTHHMMSSZ>_<8 hex>` count; proof and console folders are ignored. A run
  is *settled* when its `outcomes/analysis-ready.json` exists or a lexically later `run_*` folder exists.
  `--after RUN` (`-` = from the first) walks later runs in id order and stops at the first unsettled run, so a
  later run is never exported before an earlier one (at most `--max-runs`, default 5). `--run` exports one
  settled run (`EXPORT_RUN_UNSETTLED`, exit 3, otherwise).
- **Folder per run** (`<run_id>/`): `analysis-ready.json` (byte copy); `texts/<original_sha256>.txt` (0600; one
  `[[PAGE n]]` block per page, unit text in ordinal order) only for a record whose text state is `text_reviewed`
  and whose current extraction units are all paged and hash-bound; `TEXTS.csv`
  (`original_sha256,agency_id,request_id,pages,text_sha256,state,received_at,received_at_source`); `EXPORT.json`
  (`records-analysis-export-v1`, `schemas/records-analysis-export-v1.json`; sha256 of every other file; written
  last).
- **States.** `exported`, `TEXT_NOT_READY` (any other text state, including OCR text not yet visually checked)
  or `EXTRACTION_UNAVAILABLE` (no current, complete extraction). A not-ready record has a row and no text file.
- **Dates.** `received_at` and `received_at_source` are copied from the same run's `received-items.json` item with
  that sha256, agency and request, and are empty when that item is absent or has `null`; they are never derived
  from file times or the run time.
- **Missing manifest.** A settled run with no usable `analysis-ready.json` (absent, unparseable or for another
  run) is exported as a marker folder holding only `EXPORT.json` with `analysis_ready: "missing"`; the host
  records it as `ANALYSIS_READY_MISSING` and never skips it silently.
- **Output.** `--stdout` writes an uncompressed POSIX tar (regular files only, mtime 0, uid/gid 0, sorted names)
  so container-owned files reach the host through the container pipe; `--out DIR` (outside the project) writes
  the same folders 0700/0600 by temporary folder and rename, and an existing folder is left alone only when every member
  (names, bytes, modes, no extras or links) matches the new export, otherwise `EXPORT_OUT_EXISTS`. `--out` is held
  as an open directory (never followed through a symlink, checked outside the project, missing parts made through it)
  from before the first write to the last.
  Two exports of the same runs are byte-identical.
- **Safety.** The ledger is read with the same immutable read-only snapshot as the outputs stage. A project
  without `processing-v1/ledger.sqlite` is refused (`EXPORT_NO_LEDGER`); a busy ledger exits 75 with
  `EXPORT_LEDGER_BUSY` and no output. Symlinked run folders are refused. stderr carries codes and counts only,
  never record text.

## Receipt, retention and replay (DFGL-06, host side)

```
records analysis-sync --root R --receive (DIR | --tar FILE|-)   # receipt only
records analysis-sync --root R [--dry-run]                      # replay every pending run x agency pair
records analysis-sync --root R --last-received | --pending
```

- **Receipt cursor.** `--last-received` is the newest run id retained in `R/.analysis/runs/<run_id>/` (or `-`). Folders are received
  in id order; a folder is retained only when `EXPORT.json` (`records-analysis-export-v1`) matches every file's sha256 and every
  `exported` TEXTS.csv row matches its text. `EXPORT_INVALID` stops receipt there, so the cursor never jumps a gap and the next
  cycle re-exports from it. Tar members must be regular files with the 06A names, at most 64 MiB each. A retained id arriving
  again is `RECEIVED_UNCHANGED` (same bytes) or `EXPORT_CONFLICT` (different bytes; the retained copy is never touched).
- **Retained run store.** `.analysis/runs/` is append-only (dirs 0700, files 0600, temporary folder then rename, never overwritten,
  never pruned by code). Its `analysis-ready.json` is the copy DFGL-10 reads for `law_package` and per-record metadata.
- **Replay.** Every call walks the retained runs in id order; each run x agency pair without a final ledger outcome is placed:
  the agency profile is found by `records_agency_id` (`PROFILE_MISSING`), `texts/<sha>.txt` goes to
  `agencies/<slug>/review/inputs/records/<sha>.txt` (different existing bytes are `TEXT_CONFLICT`, never overwritten), `MANIFEST.csv`
  gets one `(file, orig_sha256)` row per sha, `inputs/SOURCES.csv` one row per `(sha, run_id)` carrying `received_at` and
  `received_at_source` verbatim, then `analysis-config` rebuilds the CONFIG (`CONFIG_INVALID`). `PROFILE_MISSING`, `TEXT_CONFLICT`
  and `CONFIG_INVALID` stay pending and retry every call (a new ledger line only when the code changes); `SYNCED`, `UNCHANGED`,
  `NO_AGENCIES` and `ANALYSIS_READY_MISSING` are final and never processed again.
- **Ledger** `.analysis/analysis-ledger.jsonl` (0600, append-only): `receive` lines (`RECEIVED`, `RECEIVED_UNCHANGED`,
  `EXPORT_INVALID`, `EXPORT_CONFLICT`) and `sync` lines (`run_id`, `agency_id`, `slug`, `outcome`, `new_texts`, `config`, `at`).
- **Output and limits.** stdout carries ids, codes and counts only. `--dry-run` writes nothing (`WOULD_SYNC` for placeable pairs).
  Exit 0 when nothing is pending and no receipt was refused, 1 otherwise. A root inside the repository or shaped like a flock
  store is `ROOT_REFUSED`. The command never writes a fact and never calls `flock_facts` or a model.
