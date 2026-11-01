---
name: flock-review
description: Use when driving one agency through the agency-review method (facts, rules, compare, F3) with tools/flock_review.py. One resumable command; never runs a model.
---
# flock-review: one agency config -> facts -> rules -> compare

`tools/flock_review.py` walks a fixed 28-stage table (facts-intake 1-7, rules-intake 8-11, compare and F3 12-20, verify and F4 21-27, report 28). It runs the
private agency-review method scripts (outside this repository; they need PyYAML) and the repo tools F1/F3, skips every stage whose
inputs are unchanged, and stops at each model-authored step (`wait` stage) until that step is marked. It never runs a model, makes
no network call and never deletes: superseded outputs move to `<out>/.review/superseded/`.

## Convert
- Convert the agency YAML to JSON once with the private method helper
  `config_to_json.py CONFIG.yaml OUT.json` (run with the method python). Keep the JSON outside the repository.

## Configure
- Add the `review` block (schema 1, slug, method_dir, method_python, out, run_date, added_by, roster_doc_id, ocr_log_doc_ids,
  training_join, region_config, f3_findings). `out` and the config stay outside the repository.

## Check
- `python3 tools/flock_review.py --check CONFIG` must end `errors=0`. Fix the named `config: <field>` first.

## Run
- `python3 tools/flock_review.py run CONFIG`. Exit 3 prints `stage <id> WAITING (<reason>)`:
  - `fact-drafts`: the named model card writes `FO/drafts/*.yaml` per the facts-intake skill;
  - `rules-read`: the card reads every rules source and spec per the rules-intake skill;
  - `assess`: the card writes `compare.assessments` per the compare skill;
  - `f3-findings`: the card writes `review.f3_findings`; its provider/model become the F3 run author.
- `run` again after a mark. Unchanged stages print `cached`; a changed input re-runs only what depends on it.

## Mark
- The card that wrote the input runs
  `python3 tools/flock_review.py mark CONFIG <stage> --provider <its provider> --model <its actual model>`.
  The operator uses `--provider local` only for input it wrote itself.

## Status
- `python3 tools/flock_review.py status CONFIG` is read-only and shows each stage, its mark and `next=`.

Exit codes: 0 done, 1 failed/validation/lock, 2 usage, 3 waiting. Logs: `<out>/.review/logs/<stage>.log`.

## Never
- Mark a stage for a model that did not write it.
- Edit outputs by hand to pass a check; fix the input and re-run.
- Run two `run`s at once (the LOCK refuses the second).
- Put a config or the out root inside the repository.
- Treat `done` as reviewed or publishable: machine-valid output is review-required, and the public-output rules still apply.

## Verify
After `counts`, stages 21-27 plan and check an independent verify pass (a different model family than the author), then run F4.
1. `run` writes `<CO>/VERIFY-PLAN.json` (seed = run_date digits, `review.verify_sample` rows, default 30: every contradicted / not met
   row plus a seeded sample of met / cannot tell rows; FINDINGS sha256 frozen), recounts FINDINGS vs BUILD-REPORT and re-runs the
   log summary into `<CO>/verify-recount` (byte-compared), then stops at `verify` (exit 3).
2. A card of another provider writes `<CO>/VERIFY.csv` (`row_id,result,reason,page_cite`; result agree / disagree / needs-source)
   and `<CO>/VERIFY.md` covering every planned id, priority first, each with a page cite. Not-posted ids stay out of both files.
3. `mark CONFIG verify --provider <its provider> --model <its actual model>`: refused (exit 3) for `local` or the assess /
   f3-findings provider. `run` writes `VERIFY-CHECK.txt` (counts only; errors name rows and fields, never text) and starts F4.
4. The same card writes `<RUN>/VERIFY.csv` per `skills/flock-verify/PROMPT.md`, then `mark CONFIG f4` with the same provider; `run`
   checks and finishes F4 (`PUBLIC-OK.csv`, `QUESTIONS.md`).
Changed FINDINGS: the old plan moves aside, a new plan is made and the verify mark waits again (upstream changed).

Never: re-sample after seeing results; edit FINDINGS after the plan; verify in the author's family; treat `agree` as acceptance of
unreviewed rows (disagree / needs-source rows are withheld downstream, and verify is not review or release).

## Report
1. After verify-check passes, `flock_review.py run CONFIG` builds the draft as stage 28 `report` with `tools/flock_report.py build
   CONFIG` (output `<compare.out>/report/`, never inside the repository; log `.review/logs/report.log`). It writes `REPORT-DRAFT.md`
   and `SOURCES.json` only when its final lint passes; a lint error fails the stage, which re-runs on the next `run`. The manual
   `python3 tools/flock_report.py build CONFIG` still works for a re-run after an upstream wording fix.
2. Before handing the draft on, `python3 tools/flock_report.py check CONFIG` exits 0 (last line ends `state=current`).
3. Shown: priority rows (contradicted / not met) and sample rows the independent check agreed with, each with numbered cites to a
   record page or rule section. Withheld and only counted: not-posted rows and rows the check disputed or could not source.
4. On `row <id>: <rule>` or `line N: <rule>`, fix the wording in ASSESSMENTS (G1 `assess` re-mark) or add the derived name to
   `review.report_denylist`, then re-run. Errors never echo the offending text.
5. REPORT-DRAFT.md is a private research draft, not legal advice. Publication goes through F9, redaction, review and G.
   Outputs: `check` writes nothing and names each file that is stale, missing or a link (refused); rerun `build` to refresh.
- **Never:** hand-edit the report, include unchecked rows as findings, or name, quote or cite a not-posted record.

## Golden
1. Run the golden before any new agency's compare runs: `python3 tools/flock_review_golden.py CONFIG --expect FILE`. It reads a
   finished run (every stage through `counts` done or skip), recomputes the counts from the stage files (facts by kind, requirements,
   findings by kind x verdict, analysis-only rows, F3 findings, F3 check errors) and compares them key by key to a counts-only JSON.
2. `--write FILE` records the recomputed counts (0600, never overwrites, refused inside the repository). Write an expect file by hand
   from the reference run's own outputs, keep it private for a real agency, and never commit real counts.
3. A `golden: <key> expected X got Y` line is a method regression to diagnose (method script, repo tool or config). Never fix it by
   editing the expect file or the inputs. `golden: <stage> not done` means finish the run first.

## Batch
`tools/flock_review_batch.py` shows where every agency config in a list stands and advances the mechanical stages. It never runs a model,
never touches kanban and never forces, re-marks or skips a stage.
1. Write LIST: one agency config path per line (`#` comments ok); a config or slug listed twice is refused. Optional `--roster ROSTER.csv`
   (F10/F11 header): a slug missing from it, not `in_scope` yes or with another applicability is `held`.
2. `python3 tools/flock_review_batch.py status LIST --roster ROSTER.csv --out DIR` (read-only; writes `DIR/BATCH-STATUS.csv`, outside the repo).
   States: invalid, held, locked, failed, done, in-flight (new/changed model output since the last mark, or an operator marker), waiting (for a model step), ready.
   `input_present` is file presence only. F4 scaffolding (a header-only VERIFY.csv) and unchanged previously marked output are not work.
   A CSV counts as work once it has a row past its header. Rules-read relies only on its explicit operator marker: shared sources/specs
   never establish agency-specific activity, regardless of their contents or timestamps.
   `--prev OLD.csv` (an earlier BATCH-STATUS.csv, read first) adds `change` lines (regressed, advanced, state, new, removed) and `DIR/BATCH-CHANGES.csv`.
3. `... status LIST --run --golden-config MBCFG --golden-expect FILE` runs each `ready` agency one at a time (`run` exit 3 is normal).
4. `... plan LIST --golden-config MBCFG --golden-expect FILE [--max 1|2]` prints at most 2 `[RUN]` card bodies (lane, method, files, mark
   command) for waiting agencies, less the in-flight ones. The operator creates the cards; after each card, `--run` again. To reserve a
   slot before any output is written, create an empty `<out>/.review/in-flight/<stage-id>` file (remove it when the card ends; stale
   markers are the operator's to reconcile). `status` and `plan` never create or remove markers.
- **Never:** skip the golden gate (`golden gate: Morro Bay golden must pass first`); put more than 2 model cards in flight (Claude share
  watcher, PLAN R10); verify in the author's family; treat `done` as publishable.
