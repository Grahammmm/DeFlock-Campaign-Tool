# Reviewing a finding: guide for volunteer reviewers

This guide is for a volunteer asked to review one draft finding in one role: factual, legal or privacy. It is task-ordered and works offline with the commands below. It does not replace the reference in [PUBLICATION.md](PUBLICATION.md#offline-review-receipts-cli) or the law-package process in [LAW-PACKAGES.md](LAW-PACKAGES.md#review-labels).

**Nothing here is legal advice.** A review records what one person checked and decided about one version of one draft. It is not a legal opinion, does not make a finding true, and does not change the review status of any law package.

## Before you accept

- **Independent.** You must not be the draft's `author`; the tool refuses that name (case and spacing ignored). Two different non-author reviewers must cover the three roles before a finding can clear the gate.
- **Competent for the role.** If you cannot read the records, cannot follow the law-package rule, or do not know the privacy risk, say so to the organizer and record nothing. Never approve to be helpful. There is no "abstain" decision: an unrecorded role simply stays a blocker.
- **Confidential.** The draft, preview, packet, cited records and your rationale are private until the organizer publishes through the redaction and review gates. Do not forward, post or quote them.
- **A role is not an identity.** Names are self-asserted offline. The roster says which roles a name may hold; it proves nobody's identity. Receipts may carry an optional SSH signature (see "Signed receipts" in [PUBLICATION.md](PUBLICATION.md#offline-review-receipts-cli)); the guide adds no key or signing steps.
- **Passing software checks is not validation.** The review gate counts receipts bound to the exact content hash. It cannot tell whether your reading was careful.

## What each command records

| Step | Writes | Means |
| --- | --- | --- |
| `finding assign` | private note in `private/review-assignments/` | Someone asked you for one role. Sends nothing, is not a review, clears no blocker. |
| `finding review-payload` | nothing (prints bytes) | The exact receipt bytes an optional signature covers. Not a receipt. |
| `finding review` | one receipt in `private/reviews/` | Your decision on the content as it is now, for your role. Not publication. |
| `finding review-status` | nothing | The authority for blockers. Signature totals and assignments are not approvals. |
| `finding diff` | nothing | Field-limited comparison of two versions (see below). |

A roster entry (`finding reviewers add`) is separate from an assignment: the roster says what a name may do in this campaign, the assignment says who was asked about this draft. Each receipt is bound to the draft's campaign id and reviewed content hash. Any edit to a reviewed field, including the agency response, makes earlier receipts stale history; they are never deleted or reused.

## Shared preparation

1. Get the current draft and a private preview or packet from the organizer (`finding preview`, `finding packet`; the packet holds the draft, preview, each cited record and the current receipts).
2. Open each cited record at its exact `locator`. A hash proves the bytes are the same; it does not show what the record says.
3. Compare `title`, `summary`, `body_md`, `classification`, `confidence`, `limitations` and `counterevidence` with each other. A summary stronger than the body is a finding in itself.
4. Run `finding review-status` and read `review_blockers`, `stale_receipts` and `assignments` before you start, and again before you record anything.
5. Read the sections you need: [PUBLICATION.md](PUBLICATION.md#offline-review-receipts-cli) for receipts, packet and signatures, [PUBLICATION.md](PUBLICATION.md#agency-right-of-reply) for agency replies, [LAW-PACKAGES.md](LAW-PACKAGES.md#effective-date-discipline) for event-date rules, [LEGAL-CLAIM-GUARD.md](LEGAL-CLAIM-GUARD.md) for the wording gate.

## Commands

Shell variables stand for your paths: `$CAMPAIGN` the campaign folder, `$DRAFT` the draft JSON, `$DRAFT_BEFORE` a saved earlier copy, `$PREVIEW_DIR` and `$PACKET_DIR` new folders outside `public/` and `content/`, `$SIGNATURE` an optional signature file, `$FINDING_ID` a published finding id.

```sh
python3 -m campaign_tool finding reviewers list --directory "$CAMPAIGN"
python3 -m campaign_tool finding preview "$DRAFT" --directory "$CAMPAIGN" --out "$PREVIEW_DIR"
python3 -m campaign_tool finding packet "$DRAFT" --directory "$CAMPAIGN" --out "$PACKET_DIR"
python3 -m campaign_tool finding review-status "$DRAFT" --directory "$CAMPAIGN"
python3 -m campaign_tool finding review-payload "$DRAFT" --directory "$CAMPAIGN" --role legal --decision approve --reviewer "Reviewer B" --rationale "Legal: rule and version match the cited text."
python3 -m campaign_tool finding review "$DRAFT" --directory "$CAMPAIGN" --role legal --decision approve --reviewer "Reviewer B" --rationale "Legal: rule and version match the cited text."
python3 -m campaign_tool finding review "$DRAFT" --directory "$CAMPAIGN" --role legal --decision approve --reviewer "Reviewer B" --rationale "Legal: rule and version match the cited text." --signature "$SIGNATURE"
python3 -m campaign_tool finding assign "$DRAFT" --directory "$CAMPAIGN" --role factual --reviewer "Reviewer A" --on 2026-03-01
python3 -m campaign_tool finding assign "$DRAFT" --directory "$CAMPAIGN" --role factual --reviewer "Reviewer C" --on 2026-03-02 --replace
python3 -m campaign_tool finding diff "$DRAFT_BEFORE" "$DRAFT" --directory "$CAMPAIGN"
python3 -m campaign_tool finding diff "$FINDING_ID" --directory "$CAMPAIGN" --from "$DRAFT"
```

- The organizer normally runs the roster, preview, packet and assign lines; `--role` is `factual`, `legal` or `privacy`; `--decision` is `approve`, `reject` or `changes_requested`.
- `review-payload` and a signed `review` need the same role, decision, reviewer, rationale, draft and campaign. It needs the campaign id, which `init` or the first `review` or `reviewers add` creates; it refuses without one. Changing the rationale or the draft changes the bytes, so a signature made earlier no longer verifies.
- A rationale is one line of 1 to 2000 characters; the reviewer name may not contain control characters. With a roster, an unlisted name or an unassigned role is refused. The same reviewer, role, decision and content cannot be recorded twice.
- `finding diff` exits `0` when the compared fields match and `3` when they differ; 3 is a result, not a failure. It compares only `title`, `summary`, `body_md`, `sources`, `classification`, `confidence`, `limitations`, `counterevidence`, `agency_id` and `also_agency_ids`.
- **An empty diff does not mean the reviewed content is unchanged.** `agency_response`, `event_date`, `rule_version`, `duty`, `exceptions` and `rule_ids` are not compared, yet changing them changes the reviewed hash. Read those fields yourself in full, and trust `review-status` (`stale_receipts`) for what needs review again.

## Factual reviewer

**Read.** Every cited record at the given locator, the summary and `body_md`, `event_date`, `limitations`, `counterevidence`, and any `agency_response` (see the last section of this guide).

**Check.** Each claim has support at a locator that says it, with the right date, agency and record identity. Separate what a record shows from what the draft infers. Missing records are a gap, not proof: no record of an event does not show it never happened. Look for contrary evidence the draft omits and for a `classification` that claims more than the records show.

**Decision.** Approve only if every claim you read is supported at its locator. Choose `changes_requested` for a fixable gap (a wrong page, a date to correct, a claim to soften or cite). Choose `reject` if the central claim is unsupported or the records show the opposite. If you could not open a record, do not guess: tell the organizer.

**Rationale.** Role, field, safe locator, observation, limitation, required action. Example: `Factual: summary sentence 2 vs record a1b2c3d4e5f6 p. 4; date matches; records do not show a later change. Approve.`

## Legal reviewer

**Read.** The cited primary text, the `rule_ids` and `rule_version` in the draft, the matching rule in the law package (`jurisdictions/<country>-<state>/package.json` or the packet from `law review-packet`), `event_date`, `duty`, `exceptions`, `classification` and `limitations`.

**Check.** The rule version in force on the event date, not today ([LAW-PACKAGES.md](LAW-PACKAGES.md#effective-date-discipline)); who the actor is and whether the rule applies to them; the duty, conditions and exceptions as the text states them; the rule's own `review` label (`verified`, `likely`, `needs_attorney_review`); and any coverage gap for earlier dates. Wording must say what the records show, not that anyone broke the law; the wording gate catches terms but is not legal validation.

**Decision.** Approve only the reading you checked against the primary text. A rule marked `likely` or `needs_attorney_review`, or a disputed reading, is not yours to certify: choose `changes_requested` (soften, cite, or send to a qualified independent reviewer) rather than approve. Choose `reject` if the draft rests on a rule that does not apply or a duty the text does not state. A draft law package is not a reviewed one, and your finding approval does not make it so.

**Rationale.** Name the rule id and version and what you compared. Example: `Legal: rule us-ca-example-1 v2026 vs cited text, event 2026-01-02 inside effective dates; exceptions stated. Rule is labelled likely; wording kept descriptive. Approve.`

## Privacy reviewer

**Read.** Title, summary, body, locators, source titles, filenames, the preview and every cited record (in the packet), including text in images and attachments, headers and file metadata.

**Check.** Direct identifiers (names, addresses, phone numbers, plates, account numbers) and indirect ones (rare role, small group, dates plus place). For each, ask whether the finding needs it. Check locators and source titles too. Redaction is the organizer's process on a copy; never ask anyone to edit an original, and remember a privacy approval is not a separate redaction sign-off. See [DATA-HANDLING.md](DATA-HANDLING.md).

**Decision.** Approve only if you found nothing that should be removed or minimized in what you inspected. `changes_requested` for each item to remove or generalize. `reject` if the finding cannot be told without exposing someone unnecessarily.

**Rationale.** Name the field and kind of problem, never the sensitive detail itself: `Privacy: body_md paragraph 3 names a private individual; not needed for the claim. Remove or generalize, then recheck.` Rationales travel in `reviews.json` inside packets.

## Choosing a decision and writing it down

- `approve`, `reject` and `changes_requested` are human judgments; the software only records them. Approve covers the version and role you inspected.
- Use `changes_requested` for repairable gaps and say what must change and that you will recheck. Use `reject` for a premise you cannot endorse.
- Template: role + claim or field + safe locator or rule citation + observation + limitation or counterevidence + required action (or why acceptable). One line, no private detail.
- Samples: approve `Factual: claim 1 supported at p. 2; no counterevidence in record.`; changes_requested `Factual: summary says "twice", record shows one entry at p. 5; correct count, then recheck.`; reject `Legal: the cited section covers a different actor; the draft's duty is not in the text.`
- A current `reject` or `changes_requested` stays `unresolved_challenge` even if someone else later approves the same content. Do not look for a way to remove or overwrite a receipt. The author revises, the content hash changes, and the new version gets fresh reviews.

## Worked example (synthetic)

Invented: the Example Records Office, author Author Z, Reviewer A (roster: factual) and Reviewer B (roster: legal, privacy). The record is a made-up text file. Prerequisites: an initialized campaign folder `$CAMPAIGN`, and a draft `$DRAFT` in `$CAMPAIGN/private/drafts/` whose `sources` cite that record by its sha256 locator `p. 1`. No ingest, network or publish command is needed.

The organizer sets the roster:

```sh
python3 -m campaign_tool finding reviewers add --directory "$CAMPAIGN" --name "Reviewer A" --role factual
python3 -m campaign_tool finding reviewers add --directory "$CAMPAIGN" --name "Reviewer B" --role legal --role privacy
python3 -m campaign_tool finding reviewers list --directory "$CAMPAIGN"
```

Author Z cannot be asked or review; an unrostered name or a role not held is refused. The organizer records who was asked, then checks status:

```sh
python3 -m campaign_tool finding assign "$DRAFT" --directory "$CAMPAIGN" --role factual --reviewer "Reviewer A" --on 2026-03-01
python3 -m campaign_tool finding assign "$DRAFT" --directory "$CAMPAIGN" --role legal --reviewer "Reviewer B" --on 2026-03-01
python3 -m campaign_tool finding assign "$DRAFT" --directory "$CAMPAIGN" --role privacy --reviewer "Reviewer B" --on 2026-03-01
python3 -m campaign_tool finding review-status "$DRAFT" --directory "$CAMPAIGN"
```

`assignments` shows each role as assigned, but `review_blockers` still lists `missing_factual_review`, `missing_legal_review`, `missing_privacy_review` and `need_two_independent_reviewers`. Reviewer A opens the record at `p. 1`, finds the summary supported, and checks the payload before recording:

```sh
python3 -m campaign_tool finding review-payload "$DRAFT" --directory "$CAMPAIGN" --role factual --decision approve --reviewer "Reviewer A" --rationale "Factual: summary matches the record at p. 1; no later change shown. Approve."
python3 -m campaign_tool finding review "$DRAFT" --directory "$CAMPAIGN" --role factual --decision approve --reviewer "Reviewer A" --rationale "Factual: summary matches the record at p. 1; no later change shown. Approve."
python3 -m campaign_tool finding review "$DRAFT" --directory "$CAMPAIGN" --role legal --decision approve --reviewer "Reviewer B" --rationale "Legal: no rule is claimed; wording descriptive; limits stated. Approve for this version."
python3 -m campaign_tool finding review "$DRAFT" --directory "$CAMPAIGN" --role privacy --decision approve --reviewer "Reviewer B" --rationale "Privacy: no personal detail in text, locators or the record. Approve."
python3 -m campaign_tool finding review-status "$DRAFT" --directory "$CAMPAIGN"
```

Now `assignments` shows `receipt_recorded` for all three roles and `review_blockers` is empty. Nothing was sent or published. The organizer saved a copy as `$DRAFT_BEFORE`; Author Z then edits the `summary`:

```sh
python3 -m campaign_tool finding diff "$DRAFT_BEFORE" "$DRAFT" --directory "$CAMPAIGN"
python3 -m campaign_tool finding review-status "$DRAFT" --directory "$CAMPAIGN"
```

The diff names `summary` and exits 3. `receipts_for_this_content` drops to 0, `stale_receipts` is 3 and the blockers return: the three approvals were for the earlier text. Both reviewers re-read the new version and record fresh receipts; the old ones remain as history.

**Reply data.** If the organizer prepared a request for comment, the draft holds an `agency_response` such as:

```json
{"status": "pending", "prepared_on": "2026-02-20", "reply_by": "2026-03-06"}
```

`pending` means a request was prepared, not that anyone asked the agency; read the `agency_response` reference in [PUBLICATION.md](PUBLICATION.md#agency-right-of-reply). A later `received` reply holds `asked_on` (the date the organizer attests they asked), `on` and `text`:

```json
{"status": "received", "prepared_on": "2026-02-20", "reply_by": "2026-03-06", "asked_on": "2026-02-21", "on": "2026-03-02", "text": "Synthetic reply text."}
```

Treat it as the agency's attributed assertion, to weigh against the records, not as a verified fact. Adding or changing it makes earlier receipts stale, and `finding diff` will not show it, so read the whole field.
