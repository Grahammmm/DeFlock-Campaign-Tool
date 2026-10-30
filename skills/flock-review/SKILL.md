---
name: flock-review
description: Use when driving one agency through the agency-review method (facts, rules, compare, F3) with tools/flock_review.py. One resumable command; never runs a model.
skill_version: 1.0
---
# flock-review: one agency config -> facts -> rules -> compare

`tools/flock_review.py` walks a fixed 20-stage table (facts-intake 1-7, rules-intake 8-11, compare and F3 12-20). It runs the
private agency-review method scripts (outside this repository; they need PyYAML) and the repo tools F1/F3, skips every stage whose
inputs are unchanged, and stops at each model-authored step (`wait` stage) until that step is marked. It never runs a model, makes
no network call and never deletes: superseded outputs move to `<out>/.review/superseded/`.

## Steps
1. Convert the agency YAML to JSON once with the private method helper
   `config_to_json.py CONFIG.yaml OUT.json` (run with the method python). Keep the JSON outside the repository.
2. Add the `review` block (schema 1, slug, method_dir, method_python, out, run_date, added_by, roster_doc_id, ocr_log_doc_ids,
   training_join, region_config, f3_findings). `out` and the config stay outside the repository.
3. `python3 tools/flock_review.py --check CONFIG` must end `errors=0`. Fix the named `config: <field>` first.
4. `python3 tools/flock_review.py run CONFIG`. Exit 3 prints `stage <id> WAITING (<reason>)`:
   - `fact-drafts`: the named model card writes `FO/drafts/*.yaml` per the facts-intake skill;
   - `rules-read`: the card reads every rules source and spec per the rules-intake skill;
   - `assess`: the card writes `compare.assessments` per the compare skill;
   - `f3-findings`: the card writes `review.f3_findings`; its provider/model become the F3 run author.
5. The card that wrote the input runs
   `python3 tools/flock_review.py mark CONFIG <stage> --provider <its provider> --model <its actual model>`.
   The operator uses `--provider local` only for input it wrote itself.
6. `run` again. Unchanged stages print `cached`; a changed input re-runs only what depends on it.
7. `python3 tools/flock_review.py status CONFIG` is read-only and shows each stage, its mark and `next=`.

Exit codes: 0 done, 1 failed/validation/lock, 2 usage, 3 waiting. Logs: `<out>/.review/logs/<stage>.log`.

## Never
- Mark a stage for a model that did not write it.
- Edit outputs by hand to pass a check; fix the input and re-run.
- Run two `run`s at once (the LOCK refuses the second).
- Put a config or the out root inside the repository.
- Treat `done` as reviewed or publishable: machine-valid output is review-required, and the public-output rules still apply.
