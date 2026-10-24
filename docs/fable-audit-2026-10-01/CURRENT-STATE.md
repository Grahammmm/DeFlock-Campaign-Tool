# Current state and reconciliation targets

Date: October 1, 2026 (Pacific). This is a GitHub metadata observation plus clearly labelled historical private test evidence. It is not a fresh production audit.

## Public repository

Main baseline: [936ea52118a8c07a5a320904891b8eb7c97fea9f](https://github.com/Grahammmm/DeFlock-Campaign-Tool/tree/936ea52118a8c07a5a320904891b8eb7c97fea9f). [Machine-readable snapshot](github-snapshot.json) records the PR heads/bases/statuses observed for this handoff.

PRs #19 through #29 are now merged. They cover links, release identity, ledger, extraction, portal handling, queue, review, detectors, runner, recovery/publication and catalog. Previous chat reports describing all of them as unmerged are stale. Merged does not mean installed or activated.

PRs #30, #31, #32 and #37 (shared contracts, discovery, site content and law package) are merged. PR #38 (per-page OCR) is also merged. Therefore older statements that no detector/OCR code exists in main must be rechecked rather than repeated.

## Open integration stack at this snapshot

| PR | Head branch | Base branch | Intended scope |
| --- | --- | --- | --- |
| [33](https://github.com/Grahammmm/DeFlock-Campaign-Tool/pull/33) | codex/workers-wizard-workspace | main | Hosted setup and Workers |
| [34](https://github.com/Grahammmm/DeFlock-Campaign-Tool/pull/34) | codex/inbox-runner-outbox-digest | codex/workers-wizard-workspace | Runner, digest, outbox |
| [35](https://github.com/Grahammmm/DeFlock-Campaign-Tool/pull/35) | codex/approvals-publish-brevo-backup | codex/workers-wizard-workspace | Review, publishing, integrations, backup |
| [36](https://github.com/Grahammmm/DeFlock-Campaign-Tool/pull/36) | codex/integration | codex/inbox-runner-outbox-digest | Combined integration and launch documentation |

PR #14 (Reel generation) is separate and optional, not a core pipeline prerequisite.

The last inspected #36 check listing showed 10 completed successes at e3dcf11304205e7d70b4838b832a57128404c563. This is observed CI metadata, not a rerun by this handoff. Its body claims broader suite counts; reproduce them before treating them as audit evidence. The body says merging it brings everything to main, but its base is not main. Verify ancestry, latest-main inclusion (including #38), conflicts and a safe owner-merge plan. Do not merge all branches blindly or drop unique changes.

Read [LAUNCH.md at the inspected integration revision](https://github.com/Grahammmm/DeFlock-Campaign-Tool/blob/e3dcf11304205e7d70b4838b832a57128404c563/LAUNCH.md). It says no live deployment from this engine was demonstrated at its stated cutoff. Its no-OCR text may conflict with later main #38. Its no-independent-review statement conflicts with the records review contract; do not silently discard independent review. Its 400/18/90 test claims and live gaps are branch-specific historical claims, not current acceptance here.

## Private candidate tests: historical, NOT current main findings

A separate automatic-preparation candidate produced: 2 preserved synthetic originals, 1 accepted extraction, 0 catalog cards; detection/review/comparison/privacy remained incomplete. Two focused tests reported 1 pass and 1 failure. Its catalog producer supplied ten support fields where the installed WP5 schema accepted five; it also confused a raw extraction receipt with accepted-stage provenance. Proposed remedy was a correctly versioned five-field input with provenance derived by WP5, preserving the failed card.

A separate native-observation decoder candidate passed 10 author checks; 12 independent checks reported 7 passes, 3 failures and 2 errors, no skips. Reported defects: malformed Unicode/oversized locator exceptions outside the promised boundary; unbounded parser/provenance metadata; whitespace/case variants bypassing an unverified-parser label check.

These candidates were not published as part of those tests. Their source may be absent, superseded or already repaired in newer branches. Reproduce on pinned current code before filing or applying a fix. Request the private exact-hash evidence via PRIVATE-EVIDENCE.md; never copy a stale candidate over current main.

## Runtime and corpus limits

Latest mailbox cutoff actually verified by this chat: September 30, 2026, 08:45:39 Pacific (15:45:39 UTC). Six enumerated folders, 118 messages and 60 attachment occurrences were in that old successful snapshot. Later arrivals are unmeasured here. Neither an outage nor current timer status follows from an old snapshot.

Historical inventory: 1,647 candidate originals, not a current accepted end-to-end count. No new corpus processing or deployment verification occurred while preparing this pack. The current host launcher, active image/parser dependencies, deployed engine revision, protected board, schedule, live signup and publication behavior need authorized operational evidence.

The owner deleted the progress-chat automation. That deletion did not change the mailbox job and is not proof the mailbox job is enabled, disabled or healthy.
