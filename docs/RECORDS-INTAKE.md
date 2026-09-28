# M0b: portable intake v3.2

This is the second M0 code-port slice, independent of the gate PR. It brings the
existing intake, preservation, extraction, quarantine and repair implementation
into the public engine. It does not activate a live campaign or scan a corpus.

## Private configuration, never committed

Create the configuration outside the engine checkout. Paths, agency expressions
and exclusions are operator configuration, not instructions derived from records.
Use private output and config directories. No default campaign roots or agencies
are embedded in the engine. A relative configured path is resolved as documented
by `campaign_tool.records.config`; explicit CLI paths are supported as before.

The JSON object has four supported keys: `roots` (array of source directories),
`output` (private intake directory), `agency_patterns` (agency identifier to regex),
and `excluded_path_fragments` (array of excluded relative path fragments).
An absent optional agency overlay leaves documents unassigned. Hints are not
verified custodians or authenticity findings. Unknown config keys are rejected.

```sh
python3 -m campaign_tool.records.intake.folder inventory --config "$RECORDS_CONFIG"
python3 -m campaign_tool.records.intake.folder extract --config "$RECORDS_CONFIG"
python3 -m campaign_tool.records.intake.folder report --config "$RECORDS_CONFIG"
```

`run` here means **filesystem inventory plus extraction**, matching legacy v3.2.
It is not the future scheduled mailbox-to-board `campaign_tool.records run`.
It does not contact IMAP or portals, run OCR/models/detectors, publish, or send mail.
`inventory`/`run` require explicit input roots; all commands require private output.
Root/output configuration is validated before creating intake output.

Parser subprocesses retain the original timeout, memory and archive limits.
The existing isolated interpreter or private parser bundle can be supplied with
`PYTHONPATH`; no dependency is installed automatically. Missing parsers remain
visible extraction failures rather than successful processing. Local OCR is M2
and requires separate installation approval. No original macros, formulas,
embedded binaries or external links are executed.

## Existing data and prior work

The `docs`, `occurrences`, `units`, preservation, history and extraction-attempt
schemas are retained. Content hashes, original occurrence identity construction,
immutable blobs, unit locators and parser version labels are retained. Existing
ledgers are not automatically migrated, copied or reprocessed by installing code.
Do not point this candidate at a canonical ledger until the M1 import/backup and
reconciliation step. Prior digests and reviews will be imported separately; an
extraction stage of `complete` is not a completed source or legal review.

Repair and scope-quarantine remain explicit administrative commands on private
outputs, not automatic intake stages. They can change derived material and must
not be run against the live corpus merely to test installation. Use a private
copy and keep receipts. Scope quarantine moves derived copies, not originals.

## Tests and baseline truth

The unchanged host baseline suite ran **20 tests: 18 passed and 2 failed** before
this port. Both failing tests expected DOCX to be unsupported, although the current
v3.2 source already contains a DOCX XML-text handler. The imported tests change
only their module path, that obsolete expected extraction status, and one test
name. The source tests are not edited. The import manifest records exact source
and destination hashes and the declared adaptations.

Supplemental tests cover explicit-root/output safety, private agency routing,
config rejection, archive worker exclusions, report-only behavior and DOCX
part/paragraph locators. A synthetic empty XML document can yield zero paragraphs;
that extraction status is not evidence that its layout or visual content was
reviewed. Embedded media yields a partial disposition for later visual review.

```sh
python3 -B -m unittest discover -v
```

Tests need the existing pinned pypdf/openpyxl parser environment. CI installs
`requirements-records-test.txt` in disposable GitHub runners only, never on the
campaign host. CI also uses [detect-secrets v1.5.0](https://github.com/Yelp/detect-secrets/blob/v1.5.0/README.md)
with credential network verification disabled, and fails on reported candidates.
Secret scanning is heuristic; manual path/content review remains required.

## Subsequent milestones

M0 is not accepted until both gate and intake PRs meet their review checks.
Then M1 imports the ledger and accepted snapshot into a catalog/private board,
M2 adds approved local OCR, M3 adds deterministic detectors, M4 prepares three
reviewed factual drafts, and M5 integrates mail/export ownership and the schedule.
Keep the pending tier/model/low-value decisions unchanged. No timer, host package,
portal permission, outbound send or website publication is authorized by this PR.
