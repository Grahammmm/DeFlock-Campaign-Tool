---
name: flock-publish
description: Use when a flock-verify run's confirmed strong findings should go to the site. Exports PUBLISH.json (every entry pending) for redaction review and G's approval; never publishes.
---

# Flock publish

`tools/flock_publish.py` is the publication-safety gate: only confirmed strong findings with law/policy and record
citations leave the private store, and every entry starts `pending`. A clean export or check is machine-valid and
review-required, not a legal conclusion.

## Export
- `python3 tools/flock_publish.py export RUNDIR --site-slug S` (S = site route, e.g. `morrobay`). It refuses a
  same-family run (exit 3), re-derives PUBLIC-OK.csv and leaves out any text with banned, privacy or vendor wording.

## Redact
- Human redaction and names review of each `text` (the pipeline's redaction gate; names are not machine-checked).

## Approve
- **NEEDS G**: G approves the exact texts (approve all / a subset / hold).
- The operator sets `status: approved`, `approved_by`, `approved_on` and `approval_ref` (G-DECISIONS line or card id).

## Check
- `python3 tools/flock_publish.py --check FILE --run <run folder>` until exit 0, before G sees approvals: any entry whose
  text, cites, topic or class drifted from the verified run is refused (`review` is the only field a human edits).

## Stage
- `python3 tools/flock_publish.py stage RUNDIR --copy COPY.json --out DIR` (DIR outside the repo, absent or empty) turns
  approved entries plus the human-written copy into private `content/findings/*.json` (`state: "staged"`, schema-checked)
  and `STAGE-MANIFEST.json`; never builds or deploys. The operator copies them into the site PR and sets `state`/`published_at` there.

## Merge
- Merge the site PR (staging deploy only).
- Production dispatch is G-only (`production.yml`).

## Status
- Any time: `python3 tools/flock_publish.py status STORE` lists every verified run's publication stage (counts only, read-only).

## Never
- Set `approved` from a model, or edit an exported `text` instead of re-running the run.
- Publish medium, weak, downgraded or rejected findings, or any text saying an agency broke the law.
- Export quotes, fact values or next steps, or commit a real PUBLISH.json to this repo.
- Trigger a production deploy.
