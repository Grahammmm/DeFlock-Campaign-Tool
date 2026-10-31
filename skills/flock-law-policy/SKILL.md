---
name: flock-law-policy
description: Use when recording California law rules or per-agency ALPR policy versions for the Flock work. Checks LAW.csv and POLICY.csv fail-closed and lists which policy version governs on a date.
skill_version: 1.1
---

# Flock law and policy

Keeps one shared LAW.csv and one POLICY.csv per agency, in the operator's private agency-facts
store (outside the repo). No model step: the tool validates, it does not read or fetch documents.
A clean check is machine-valid and review-required, not legal acceptance.

## Inputs
- LAW.csv, one row per rule in the law package (`campaign_tool.law`, jurisdiction `us-ca`).
- `agencies/<slug>/POLICY.csv`, one row per policy version found for that agency.
- Column lists and rules: `tools/flock_law_policy.py` (`LAW_HEADER`, `POLICY_HEADER`).

## Steps
1. Transcribe LAW rows from the official text the acquisition owner has already fetched:
   `rule_id`, `review` and `effective_from` are copied exactly from the law package.
2. Transcribe POLICY rows: hash the policy document bytes (`doc_sha256`); put the footer date
   in `footer_version_date`. Use `adoption_evidence` `unverified` until adoption is shown.
3. For adopted versions give short evidence text, its `adoption_sha256` and `governs_from`.
4. Check: `python3 tools/flock_law_policy.py --check STORE` (or one CSV); exit 0 only. Two adopted versions
   of one agency + `policy_id` whose governing windows share a day (ends inclusive) are refused.
5. List versions: `python3 tools/flock_law_policy.py versions STORE --agency SLUG [--on YYYY-MM-DD]`.
   Exit 3 with `no adopted version established for DATE` means the date is not covered.
6. Fix each reported `row N: <field> <reason>` at the source; the tool never repairs a row.

## Outputs
- Counts `law=L policy=P errors=K`, row numbers, ids, labels and dates. Never duty or evidence text.

## Never
- Use a memo hash as `text_sha256` or `doc_sha256`; hash the fetched official text or document bytes.
- Upgrade or change a `review` label; copy the package label. Do not edit the law package.
- Fill `governs_from` or `governs_to` without adoption evidence; the footer date is not adoption.
- Use a host other than `leginfo.legislature.ca.gov` or `oag.ca.gov` for `official_url`.
- Download anything; missing text or bytes go to the acquisition owner.
- Put a real LAW/POLICY row, store path or hash in the repo, a PR or a log.

## Golden case
- `python3 -B tools/flock_law_policy.py --check tests/fixtures/flock/law-sample.csv` exits 0 (synthetic);
  likewise `policy-sample.csv`, whose `versions ... --agency sample-city-police-department --on 2025-07-01` exits 3.

## Changelog
- 1.0: LAW.csv and POLICY.csv schema, `--check` and `versions`.
- 1.1: `--check` refuses overlapping governing windows for one agency + `policy_id`.
