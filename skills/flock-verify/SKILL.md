---
name: flock-verify
description: Use when a different-family model must re-check one agency's flock-compare findings. Records confirm/downgrade/reject per finding with the page re-read, then writes PUBLIC-OK.csv (confirmed strong only) and QUESTIONS.md (records to seek).
skill_version: 1.1
---

# Flock verify

A verifier from a different model family than the run's author tries to break each finding in a
flock-compare run, following `PROMPT.md`. The tool guards the family, checks VERIFY.csv and writes the
outputs. A clean check is machine-valid and review-required, not semantic or legal acceptance.

## Steps
1. `python3 tools/flock_verify.py start RUNDIR --provider P --model M`, with P and M copied from your
   actual runtime line. It exits 3 when your family equals RUN.json's author family or either is unknown.
2. Model pass per `skills/flock-verify/PROMPT.md`, writing only VERIFY.csv in the run folder.
3. `python3 tools/flock_verify.py --check RUNDIR`; fix each `row N: <field> <reason>` until exit 0.
4. `python3 tools/flock_verify.py finish RUNDIR --golden tests/fixtures/flock/morrobay-golden.csv`
   (Morro Bay). It writes PUBLIC-OK.csv and QUESTIONS.md once (0600) and prints counts only.
5. Is it still current? `python3 tools/flock_verify.py current RUNDIR` (read-only; exit 0 only when both files match a fresh
   finish). Stale or missing means: move both files aside and run finish again.

## Never
- Verify a run authored by your own family, or take the family from a profile name.
- Put a strong finding into PUBLIC-OK without a `confirm`, or upgrade a rating or review label.
- Write a question that asks for explanations; QUESTIONS.md seeks existing records only (for flock-followup).
- Publish, send or quote real findings. PUBLIC-OK.csv is an eligibility list; publication still goes
  through redaction and review, and any text saying an agency broke the law is a stop-and-ask item for G.

## Changelog
- 1.1: `current`: PUBLIC-OK.csv and QUESTIONS.md on disk vs a fresh finish (read-only).
