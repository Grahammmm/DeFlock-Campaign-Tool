# Writing a finding: guide for the person who writes it

This guide follows one finding from a record you already ingested to a page on your local site, then through a correction and a withdrawal, in the order you do the work. It is the author's side. The reviewer's side is the [review guide](REVIEW-GUIDE.md). Every step names its command as it exists today and links the reference section in [PUBLICATION.md](PUBLICATION.md), which stays the reference; nothing is moved out of it.

**Nothing here is legal advice, and nothing here sends, posts, uploads or deploys anything.** A finding says what specific records show. It is not a verdict on anyone's conduct.

## Before you start

- **A campaign folder outside this repository.** `$CAMPAIGN` below is a folder made by `init` and `content init`. Originals, drafts, receipts and reviews live under its `private/`; keep it out of git and out of the cloud.
- **Evidence you already hold.** You have ingested the record (`ingest`, see the [README](../README.md#preserve-a-document)). A hash identifies bytes, not truth: it shows the record is unchanged, not that it is accurate, complete or authentic.
- **Privacy.** Do not put a name, address, phone number, plate or account number in a title, summary, locator or source title unless the finding needs it. Redaction is its own process, on a copy, never the original ([DATA-HANDLING.md](DATA-HANDLING.md)).
- **Competence.** If you cannot read the record or the rule it touches, say so and ask someone who can. Missing records are a gap, not proof of anything.
- **Independence.** You are the draft's `author`. You cannot review it and you cannot be its `--approved-by` publisher. Two other people cover three roles ([REVIEW-GUIDE.md](REVIEW-GUIDE.md)).
- **Three different places.** `finding publish` writes `content/findings/<slug>.json` (local content). `build` writes `public/` (a local site you can open). Putting that site on the internet is a separate, approved step this guide does not cover.

Task checklist: register sources, start the draft, write it, check wording and law, preview, packet, right of reply, assign reviewers, publish locally, correct, draft the social, press and newsletter copies, withdraw.

## Practice setup (synthetic example)

The worked example is invented: the Example Records Office, you as Author Z, Reviewer A and Reviewer B, and a two-line record. Use a disposable folder. Placeholders in the commands are your values: `$CAMPAIGN` the folder, `$RECORD` the record file, `$PREFIX` the 12-character hash prefix from step 1, `$DRAFT` the draft path and `$FINDING_ID` its id (both printed by step 2), `$PREVIEW_DIR`, `$PACKET_DIR` and their `_2` twins new folders outside `public/`, `content/` and `private/`, `$TODAY` today's date (UTC) as `YYYY-MM-DD` and `$REPLY_BY` a deadline on or after it.

Save this as the file `$RECORD`:

```text
Example Records Office - retention schedule (invented).
Page 1: ALPR images are kept for 30 days.
```

```sh
python3 -m campaign_tool init --directory "$CAMPAIGN" --name "Practice Campaign" --county "Example County" --state CA
python3 -m campaign_tool content init --directory "$CAMPAIGN"
```

`content init` leaves `[EDIT ME: ...]` lines in `content/site.json` and no agencies. Replace those lines with plain sentences before `build`, and add `"base_url": "https://campaign.example.invalid"` (an address that never resolves; the drafts in step 10 need a base URL). Write `content/agencies.json` with the one invented agency (an agency id is two letters, a hyphen and a short name; real ones start with the state, here `ex` marks it as invented):

```json
[{"agency_id": "ex-records-office", "name": "Example Records Office (fictional)", "kind": "sheriff"}]
```

```sh
python3 -m campaign_tool ingest --directory "$CAMPAIGN" --file "$RECORD" --source-id example-production-1
```

Dates in the example are illustrative. The tool refuses a `--on` or `--date` after today, a correction dated before publication, and a reply out of order, so a one-day rehearsal can use `$TODAY` throughout; a real campaign uses the real dates.

## 1. Register the evidence

Reference: [PUBLICATION.md, before publishing: evidence check](PUBLICATION.md#before-publishing-evidence-check) and [SITE-CONTENT.md, adding a source record](SITE-CONTENT.md#adding-a-source-record).

A finding cites records from the public source library, `content/sources.json`. `status --list` shows what you have ingested with 12-character prefixes, and `sources add` registers one record. The library holds metadata only: the full original stays in `private/objects/`. `--public-url` is recorded, never fetched or uploaded. `--agency-id` must be an agency in `content/agencies.json`, and `--fragment` names the library group the record sits in.

```sh
python3 -m campaign_tool status --directory "$CAMPAIGN" --list
python3 -m campaign_tool sources add --directory "$CAMPAIGN" --sha256 "$PREFIX" --title "Retention schedule (invented)" --agency-id ex-records-office --received-at 2026-02-10T17:00:00+00:00 --fragment example-retention
python3 -m campaign_tool sources list --directory "$CAMPAIGN"
```

## 2. Start the finding

Reference: [PUBLICATION.md, offline review receipts](PUBLICATION.md#offline-review-receipts-cli).

`finding new` writes a private draft skeleton in `private/drafts/` with every field a reviewer needs. The draft id, slug and path are generated and printed; they are not fixed values, so copy yours into `$DRAFT` and `$FINDING_ID`. Nothing is published.

```sh
python3 -m campaign_tool finding new --directory "$CAMPAIGN" --agency ex-records-office --title "Retention schedule lists 30 days" --source "$PREFIX" --author "Author Z"
```

If the story is that an agency denied a request, or closed it, without producing records, start from the request instead ([PUBLICATION.md, drafting a gap finding](PUBLICATION.md#drafting-a-gap-finding-from-a-request)). A gap draft records what the agency said and when; it does not show that nothing happened, and it is not for a request that produced even some records. Do not combine `--gap` with `--agency` or `--source`.

```bash
python3 -m campaign_tool finding new --directory "$CAMPAIGN" --gap ex-records-office --request main --author "Author Z"
```

If the lead is a count in an agency's own ALPR search log, there is a third start. Save the `kit-search-log --json` output of the log as `$SUMMARY` ([SEARCH-LOG.md](SEARCH-LOG.md)), ingest and register that same CSV with the commands of step 1 (`$SEARCH_LOG` is the CSV), then run `finding new --search-log`. The tool re-reads the stored record and refuses unless its hash is registered in `content/sources.json` for that agency and every count in the summary still matches; it takes the agency only from `--agency` and refuses `--title`, `--source`, `--gap` and `--request` with it. The draft holds the period, total, purpose-category counts and the ten busiest searching organizations as tables, cites the log as `Search log <12 hex>`, and leaves the title, summary, classification and confidence as `[EDIT ME: ...]`: a count is a lead, not evidence, so you still interpret it, add limitations, and take it through steps 3 to 8 like any draft. No purpose text, user name, case number or plate is copied, and nothing is published.

```bash
python3 -m campaign_tool kit-search-log --directory "$CAMPAIGN" --csv "$SEARCH_LOG" --agency ex-records-office --json
python3 -m campaign_tool finding new --directory "$CAMPAIGN" --agency ex-records-office --search-log "$SUMMARY" --author "Author Z"
```

## 3. Write the finding

Reference: [PUBLICATION.md, offline review receipts](PUBLICATION.md#offline-review-receipts-cli).

Open the draft and replace every `[EDIT ME: ...]` marker. Edit by hand; no command fills them. Keep the generated `id`, `slug` and each source's `sha256`, and never alter an original or a receipt.

- `summary`: one or two plain sentences stating what the records show.
- `body_md`: separate three things: what the record says (observation), what the agency says about itself (attributed assertion: say who said it), and what you infer (say it is an inference and why). Cite each source by its locator.
- `classification` and `confidence`: pick the weakest honest label. `documented_fact` is a plain statement a record makes; a conflict label needs more ([SITE-CONTENT.md](SITE-CONTENT.md#findings)).
- `event_date`: the day of the event the record shows, not today.
- `sources[].locator`: the page or section where the claim sits.
- `limitations` and `counterevidence`: what the records cannot show, and anything that points the other way (`None found.` if you looked).

For the example, set these fields (the `locator` goes on the one existing source entry):

```json
{
  "classification": "documented_fact",
  "confidence": "verified",
  "event_date": "2026-02-10",
  "summary": "The invented retention schedule states that plate images are kept for 30 days.",
  "body_md": "The Example Records Office produced a one-page retention schedule (page 1). It states that plate images are kept for 30 days. The schedule states a period; it does not show how long images were actually kept.\n",
  "sources": [{"locator": "p. 1"}],
  "limitations": ["The schedule is one page and does not show what the office did in practice."],
  "counterevidence": ["None found in the records cited."]
}
```

## 4. Check the wording and the law, then preview

Reference: [PUBLICATION.md, offline review receipts](PUBLICATION.md#offline-review-receipts-cli), [PUBLICATION.md, before publishing](PUBLICATION.md#before-publishing-evidence-check) and [LAW-PACKAGES.md, effective-date discipline](LAW-PACKAGES.md#effective-date-discipline).

`finding review-status` is the read-only check. It lists `review_blockers` by name: `edit_marker:summary` (a marker left in a field), `unknown_classification`, and the wording gate `legal_conclusion_wording_in_summary` (also `_title` and `_body_md`) when text states a legal conclusion. Rephrase as "the record shows ..." instead; the gate catches words, it is not legal validation, so do not look for other words. There is no separate wording-check or law-check command.

If the finding rests on a legal rule, add `rule_ids` (and `rule_version`, `duty`, `exceptions`) copied from the campaign's law package, which `python3 -m campaign_tool.law show us-ca` lists. The preview, `content-check` and `build` refuse a rule the package lacks or that was not in force on `event_date`. Check that against the primary text yourself. The shipped package is a draft: citing it does not make it reviewed, and the page says so. The example cites no rule, so none is added.

```json
{"rule_ids": ["RULE-ID-FROM-LAW-SHOW"], "rule_version": "VERSION", "duty": "What the rule requires, as it states it.", "exceptions": "Exceptions the rule states, or None stated."}
```

`finding preview` is the private rendered check. It writes the page `build` would make, with a "Draft - not published" banner, to a new folder and never touches `public/`. `content-check` reads published content, not private drafts.

```sh
python3 -m campaign_tool finding review-status "$DRAFT" --directory "$CAMPAIGN"
python3 -m campaign_tool finding preview "$DRAFT" --directory "$CAMPAIGN" --out "$PREVIEW_DIR"
```

## 5. Make the outside-review packet

Reference: [PUBLICATION.md, offline review receipts](PUBLICATION.md#offline-review-receipts-cli).

A packet is a new private folder holding the draft, the preview, each cited record, the receipts and a manifest, for a reviewer outside your team (an attorney, a partner). It contains the originals, so treat it as confidential and hand it over yourself, privately. The tool never zips, uploads or sends it. Do not hand it to the agency.

```sh
python3 -m campaign_tool finding packet "$DRAFT" --directory "$CAMPAIGN" --out "$PACKET_DIR"
```

## 6. Give the agency its right of reply

Reference: [PUBLICATION.md, agency right of reply](PUBLICATION.md#agency-right-of-reply).

You may invite the agency to comment before you publish; publishing without asking stays allowed. `reply-request` writes a private, never-sent note for you to use (no address, no hash, no legal claim) and stores `pending` on the draft. **Pending means prepared, not asked.** Asking the agency is your own manual step, outside this tool. `reply` then records what happened. `--asked-on` is your attestation of the day you really asked. `received` needs `--text`, the reply as written; `declined` may carry one; `no_response` is never automatic: it is refused before `--by` has passed.

```sh
python3 -m campaign_tool finding reply-request "$DRAFT" --directory "$CAMPAIGN" --by "$REPLY_BY"
python3 -m campaign_tool finding reply "$DRAFT" --directory "$CAMPAIGN" --status received --on "$TODAY" --asked-on "$TODAY" --text "The office says the schedule was current when it was produced."
```

The reply is reviewed content. Recording or changing it makes earlier receipts stale, and the page shows it under "Agency response" as the agency's statement, not as a verified fact. Nothing in `finding publish` waits for the deadline, and a pending request is not shown on the page: if you told the agency you would wait, wait. Make the preview and the packet again into new folders, so reviewers see the reply:

```sh
python3 -m campaign_tool finding preview "$DRAFT" --directory "$CAMPAIGN" --out "$PREVIEW_DIR_2"
python3 -m campaign_tool finding packet "$DRAFT" --directory "$CAMPAIGN" --out "$PACKET_DIR_2"
```

## 7. Assign reviewers and collect reviews

Reference: [PUBLICATION.md, reviewer roster](PUBLICATION.md#reviewer-roster-role-assignment-not-authentication) and [PUBLICATION.md, not done](PUBLICATION.md#not-done); the reviewers' side is the [review guide](REVIEW-GUIDE.md).

Add each reviewer to the private roster with the roles they may hold, then record who was asked for which role. A roster entry and an assignment send nothing and are not reviews; names are self-asserted, roles are not identity. You cannot assign yourself.

```sh
python3 -m campaign_tool finding reviewers add --directory "$CAMPAIGN" --name "Reviewer A" --role factual
python3 -m campaign_tool finding reviewers add --directory "$CAMPAIGN" --name "Reviewer B" --role legal --role privacy
python3 -m campaign_tool finding reviewers list --directory "$CAMPAIGN"
python3 -m campaign_tool finding assign "$DRAFT" --directory "$CAMPAIGN" --role factual --reviewer "Reviewer A" --on "$TODAY"
python3 -m campaign_tool finding assign "$DRAFT" --directory "$CAMPAIGN" --role legal --reviewer "Reviewer B" --on "$TODAY"
python3 -m campaign_tool finding assign "$DRAFT" --directory "$CAMPAIGN" --role privacy --reviewer "Reviewer B" --on "$TODAY"
```

The reviewers then read the draft, the preview and the packet and record decisions. These three lines are theirs to run, shown so you know what to wait for; do not run one under any name to get past the gate. Publication needs a current approval for factual, legal and privacy by at least two different non-author people, and no unresolved challenge. A `reject` or `changes_requested` stays `unresolved_challenge` even if someone later approves; you revise, and the new version gets fresh reviews. Never delete a receipt.

```sh
python3 -m campaign_tool finding review "$DRAFT" --directory "$CAMPAIGN" --role factual --decision approve --reviewer "Reviewer A" --rationale "Factual: summary matches the record at p. 1; no later change shown. Approve."
python3 -m campaign_tool finding review "$DRAFT" --directory "$CAMPAIGN" --role legal --decision approve --reviewer "Reviewer B" --rationale "Legal: no rule is claimed; wording is descriptive and limits are stated. Approve for this version."
python3 -m campaign_tool finding review "$DRAFT" --directory "$CAMPAIGN" --role privacy --decision approve --reviewer "Reviewer B" --rationale "Privacy: no personal detail in the text, locators or record. Approve."
```

Run `finding review-status` again: until `review_blockers` is empty you will see `missing_factual_review`, `missing_legal_review`, `missing_privacy_review` or `need_two_independent_reviewers`. An edit to any reviewed field, including the agency reply, makes every earlier receipt stale.

## 8. Publish locally

Reference: [PUBLICATION.md, offline review receipts](PUBLICATION.md#offline-review-receipts-cli), [PUBLICATION.md, evidence check](PUBLICATION.md#before-publishing-evidence-check) and [PUBLICATION.md, publication gate](PUBLICATION.md#publication-gate-one-check-at-every-publishing-path).

`finding publish` copies the reviewed draft to `content/findings/` only if the review gate is clear, `--approved-by` is a named person who is not the author, and every cited record is registered and stored. It does not build or deploy. The four checks after it each look at one thing: `content-check` the content tree, `evidence --check` that cited records are registered and stored, `status --check` that stored originals still match their hashes, `build --check` that the local site builds and its links resolve. None is independent factual, legal or privacy approval. Open `public/findings/<slug>.html` to see the page.

```sh
python3 -m campaign_tool finding publish "$DRAFT" --directory "$CAMPAIGN" --approved-by "Organizer Y"
python3 -m campaign_tool content-check --directory "$CAMPAIGN"
python3 -m campaign_tool evidence --directory "$CAMPAIGN" --check
python3 -m campaign_tool status --directory "$CAMPAIGN" --check
python3 -m campaign_tool build --directory "$CAMPAIGN" --check
```

## 9. Correct a published finding

Reference: [PUBLICATION.md, offline review receipts](PUBLICATION.md#offline-review-receipts-cli) and [PUBLICATION.md, corrections and withdrawal](PUBLICATION.md#corrections-and-withdrawal).

Revise the private draft by hand and keep its `id` and `slug`. For the example, make the summary name its page:

```json
{"summary": "Page 1 of the invented retention schedule states that plate images are kept for 30 days."}
```

`finding diff` compares a few public fields and exits 0 for no differences or 3 when they differ; 3 is a result, not a malfunction. An empty diff does not mean the reviewed content is unchanged: the reply, `event_date` and the legal-basis fields are not compared, so read the whole draft and trust `review-status`. Run `review-status`, then have both reviewers record fresh receipts (the three review lines of step 7 again); the old ones remain as history. Then `finding correct` replaces the reviewed fields with the draft's, keeps the published date and earlier corrections, and appends a dated note readers see. A note-only correction, without `--from`, changes no text; never use it to slip in changed wording.

```sh
python3 -m campaign_tool finding diff "$FINDING_ID" --directory "$CAMPAIGN" --from "$DRAFT"
python3 -m campaign_tool finding review-status "$DRAFT" --directory "$CAMPAIGN"
python3 -m campaign_tool finding correct "$FINDING_ID" --directory "$CAMPAIGN" --date "$TODAY" --note "Summary now names the page of the schedule that states the period." --approved-by "Organizer Y" --from "$DRAFT"
python3 -m campaign_tool build --directory "$CAMPAIGN" --check
```

```bash
python3 -m campaign_tool finding correct "$FINDING_ID" --directory "$CAMPAIGN" --date "$TODAY" --note "A note that changes no text." --approved-by "Organizer Y"
```

## 10. Draft the social, press and newsletter copies

Reference: [PUBLICATION.md, social drafts](PUBLICATION.md#social-drafts-never-posted), [PUBLICATION.md, press drafts](PUBLICATION.md#press-drafts) and [NEWSLETTER.md, drafts](NEWSLETTER.md#drafts).

Each command writes private drafts from published findings only and sends or posts nothing. Review every draft before anyone uses it. A draft you edited is kept; the fresh version goes to a `.suggested` folder. Make them after publication or a correction, and run them again after the next correction. The newsletter command here is the offline `campaign_tool.newsletter` module; it needs the built `public/` to check its links. The hosted approval flow in [PUBLICATION.md](PUBLICATION.md#propose-approve-execute) is frozen context only. There is no send or record-sent step in this guide.

```sh
python3 -m campaign_tool social --directory "$CAMPAIGN"
python3 -m campaign_tool press draft --directory "$CAMPAIGN" --finding "$FINDING_ID" --date "$TODAY"
python3 -m campaign_tool.newsletter --directory "$CAMPAIGN" --from-content --site "$CAMPAIGN/public"
```

## 11. Withdraw a finding

Reference: [PUBLICATION.md, offline review receipts](PUBLICATION.md#offline-review-receipts-cli), [PUBLICATION.md, corrections and withdrawal](PUBLICATION.md#corrections-and-withdrawal) and [PUBLICATION.md, not done](PUBLICATION.md#not-done).

`finding withdraw` moves the finding from `content/findings/` to a private copy under `private/withdrawn/`, with your reason, and keeps its id and slug reserved. The next `build` leaves it out. It makes no notice page, and it cannot recall anything already shared: published pages, packets, social, press and newsletter copies. The drafts you made stay on disk (`social` lists the old one as `stale`); delete or rewrite them by hand and tell anyone who already received a copy.

```sh
python3 -m campaign_tool finding withdraw "$FINDING_ID" --directory "$CAMPAIGN" --reason "Practice finding retired."
python3 -m campaign_tool build --directory "$CAMPAIGN" --check
```

When the page is wrong in a way you can fix, correct it (step 9); withdraw when it should not stand. Deploying or removing the site on the internet, and sending any draft, are outside this guide and need the campaign's own approval.

Private gap and possible-misuse proposals from records runs are described in [RECORDS-GAP-PROPOSALS.md](RECORDS-GAP-PROPOSALS.md).
