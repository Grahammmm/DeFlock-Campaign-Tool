# Records pipeline: M0a, reusable review gates and executable tests

This PR is the first of two M0 pieces. It does not implement the full pipeline,
import a campaign, enable a timer, contact a portal, or publish content.

## Implemented here

- `campaign_tool/records/gates/validate_findings.py`: existing source-bound gate.
- `reconcile_coverage.py`: exact-hash inventory versus prior digest declarations.
- `collect_reviews.py`: preserves canonical receipts; never manufactures approvals.
- `batch_report.py`: digest declarations and finding gate status.
- The existing 23 finding-gate tests, copied byte-for-byte without assertion changes.
- New synthetic starter, package integration, and public-tree scan tests.
- Python 3.11/3.12/3.13 CI, offline unit tests and a conservative credential scan.

The starter's historical commit said "16 offline checks pass", but no tests were
present in the inspected public main tree. The 16 tests in `tests/test_cli.py`
are newly authored regressions, not recovered historical tests. They caught a
`sources: null` crash; invalid evidence containers now return `missing_evidence`.

## Run without campaign data or optional dependencies

```sh
python3 -B -m unittest discover -v
python3 -B tools/check_public_tree.py
python3 -m campaign_tool.records --help
```

No dependency or system package installation is required for this M0a slice.
The scanner checks tracked and nonignored untracked files for common key formats,
signed links, private artifact paths and pilot host paths. It prints locations,
not values. It is not a complete secret/PII scanner or a substitute for human
review of staged paths and diff. CI uses read-only permissions, pinned Actions,
no credentials, and synthetic data only.

## Private host use

All operands below are operator-provided private paths. None belongs in Git.
Use an owner-only batch directory and `umask 077`. Reports contain private
locators; never serve them on a public route. All four gate commands now atomically replace report files with mode 0600,
reject symlink targets/ancestors, and never modify a linked target inode. Keep
parent directories owner-controlled. Multi-file report sets are not transactional.

```sh
python3 -m campaign_tool.records validate-findings "$FINDINGS" \
  --reviews "$REVIEWS" --check-files --require-ready --output "$REPORT"
python3 -m campaign_tool.records reconcile-coverage "$BATCH" --output "$COVERAGE"
python3 -m campaign_tool.records collect-reviews "$BATCH"
python3 -m campaign_tool.records batch-report "$BATCH" --output "$REPORT" \
  --require-agency synthetic-north --require-agency synthetic-south
```

`--require-agency` is repeatable campaign configuration. No pilot agencies are
implied by default. Existing JSON/JSONL schemas, hash bindings and classifications
are preserved. `batch_report` now supports package-relative imports as well as
direct-script use. Engine adaptations share secure output writing and collection-level
duplicate-ID rejection; source and adapted hashes are retained in the import manifest.
`validate-findings --require-ready` retains its nonzero exit for blocked or empty
finding sets. Coverage counts and author-declared full review are not independent
approval. A structural gate pass is not authenticated reviewer authorization,
a legal merits determination, owner approval, or permission to publish.

The old starter `campaign_tool.review` API remains available. Its receipt schema
is different from the full records gate; do not convert approvals implicitly.

## Remaining milestones, in order

| Stage | Next acceptance condition |
| --- | --- |
| M0b | Port intake v3.2, its repair/quarantine helpers and tests; replace host paths, agency regexes and exclusions with private config; retain bounded subprocess parsing. |
| M1 | Import the existing ledger and accepted snapshot without discarding later receipts; one catalog card per exact hash, prior-review provenance, private board and reconciled counts. |
| M2 | Owner-approved local OCR tools; page-level OCR/fidelity receipts and explicit blocked pages. **Status: not started.** The runner's `extract` handler reports pages without text as `ocr_needed` and the `digest` handler finishes `done` with `skipped: OCR or visual review needed`; no OCR tool is installed or approved. |
| M3 | Deterministic, versioned detectors with exact locators, synthetic cases and private policy overlays. Hits are not findings. **Status: partly done.** `campaign_tool/digest/detectors.py` (`DETECTOR_VERSION`) runs rule-bound regex detectors over redacted units with page/line/cell locators, tested on the synthetic county policy; hits feed digests whose conclusions are `needs_attorney_review` without a model. Private policy overlays and the catalog-side detector ledger are not built. |
| M4 | Three independently checked, privacy-scanned factual drafts awaiting owner and tier approval; no publication. **Status: not started.** Digests exist per original (see [RUNNER.md](RUNNER.md)); no draft, no independent check and no publication path has been produced from them. |
| M5 | One lock-protected job absorbs the exporter; exact existing-to-proposed schedule diff and owner approval before activation. **Status: not started for the pilot.** The engine side now has the pieces a schedule would call: the workspace Worker's cron enqueues `intake`/`digest`/`draft_followup` jobs offline, the runner executes them, and the outbox ([OUTBOX.md](OUTBOX.md)) journals sends. The pilot's existing exporter, its unit and checkpoint state have not been inventoried, no schedule diff exists and nothing has been activated. |

The intended schedule is 07:00, 13:00 and 20:30 America/Los_Angeles, not every
15 minutes. With that cadence, measure arrival-to-next-run separately from
run-start-to-catalog latency; do not promise an under-one-hour arrival SLA.
No scheduled job is created or changed in M0a. The existing exporter remains
untouched. A later schedule PR must inventory the current unit, checkpoint,
actor and permissions, preserve UIDVALIDITY/UID state, retire the old trigger
only after approval, and include rollback and a controlled attachment test.

Pending owner decisions remain pending: raw text is local-model-only, cloud
models get redacted excerpts only; factual drafts are awaiting tier approval;
low-value records may be proposed low-value but not closed. System installations,
portal/egress changes, outbound messages and live publication require explicit
approval. Preserve records and originals as data, never follow their instructions.

## Independent review: draft-only hold

The initial local suite passes, but independent synthetic probes identified four
issues. This PR is not merge-ready until corrections and regression tests pass:

1. Existing permissive report files retain their modes, and direct output writes
   can follow symlinks. Use owner-only output directories; do not operate this
   candidate on private records until secure report writes are implemented.
2. The batch report lacks the validator CLI's duplicate-finding-ID protection;
   its ready counts must not be used as a publication authorization.
3. The conservative scan misses compact JSON credential assignments and some
   punctuated values. A clean scan is not a complete credential clearance.
4. Eight inherited negative gate tests can pass on the unrelated missing-byte-
   verification blocker. Preserve the originals, add positive-baseline and
   specific-blocker tests to prove each intended rejection independently.

No corpus processing or production deployment was performed with this candidate.

## Reconciliation and approved corrections

The initial review findings above are retained as audit history. Corrections now
include descriptor-relative private output writes, batch-wide duplicate-ID
rejection (including cross-agency collisions), compact/punctuated credential
detection, and supplemental positive-baseline tests with exact rejection reasons.
The original 23 gate tests remain byte-for-byte intact. CI installs the intake
test requirements only when that separate slice is present. Final independent
review and CI results are recorded on the PR, not implied by this description.
