# Records requests: organizer guide

This guide takes a new organizer through one records request in the order you do the work: choose agencies, check their contacts, draft, copy, send by hand or post, record what happened, correct a mistake, keep track of dates, follow up, reply to a denial and import requests you sent before you used the tool. Every step names its command as it exists today and links the reference section in [AGENCY-DISCOVERY.md](AGENCY-DISCOVERY.md) or [AGENCY-TIMELINE.md](AGENCY-TIMELINE.md); the references stay where they are and are not repeated here.

**Private and offline. Not legal advice.** Nothing in this guide, and no command in it, sends, prints, pays, uploads or publishes anything: you send each request yourself, outside the tool, and the tool only records that you did. Dates it shows are provisional reminders counted from the day you say a request was sent, not legal deadlines, and a draft that passes `kit-check` has not had a legal review. This guide draws no conclusion about any agency.

Checked against main be0717a74b8f988209180b717fa86e0ee342d90d.

## Before you start

- **A campaign folder outside this repository.** `../my-campaign` below is a folder made by `init`. Everything the steps write (`kit/agencies.json`, `kit/sent.json`, `kit/assignments.json`, drafts, follow-ups) is private to it; keep it out of git and out of shared drives. `kit/sent-undone.json` keeps what an undo removed.
- **Everything is by hand.** Where a step says "send", you do it in your own mail, portal or post. Output such as `kit/outgoing/*.letter.html` is a printable file; it does not print or post itself.
- **Privacy.** Contacts, owners and reasons you record are private notes, never public proof. Use job titles and teammate aliases, not personal names, direct lines or addresses.
- **A record is evidence of what it says.** A hash shows that bytes are unchanged, not that they are accurate or complete.

Task checklist: open the campaign, choose agencies, verify the contact, set the request, draft, check, copy, send by hand, record it as sent, assign a teammate, record the agency's responses, record your own contacts, undo a mistake, watch due dates, follow up, reply to a denial, read the timing and coverage, import requests you sent earlier.

## Practice setup (synthetic example)

The example is invented: the fictional `ca-example-police` (`police.example`), a teammate alias `Team Blue`, a requester `A. Organizer` and a postal block marked SYNTHETIC. The practice campaign starts in a real seed county (Alpine) only so that the offline seed has a location; the seed's suggestions are unverified and say nothing about ALPR use, and the example agency is not one of them. Nothing is fetched from `police.example`: the source address is only written down. Dates are fixed past dates, and `--as-of` is used where a command supports it, so you get the same answers any day.

Use a disposable folder. Files you are asked to save go in `../practice-files/` (make that folder first). Hash prefixes `$ACK_PREFIX` and `$RECORDS_PREFIX` are the 12-character values printed in step 11 for your own run; they differ if you change a file. A trailing `# exit N` shows the exit code a step ends with.

## 1. Open a private campaign

```sh
python3 -m campaign_tool init --directory ../my-campaign --name "Example Campaign" --county Alpine --state CA
python3 -m campaign_tool kit --directory ../my-campaign
```

`init` writes the campaign folder; `kit` writes `kit/` with `agencies.json` (yours to edit, never overwritten), one draft per selected sheriff, police and CHP entry in `kit/requests/`, and `summary.json` with `sent: false`. Reference: [what the kit writes](AGENCY-DISCOVERY.md#what-the-kit-writes). To adopt a folder you already have, see step 19; `setup` is the guided form of the same start ([README](../README.md#start-locally)).

## 2. Choose agencies

Only selected agencies get drafts. Deselect the two law-enforcement agencies this practice county suggested and add the example agency:

```sh
python3 -m campaign_tool kit-agency deselect --directory ../my-campaign --id ca-alpine-county-sheriff
python3 -m campaign_tool kit-agency deselect --directory ../my-campaign --id ca-alpine-county-chp
python3 -m campaign_tool kit-agency add --directory ../my-campaign --id ca-example-police --name "Example Police Department" --kind police --note "SYNTHETIC demo agency"
```

The new entry is `selected: true`, `verified: false`. `kit-agency select --id ID` undoes a deselect, and `kit-agency import --csv FILE [--check]` adds many at once:

```bash
python3 -m campaign_tool kit-agency select --directory ../my-campaign --id ca-alpine-county-sheriff
python3 -m campaign_tool kit-agency import --directory ../my-campaign --csv ../practice-files/more-agencies.csv --check
```

Reference: [add many agencies from a spreadsheet](AGENCY-DISCOVERY.md#add-many-agencies-from-a-spreadsheet).

## 3. Verify the official contact

Open the agency's own website, find where it says to send a records request, and write down the address and today's date. For a postal request record the postal address from the same page (`|` separates 2 to 5 lines):

```sh
python3 -m campaign_tool kit-verify-agency --directory ../my-campaign --agency-id ca-example-police --source-url https://police.example/records --checked-on 2026-10-01 --mailing-address "Records Unit|123 Example Street|Exampleville, CA 90000|SYNTHETIC - NOT A REAL ADDRESS"
```

The page is never fetched; in this example `police.example` is not a real page, so nothing was verified against anything. The entry becomes `verified: true`. For email or a portal, give `--records-email` or `--portal` instead (or `kit-verify-agency import --csv FILE` for many):

```bash
python3 -m campaign_tool kit-verify-agency --directory ../my-campaign --agency-id ca-example-police --source-url https://police.example/records --checked-on 2026-10-01 --records-email records@police.example
python3 -m campaign_tool kit-verify-agency --directory ../my-campaign --agency-id ca-example-police --source-url https://police.example/records --checked-on 2026-10-01 --portal https://police.example/portal
```

References: [what organizers must verify before sending](AGENCY-DISCOVERY.md#what-organizers-must-verify-before-sending), [recording a checked contact](AGENCY-DISCOVERY.md#recording-a-checked-contact).

## 4. Set the requester, scope, dates and fee cap

Save this private file, then run `kit` again; it fills every draft from it:

```json save ../my-campaign/kit/request-settings.json
{"schema_version": 1, "requester": ["A. Organizer", "PO Box 1, Exampleville, CA 90000 (SYNTHETIC)"], "fee_cap_usd": 50, "date_from": "2023-01-01", "date_to": "today"}
```

```sh
python3 -m campaign_tool kit --directory ../my-campaign
```

`kit --scope NAME` (repeatable) drafts only some request scopes. Reference: [request settings](AGENCY-DISCOVERY.md#request-settings).

## 5. Draft and edit

`kit/requests/ca-example-police.md` is the draft. The first check lists what is still unfinished:

```sh
python3 -m campaign_tool kit-check --directory ../my-campaign  # exit 3
```

Output ends `1 of 1 drafts need attention`; the line 6 it names is the `To:` line, still `[official records custodian - verify on the agency's website]`. Edit it in a text editor, replacing line 6 of the draft with:

```text replace-line-6 ../my-campaign/kit/requests/ca-example-police.md
To: Records Unit, Example Police Department (SYNTHETIC)
```

A later `kit` keeps a draft you edited and puts the fresh one in `requests.suggested/`. Reference: [what the kit writes](AGENCY-DISCOVERY.md#what-the-kit-writes).

## 6. Check

```sh
python3 -m campaign_tool kit-check --directory ../my-campaign
```

It is read-only. It lists unfilled `[placeholders]`, `fee cap missing`, `date range missing` and `recipient not verified` per selected agency, and exits 3 while any draft needs attention; here it prints `ca-example-police: OK`. Passing means the draft has no unfilled slots and a recorded contact, not that anyone reviewed the wording or the law. Reference: [recording a hand-sent request](AGENCY-DISCOVERY.md#recording-a-hand-sent-request).

## 7. Copy for email, portal or post

This example posts a letter:

```sh
python3 -m campaign_tool kit-copy --directory ../my-campaign --agency ca-example-police --for mail
```

It writes `kit/outgoing/ca-example-police.letter.html` and `.letter.txt` (private, mode 0600); nothing is printed or posted. For email or a portal the same command writes the sendable text only:

```bash
python3 -m campaign_tool kit-copy --directory ../my-campaign --agency ca-example-police --for email
python3 -m campaign_tool kit-copy --directory ../my-campaign --agency ca-example-police --for portal
```

`--for email` needs a verified email (without one it stops with exit 3, see the table below); `--for mail` needs a verified postal address and `requester`. Reference: [recording a hand-sent request](AGENCY-DISCOVERY.md#recording-a-hand-sent-request).

## 8. Send it by hand

Review the letter, print it yourself and post it (or send the email, or file the portal form). The tool does nothing here. In this guide the step is simulated: **Demo: assume you sent this by hand; the tool sends nothing.** Approval to send, and to pay any fee, happens outside the tool ([what organizers must verify](AGENCY-DISCOVERY.md#what-organizers-must-verify-before-sending)).

## 9. Record it as sent

```sh
python3 -m campaign_tool kit-sent --directory ../my-campaign --agency ca-example-police --date 2026-10-05 --channel mail
```

`kit/sent.json` now holds the date, channel and a hash of the draft. A second record for the same request is refused; `--replace` corrects one that has no steps yet. A further request to the same agency gets its own name and its own draft file. Copy the draft, narrow it by hand in real use, and record it with `--request`:

```sh
cp ../my-campaign/kit/requests/ca-example-police.md ../my-campaign/kit/requests/ca-example-police--narrowed.md
python3 -m campaign_tool kit-sent --directory ../my-campaign --agency ca-example-police --request narrowed --date 2026-10-09 --channel mail
```

The first request is named `main`. References: [recording a hand-sent request](AGENCY-DISCOVERY.md#recording-a-hand-sent-request), [several requests to one agency](AGENCY-DISCOVERY.md#several-requests-to-one-agency). If you filed on MuckRock or a portal, `kit-sent --request-url` keeps the link back ([portal](AGENCY-TIMELINE.md#requests-filed-on-an-agency-portal-kit-requests), [MuckRock](AGENCY-TIMELINE.md#requests-filed-on-muckrock-kit-requests)).

## 10. Assign a teammate

```sh
python3 -m campaign_tool kit-assign --directory ../my-campaign --agency ca-example-police --to "Team Blue"
```

The owner is an alias the team chose, kept in private `kit/assignments.json`; it changes no date or step. `--clear` removes it, and `kit-status`, `kit-due` and `today` take `--owner "Team Blue"` to show one person's requests. Reference: [who owns a request](AGENCY-TIMELINE.md#who-owns-a-request-kit-assign).

## 11. Record what the agency did

Record each step when you learn of it, with the date it happened. A letter or message you hold can be ingested first and attached as *correspondence*; records the agency produced are linked separately as *records*, and the two are never mixed. Save a short synthetic acknowledgement and a synthetic produced record:

```text save ../practice-files/ack-letter.txt
SYNTHETIC acknowledgement letter. Example Police Department received the request.
```

```text save ../practice-files/records.txt
SYNTHETIC produced record: ALPR policy excerpt.
```

```sh
python3 -m campaign_tool ingest --directory ../my-campaign --file ../practice-files/ack-letter.txt --source-id synthetic-ack-letter
python3 -m campaign_tool ingest --directory ../my-campaign --file ../practice-files/records.txt --source-id synthetic-records
python3 -m campaign_tool status --directory ../my-campaign --list --source-id synthetic-ack-letter
python3 -m campaign_tool status --directory ../my-campaign --list --source-id synthetic-records
```

Copy each `sha256_prefix` (12 characters) from the two `status` results into `$ACK_PREFIX` and `$RECORDS_PREFIX`. Then:

```sh
python3 -m campaign_tool kit-response --directory ../my-campaign --agency ca-example-police --date 2026-10-08 --kind acknowledged --correspondence $ACK_PREFIX
python3 -m campaign_tool kit-response --directory ../my-campaign --agency ca-example-police --date 2026-10-22 --kind determination --expected-on 2026-11-02
python3 -m campaign_tool kit-response --directory ../my-campaign --agency ca-example-police --date 2026-10-30 --kind produced --partial
```

`--kind` is `acknowledged`, `extension`, `determination`, `produced`, `denied`, `closed`, `fee_estimate`, `fee_paid`, `fee_waived`, `clarification_requested`, `clarified` or `withdrawn`; a date may not be before the send date or the previous step. `--partial` keeps the request open; the stated production date is the agency's own, not a legal deadline. A fee estimate is a record of what the agency quoted and `fee_paid` records that you paid it elsewhere; the tool pays nothing. A download link the agency gives you is not stored or fetched; record only the date it expires with `--download-expires-on` (the request stays "download pending" until you link the records). Fees and downloads are covered in the [recording section](AGENCY-DISCOVERY.md#recording-a-hand-sent-request) and [expiring links](AGENCY-TIMELINE.md#download-links-that-expire-kit-requests). Other references: [stated production date](AGENCY-TIMELINE.md#stated-production-date-kit-requests), [agency reference number and correspondence](AGENCY-TIMELINE.md#agency-reference-number-kit-requests), [withheld basis](AGENCY-TIMELINE.md#withheld-basis-kit-requests).

## 12. Record your own contacts

When you phone the records office, a contact is a note on the request, not a step the agency took, so it never changes a status or date:

```sh
python3 -m campaign_tool kit-contact --directory ../my-campaign --agency ca-example-police --on 2026-10-14 --channel phone --with "Records Supervisor" --note "Asked when to expect a determination"
```

`--with` is a job title, never a person's name, address or phone number; the note is never printed back. The command ends `Recorded only. Nothing was sent.` Reference: [your own contact with the records office](AGENCY-TIMELINE.md#your-own-contact-with-the-records-office-kit-contact).

## 13. Undo a mistake

Suppose you recorded `closed` by mistake. A closed request cannot take further steps, so take the latest step back:

```sh
python3 -m campaign_tool kit-response --directory ../my-campaign --agency ca-example-police --date 2026-10-30 --kind closed --note "Recorded by mistake"
python3 -m campaign_tool kit-response-undo --directory ../my-campaign --agency ca-example-police --reason "Closed was recorded by mistake"
```

Undo removes exactly the last recorded step (not the latest date), and only of a request you sent by hand. The removed step, its hashes and your reason are kept in private `kit/sent-undone.json`: undo archives, it does not delete. There is no redo; record the corrected step again with `kit-response`. Your contacts are untouched. Reference: [taking back the latest step](AGENCY-TIMELINE.md#taking-back-the-latest-step-kit-response-undo).

## 14. Due dates and reminders

```sh
python3 -m campaign_tool kit-due --directory ../my-campaign --as-of 2026-10-30  # exit 3
python3 -m campaign_tool today --directory ../my-campaign --as-of 2026-10-30
```

`kit-due` lists requests still waiting under `open`, with a provisional `due` date, `days` left (negative once past) and a `state`, and exits 3 when any is past due. In the example `main` is `producing` (a partial production, so still open, with no due date) and `narrowed` is `past_due`. The dates come from the draft law package and the day you recorded as sent; the period really runs from the agency's receipt, so treat them as reminders, not as a finding that anyone is late. `today` is the daily to-do list. To get the same dates into your own calendar:

```bash
python3 -m campaign_tool kit-calendar --directory ../my-campaign --out ../practice-files/open-requests.ics
```

References: [recording a hand-sent request](AGENCY-DISCOVERY.md#recording-a-hand-sent-request), [private calendar](AGENCY-TIMELINE.md#private-calendar-kit-calendar), [expiring links](AGENCY-TIMELINE.md#download-links-that-expire-kit-requests).

## 15. Draft a follow-up

A follow-up is offered only for a request that `kit-due` lists as `past_due`:

```sh
python3 -m campaign_tool kit-followup --directory ../my-campaign --agency ca-example-police --request narrowed --as-of 2026-10-30
```

It writes a private draft, `kit/followups/ca-example-police--narrowed-2026-10-30.md`, to review and send yourself; the tool sends nothing and `sent.json` is unchanged. It refuses a request that was answered or closed, is still being produced or is not past due (the table below), and stops with exit 3 while a clarification you owe is open. `--cite-contact` adds one line naming your latest recorded contact. Suppose the agency then answers the narrowed request with a record you ingest and link:

```sh
python3 -m campaign_tool kit-response --directory ../my-campaign --agency ca-example-police --request narrowed --date 2026-11-02 --kind produced --record $RECORDS_PREFIX
```

`--record` links an ingested record as *production*; correspondence uses `--correspondence`. Reference: [recording a hand-sent request](AGENCY-DISCOVERY.md#recording-a-hand-sent-request), [clarification requested](AGENCY-TIMELINE.md#clarification-requested-kit-requests).

## 16. Reply to a denial

Record a denial as the agency wrote it, quoting its stated reason and never the content of a record, then draft the reply:

```sh
python3 -m campaign_tool kit-response --directory ../my-campaign --agency ca-example-police --date 2026-10-31 --kind denied --withheld-basis "Exemption cited by the agency (SYNTHETIC)"
python3 -m campaign_tool kit-denial-reply --directory ../my-campaign --agency ca-example-police --rule ca-civ-1798.90.52-access-records
```

`kit-denial-reply` writes a private `kit/followups/ca-example-police-denial-2026-10-31.md` asking which exemption the agency relied on and for a written explanation, with the placeholders `[EDIT ME: what you asked for]` and `[name]` left for you to fill. It never picks an exemption: each `--rule` must be a rule id in the draft law package, is cited with that package's status, and no `--rule` gives a draft with no citations. The draft is not legal advice; have someone qualified read it before it goes anywhere. It works on the `main` request of the agency. Reference: [recording a hand-sent request](AGENCY-DISCOVERY.md#recording-a-hand-sent-request), [withheld basis](AGENCY-TIMELINE.md#withheld-basis-kit-requests).

## 17. How long it took

```sh
python3 -m campaign_tool kit-timing --directory ../my-campaign --as-of 2026-11-03
```

A private report of **calendar days** between recorded steps for each request: first step, determination, first and final production, and the open age while a request is unanswered. Extension and clarification waits are their own spans and are never subtracted. Every figure is labelled `calendar days; provisional; not a legal deadline`; it describes what you recorded and judges nothing. `--csv FILE` writes the same numbers to a new private file. Reference: [response times](AGENCY-TIMELINE.md#response-times-kit-timing).

## 18. Coverage

```sh
python3 -m campaign_tool kit-coverage --directory ../my-campaign
python3 -m campaign_tool kit-status --directory ../my-campaign --owner "Team Blue"
```

`kit-coverage` has one row per selected agency's request: whether it has a draft, when and how it was sent, the last step, the records linked, the sources and findings on file for the agency, the request name and its owner, with a totals line. Here `ca-example-police` shows records for `main` (none yet) and `narrowed` (1 linked). `kit-status` lists each request with its provisional dates, `last_step` and any `undone_steps`; `--csv FILE` writes it to a new private spreadsheet. Reference: [recording a hand-sent request](AGENCY-DISCOVERY.md#recording-a-hand-sent-request), [track requests in a spreadsheet](AGENCY-DISCOVERY.md#track-requests-in-a-spreadsheet).

## 19. Import requests you sent before using the tool

`import-existing` copies a folder of files into a campaign as it is and parses nothing; it does not record any request:

```bash
python3 -m campaign_tool import-existing --directory ../my-campaign --label earlier-files --from ../earlier-folder
```

To track requests you already sent, add the agency, then import a CSV. The sheriff here is a second invented agency; a verified contact is not needed. Save the file:

```sh
python3 -m campaign_tool kit-agency add --directory ../my-campaign --id ca-example-sheriff --name "Example County Sheriff" --kind sheriff
```

```csv save ../practice-files/sent-before.csv
agency_id,request,sent_on,channel,reference,letter_record
ca-example-sheriff,,2026-03-02,email,R0123-030226,
ca-example-sheriff,contract-2026,2026-03-09,portal,,
```

Preview first (nothing is written), then apply:

```sh
python3 -m campaign_tool kit-sent-import --directory ../my-campaign --csv ../practice-files/sent-before.csv
python3 -m campaign_tool kit-sent-import --directory ../my-campaign --csv ../practice-files/sent-before.csv --apply
```

The whole file is checked before anything is written: one bad row (for example a date that is not real, or a request already recorded) refuses all of it, names the line and exits 2. Running an applied file again is refused. An imported entry is your statement of the past: it has no draft and no draft hash, and it asserts nothing about the letter, which is linked only if you pass an ingested `letter_record` prefix. To add the agency's replies from a portal or letter history, use a second CSV:

```csv save ../practice-files/timeline.csv
date,kind,note,amount,expected_on,partial
2026-03-05,acknowledged,,,,
2026-03-12,determination,,,2026-03-20,
```

```sh
python3 -m campaign_tool kit-response-import --directory ../my-campaign --agency ca-example-sheriff --csv ../practice-files/timeline.csv
python3 -m campaign_tool kit-response-import --directory ../my-campaign --agency ca-example-sheriff --csv ../practice-files/timeline.csv --apply
```

Reference: [requests sent before you used the tool](AGENCY-TIMELINE.md#requests-sent-before-you-used-the-tool-kit-sent-import), [a transcribed timeline in one write](AGENCY-TIMELINE.md#a-transcribed-timeline-in-one-write-kit-response-import). Imported requests then take `kit-contact`, `kit-assign` and `kit-response-undo` like any other.

## When a command refuses

Every refusal writes nothing. The messages below are the tool's own.

| Situation | What the tool says | Next action |
| --- | --- | --- |
| Draft still has placeholders or no verified recipient | `kit-check` exit 3: `line 6 [official records custodian - verify on the agency's website]`, `recipient not verified` | Edit the draft; run `kit-verify-agency` |
| Email copy with no recorded email | `Stopped: ca-example-police has no verified records_email; run kit-verify-agency first` (exit 3) | Record the email from the official page, or copy for post or portal |
| Request recorded twice | `Stopped: ca-example-police already recorded as sent on 2026-10-05; use --replace to correct it` | Use `--request NAME` for a further request |
| Unknown request name | `Stopped: no sent request ca-example-police/nosuch; run kit-sent --request nosuch first` | Record it with `kit-sent --request` |
| Date before the send date | `Stopped: --date 2026-09-01 is before the request was sent on 2026-10-05` | Check the date you were told |
| Follow-up while records are still coming | `Stopped: records still coming: next installment not stated; not passed` | Wait for the next installment, or record it; a follow-up is for a request that is past due and unanswered |
| Contact `--with` is a name or number | `Stopped: --with is a job title, not a person's name: no email address or phone number` | Use a job title |
| Waiting on you (clarification requested) | `kit-followup` stops with exit 3 and writes nothing | Record your reply with `--kind clarified` |
| Export file already exists | `kit-status --csv` refuses an existing file, naming it | Choose a new file name |
| Import row is malformed | `Stopped: 1 problem(s); kit/sent.json was not changed:` then `line 3: sent_on: must be a real YYYY-MM-DD date` (exit 2) | Fix the row; re-run the preview |

None of these is a ruling on the request, and none is legal advice. For anything the tool cannot decide, such as whether an exemption applies or what to do about a missed date, ask a qualified person.
