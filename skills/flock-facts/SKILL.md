---
name: flock-facts
description: Use when importing or checking per-agency Flock public-record facts. Loads an Oct 27 style FACTS.csv into the private store and fails closed on a row without page, sha256 or quote.
---

# Flock facts

Keeps one private CSV of cited facts per agency, in the operator's private agency-facts
store (outside the repo). No model step: the tool validates, it does not read documents.

## Inputs
- A v1 FACTS.csv: `agency,request,fact_type,value,source_sha256,page,verbatim_quote`
  (UTF-8, CRLF accepted), or a v2 file or store directory. Columns: `docs/FLOCK-FACTS.md`.
- The operator's private store path, and the importer's name (`--added-by`).

## Check
- Check the source first: `python3 tools/flock_facts.py --check SRC.csv`.
- Fix each reported `row N: <field> <reason>` at the source. A page that cannot be read
  becomes an `UNREADABLE` row with page and sha256, for the extraction owner.
- Verify the store: `python3 tools/flock_facts.py --check STORE`; exit 0 only. It also names a `policy` fact not citing its agency's POLICY.csv `doc_sha256` and a `law` fact not citing a LAW.csv `text_sha256`.
- Treat a clean check as machine-valid and review-required, not accepted.
- Whole-store check, read-only: `python3 tools/flock_store_check.py STORE [--backups DIR --max-backup-age-days N]`; exit 0 only (docs/FLOCK-FACTS.md "Store check").

## Import
- Import: `python3 tools/flock_facts.py import SRC.csv --store STORE --added-by NAME`.
- Expect `imported=I duplicates=D errors=0`. Any error leaves the store untouched.
- Re-import is safe: duplicates are skipped by the dedupe key.

## Coverage
- `python3 tools/flock_facts.py coverage --store STORE --agency SLUG --inventory INVENTORY.csv`

## Status
- Review state: `python3 tools/flock_facts.py status --store STORE [--agency SLUG] [--list unreviewed|disputed]`; ids, never values.

## Quote-check
- Quotes on their page: `python3 tools/flock_facts.py quote-check --store STORE --text EXPORT [--agency SLUG]` (EXPORT: the extraction owner's page-text copy outside repo and store); ids and outcomes, never text; exit 3 on not-found or page-hash-mismatch.

## Stale
- Stale facts: `python3 tools/flock_facts.py stale --store STORE --date YYYY-MM-DD [--agency SLUG] [--findings FINDINGS.csv]`; expired, superseded, disputed or not-yet fact ids and the findings citing them; exit 3 when a finding cites one.

## Back up
- Back up the store locally: `python3 tools/flock_backup.py create STORE --out FILE.tar`, then `verify` (docs/BACKUP.md "Flock store").

## Review
- A second person reviews; use review, not hand edits: `python3 tools/flock_facts.py review --store STORE --agency SLUG --ids ID[,ID...] --set checked|disputed --reviewer NAME [--date YYYY-MM-DD] [--note TEXT]`; refused when NAME is the fact's `added_by`; each change goes to FACT-REVIEWS.csv; ids/counts only.

## Unreadable
- Unreadable pages: `python3 tools/flock_facts.py unreadable --store STORE --out FILE.csv [--agency SLUG]` writes a private (agency, sha256, page) list, outside the repo and STORE/agencies; hand the file to the extraction owner; do not re-OCR here.

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
