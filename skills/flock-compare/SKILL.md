---
name: flock-compare
description: Use when comparing one agency's Flock/ALPR facts with California law and its policy. Scaffolds a private run folder, then fails closed on findings without a rating, cited fact, rule or policy clause, or with banned wording.
---

# Flock compare

A model writes FINDINGS.csv/.md for one agency from its FACTS.csv, the shared LAW.csv and its
POLICY.csv, following `PROMPT.md`. The tool only scaffolds the run and checks the output; the
reasoning lives in the prompt. A clean check is machine-valid and review-required, not semantic
or legal acceptance. The verify step (flock-verify) is a separate run by a different family.

## Inputs
- The operator's private store (outside the repo) with `agencies/<slug>/FACTS.csv` (flock-facts),
  `LAW.csv` and `agencies/<slug>/POLICY.csv` (flock-law-policy).
- Provider and model from the worker's actual runtime line, never a profile name.
- One applicability line (≤ 200 characters): the agency's role and which law applies to it.

## Scaffold
- `python3 tools/flock_compare.py scaffold --store DIR --agency SLUG --date YYYY-MM-DD
  --provider P --model M --applicability TEXT` (P in anthropic, openai, xai, google, local).
  It writes `agencies/<slug>/runs/<date>/` with RUN.json, a header-only FINDINGS.csv and FINDINGS.md.

## Model run
- Model run per `skills/flock-compare/PROMPT.md`, writing only inside the run folder.

## Check
- `python3 tools/flock_compare.py --check RUNDIR`; exit 0 only (`findings=F errors=K`).
  A finding citing a `disputed` fact (`fact_ids` or `quote_fact_id`) is an error: review the fact again or drop it.
- Morro Bay: add `--golden tests/fixtures/flock/morrobay-golden.csv`. A mismatch becomes a
  PROMPT.md fix and a rerun, never a hand edit of the findings.

## Summary
- `python3 tools/flock_compare.py --summary RUNDIR`; a `policy off-date` line (a cited POLICY version not governing
  on the RUN.json date) is a PROMPT/finding fix (cite the governing version), never a POLICY.csv edit.
  Each `topic |` line ends `unreviewed_cited=K` (findings citing an `unreviewed` fact; a count, not a gate); `facts |` ends `unreviewed_cited_facts= checked_cited=`.

## Diff
- Compare with the previous run: `python3 tools/flock_compare.py --diff OLDRUN NEWRUN`

## Outputs
- Row numbers, field names, ids and counts. Never claim, quote or fact text.
- `--summary` (clean runs only, else refused) gives rating counts per topic and cited/uncited LAW rules, POLICY rows and facts; a report, not a review verdict.
- `--diff` (read-only; no clean `--check` needed) lists findings added, removed and changed by id, flags `[old public-ok]` ids, and never prints text, fact ids, rule ids or policy refs.
- RUN.json records the input hashes; if FACTS, LAW, POLICY or the law package change, `--check`
  fails until the comparison is redone in a new run folder.

## Never
- OCR, extract, download or edit facts; an unreadable page goes back to the coordinator.
- Upgrade a law `review` label, or rate strong on a `needs_attorney_review` rule.
- Write that an agency violated the law; that is a stop-and-ask item for G.
- Commit or quote real FACTS/LAW/POLICY/FINDINGS text, or publish a finding. Only strong + verified
  findings can reach PUBLIC-OK, and publication still goes through redaction and review.

## Golden case
- Synthetic: `tests/fixtures/flock/compare-facts.csv` imported with flock-facts, `law-sample.csv`
  as LAW.csv, `policy-sample.csv` as POLICY.csv, then `findings-sample.csv` checks clean with
  `--golden tests/fixtures/flock/morrobay-golden.csv` (`findings=7 errors=0`).
- Morro Bay golden: topics, classes, cited rule or clause and maximum ratings from the private
  LEGAL-DEEPDIVE §§1–5. Omitting a class is allowed (more conservative).
