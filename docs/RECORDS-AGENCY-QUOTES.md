# Agency quote ledger (REC-LINT-QUOTE-1 rule 3, quote rule Q3)

`records agency-lint-quotes` rules Q1/Q2 compare a quote with our own extracted page text. For an OCR page that
is the same text the quote came from, so they cannot show the quote is in the original. Q3 checks that a
recorded check of the original page exists and is consistent. **It only checks the ledger. It never performs or
claims a visual check, never writes the ledger, and prints places and rule names only, never quoted text.**
Whoever checks the original page records the result by hand; sends and publication stay G-approved.

## quote-verify.tsv

UTF-8, tab separated, exact header, one row per quoted passage of three or more words that has a PDF pinpoint:

```text
quote_id	doc_id	page	original_sha256	method	render_sha256	verdict	checked_by	checked_at
```

| Column | Meaning |
| --- | --- |
| `quote_id` | `LINE:COL:SHA256` of the opening quote mark's place in the draft and the SHA-256 of the exact UTF-8 text between the marks. Editing the quote or moving it invalidates the row. |
| `doc_id`, `page` | Must equal the draft pinpoint `(D1 p.3)`; `page` is 1-based and within the inventory page count. |
| `original_sha256` | Lowercase hex; must equal the `sha256` in the agency-pages `inventory.tsv` **and** the digest of the original file under `--original-root`. |
| `method` | `native-reextract`, `visual-render` or `none`. |
| `render_sha256` | Declared digest of the rendered page image (lowercase hex), or empty. A declared value is not proof that anyone looked at it. |
| `verdict` | `VERIFIED-ORIGINAL`, `UNVERIFIED-OCR-ONLY` or `UNVERIFIED-UNREADABLE`. |
| `checked_by` | A role token such as `records-reviewer`, never a person's name. |
| `checked_at` | ISO 8601 timestamp with a time zone, e.g. `2026-11-19T10:00:00-08:00`. |

`VERIFIED-ORIGINAL` needs `native-reextract` on a page whose inventory `text_layer` is `native` and which is not
listed under `no text layer:` in `notes`, or `visual-render` with a `render_sha256`. OCR pages, `native+ocr`
documents (treated as OCR, because the inventory cannot say which pages are text-bearing), `none` documents and
no-text pages need `visual-render` plus `render_sha256`. `none` is never verified.

## Draft claim

`[VERIFIED-ORIGINAL]` written directly after the attribution's closing parenthesis (spaces or tabs allowed), for
example `"..." (D1 p.3) [VERIFIED-ORIGINAL]`, must rest on a `VERIFIED-ORIGINAL` row. Nothing else in the prose is
read as a claim.

## Commands (synthetic names)

```text
records agency-lint-quotes --draft draft.md --pages out/pages --ledger quote-verify.tsv \
  --inventory out/inventory.tsv --original-root /path/to/original-root
records agency-lint draft.md --agency-dir out --quote-ledger quote-verify.tsv \
  --inventory out/inventory.tsv --original-root /path/to/original-root
```

The three options go together; without them only Q1/Q2 run (and `records agency-lint` reports Q3 as not run).
`--original-root` is the root given to `records agency-pages`. Output is `LINE:COL Q3 REASON` with one reason per
failing quote, first applicable of `source_unsupported` (message or missing pinpoint), `ledger_missing`,
`pinpoint_mismatch`, `original_mismatch`, `method_invalid`, `claim_unverified`. Exit 1 on hits, 2 for a refused
or malformed input.

## Lint receipt (DF2222-11)

`records agency-lint DRAFT --agency-dir DIR ... --receipt OUT` also writes a new 0600 `records-agency-lint-receipt-v1` JSON: the
SHA-256 of the exact draft bytes (pass the extracted letter body, not the markdown), a configuration fingerprint (each
lint name with its module `RULES_VERSION`, plus the `--agency` option), one SHA-256 or `not given` per fixed input role
(`team_list`, `allow`, `agencies`, `quote_ledger`, `inventory`, `ask_inventory`, `asks`, `events`, `package`, `pages`,
`originals`, `playbook`), each lint result (`pass`, `fail` with a hit count, or `skipped` with a fixed reason; a text-only
quote run is `skipped q3_not_run`) and `created_at`. Never a path, name or matched text. Bump a lint module's `RULES_VERSION`
whenever its rules change. In receipt mode an incomplete run exits 3; an operational error, an existing OUT, or an input
that changes during the run writes nothing and exits 2. Inputs must stay stable while the lints run.

`records agency-draft-export --lint-receipt FILE` takes the same input options, recomputes the body hash, the configuration
fingerprint and every role hash from the current files, and refuses with `LINT_RECEIPT_STALE` (reason `body_changed`,
`config_changed`, `input_changed:ROLE`, `input_added:ROLE` or `input_missing:ROLE`) or `LINT_RECEIPT_UNVERIFIABLE` (unreadable
input, unknown schema/lint/role, malformed hash). A verified receipt puts `agency lints: passed|failed|incomplete (at export;
checked TIME; ...)` in front of the draft's `why`, so `/review` shows it (no schema change); without a receipt the draft says
`agency lints: not run`. A receipt is a mechanical record, not an approval or a signature.
