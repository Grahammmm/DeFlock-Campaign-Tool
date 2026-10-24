# Fable: DeFlock Campaign Tool audit and continuation handoff

**Start here.** Owner-requested public handoff, October 1, 2026, America/Los_Angeles. This package is an audit assignment and evidence map, NOT a completed audit or a launch certificate.

## Assignment

Independently audit the whole repository and its integration branches. Establish what actually works from email arrival to preserved bytes, extraction/OCR, catalog, detector coverage, review, event-date rule comparison, privacy, exact owner approval, website publication, correction and rollback. Then fix verified software defects through small tested PRs. Focus first on a bounded end-to-end infrastructure slice; do not attempt to clear the entire real-record backlog before the infrastructure works.

## Read in this order

1. [Audit instructions](AUDIT-INSTRUCTIONS.md): scope, method, deliverables and continuation rules.
2. [Current state](CURRENT-STATE.md) and [machine-readable GitHub snapshot](github-snapshot.json).
3. [Sanitized full build brief](BUILD-BRIEF.md): WP0-WP9, schemas, stages and acceptance requirements.
4. [Decisions and boundaries](DECISIONS-AND-BOUNDARIES.md): later owner directions and unresolved conflicts.
5. [Historical audit findings](HISTORICAL-AUDIT.md): September 30 baseline, not current failures.
6. [Evidence and acceptance matrix](ACCEPTANCE.md).
7. [Review and detector requirements](REVIEW-AND-DETECTORS.md).
8. [Private evidence request](PRIVATE-EVIDENCE.md): what cannot safely go in public Git.

Also read repository [AGENTS.md](../../AGENTS.md), [README](../../README.md), [ROADMAP](../ROADMAP.md), [CONTRACTS](../CONTRACTS.md), [DATA-HANDLING](../DATA-HANDLING.md), [SECURITY](../../SECURITY.md), [CONTRIBUTING](../../CONTRIBUTING.md), and every module-specific document relevant to the inspected code. Follow repository contributor constraints; this handoff does not override them.

## Code targets

- [Pinned main baseline](https://github.com/Grahammmm/DeFlock-Campaign-Tool/tree/936ea52118a8c07a5a320904891b8eb7c97fea9f)
- [Integration PR #36](https://github.com/Grahammmm/DeFlock-Campaign-Tool/pull/36), head `e3dcf11304205e7d70b4838b832a57128404c563`
- [Open pull requests](https://github.com/Grahammmm/DeFlock-Campaign-Tool/pulls)

Audit BOTH main and the combined integration work. #36 currently targets #34's branch, not main. Do not assume merging #36 alone lands the stack on main, or that its tree contains later main fixes. Record fresh heads and ancestry before starting. Keep the historical source snapshot above in the audit report.

## Success means evidence, not activity

Produce a findings-first report, a reconciled requirement-to-code matrix, reproducible synthetic acceptance receipts, a small-PR repair plan and a concrete deployment/rollback checklist. State exactly what cannot be tested without private host access. Public code review cannot certify corpus completion or production operation.

The progress-chat automation was explicitly deleted by the owner. Do not recreate it. It was never the mailbox-intake job.
