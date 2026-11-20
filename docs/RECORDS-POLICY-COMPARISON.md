# Qualifying the policy comparison (custodian and G guide)

Words only: this guide runs nothing, installs nothing, sends nothing and publishes nothing.

Checked against `main` commit `342cc0b06f4f40510aa12b17a7b7c8e7bed6f67c`, which includes `records comparison-input` (DF1117L-09).
After later changes to the comparison code, re-walk every command, option, file and page below and update this line before relying on it.

## 1. What `comparison.json` is, and is not

`run-outputs/<run>/comparison.json` (with `comparison.md`) is written per run by the outputs stage
(`campaign_tool/records/outcomes/comparison.py`). It is:

- **documentary screening**: it compares cited documentary criteria, nothing more;
- **private and review required** (`review_required` is true, `legal_conclusion` is false, every row has `needs_attorney_review`);
- measured against the `us-ca` law package (`jurisdictions/us-ca/package.json`), whose status is **still draft**. Each row labels
  the package status and revision it was read against. That is package metadata, not legal acceptance.

It is **never** a legal conclusion, a finding that an agency complied or failed, or a publication-ready statement. A missing or
unreadable report is not an all-clear and not an empty success.

## 2. What `meets`, `gap` and `unknown` mean

| status | meaning |
| --- | --- |
| `meets` | the supplied, cited documentary predicates match the criterion. It does not mean the agency complies with the law. |
| `gap` | a cited documentary discrepancy. It is not a violation. |
| `unknown` | unresolved, incomplete, unqualified or conflicting evidence. Absent or redacted production is not proof that a control does not exist. |

The disclaimer printed on every report says the same; the code constant is `comparison.DISCLAIMER`.

## 3. Why live rows are `unknown` until a reviewed input is installed

The report reads one optional custodian file, `PROJECT/agency-fact-sheets.json` (schema `records-agency-fact-sheets-v1`). Without it
the report carries the agency and request ids from the ledger and **no evidence**, so every row is `unknown` and `input_status` is
`fact_sheets_missing`. Even after install, a row stays `unknown` unless every predicate it needs is present, cited, attested and
applicable. Nothing in the ledger (timestamps, statuses) proves receipt, a determination or which law applies.

Two similarly named files are different. `agency-fact-sheets.json` (with a hyphen) is the custodian's **input** to the comparison.
`agency-factsheets.json` (no hyphen) is the per-run **source-excerpt report** from `campaign_tool/records/outcomes/factsheets.py`
that `/facts` shows. Neither is derived from the other.

## 4. The three steps

All work is private. Keep the worksheet and compiled file **outside** the checkout, the fact store and the project: both commands
refuse those locations and refuse to overwrite an existing output file. Use a synthetic or authorized TEST setting to qualify the
workflow before any real data.

### Step 1: make the worksheet

```sh
records comparison-worksheet --comparison run-outputs/<run>/comparison.json --facts-store DIR --out FILE
```

All three options are required. The command reads the run's `comparison.json`, the sibling `CURRENT-REQUEST-REGISTER.json` of the
same run and the reviewed fact store, and writes a new `records-comparison-worksheet-v1` file (mode 0600). It lists:

- every agency and request in the report, with one blank entry per criterion, every key unset and nothing attested;
- **checked** fact-store facts as *candidates* only (quote, page, effective dates, a short source hash prefix). Disputed and
  unreviewed facts are counted, never listed;
- request timing as a **proposal** from the register only: a recorded receipt or a submission date, never a confirmation. A
  submission date is not a recorded receipt.

It prints counts only. An unfilled worksheet compiles to all `unknown`.

### Step 2: the custodian fills it

Only the custodian may set a predicate, a role, applicability or an attestation. No tool and no model fills these.

- Set `custodian_attested` and `applicability_confirmed` only when the cited record supports the **exact** predicate for the exact
  agency, role and scope, time, operative policy and version, and data layer. Record `reviewer_role` and `review_date` for the sheet.
- **A citation must show it.** Each citation names a `fact_id` that is *checked* in the store and has a page. The compiler takes the
  page from the store row, not from the worksheet, and refuses a mismatch.
- Predicates are exact booleans. **`false` means an affirmative cited discrepancy**: the cited record says the opposite of the
  criterion. **It never means "not found" or "not produced".** For absent, redacted or unresolved evidence, leave the value unset
  (or `state` missing, redacted or unresolved) so the row stays `unknown`.
- Policy and practice are separate rows. A documented policy never establishes practice, and a configured relationship is not a disclosure.
- Timing: the custodian confirms receipt, the calendar (with applicable holidays) and any extension notice. A proposed date is not a
  confirmation. Dates are agency-local civil dates.

### Step 3: compile it

```sh
records comparison-input --worksheet FILE --facts-store DIR --comparison run-outputs/<run>/comparison.json --out FILE
```

All four options are required. The compiler emits a field only when it is attested, applicability-confirmed, cited by a fact that is
checked in the store **at compile time**, and has every required key. Otherwise the field is omitted and stays `unknown`. It never
derives a value, and writes only a new `--out` file in `records-agency-fact-sheets-v1` form. It prints one `agency=… emitted=…
incomplete=…` line per agency. A refusal names a code and a position (agency, field, key), never a value or a path.

### Then: the attended install (custodian only)

Qualify the compiled file in the TEST setting first. The custodian then installs it as `PROJECT/agency-fact-sheets.json` as a
separate, attended act. This guide does not do it and no command above does it. Nothing here starts the live pipeline. The next
report written by a run reads the file; an existing `comparison.json` does not change.

## 5. Reading the result

`GET /comparison` (nav label "Policy comparison", read-only):

1. The disclaimer, then the **Input** line. "No reviewed comparison input is installed; every row is unknown" means step 3's install
   has not happened for this run. "Fact sheets were supplied" does not mean they were reviewed or approved.
2. The per-agency counts of `meets`, `gap` and `unknown`, and the "Missing inventory tables" line (an incomplete inventory is not complete coverage).
3. Filters in the query string: `?agency=<agency id>` and `?status=meets|gap|unknown`. A malformed, repeated or unknown filter is refused.
4. Each row shows the expected criterion, the reason code in plain words, the observed values and the evidence locators.
   Resolve every `gap` and `unknown` by review of the cited page, not by this page.

`GET /facts` (nav label "Agency fact sheets", read-only), with `?agency=<agency id>` for one agency: the per-run source-excerpt report
(`agency-factsheets.json`). It shows page-cited excerpts marked "REVIEW REQUIRED". It is **not** the compiled input and not a legal
assessment. `NOT FOUND` means no supported page-cited statement was found; it does not mean the conduct is absent.
If either page says "unavailable", that is not an all-clear.

## 6. What stays human

- Attorney review of every row, before any use.
- The custodian's attestations, applicability, receipt, calendar and extension confirmations, and the install in step 3.
- Publication of anything derived from this work, and any send. Both stay G-approved, behind the redaction and review gates.
