---
name: flock-publish
description: Use when a flock-verify run's confirmed strong findings should go to the site. Exports PUBLISH.json (every entry pending) for redaction review and G's approval; never publishes.
skill_version: 1.0
---

# Flock publish

`tools/flock_publish.py` is the publication-safety gate: only confirmed strong findings with law/policy and record
citations leave the private store, and every entry starts `pending`. A clean export or check is machine-valid and
review-required, not a legal conclusion.

## Steps
1. `python3 tools/flock_publish.py export RUNDIR --site-slug S` (S = site route, e.g. `morrobay`). It refuses a
   same-family run (exit 3), re-derives PUBLIC-OK.csv and leaves out any text with banned, privacy or vendor wording.
2. Human redaction and names review of each `text` (the pipeline's redaction gate; names are not machine-checked).
3. **NEEDS G**: G approves the exact texts (approve all / a subset / hold).
4. The operator sets `status: approved`, `approved_by`, `approved_on` and `approval_ref` (G-DECISIONS line or card id).
5. `python3 tools/flock_publish.py --check FILE` until exit 0.
6. Copy it to the site as `data/locations/<site-slug>-findings.json` in a site PR.
7. Merge the site PR (staging deploy only).
8. Production dispatch is G-only (`production.yml`).

## Never
- Set `approved` from a model, or edit an exported `text` instead of re-running the run.
- Publish medium, weak, downgraded or rejected findings, or any text saying an agency broke the law.
- Export quotes, fact values or next steps, or commit a real PUBLISH.json to this repo.
- Trigger a production deploy.
