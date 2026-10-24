# Flock LIVE enrollment policy: options for G (decision D6)

Status: DRAFT, REVIEW REQUIRED. Written for the Fable audit item F-050 / decision D6.
This page grants no enrollment. It runs nothing, asks nothing yet and lifts no hold.
Any LIVE enrollment is a later, attended step that needs G's own word for it.
The existing operator brings these options to G only after independent review.

## What "enrollment" means here, in plain words

When the pipeline has read a document, it writes the result (the text, its page
count and its fingerprints) into the ledger. Putting that result into the ledger is
"enrollment". Enrollment is bookkeeping only: it does not mark any stage accepted,
does not clear any alert, and every extracted fact stays "review required"
(docs/RECORDS-EXTRACTION-LEDGER.md, section "Stage authority and runner result").

Today the only enrollment G has approved is D5-A: exactly 20 already-read TEST
results into the TEST ledger, TEST only, and a run needs G's word again
(G-DECISIONS.log line 407; lifted as a hold on line 397). The overnight authority
(line 468) also names only "D5-A exactly-20 TEST enrollment". Nothing on record
allows LIVE enrollment. Every option below that enrolls anything on LIVE therefore
needs a NEW G authority, and says so.

How many LIVE results are waiting is UNVERIFIED. The last cited scheduled run
(line 461, 2026-10-23 20:31 PDT) produced 0 new extraction outputs (extract 424 =
0 out, 23 failed, 401 skipped). The operator must measure the waiting count (counts
only, no record content) before asking. The 400 "preserve pending" items (line 469)
are NOT in scope here: they first need the resolution policy G chose on line 504
(strict linking for the 141, adopt documents among the 258) to be built, rehearsed
and applied through its own card.

## Rules every option keeps (no option bypasses these)

- D5-A first: the exactly-20 TEST enrollment has run and its result has been
  reviewed before any LIVE batch. Whether it has run yet: UNVERIFIED.
- One reviewed card per LIVE batch, with exact-head independent review of any tool
  it uses. The batch list is fixed in advance (IDs and counts only).
- Before each batch: a fresh data backup by the G-log 432-434 method, the reviewed
  timer gates (timer_gate pre-stop / pre-resume), and a scratch rehearsal on a copy
  that reports counts only.
- During and after: the operator is present (attended). The run report passes the
  F-003 counts check (tools/run_report_check.py). Note that check covers counts
  only, not rollback evidence.
- Automatic rollback if any rollback trigger fires: restore the pre-batch backup
  through the reviewed rollback tool (line 468 method). No original document is
  deleted.
- Still held under every option: outbound messages, publication, data deletion,
  credentials, force-push, off-box export, stage acceptance (including S5; an
  enrollment never supplies it), live alert clearing, the 400 preserve-pending
  items and any human or legal
  conclusion. Nothing here moves to the website (line 527: findings go to a human
  first).

## Options (ranked, recommendation first)

### 1. RECOMMENDED: Staged batches. Reply: STAGED

One G decision sets the rule. Each LIVE batch is a fixed list of at most 20 items
per run (the same size as D5-A). Each batch is attended and runs only after the
previous batch's receipts have been reviewed clean. The first batch also needs G's
go on its exact list when the operator reads it out. Later batches proceed under
this rule without asking again, one at a time, and stop at the first rollback.
- Pros: G decides once; a mistake affects at most 20 items, and rollback is small
  and quick to check; it matches how D5-A was shaped.
- Cons: slower if many items are waiting; each batch costs one attended session.
- Cap per run: 20 items, fixed list. No automatic continuation after a failure.
- Receipts per batch: backup hash receipt; the engine's enrollment results (import
  and run IDs, original/receipt hashes, "reused" flag, zero stage promotions);
  the run report and its F-003 check output; an aggregate counts note.
- Rollback: restore the pre-batch backup; the batch list is re-queued, not lost.
- Needs NEW G authority: yes (LIVE enrollment is not covered by lines 407 or 468).

### 2. One batch, then ask again. Reply: ONCE

Exactly one attended LIVE batch of at most 20 fixed items. Then stop and bring the
result to G before anything else is enrolled.
- Pros: the smallest possible first step; G sees a real result before deciding
  further.
- Cons: G has to decide again (D6 asked to decide once); the wait grows.
- Cap per run: 20 items, one run only.
- Receipts and rollback: as option 1.
- Needs NEW G authority: yes, for this one batch only.

### 3. Hold LIVE enrollment for now. Reply: HOLD

No LIVE enrollment until D5-A TEST has run and been reviewed, and the line-504
resolution work for the old items has been applied. Then the operator asks again.
- Pros: zero LIVE risk; uses no new authority.
- Cons: "tracking v1 live" (line 527) cannot finish while LIVE results stay
  unenrolled; work piles up.
- Cap per run: 0. Receipts: none needed. Rollback: nothing to roll back.
- Needs NEW G authority: no. This is the current state.

### 4. Everything eligible in one attended run. Reply: ALL

One attended run enrolls every LIVE item that is eligible at that moment, using the
engine's existing limits and no 20-item cap.
- Pros: fastest; one session.
- Cons: the largest blast radius; a fault touches every item at once, and rollback
  takes longer to verify. The count is UNVERIFIED, so G would be approving an
  unknown size. NOT recommended.
- Cap per run: no batch cap beyond the engine's existing limits.
- Receipts and rollback: as option 1, for the whole run.
- Needs NEW G authority: yes, and wider than options 1 and 2.

## When and how G is asked

D6 is marked "later" in the audit (00-AUDIT.md section 7). When the operator asks,
the message carries this page, the measured waiting count and an HH:MM PDT deadline.
D6 is not in the reversible class of G decision 4, so nothing proceeds after the
deadline: the deadline is only when the operator asks again. G's reply word picks
the rule. Even then, the first LIVE batch is a separate attended step with G's word
on its exact list.

## Sources (read-only)

DeFlock baseline commit 93995a1a615f6aaca7bfb0fbeb39a7f3aa367997, including
docs/RECORDS-EXTRACTION-LEDGER.md, docs/RECORDS-MAIL-ADOPTION.md and
docs/RECORDS-LEGACY-MAIL-RESOLUTION.md. G-DECISIONS.log lines 397, 407, 461, 468,
469 and 504 (lines 1-513 verified byte-identical to the round-2 source pin,
sha256 cefbeb7a...); line 527 was added after that pin and was read on 2026-10-24
at about 12:45 PDT (context only, not a pinned input). Fable OCR decisions
20-G-DECISIONS.md (sha256 e73fc784...).
Fable audit 00-AUDIT.md section 7 (sha256 cd3f608a...).
