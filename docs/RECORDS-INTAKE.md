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
immutable blobs and parser lineage labels are retained. Corrected DOCX locators
are revision-bound as described below; old extraction attempts remain history. Existing
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

## PR10 review corrections

The engine revision is `portable-intake-2`; the preserved source parser lineage
remains `flock-intake-3.2`. Each new digest binds a parser-affecting policy hash.
Archive/mail exclusion changes invalidate only those container digests, not all
finished child text or unrelated files. Missing pre-port policy hashes require
one refreshed container/DOCX extraction; unchanged legacy non-container text is
retained. Agency routing and host/output paths do not invalidate extraction.
DOCX source ordinals count every paragraph, including empty and table paragraphs,
and restart within each XML part. This remains XML text extraction, not rendering.

An extraction reconciles current child edges against a completed child inventory.
Exclusions are disclosed as partial extraction, but exclusion-only results can
establish a complete child inventory for the remaining scope. Other partial or
failed enumeration never proves an unseen child absent. Explicitly excluded old
links are retired even if another member fails. Each retired edge goes to
`edge_history`, with reason and prior parser version; blobs, receipts, extracted
child units and prior attempts remain preserved. Shared children stay active
through another current parent or a directly inventoried original. An inactive
child's retained units are history and are not counted in active coverage.

Configured exclusions match paths relative to an input root or member names,
never a host directory above that root. Both administrative helpers now accept
`--config` and retain agency routing during reports and config during extraction;
the legacy explicit `--output` invocation still works with unassigned routing.

```sh
python3 -m campaign_tool.records.intake.repair_intake --config "$RECORDS_CONFIG"
python3 -m campaign_tool.records.intake.scope_quarantine --config "$RECORDS_CONFIG" "$EXACT_PRIVATE_SOURCE_PATH"
```

The new regression suite uses temporary synthetic originals only and does not
promote extraction to independent review, run a live corpus, or enable a service.
