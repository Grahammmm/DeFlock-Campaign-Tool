# Records analysis lane: export (DFGL-06A, container side)

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
  the same folders 0700/0600 by temporary folder and rename, and an identical existing folder is left alone.
  Two exports of the same runs are byte-identical.
- **Safety.** The ledger is read with the same immutable read-only snapshot as the outputs stage. A project
  without `processing-v1/ledger.sqlite` is refused (`EXPORT_NO_LEDGER`); a busy ledger exits 75 with
  `EXPORT_LEDGER_BUSY` and no output. Symlinked run folders are refused. stderr carries codes and counts only,
  never record text.
