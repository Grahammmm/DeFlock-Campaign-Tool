# Agency run guide: from evidence to a draft in the console

This guide is for an organizer starting the next agency after the Sheriff. It walks the
agency-run commands in order: what each step is for, the command, the file it writes, and what a
person must still decide. It is words only. Every command and option below was checked against
`records <command> --help` on main commit `35788fa9da571b79dc19dfb2a34a08734d0fc904`.

Examples use symbolic labels (`PRIVATE_ROOT`, `AGENCY_DIR`, `AGENCY_KEY`). Substitute your own
local paths **outside the checkout**. Use `python3 -m campaign_tool.records` for each command.

## Ground rules

- **Every output is a machine lead for a person to check.** A clean result is never a legal
  finding, a send approval or a publication approval.
- **Sending and publication are never automated here.** No step in this guide sends mail,
  publishes a page, deploys, queues a draft for approval, approves or signs. Sends and
  publication stay G-approved human actions, taken outside this guide.
- **Private inputs are never committed.** Keep the team list, the records root, the mail-store
  receipts, the alias and allow files, the quote ledger, the draft mapping and every output
  below in a private folder outside git. Do not paste their paths, names or contents into a pull
  request, an issue or a card.
- **`skipped` means the check did not run** (an input it needs was not given or not found). It is
  neither a pass nor a finding. **`unknown` means there was not enough data to tell.** It is not a
  pass and not a negative finding about the agency. A coverage gap is not an observed zero.
- Related terms stay distinct where printed: `FAIL` (the check ran and hit), `incomplete` (it ran
  but could not cover everything), `skipped` (not run), `not rated` or `unrated` (nobody has
  rated it) and "not run" (no receipt exists).

## Private folders

Make one private agency folder (`AGENCY_DIR`) for the agency. Steps 1, 2 and 3 refuse to write
into a folder that is not empty (and `agency-evidence`/`agency-pages` refuse one inside the
records root), so give each its own new output folder, then copy or move the named files into
`AGENCY_DIR` so that `agency-status` and the lints can find them by name.
`asks.tsv`, `FINDINGS.md`, `NEXT-RESPONSE.md` and `drafts/` are prepared by a person; no command
in this guide writes them.

## 1. Evidence index: `agency-evidence`

**Purpose:** list what records exist for the agency, split into originals and leads.
Read-only on the records root; it never fetches a link.

```
python3 -m campaign_tool.records agency-evidence --root PRIVATE_ROOT --out EVIDENCE_OUT ALIAS_FILE
```

**Writes:** `evidence-index.tsv` and `EVIDENCE-INDEX.md` in `EVIDENCE_OUT`.
**A person still judges:** that the alias file names the right agency, which entries are
originals versus leads, and every skipped or refused path.

## 2. Events: `agency-events`

**Purpose:** turn mail-store receipts into Pacific-dated agency events. Read-only; sends nothing.
It does not retrieve mail; give it receipts you already hold.

```
python3 -m campaign_tool.records agency-events --receipts RECEIPTS --agency-aliases ALIASES --out EVENTS_OUT
```

**Writes:** `events.tsv`, `manual-events.jsonl` and `unclassified.tsv` in `EVENTS_OUT`.
Check a written file with `--validate FILE` (add `--json` for one JSON object).
**A person still judges:** each event's classification and date against the source message, and
everything left in `unclassified.tsv`. An unclassified row is `unknown`, not a decision.

## 3. Pages: `agency-pages`

**Purpose:** one page-numbered text file per original, plus an inventory. It never runs OCR;
`--ocr-dir` only reads OCR text you already produced.

```
python3 -m campaign_tool.records agency-pages --index EVIDENCE_OUT/evidence-index.tsv --root PRIVATE_ROOT --out PAGES_OUT
```

**Writes:** `inventory.tsv` and a `pages/` folder of `*_pNNN.txt` files in `PAGES_OUT`.
**A person still judges:** coverage, blank or missing text, and any OCR text against the
original page image. Text that matches is not proof the image was read.

## 4. Policy matrix: `agency-policy-matrix`

**Purpose:** a skeleton of the law-package rules and the agency's stored policy sections.

```
python3 -m campaign_tool.records agency-policy-matrix --policies POLICIES --agency AGENCY_KEY --package PACKAGE --out POLICY-MATRIX.tsv
```

**Writes:** the new file named by `--out` (it must not already exist).
**A person still judges:** everything. The evidence, verdict and confidence columns are always
empty; a person fills them. "No stored policy" does not mean no policy exists, and a package that
verifies is not a legal conclusion.

## 5. Response sheet: `agency-response-sheet`

**Purpose:** a phone-sized Markdown sheet of the agency's machine-read responses.

```
python3 -m campaign_tool.records agency-response-sheet --project PROJECT --agency AGENCY_KEY --out RESPONSE-SHEET.md [--run-id RUN_ID]
```

**Writes:** the Markdown file named by `--out`.
**A person still judges:** the sheet is a machine reading, review required, not a legal finding.
Cross-check each rating and flag against the actual response. With no run data it says
`unknown: no run data`. Unrated is not a disposition, an exemption cited is not proof of
withholding, and "not addressed" means only "not found in the text that was read".

## 6. Cross-agency logs: `agency-cross-logs`

**Purpose:** count other agencies' produced logs that name this agency as the searcher.

```
python3 -m campaign_tool.records agency-cross-logs --index EVIDENCE_OUT/evidence-index.tsv --root PRIVATE_ROOT --aliases ALIASES [--ori ORI]
```

**Writes:** no file. It prints counts, a Pacific date range, case fill and coverage to stdout;
capture the output yourself if you want to keep it, in `AGENCY_DIR`.
**A person still judges:** attribution, rows that look duplicated, unresolved rows and files that
could not be read. PDF logs are listed as not parsed. A gap in coverage is `unknown`, never
evidence of no searches.

## 7. Status: `agency-status`

**Purpose:** show which stage files exist in `AGENCY_DIR`, how old they are, and the next missing
artifact. Read-only. It has no `--help`; the folder is its only argument.

```
python3 -m campaign_tool.records agency-status AGENCY_DIR
```

**Writes:** no file; prints to stdout. It looks for `EVIDENCE-INDEX.md`, `evidence-index.tsv`,
`events.tsv`, `inventory.tsv`, `pages/`, `FINDINGS.md`, `drafts/` and `NEXT-RESPONSE.md`, by
name. An empty folder reports each stage `missing` and `next: evidence index`.
**A person still judges:** whether a present file is correct and fresh. Present does not mean
reviewed, and nothing is validated.

## 8. Next agency: `agency-next`

**Purpose:** rank which agency to work next from a private agencies table. Read-only; "on our
records", not a legal finding.

```
python3 -m campaign_tool.records agency-next --agencies AGENCIES_TABLE [--today DATE] [--json]
```

**Writes:** no file; prints ranked suggestions to stdout.
**A person still judges:** the dates, the priorities and who acts next.

## 9. Lint: `agency-lint` and the receipt

**Purpose:** one mechanical check of the human-written letter body (`LETTER-BODY.txt`, see below)
against your private inputs. Nothing is sent. Hit text is never printed, only the place and a rule code.

```
python3 -m campaign_tool.records agency-lint --agency-dir AGENCY_DIR --package PACKAGE \
  --team-list TEAM_LIST --allow ALLOW_FILE --agencies AGENCIES_TABLE --agency AGENCY_KEY \
  --inventory PAGES_OUT/inventory.tsv --original-root PRIVATE_ROOT --quote-ledger QUOTE_LEDGER \
  --receipt LINT-RECEIPT.json LETTER-BODY.txt
```

**Lint the extracted body, not the structured Markdown.** The receipt binds the SHA-256 of
exactly the bytes `agency-lint` reads. Export (step 10) hashes only the letter body it extracts
from the structured Markdown, so linting the whole Markdown file always ends in
`LINT_RECEIPT_STALE`. Save the body as its own file, `LETTER-BODY.txt`, with exactly the bytes
export extracts: the lines of the first blockquote under the mapped heading, each with its
leading `> ` (or `>`) removed; without the `Subject:` line and the one blank line after it;
without trailing blank lines; and ending in exactly one newline. Export refuses a missing
heading (`section_unknown`), a missing blockquote (`letter_missing`) or a missing first
`Subject:` line (`subject_missing`).

Each lint runs in a fixed order and prints `name: result`. `agency-lint` is the one entry point;
the others can also be run alone:

1. **asks**: the draft's asks against what the agency already gave or answered. Needs
   `inventory.tsv`, `asks.tsv` and `events.tsv` in `AGENCY_DIR`. Alone:
   `agency-lint-asks --draft LETTER-BODY.txt --agency-dir AGENCY_DIR [--json]`.
2. **cites**: loose citations, unsourced facts, bare "overdue" wording. Needs `--package`. Alone:
   `agency-lint-cites --draft LETTER-BODY.txt --package PACKAGE [--json]`. A section number found in a
   package is not a human review of that package.
3. **quotes**: quoted words against the extracted page text in `AGENCY_DIR/pages`. Alone:
   `agency-lint-quotes --draft LETTER-BODY.txt --pages PAGES [--ledger QUOTE_LEDGER --inventory INVENTORY --original-root ROOT]`.
   This is a text check only; it does not look at page images. Rule Q3 runs only when you give
   the quote ledger, inventory and original root together, and then checks only that the
   ledger agrees. The ledger is a person's record of comparing a quote with the original page;
   no lint writes it. Message quotes it cannot match to pages make the result `incomplete`.
4. **out** (outgoing privacy and residue): our team's names or contacts not on the allow file, and
   internal working marks. Needs `--team-list` and `--allow`. Alone:
   `agency-lint-out --draft LETTER-BODY.txt --team-list TEAM_LIST --allow ALLOW_FILE`.
5. **precedent**: another agency named as a release precedent. Needs `--agencies` and `--agency`.
   Alone: `agency-lint-precedent --draft LETTER-BODY.txt --agencies AGENCIES_TABLE --agency AGENCY_KEY`.
   Notes (comparison or quoted) do not fail; only a refusal does.
6. **banned phrases**: the playbook's banned wording. No extra input; it always runs.

The summary line is `PASS` (exit 0), `FAIL` (exit 1) or `INCOMPLETE` (exit 3, something was
skipped or incomplete). A missing input gives `skipped: <reason>`; it never counts as a pass.
Give the quote options all three or none.

**The receipt.** With `--receipt` the command also writes a new local file (hashes and fixed
codes only, no letter text) binding the exact draft body, the lint rules and every input given.
It is an operator record, not a signature or an approval. A recorded `fail` stays a fail, and a
skipped lint stays skipped. If the draft body, the rules or any input changes afterward, the
receipt no longer matches: fix the cause and rerun `agency-lint` on the intended inputs. No
receipt at all is shown as "agency lints: not run".
**A person still judges:** every finding and every skip; whether the letter is true, fair and
fit to send. A clean lint approves nothing.

## 10. Draft export: `agency-draft-export`

**Purpose:** turn the lint-checked letter body plus your operator mapping into console review copies,
and prove each through the draft-import dry run. Nothing is applied, approved or sent.

```
python3 -m campaign_tool.records agency-draft-export --md DRAFT.md --mapping MAPPING.json \
  --project PROJECT --out EXPORT_OUT --lint-receipt LINT-RECEIPT.json \
  --agency-dir AGENCY_DIR --package PACKAGE --team-list TEAM_LIST --allow ALLOW_FILE \
  --agencies AGENCIES_TABLE --agency AGENCY_KEY --inventory PAGES_OUT/inventory.tsv \
  --original-root PRIVATE_ROOT --quote-ledger QUOTE_LEDGER
```

**Input shape.** `DRAFT.md` is the structured Markdown: a heading (one to three `#`) whose text
equals the mapping's `source_section` exactly, followed by a blockquote whose first line is
`Subject: ...`, then (optionally) one blank quoted line, then the body. Only the first
blockquote under the first matching heading is used. The mapping gives each draft its own
`source_section`, so each mapped draft has its own extracted body and needs its own
`LETTER-BODY.txt`, its own `agency-lint` run and its own receipt; one receipt cannot cover two
different bodies. Run export once per receipt with the mapping entry for that body.

**Writes:** one `DRAFT_KEY.json` per mapped draft in `EXPORT_OUT`, which must be new or empty and
outside the project. Pass the **same** lint options as step 9 (the receipt's inputs, not the draft path) so the
receipt is checked against the same inputs. If the body, rules or an input differ, export refuses with
`LINT_RECEIPT_STALE` (with the reason) or `LINT_RECEIPT_UNVERIFIABLE`; rerun step 9, do not
work around it. The receipt state is written at the front of each draft's reason line.
**A person still judges:** that the body, recipient and mapped evidence are exactly what you
meant. The mapping (ids, section, why, citations, recipient basis) is the operator's, not
the Markdown's. Exporting is not queuing.

## 11. Import dry run: `draft-import`

**Purpose:** confirm the console would accept one exported draft. Dry-run is the default.

```
python3 -m campaign_tool.records draft-import --project PROJECT --file EXPORT_OUT/DRAFT_KEY.json
```

**Writes:** no file and no queue entry. It prints `would_queue` or `existing`, or a refusal.
`would_queue` does **not** put a draft in the console. `--apply` exists but is a separate,
authorized, queue-only step outside this guide, described under
[Import one correspondence draft](RECORDS-ADMIN-PANEL.md#import-one-correspondence-draft-queue-only).
This guide gives no apply command. Even `--apply` never approves, signs, places or sends.
**A person still judges:** the dry-run result and any refusal, before anyone hands the file on.

## 12. Review in G's console

**Purpose:** a person reads the pending draft. After the separate queue-only handoff, open the
**Review** link on Home or `/review?id=DRAFT_ID`; see the
[review page](RECORDS-ADMIN-PANEL.md#review-page).
**Writes:** opening the page creates no file.
**A person still judges, and only G decides:** the whole letter, recipient, thread and evidence,
the caveats, the receipt state and time, and whether to edit or leave it pending. Copy approval
is not permission to send or publish. Sending and publication remain separate G-approved
human actions.

## What is never automated

Sending, publication, deployment, queue approval, signing, legal findings, verdicts, the
letter's wording, original-page image checks, OCR, and live mail retrieval. Nothing in this
guide does them, and a green lint, a valid receipt or a `would_queue` result does not stand in
for them.
