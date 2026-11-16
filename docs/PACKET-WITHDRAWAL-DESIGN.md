# Packet withdrawal design (FLK-CON-2)

**Status: proposed design; not implemented; no send, publication or deploy authority.**
Merging this document registers no approval kind, installs no key, changes no
switch and withdraws nothing. Every function, action, schema and scope named
below as *proposed* does not exist; a later build needs its own authorization.
`docs/TRUST-ANCHOR-PLAN-A.md` says only "Withdrawal needs a separate design";
this is that design.

Baseline checked: `origin/main` at `eb80a554` (merge of #1649, CONDRAFT-02).

## 0. Baseline facts (verified on that main)

| Fact | Where |
| --- | --- |
| Packet states `proposed`, `approved`, `rejected`, `cancelled`, `written`; `expired` is derived only for `proposed`/`approved` and uses `age > EXPIRY_SECONDS` (14 days). | `campaign_tool/records/public_packets.py` `fold`, `_expired`, `_view` |
| `decide` (reject/cancel) refuses anything but `proposed`/`approved` (`packet_not_open`): a written packet cannot be cancelled. | `public_packets.decide` |
| `write_change` re-verifies the signature, needs switch ON, and writes `<id>-r<rev>.json` plus a `written` event with `file_sha256`. A second call on a written row returns `existing`; it is not a fresh approval. Its `target` field is advisory. | `public_packets.write_change` |
| Approval authority is `approval.payload(scope, subject, content, round_)` + `approval.verify`, against the protected `TRUSTED_SIGNERS`; `TrustUnavailable` means retry, never consume. | `campaign_tool/approval.py` |
| Export binds written files by hash, validates structure only (structure is not authentication), and has no remove operation. | `packet_changes.py` `export_changes`, `public_change`, `read_export` |
| Site staging requires a clean, separate site checkout, writes `content/updates/<packet>.json` (it ignores the change's `target`), refuses a lower or conflicting revision, never deletes, then runs the site's `npm run check`/`build`/`scan:secrets`. It opens no PR. | `tools/stage_packet_changes.py` |
| Console packet actions are only `packet_approve` and `packet_reject`, revision-bound. | `console_journal.py`, `console_apply.py` |
| **CONDRAFT-02 has landed** (the spec author saw it unlanded): `draft_templates.REVIEW_COPY_KINDS = ("draft-agency-summary", "draft-correction-notice")`; `draft_import.validate` requires `target = {website, page, section}` and `correction = {prior_statement, prior_version, linkage}` for the correction kind, with citations carrying a `verification` value. Correction rows live in the approval queue (scope `records-draft-review-v1`) and their approval is copy-review only. | `draft_templates.py`, `draft_import.py`, `approval_queue.py` |
| Revoked-principal handling today: written packets stay written and exported; operators list them for the site-PR reviewer to exclude. | `docs/TRUST-ANCHOR-PLAN-A.md` |
| `docs/PUBLICATION.md` has a separate Worker finding withdrawal lifecycle. It is a different workflow and the frozen `workers/`/`runner/` are not touched by this design. | `docs/PUBLICATION.md` |

## 1. Three distinct things "written" can mean

1. **Written**: a private change file exists in the project (`public_packets` `written`).
2. **Staged**: that content is in a local draft of the site checkout.
3. **Public**: a site PR was reviewed, merged and deployed.

`written` is never evidence of 2 or 3. A withdrawal design must therefore
record, separately, which of these the original reached, and must never claim
"withdrawn from the public site" before independent evidence of deployment of
the exact withdrawing version (section 9).

## 2. Who may propose, approve and execute

| Role | May | May not |
| --- | --- | --- |
| Authorized campaign organizer / operator | Propose, edit (supersede) and cancel an *unexecuted* withdrawal. | Sign, enable switches, override the trust path. |
| Automation (coordinator, drafter, Terra import) | Prepare a proposal body for a human to propose. | Sign or approve; an actor label (`_actor`) is provenance, never authentication. |
| G / the currently trusted human approval principal | Sign the exact withdrawal bytes through `campaign_tool.approval`. | Nothing in this design lets a signature authorize send, publication or deploy. |
| Site reviewer (human, site's own process) | Review, merge the draft site PR. | Not part of the engine. |
| G | Approve any send and any publication/deploy, separately and later. | n/a |

No key is installed, no namespace or trust path changes, no override exists.

## 3. Signed withdrawal approval

Same anchor, new kind. Proposed decision kind `public-packet-withdrawal`, proposed
approval scope `public-packet-withdrawal-v1` (distinct from `public-packet-v1`,
`public-packets-switch-v1` and `records-draft-review-v1`). It is **not** a fifth
ordinary packet kind and today's `public_packets.clean`/`KINDS` are unchanged.

The signed text is `approval.payload(scope, subject, content, round_)` with
`subject` = project-qualified withdrawal id and `round_` = withdrawal revision.
`content` is a canonical, sorted-key JSON/text rendering containing exactly:

- project and campaign binding (`project_id`, campaign id when configured);
- withdrawal id and revision;
- original packet id and revision, the original `file_sha256` from its
  `written` event, and the exact current site projection path and hash
  (`content/updates/<packet>.json` and its sha256, or "absent from site");
- `mode`: `remove` or `replace`;
- the full public explanation text (reviewable bytes, not an id);
- the private reason is **never** in signed public content; only a bounded
  private-reason reference hash may be recorded in the private journal;
- correction row id, revision, body sha256, evidence sha256 and exact
  `target` (section 5);
- for `replace`: replacement packet id, revision and change sha256;
- site repository identity and the inspected base commit;
- proposal timestamp and expiry.

Canonical encoding is fixed (UTF-8, sorted keys, no extra whitespace); no decision
field is enriched after signing. A signature over an ordinary packet, a copy
review, a switch, or an unsigned actor never authorizes a withdrawal, because the
scope header is part of the signed bytes.

## 4. States and transitions

The original packet aggregate is unchanged: `proposed`, `approved`, `rejected`,
`cancelled`, derived `expired`, `written`. It is **never renamed "withdrawn"**.
A separate **withdrawal aggregate** is added (all proposed).

### 4.1 Withdrawal aggregate (private journal, append-only, same lock)

| # | From | Event | Guards | To | Owner (existing module; proposed function) | Receipt | Retry |
| --- | --- | --- | --- | --- | --- | --- | --- |
| W1 | (none) | propose | original is `written` (not any other state); original id/revision/hash re-read under project lock; no open withdrawal for same original+revision (duplicate); correction linkage present (section 5); campaign config loads | `proposed` | `public_packets.py`; proposed `propose_withdrawal` | `withdrawal` event with digest | identical replay returns the existing row |
| W2 | `proposed` | edit | not expired; payload changed | `superseded` (old rev) + new `proposed` (rev+1) | `public_packets.py`; proposed `propose_withdrawal` (same id, rev+1) | `withdrawal` event | fresh signature always required |
| W3 | `proposed` | approve | not expired (`age > 14d` refuses `withdrawal_expired`); revision matches; `approval.verify` of withdrawal payload succeeds | `approved` | `public_packets.py`; proposed `approve_withdrawal` | `withdrawal_approved` (principal, signature, ts) | `TrustUnavailable` → `approval_unavailable`, DEFERRED, nothing consumed, retry after anchor returns |
| W4 | `proposed`, `approved` | reject | revision matches | `rejected` | `public_packets.py`; proposed `decide_withdrawal` | `withdrawal_rejected` | terminal; re-propose as new revision |
| W5 | `proposed`, `approved` | cancel (by proposer/operator) | not yet `change_written` | `cancelled` | same | `withdrawal_cancelled` | terminal; never executes a removal |
| W6 | `proposed`, `approved` | time passes | `now - proposed_ts > 14d` from the *withdrawal's own* proposal time | `expired` (derived, not stored) | `public_packets._expired`/`_view` pattern | none | re-propose |
| W7 | `approved` | write withdrawal change | switch ON; not expired; signature re-verified now (anchor, principal equals recorded signer); original still `written` with same hash; correction/replacement still bound; duplicate/conflicting file absent or byte-identical | `change_written` | `public_packets.py`; proposed `write_withdrawal_change` | `withdrawal_written` (file, sha256) | existing identical artifact reused only on exact binding; conflict refuses |
| W8 | `change_written` | — | terminal for the private aggregate; cannot be cancelled "as if the site were restored" | `change_written` | — | — | a later correction is a new withdrawal or supersede-by-new-proposal |

Trust-unavailable is never a terminal state anywhere in this table.

### 4.2 Downstream evidence states (recorded facts, never inferred)

`draft_prepared`, `draft_check_failed`, `site_review_pending`, `merge_recorded`,
`deployment_unconfirmed`, `deployment_recorded`.

| # | From | Event | Owner | Notes |
| --- | --- | --- | --- | --- |
| D1 | `change_written` | export withdrawal change | `packet_changes.py`; proposed `export_changes` extension | authenticates (section 6), exports public-safe fields only |
| D2 | exported | stage draft | `tools/stage_packet_changes.py`; proposed `stage` operation `remove`/`replace` | clean separate site checkout; preflights all operations before any edit |
| D3 | D2 ok | `draft_prepared` | same | site `npm run check`/`build`/`scan:secrets` all passed; output says `publication: not_performed` |
| D3f | D2 | `draft_check_failed` | same | diagnostic draft left, explicitly non-published; no success receipt |
| D4 | `draft_prepared` | human opens/reviews draft site PR | external: the site's human-controlled process (no existing engine executor) | existing `site_prs.py` snapshot (CON-SITEPR-1) may *display* the captured open draft PR read-only; it does not open or merge anything |
| D5 | reviewed | `merge_recorded` | external site reviewer | operator records PR number and merge commit by hand |
| D6 | merged | `deployment_recorded` / `deployment_unconfirmed` | external; G approves deploy | `deployment_unconfirmed` is the default until exact-version evidence is attached |

Console surface: any later UI/replay for W3/W4/W5 extends `console_journal.py`
(`packet_*` actions) and `console_apply.py` (`CODES`, store faults) with
revision binding and `STALE_REVISION` refusal exactly as for `packet_approve`/`packet_reject`.
`console_packets.py` would render pending withdrawals read-only.

## 5. Link to the correction notice

Every proposed withdrawal links to one CONDRAFT-02 `draft-correction-notice`
review copy in the approval queue (`approval_queue.py`):

- queue row kind is `draft-correction-notice`;
- `evidence.target = {website, page, section}` matches the withdrawn
  content's public location;
- `evidence.correction.prior_statement`/`prior_version` match the original
  packet text and the projection hash being removed;
- `evidence.correction.linkage` states why the notice relates to this packet;
- the withdrawal payload binds row id, revision, body sha256 and evidence sha256.

Absent, inconsistent or stale linkage (row edited, rejected, wrong target,
wrong prior version) is a **HOLD**, `correction_link_invalid`; no write.

Rules:

1. Copy-review approval of the correction row is **not** a withdrawal approval
   and not a publication approval. `public_packets.from_queue` must not become a
   shortcut for correction copies.
2. Citations keep their `verification` caveats; an `unverified`, `tracker-only`
   or `missing-original` citation stays caveated and does not become publishable
   merely because it is linked. Source, redaction and public-packet gates still
   apply to any public text.
3. `remove`: the old text is removed and a separately reviewed, minimal, dated
   public explanation is shown. It never exposes the private reason and never
   repeats the sensitive withdrawn text.
4. `replace`: additionally binds an explicit replacement packet (id, revision,
   change hash) that passed its own ordinary `public-packet-v1` approval and
   `write_change`. "Latest" is never inferred.
5. If the correction contract changes (field set in `draft_import.validate`),
   this design needs a refresh; a builder must not guess fields.

## 6. Producing the draft site-PR

1. `packet_changes.py` (proposed): a distinct versioned artifact, e.g.
   `records-site-withdrawal-v1`, and manifest operation field. Current
   `records-site-change-v1` insert/update data and manifest omission are never
   read as delete authority (`stage` today only adds/updates).
2. Before export it authenticates: re-verifies the withdrawal signature and
   the original packet binding (structural `public_change` hashes alone are not
   signature verification) and exports only public-safe fields.
3. `tools/stage_packet_changes.py` (proposed `remove`/`replace` branch) preflights,
   before any edit: separate clean campaign-site checkout (`site_checkout_required`,
   `site_checkout_dirty`, `stage_boundary`), repository identity and base commit
   match the signed ones, fixed allowlisted paths only, no traversal or symlink,
   target file bytes equal the signed projection hash, and no contradictory or
   duplicate operations.
4. It then deletes (remove) or replaces (replace) only that exact projection,
   preserves unrelated packets, assets and baseline roots, writes the approved
   public explanation, updates only intentionally changed generated assets and
   receipts, and runs the site's own `npm run check`, `build`, `scan:secrets`.
5. The result is a **draft for a site PR** only. Opening, reviewing and merging
   the PR and any deploy are human steps outside the engine, and deploy stays
   G-approved.
6. Unrecognized site layout, target drift, partial failure or unconfirmed
   downstream state is a safe HOLD with named code. It never touches a live
   checkout and never fetches originals or private mail.

## 7. What stays immutable

- Original packet journal lines, signatures, `written` event, written file and
  hash.
- Every withdrawal revision, decision and signature; correction review history
  (`approval_queue.history`).
- Export receipts, site commit/PR references, and the dated public correction
  history where safe.
- Withdrawal events are additive. An **active withdrawal tombstone** (an
  `approved`/`change_written` withdrawal for original id+revision) makes the
  proposed export/stage path refuse to re-add that packet text, so a stale
  export from before the withdrawal cannot reintroduce it. Old export
  manifests alone are never authority to publish.
- Historical exports and backups remain historical. Removal from the public
  site is not secure erasure from Git history, backups, caches or messages
  already sent.
- Not allowed: history rewrite, deleting source records, expunging signatures,
  enabling switches, key recovery, automatic rollback or deploy.

## 8. Refusal paths (content-free named codes, no mutation except audit receipts)

All checks run under the existing project lock (`public_packets._Locked`) and
re-read state first.

| Condition | Code (proposed unless existing) | Effect |
| --- | --- | --- |
| Original not `written` | `packet_not_written` | refuse |
| Signature not valid for the withdrawal scope, or signed by wrong payload | `approval_required` (existing) | refuse |
| Withdrawal key revoked **after** approval was recorded | `approval_required` at W7 (re-verify) | blocks new action; recorded approval is evidence only |
| Anchor/verifier unavailable | `approval_unavailable` (existing) | DEFERRED; not consumed; retry when same anchor returns |
| Withdrawal older than 14 days (`age > 14d`) | `withdrawal_expired` | refuse; re-propose |
| Revision changed, stale console form | `stale_revision`/`packet_stale` (existing) | refuse |
| Site base commit, target hash, correction body/evidence or replacement changed since signing | `withdrawal_stale` | refuse without edits; supersede → re-propose → fresh signature |
| Correction missing/inconsistent | `correction_link_invalid` | HOLD |
| Duplicate open withdrawal for same original | `withdrawal_duplicate` | return existing or refuse |
| Switch OFF | `switch_off` (existing) | refuse write |
| Existing artifact differs from expected bytes | `withdrawal_file_conflict` | refuse, never overwrite |

**Revoked original key.** If the *original* packet's signer was revoked, the
packet stays `written` evidence. A currently trusted human may still sign a
withdrawal of it (the withdrawal scope is verified on its own signature).
Original revocation is neither automatic removal nor permission to republish.
An old written original never expires out of withdrawal eligibility.

**Crash/replay.** An identical existing artifact is reused only when the
withdrawal id, revision and file hash all match its receipt; a file without a
receipt, or a receipt without its file, or conflicting content refuses and is
reported; nothing is blindly retried or overwritten.

## 9. Claims

No document, receipt or UI may say "withdrawn from the public site" until
independent exact-version deployment evidence (D6 `deployment_recorded`)
exists. Until then the words are "withdrawal change written", "draft prepared",
"awaiting site review", etc.

## 10. Tests a later build must pass

Synthetic fixtures only; owners are the existing test modules named.

**Eligibility and immutability** (`tests/records/test_public_packets.py`)
- Written original: remove and replace both succeed to `change_written`; original `written` row, file bytes, hash and signature are byte-identical afterwards.
- Original `proposed`, `approved`, `rejected`, `cancelled`, `expired` each refuse `packet_not_written`.
- An old written original (>14 days) is still eligible.

**Authorization** (`tests/records/test_public_packets.py`, `tests/records/test_approval_packet_recovery.py`)
- Signature for an ordinary packet, a switch, a draft-copy review, or an unsigned actor never approves a withdrawal.
- Wrong project, target, revision, body, evidence or replacement refuses.
- Currently trusted key succeeds; revoked withdrawal key refuses; unavailable anchor defers and then succeeds on retry without consuming the approval.
- Revoked original key plus separately current-key withdrawal still succeeds.

**Expiry and staleness**
- Exactly 14 days passes; 14 days + 1 second refuses `withdrawal_expired`.
- Changed site base/target/correction/replacement after signing refuses without edits; re-proposal gives a new revision that needs a new signature; the old signature is not reused.

**Correction linkage** (`tests/records/test_draft_import.py`, `tests/records/test_public_packets.py`)
- Missing row, wrong kind, wrong `target`, wrong `prior_version`, rejected/edited row → HOLD.
- `unverified` citation stays caveated and cannot cross the source/publication gates.
- Tests assert no mail placement, send, switch enable, approval signing or publication call occurs.

**Replay and concurrency**
- Idempotent proposal; duplicate open withdrawal; concurrent edits; interrupted artifact/receipt writes; conflicting existing file; stale console journal form (`tests/records/test_console_packets.py`, console journal tests); no overwrite or unrelated-state rollback.

**Export and site draft** (`tests/records/test_packet_changes.py`, stage-tool tests)
- Clean separate synthetic site: only the exact projection is removed or replaced; other packets, assets and baseline roots preserved.
- Refuse: wrong repository, dirty tree, path traversal, symlink, missing target, hash drift, malformed export, duplicate/contradictory operations.
- Failed site check leaves a clearly non-published diagnostic draft and no success receipt.
- Existing insert/update exports still never delete.

**Tombstone**
- A stale export containing the withdrawn packet is refused by export/stage while the tombstone is active; remove-only explanation contains neither old sensitive text nor private reason.

**Claims**
- No "withdrawn" claim before `deployment_recorded`; draft, reviewed PR, merge and deployment are distinct states.

**Controls that must stay green**: `tests.records.test_public_packets`, `tests.records.test_approval_packet_recovery`, `tests.records.test_packet_changes`, `tests.test_public_tree`, `tests.test_check_pin_guard`, `tools/check_public_tree.py`, `tools/check_pin_guard.py --all`, `scripts/scan-secrets.mjs`.

## 11. Holds and follow-on boundary

- Any change to what is published, to the approval chain or to a redaction gate
  by the later build needs G's decision; the build must stop with NEEDS G.
- Opening/merging the site PR and deployment remain external human steps.
- If CONDRAFT-02 or site-PR snapshot interfaces change, refresh section 5/4.2 first.
- Follow-on build (separate card and authorization): proposed `public_packets`
  withdrawal aggregate and tests; `packet_changes`/`stage_packet_changes`
  remove/replace operation and tests; console journal actions. This design PR
  clears no live hold and authorizes none of it.
