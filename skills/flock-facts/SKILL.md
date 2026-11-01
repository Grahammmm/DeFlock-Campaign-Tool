---
name: flock-facts
description: Use when importing or checking per-agency Flock public-record facts. Loads an Oct 27 style FACTS.csv into the private store and fails closed on a row without page, sha256 or quote.
skill_version: 1.2
---

# Flock facts

Keeps one private CSV of cited facts per agency, in the operator's private agency-facts
store (outside the repo). No model step: the tool validates, it does not read documents.

## Inputs
- A v1 FACTS.csv: `agency,request,fact_type,value,source_sha256,page,verbatim_quote`
  (UTF-8, CRLF accepted), or a v2 file or store directory. Columns: `docs/FLOCK-FACTS.md`.
- The operator's private store path, and the importer's name (`--added-by`).

## Steps
1. Check the source first: `python3 tools/flock_facts.py --check SRC.csv`.
2. Fix each reported `row N: <field> <reason>` at the source. A page that cannot be read
   becomes an `UNREADABLE` row with page and sha256, for the extraction owner.
3. Import: `python3 tools/flock_facts.py import SRC.csv --store STORE --added-by NAME`.
4. Expect `imported=I duplicates=D errors=0`. Any error leaves the store untouched.
5. Re-import is safe: duplicates are skipped by the dedupe key.
6. Verify the store: `python3 tools/flock_facts.py --check STORE`; exit 0 only.
7. Treat a clean check as machine-valid and review-required, not accepted.
8. Coverage: `python3 tools/flock_facts.py coverage --store STORE --agency SLUG --inventory INVENTORY.csv`
9. Review state: `python3 tools/flock_facts.py status --store STORE [--agency SLUG] [--list unreviewed|disputed]`; ids, never values.
10. Back up the store locally: `python3 tools/flock_backup.py create STORE --out FILE.tar`, then `verify` (docs/BACKUP.md "Flock store").

## Outputs
- `STORE/agencies/<slug>/FACTS.csv` (schema v2, one folder per agency slug).
- Counts and row numbers on stdout; never a value or quote.
- Coverage prints fact rows, pages and UNREADABLE rows per posted document; withheld documents are counted, never named.

## Never
- Put the store, a real FACTS.csv or any fact value in the repo, a PR, a log or a commit.
- Import a row that lacks a page, a 64-hex sha256 or a verbatim quote.
- Guess text from an unreadable page; use an `UNREADABLE` row.
- Edit a store file by hand, or change `fact_id`; the tool derives it.
- Run OCR, download records, or read or write the canonical ledger from this skill.

## Golden case
- `tests/fixtures/flock/facts-sample.csv` (synthetic):
  `python3 -B tools/flock_facts.py --check tests/fixtures/flock/facts-sample.csv` exits 0.
- Morro Bay (87 facts) is imported by the operator into the private store, counts only.

## Changelog
- 1.0: fact store schema v2, `import` and `--check` CLI.
- 1.1: `coverage`: cited facts per posted INVENTORY document; withheld counted, never named.
- 1.2: `status`: rows by kind, review status and UNREADABLE per agency; `--list` prints ids to review.
