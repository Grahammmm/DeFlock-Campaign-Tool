---
name: flock-publish
description: Use when a flock-verify run's confirmed strong findings should go to the site. Exports PUBLISH.json (every entry pending) for redaction review and G's approval; never publishes.
skill_version: 1.3
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
5. `python3 tools/flock_publish.py --check FILE --run <run folder>` until exit 0, before G sees approvals: any entry whose
   text, cites, topic or class drifted from the verified run is refused (`review` is the only field a human edits).
5a. Before staging and after any facts review: `python3 tools/flock_publish.py recheck RUNDIR [--today D] [--dry-run]`. An approved
   entry citing a disputed, removed or expired fact moves to `needs-review` (exit 3; not approved anywhere) and needs a fresh human
   review and G's re-approval. Only a FACTS.csv edit is allowed past its drift gate; any other drift refuses (exit 1, no write).
6. `python3 tools/flock_publish.py stage RUNDIR --copy COPY.json --out DIR` (DIR outside the repo, absent or empty) turns
   approved entries plus the human-written copy into private `content/findings/*.json` (`state: "staged"`, schema-checked)
   and `STAGE-MANIFEST.json`; never builds or deploys. The operator copies them into the site PR and sets `state`/`published_at` there.
7. Merge the site PR (staging deploy only).
8. Production dispatch is G-only (`production.yml`).
9. Any time: `python3 tools/flock_publish.py status STORE` lists every verified run's publication stage (counts only, read-only).
10. brief (private draft; **NEEDS G** before any send): `python3 tools/flock_publish.py brief STORE --agency SLUG --out FILE.md`
   (same as `python3 tools/flock_publish_brief.py STORE --agency SLUG --out FILE.md`) writes one 0600 Markdown draft of the agency's
   approved entries from runs that pass `--check`; FILE is new and outside the repo and `STORE/agencies`. Never sent, posted or built.
11. Withdraw or correct a published entry. **NEEDS G**: G approves a withdrawal or correction; record his approval_ref.
   `python3 tools/flock_publish.py withdraw RUNDIR --finding ID --on D --by NAME --approval-ref REF`, or
   `python3 tools/flock_publish.py correct RUNDIR --finding ID --replaced-by NEWRUNDIR:NEWID --on D --by NAME --approval-ref REF` (NEWID approved in
   a run of the same agency, with new text). The entry becomes terminal `withdrawn`/`superseded` with a row in private `PUBLISH-CHANGES.csv`;
   `--check --run` and `stage` then refuse any approved entry with that text in the agency's runs. The operator removes or replaces the
   site file in a site PR (the tool never edits a site).

## Never
- Set `approved` from a model, or edit an exported `text` instead of re-running the run.
- Publish medium, weak, downgraded or rejected findings, or any text saying an agency broke the law.
- Export quotes, fact values or next steps, or commit a real PUBLISH.json to this repo.
- Trigger a production deploy.
