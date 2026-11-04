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
- For source text, `python3 tools/flock_redact.py propose STORE SLUG SHA DOCUMENT --scan-receipt RECEIPT`
  proposes category/byte locators only. Follow [the signing ceremony](../../docs/REDACTION.md):
  verified human sign-off, staged apply, private unchanged original. Eligibility never approves publication.

## Before you approve
- `python3 tools/flock_review_queue.py show STORE [--agency SLUG [--run RUN]] --out NEWDIR [--json]` writes a private review queue.
  `--run RUN` (needs `--agency`) shows that one run only and names it in the header and summary row; an unknown, malformed or linked run is refused before NEWDIR is made.
  With `--json` it also writes `review-queue.json` (`flock-review-queue-v1`: ids, statuses, verdicts, warning codes, not-reviewable reasons and counts, no finding text), both files or neither; it is review-required and approves nothing.
- Read each independent challenge, warning, citation and receipt; this report approves nothing.

## Approve
- **NEEDS G**: G approves the exact texts (approve all / a subset / hold).
- The operator sets `status: approved`, `approved_by`, `approved_on` and `approval_ref` (G-DECISIONS line or card id),
  and `content_hash` to the value the review queue prints beside that exact text. The hash binds the approval to the
  text as approved: any later change to the entry or the run voids it. An approval without it (recorded before
  DF-APPROVAL-TIME-BIND) approves nothing until a person re-reads the text and records its hash; never copy hashes in bulk.

## Check
- `python3 tools/flock_publish.py --check FILE --run <run folder>` until exit 0, before G sees approvals: any entry whose
  text, cites, topic or class drifted from the verified run is refused (`review` is the only field a human edits).

## Recheck
- Before staging and after any facts review: `python3 tools/flock_publish.py recheck RUNDIR [--today D] [--dry-run]`. An approved
  entry citing a disputed, removed or expired fact moves to `needs-review` (exit 3; not approved anywhere) and needs a fresh human
  review and G's re-approval. Only a FACTS.csv edit is allowed past its drift gate; any other drift refuses (exit 1, no write).

## Stage
- `python3 tools/flock_publish.py stage RUNDIR --copy COPY.json --out DIR [--dry-run]` (DIR outside the repo, absent or empty) turns
  approved entries plus the human-written copy into private `content/findings/*.json` (`state: "staged"`, schema-checked)
  and `STAGE-MANIFEST.json`; never builds or deploys. `--dry-run` replaces `--out`: run it first; it writes nothing. The operator copies them into the site PR and sets `state`/`published_at` there.

## Merge
- Merge the site PR (staging deploy only).
- Production dispatch is G-only (`production.yml`).

## Status
- Any time: `python3 tools/flock_publish.py status STORE` lists every verified run's publication stage (counts only, read-only).
- `status STORE --json` prints one `flock-publish-status-v1` document (same rows, same exit 0/1/2): `runs` lists `agency`, `run`,
  `stage`, `findings`, `pending`, `approved`, `errors` (integers, or null where the text shows `-`); `totals` has `runs` and one
  count per stage (`no_export` … `approved`, plus `link_refused`); `note` is review-required. No agencies folder: `{"error", "schema"}`, exit 2.

## Brief
- Private draft (**NEEDS G** before any send): `python3 tools/flock_publish.py brief STORE --agency SLUG --out FILE.md`
  (same as `python3 tools/flock_publish_brief.py STORE --agency SLUG --out FILE.md`) writes one 0600 Markdown draft of the agency's
  approved entries from runs that pass `--check`; FILE is new and outside the repo and `STORE/agencies`. Never sent, posted or built.
  Before asking G: `... brief STORE --agency SLUG --against FILE.md` re-derives it read-only (exit 0 current, 3 drifted by finding id).

## Withdraw
- Withdraw or correct a published entry. **NEEDS G**: G approves a withdrawal or correction; record his approval_ref.
  `python3 tools/flock_publish.py withdraw RUNDIR --finding ID --on D --by NAME --approval-ref REF`, or
  `python3 tools/flock_publish.py correct RUNDIR --finding ID --replaced-by NEWRUNDIR:NEWID --on D --by NAME --approval-ref REF` (NEWID approved in
  a run of the same agency, with new text). The entry becomes terminal `withdrawn`/`superseded` with a row in private `PUBLISH-CHANGES.csv`;
  `--check --run` and `stage` then refuse any approved entry with that text in the agency's runs. The operator removes or replaces the
  site file in a site PR (the tool never edits a site).
- Check first: add `--dry-run` to the same `withdraw`/`correct` command. Every check runs and the tool prints
  `would withdrawn | ID | D` or `would superseded | ID | by RUN:NEWID | D`, then `dry run | nothing written` (exit 0; a refusal
  exits 1 ending `nothing written (dry run)`). Neither `PUBLISH-CHANGES.csv` nor `PUBLISH.json` is written; then run it without `--dry-run`.

## Never
- Set `approved` from a model, or edit an exported `text` instead of re-running the run.
- Publish medium, weak, downgraded or rejected findings, or any text saying an agency broke the law.
- Export quotes, fact values or next steps, or commit a real PUBLISH.json to this repo.
- Trigger a production deploy.
