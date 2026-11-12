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

## Tick and model handoff (DFGL-07f, host side)

```
records analysis-tick --root R [--max-agencies N=20] [--dry-run]
```

- **Gate.** Without `R/.analysis/LANE-ON` the command prints `ANALYSIS_LANE_OFF`, exits 0 and writes nothing. The operator creates that file;
  the command never does. A root inside the repository or shaped like a flock store is `ROOT_REFUSED` (exit 1).
- **Advance.** For each `agencies/*/review-config.json` (sorted, at most `--max-agencies`) it reads `flock_review.py status CONFIG --json`
  and, when the key below moved, runs `flock_review.py run CONFIG` (a subprocess of the engine checkout's own tool; a missing tool is
  `ERROR REVIEW_TOOL_MISSING`) and reads the status again. No model runs; every wait stage stays a wait until a model card writes its
  files and calls `flock_review.py mark`.
- **Outcome per agency** (one line each in `.analysis/analysis-ledger.jsonl`, `kind: tick`, with `status_sha256`, `config_sha256` and
  `review_sha256`): `WAITING <stage>` (the DAG stopped at a wait), `DONE`, `ADVANCED <from>→<to>` (stopped at a non-wait stage),
  `UNCHANGED`, `ERROR FAILED <stage>`, `ERROR LOCKED`, `ERROR CONFIG`, `ERROR REVIEW_TOOL_MISSING`.
- **UNCHANGED key.** The status document, the config and the review state (the bytes of `compare.facts` FACTS.csv followed by its sibling
  FACT-REVIEWS.csv, `missing` for an absent file) all equal the agency's last non-error ledger line: only `status` runs. A human review
  changes the review state, so the next tick runs the DAG (only `report` reruns) and the tick after is `UNCHANGED`. Errors are never
  `UNCHANGED`: a failed stage is retried each tick, and a held `.review/LOCK` (a model card is marking) is `ERROR LOCKED` (exit 0).
- **Output.** `<slug> <outcome>` per agency (an `UNCHANGED` line adds the wait it still sits at) and
  `analysis: A advanced, W waiting for a model, D done, E errors`. Exit 0 unless an `ERROR` other than `ERROR LOCKED`. `--dry-run` runs
  `status` only and writes nothing (`WOULD_RUN`).
- **`.analysis/needs-model.json`** (`records-needs-model-v1`, `schemas/records-needs-model-v1.json`, example `docs/examples/needs-model.example.json`;
  atomic, 0600, rewritten every tick): one entry per agency whose next stage is a wait: `agency`, `records_agency_id`, `stage`, `role`
  (`verify` for `verify` and `f4`, else `author`), `author_family` (provider of the `f3-findings` mark, else `assess`, else null),
  `verify_family` (provider of the `verify` mark, for `f4` only), `input_fp`, `wait_files` (relative to the root when inside it),
  `config`, `sources` (SOURCES.csv, for `fact-drafts` only). Slugs, stage ids, families, hashes and paths only; never record text.
  `flock_review.py mark` refuses a `verify`/`f4` mark from the author's family (exit 3), so that wait stays open.

## Finding packets in the host outbox (DFGL-07g)

After an agency's tick outcome leaves the DAG at `DONE` (also on an `UNCHANGED` tick), `analysis-tick` rebuilds that agency's packets every cycle
and prints one `<slug> PACKET_*` line (and one `kind: packet` ledger line) holding ids, codes and counts only:

- **Joins.** Each `PUBLIC-OK.csv` row is joined by `finding_id` to the F3 run's `FINDINGS.csv`. Facts are read once with
  `flock_facts.read_reviewed` (shared lock): a pending journal holds every finding `EVIDENCE_REVIEW_TXN_PENDING`, an invalid or raw-edited review
  log `EVIDENCE_REVIEW_STATE_INVALID`. A cited fact must exist (`EVIDENCE_FACT_MISSING`), be `checked` (`EVIDENCE_FACT_UNREVIEWED`,
  `EVIDENCE_FACT_DISPUTED`) and be a `record` whose `source_sha256` is in `SOURCES.csv` (`EVIDENCE_SOURCE_UNKNOWN`).
- **Documents.** One per cited source sha; `date_received` is the first 10 characters of the earliest `received_at` (not `YYYY-MM-DDT…Z` →
  `EVIDENCE_DATE_UNAVAILABLE`; never a file or run time). `request_id` is the common `request_id`, else empty. Provenance `run_id`,
  `received_at` and `received_at_source` all come from that same earliest row.
- **Law.** The newest retained `analysis-ready.json` listing the agency names the package (a run with no `analysis-ready.json` is skipped; an unreadable one is `EVIDENCE_LAW_UNAVAILABLE`); `campaign_tool.law.load_package` must report the same
  status (`EVIDENCE_LAW_UNAVAILABLE`, `EVIDENCE_LAW_MISMATCH`). Citations (all `verified`) are one per checked fact plus the citation of each
  `verified` rule of a `reviewed` package; every other rule id and every policy ref goes to `provenance.withheld_*`. A draft package adds the
  `Research draft - not legal advice. The <id> law package is draft and unreviewed.` note to the text.
- **Pre-validation.** Each packet passes `public_packets.clean` unchanged (`EVIDENCE_REFUSED <code>`, `EVIDENCE_TITLE_INVALID`,
  `EVIDENCE_AGENCY_INVALID`); the text is never rewritten. Nothing here approves or publishes: G's signature remains the only approval.
- **Outbox.** `.analysis/packet-outbox/<slug>-<key[:12]>.json` (0600, atomic, schema `records-analysis-packet-v2`: `agency_id`, `slug`, `key`,
  `inputs` hashes, `packets[]` of `{packet, provenance}`, `held[]` of `{finding_id, code, ids}`), written only when `packets` is non-empty and
  the key is new. Outcomes: `PACKET_STAGED <file> n=<packets> held=<held> <code>=<n>…`, `PACKET_UNCHANGED`, `PACKET_NOTHING_OK`, `PACKET_ALL_HELD`.
  `--dry-run` computes and prints but writes no file and no ledger line.
