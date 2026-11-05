# Troubleshooting: common refusals and the safe next step

**Not legal advice.** This guide explains what the campaign tool's own messages mean and which command to try next. It does not tell you what the law requires, whether a request, fee, agenda or finding is legally sound, or that any agency did anything. It is also not a way around independent review, contact verification, privacy checks or backup verification: every step below keeps those in place.

## How to match a message

Search this page for the first words the tool printed. A refusal usually appears on standard error as `Stopped: ` followed by the message, and sometimes a `Fix:` line after it. Match the words after that wrapper. Four other shapes exist:

- `backup --into` prints its whole message as one line that starts with `backup: ` (that part belongs to the message, and the entry quotes it).
- `backup` and `restore` problems that are not that line print `backup failed: ` before the message.
- `kit-check` is a report, not a refusal. Each problem row is `<agency_id>: <problem>`, and the command exits 3 when any row needs attention. Match the words after the agency id.
- `content-check` is read-only and lists every problem that `build` would refuse; it exits 3 when it lists any.

Each entry quotes only the fixed opening of its message. The rest of the real message can add a path, date, count, name or ID, so an entry never repeats a variable part. The quoted opening is checked against the code by `tests/test_troubleshooting_doc.py`, so a reworded message makes that test fail until this page is updated.

The examples use a synthetic campaign folder `../my-campaign`, a synthetic agency id `example-sheriff` and dates and URLs on `example.gov`. Replace each with your real value, taken from your own records or the agency's official page, and never invent one. Example commands are shown so the syntax is right; running one does not make the claim in it true. Where the fix is to edit a private file, copy it first so the original and your edit both survive, then re-run the check command named.

Things this tool never does for you, and no entry below suggests otherwise: staging a meeting is not verifying its agenda; recording `kit-sent` is not sending the request; a fee estimate is not a payment; `finding publish` writes local content and is not a deployment; `verify` checks integrity, not who made the archive.

## kit

### The agency seed file is missing
Message opening: `No agency seed at`
Source: [campaign_tool/discovery.py](../campaign_tool/discovery.py)
Meaning: The campaign's state or jurisdiction has no offline agency seed in this copy of the tool. Only supported jurisdictions have one, and the tool does not fetch or invent one.
Next: Check what the campaign is set to with `python3 -m campaign_tool doctor --directory ../my-campaign`. If the jurisdiction is not supported, stop and read [agency discovery](AGENCY-DISCOVERY.md); do not hand-write a seed.

### No county or city matches the location
Message opening: `No county or city named`
Source: [campaign_tool/discovery.py](../campaign_tool/discovery.py)
Meaning: The campaign's `location_query` (or county) did not match any county or city in the seed. The full message may suggest close names.
Next: Copy `campaign.json`, then correct the `location_query` field to an exact supported name, and run `python3 -m campaign_tool kit --directory ../my-campaign` again.

### The location matches more than one place
Message opening: `Ambiguous location`
Source: [campaign_tool/discovery.py](../campaign_tool/discovery.py)
Meaning: The name fits both a county and a city, or several places. The message lists the candidates.
Next: Copy `campaign.json`, set `location_query` to one candidate with its county or city qualifier as listed, then run `python3 -m campaign_tool kit --directory ../my-campaign`.

## kit-verify-agency

### The kit has not been created yet
Message opening: `run kit first: kit/agencies.json is missing`
Source: [campaign_tool/kit.py](../campaign_tool/kit.py)
Meaning: There is no `kit/agencies.json` to record a contact in. Nothing was changed.
Next: Create the kit with `python3 -m campaign_tool kit --directory ../my-campaign`, then record only contacts you checked on the agency's official page.

### No contact was given
Message opening: `give at least one of records_email, records_url, portal`
Source: [campaign_tool/kit.py](../campaign_tool/kit.py)
Meaning: A verification needs at least one contact you actually checked: an email, a records page or a portal.
Next: Re-run with the value from the official page, for example `python3 -m campaign_tool kit-verify-agency --directory ../my-campaign --agency-id example-sheriff --source-url https://example.gov/records --checked-on 2026-01-15 --records-email records@example.gov`.

### The records email is not a plain address
Message opening: `records_email must be a plain address like records@agency.example`
Source: [campaign_tool/kit.py](../campaign_tool/kit.py)
Meaning: The value has a name, a display form, a space or extra text around the address.
Next: Copy only the address from the official page: `python3 -m campaign_tool kit-verify-agency --directory ../my-campaign --agency-id example-sheriff --source-url https://example.gov/records --checked-on 2026-01-15 --records-email records@example.gov`.

### The checked-on date is not a real date
Message opening: `checked_on must be a real YYYY-MM-DD date`
Source: [campaign_tool/kit.py](../campaign_tool/kit.py)
Meaning: The date is malformed or does not exist, for example a 31st of a 30-day month.
Next: Give the real day you read the official page: `python3 -m campaign_tool kit-verify-agency --directory ../my-campaign --agency-id example-sheriff --source-url https://example.gov/records --checked-on 2026-01-15 --records-url https://example.gov/records`.

### A portal vendor has no portal URL
Message opening: `portal_vendor needs a portal_url`
Source: [campaign_tool/kit.py](../campaign_tool/kit.py)
Meaning: In an import CSV, a row names a `portal_vendor` but leaves `portal_url` empty. `portal_vendor` is a CSV column, not a command option. All rows are refused together.
Next: Copy the CSV, fill `portal_url` from the agency's verified portal page (or empty both columns), and check it without writing: `python3 -m campaign_tool kit-verify-agency import --directory ../my-campaign --csv ../contacts.csv --check`. Run the same command without `--check` to import.

## kit-check

### The request draft is missing
Message opening: `draft missing or not a regular file`
Source: [campaign_tool/kit_check.py](../campaign_tool/kit_check.py)
Meaning: The row's `kit/requests/<agency_id>.md` does not exist, or it is a link or other special file, so it was not read.
Next: If it was never created, run `python3 -m campaign_tool kit --directory ../my-campaign`; it keeps drafts you already edited. If it is a symlink, leave it alone and ask who made it, then check again with `python3 -m campaign_tool kit-check --directory ../my-campaign`.

### The fee cap is still blank
Message opening: `fee cap missing`
Source: [campaign_tool/kit_check.py](../campaign_tool/kit_check.py)
Meaning: The draft still has the bracketed fee-cap placeholder, or the fee-cap sentence is gone. The cap is the amount your campaign decides it will accept before being asked again; the tool never decides it.
Next: Copy `kit/requests/<agency_id>.md`, replace the bracketed cap with the amount your campaign approved, then run `python3 -m campaign_tool kit-check --directory ../my-campaign`.

### The date range is still blank
Message opening: `date range missing`
Source: [campaign_tool/kit_check.py](../campaign_tool/kit_check.py)
Meaning: The draft's line `The date range for this request is FROM through TO.` is unfilled or was edited away.
Next: Copy the draft and replace every date-range placeholder with the dates your campaign wants: the one in the `Subject:` line and the one in the body sentence. Then run `python3 -m campaign_tool kit-check --directory ../my-campaign`.

### The recipient is not verified
Message opening: `recipient not verified`
Source: [campaign_tool/kit_check.py](../campaign_tool/kit_check.py)
Meaning: `kit/agencies.json` does not mark this agency `verified: true`. A suggested address from the seed is not a verified contact.
Next: Check the agency's official page yourself, then record what you saw: `python3 -m campaign_tool kit-verify-agency --directory ../my-campaign --agency-id example-sheriff --source-url https://example.gov/records --checked-on 2026-01-15 --records-email records@example.gov`. Then run `python3 -m campaign_tool kit-check --directory ../my-campaign`.

## kit-sent

### The request name is not allowed
Message opening: `--request must be 1-40 characters a-z, 0-9 or -, starting with a letter or digit`
Source: [campaign_tool/kit_track.py](../campaign_tool/kit_track.py)
Meaning: A further request to the same agency needs a short lowercase name.
Next: Pick a name such as `second-request`: `python3 -m campaign_tool kit-sent --directory ../my-campaign --agency example-sheriff --date 2026-01-15 --channel email --request second-request`.

### The request URL is not an https page
Message opening: `--request-url must start with https:// and name a host`
Source: [campaign_tool/kit_track.py](../campaign_tool/kit_track.py)
Meaning: The page of the request you filed must be a full `https://` address. It is only recorded, never fetched.
Next: Paste the real page address: `python3 -m campaign_tool kit-sent --directory ../my-campaign --agency example-sheriff --date 2026-01-15 --channel portal --request-url https://records.example.gov/requests/1234`.

### The portal was never verified
Message opening: `record the agency's portal first (kit-verify-agency --portal)`
Source: [campaign_tool/kit_track.py](../campaign_tool/kit_track.py)
Meaning: A portal filing is only recorded against a portal you verified on the agency's official page.
Next: Verify the portal, then record the filing: `python3 -m campaign_tool kit-verify-agency --directory ../my-campaign --agency-id example-sheriff --source-url https://example.gov/records --checked-on 2026-01-15 --portal https://records.example.gov`.

### A request URL was given for email or mail
Message opening: `--request-url is only for portal or muckrock requests`
Source: [campaign_tool/kit_track.py](../campaign_tool/kit_track.py)
Meaning: Email and mail filings have no web page to record.
Next: Leave `--request-url` out: `python3 -m campaign_tool kit-sent --directory ../my-campaign --agency example-sheriff --date 2026-01-15 --channel email`. Recording is not sending; send the request yourself first.

### The sent date is not a real date
Message opening: `--date must be a real YYYY-MM-DD date`
Source: [campaign_tool/kit_track.py](../campaign_tool/kit_track.py)
Meaning: The date is malformed or does not exist.
Next: Give the day you actually sent it, never a day you wish to claim: `python3 -m campaign_tool kit-sent --directory ../my-campaign --agency example-sheriff --date 2026-01-15 --channel mail`. The same opening is printed by `kit-response` for its own `--date`.

## kit-response

### Scope and disposition were not given together
Message opening: `--scope and --disposition go together: give both or neither`
Source: [campaign_tool/kit_track.py](../campaign_tool/kit_track.py)
Meaning: What the agency said about a scope is recorded as a pair. This is your record of their answer, not a legal finding.
Next: Give both: `python3 -m campaign_tool kit-response --directory ../my-campaign --agency example-sheriff --date 2026-02-01 --kind produced --scope alpr-policy --disposition produced`.

### A withdrawal has no reason
Message opening: `withdrawn needs --note T: why the campaign withdrew its own request`
Source: [campaign_tool/kit_track.py](../campaign_tool/kit_track.py)
Meaning: Withdrawn means your campaign withdrew its own request, and it needs a short plain reason. It is not for an agency closing a request.
Next: Only if that really happened: `python3 -m campaign_tool kit-response --directory ../my-campaign --agency example-sheriff --date 2026-02-01 --kind withdrawn --note "Campaign narrowed the request"`.

### There is no clarification request to answer
Message opening: `no open clarification request to answer`
Source: [campaign_tool/kit_track.py](../campaign_tool/kit_track.py)
Meaning: You recorded `clarified` but the request has no recorded, unanswered `clarification_requested` step.
Next: Look at what is actually recorded with `python3 -m campaign_tool kit-status --directory ../my-campaign`. Record `clarification_requested` first only if the agency really asked.

### A fee estimate has no amount
Message opening: `fee_estimate needs --amount USD`
Source: [campaign_tool/kit_track.py](../campaign_tool/kit_track.py)
Meaning: A fee estimate needs the amount the agency stated. Recording it pays nothing and approves nothing.
Next: `python3 -m campaign_tool kit-response --directory ../my-campaign --agency example-sheriff --date 2026-02-01 --kind fee_estimate --amount 245.50`.

### Records were attached to a step that is not a production
Message opening: `--record is only for produced steps`
Source: [campaign_tool/kit_track.py](../campaign_tool/kit_track.py)
Meaning: Ingested records are linked only on a `produced` step.
Next: Store the records first with `python3 -m campaign_tool ingest --directory ../my-campaign --dir ../production-001 --source-id example-production-001`, then link a prefix from `status --list` on a produced step: `python3 -m campaign_tool kit-response --directory ../my-campaign --agency example-sheriff --date 2026-02-01 --kind produced --record 0123456789ab`.

## sources add

### The hash prefix is too short or not hex
Message opening: `--sha256: give at least 12 lowercase hex characters of an ingested hash`
Source: [campaign_tool/sources_cmd.py](../campaign_tool/sources_cmd.py)
Meaning: `sources add` names a stored record by the start of its SHA-256 hash.
Next: List the stored prefixes with `python3 -m campaign_tool status --directory ../my-campaign --list`, copy one, then run `python3 -m campaign_tool sources add --directory ../my-campaign --sha256 0123456789ab --title "Example policy" --agency-id example-sheriff --received-at 2026-01-15 --fragment policy`.

### Nothing has been ingested yet
Message opening: `no ledger: run ingest first`
Source: [campaign_tool/sources_cmd.py](../campaign_tool/sources_cmd.py)
Meaning: The campaign has no intake ledger, so no hash can match.
Next: Store the record you actually received: `python3 -m campaign_tool ingest --directory ../my-campaign --file ../records/policy.pdf --source-id example-production-001`, then run `sources add` again.

### That hash is not in the ledger
Message opening: `not ingested: run ingest first`
Source: [campaign_tool/sources_cmd.py](../campaign_tool/sources_cmd.py)
Meaning: No stored record starts with that prefix.
Next: Compare against `python3 -m campaign_tool status --directory ../my-campaign --list`; if the record is not there, ingest it with `python3 -m campaign_tool ingest --directory ../my-campaign --file ../records/policy.pdf --source-id example-production-001`.

### There is no content folder
Message opening: `content/ is required`
Source: [campaign_tool/sources_cmd.py](../campaign_tool/sources_cmd.py)
Meaning: The source list lives under `content/`, which this campaign has not created.
Next: `python3 -m campaign_tool content init --directory ../my-campaign`, then repeat `sources add`. If `content/` exists elsewhere in your work, do not overwrite it.

## finding review

### The role is not one of the allowed roles
Message opening: `--role must be one of`
Source: [campaign_tool/findings.py](../campaign_tool/findings.py)
Meaning: A review is recorded for exactly one role, and the message lists them (factual, legal, privacy).
Next: `python3 -m campaign_tool finding review ../my-campaign/drafts/example-finding.json --directory ../my-campaign --role factual --decision approve --reviewer "Reviewer A" --rationale "Factual: summary matches page 1 of the record."`. Use the role you actually reviewed.

### The author tried to review their own finding
Message opening: `the author cannot review their own finding`
Source: [campaign_tool/findings.py](../campaign_tool/findings.py)
Meaning: Independent review is the point; the author's name cannot also be the reviewer.
Next: Ask a different real person to review and record under their own name. Check what is still missing with `python3 -m campaign_tool finding review-status ../my-campaign/drafts/example-finding.json --directory ../my-campaign`. Do not use another person's name; see the [review guide](REVIEW-GUIDE.md).

## finding publish

### Review blockers remain
Message opening: `review blockers:`
Source: [campaign_tool/findings.py](../campaign_tool/findings.py)
Meaning: Publishing needs current, independent approvals for every required role. Something is missing, stale because the draft changed, or rejected.
Next: Read the blockers with `python3 -m campaign_tool finding review-status ../my-campaign/drafts/example-finding.json --directory ../my-campaign`, get the needed independent reviews, and only then publish. See the [review guide](REVIEW-GUIDE.md).

### The author tried to approve publication
Message opening: `the author cannot approve publication of their own finding`
Source: [campaign_tool/findings.py](../campaign_tool/findings.py)
Meaning: `--approved-by` must be a named person who is not the author.
Next: Ask a different real person to approve: `python3 -m campaign_tool finding publish ../my-campaign/drafts/example-finding.json --directory ../my-campaign --approved-by "Approver B"`. This writes local content only; it does not deploy.

### Cited records are not registered and stored
Message opening: `cited records not registered and stored:`
Source: [campaign_tool/findings.py](../campaign_tool/findings.py)
Meaning: A record the finding cites is not in the source list, or its original is not stored.
Next: Ingest the record, register it, and re-run review if you edit the draft, because edits make earlier approvals stale. Check with `python3 -m campaign_tool evidence --directory ../my-campaign --check` and see the [findings guide](FINDINGS-GUIDE.md).

## build

### public is a symlink
Message opening: `public/ must not be a symlink`
Source: [campaign_tool/site.py](../campaign_tool/site.py)
Meaning: The build refuses to write through a link, which could overwrite files elsewhere.
Next: Do not delete the link or its target. Preserve both, move the link aside yourself after checking where it points, and let the build create a real folder: `python3 -m campaign_tool build --directory ../my-campaign`.

### Preview needs content
Message opening: `build --preview needs content/; the starter has nothing to compare`
Source: [campaign_tool/site.py](../campaign_tool/site.py)
Meaning: `--preview` compares a content-based site against `public/`, and there is no `content/`.
Next: Run `python3 -m campaign_tool build --directory ../my-campaign` for the starter, or create content with `python3 -m campaign_tool content init --directory ../my-campaign` if you meant to use it.

## content-check

### The site file is missing
Message opening: `content/site.json is required when content/ exists`
Source: [campaign_tool/content.py](../campaign_tool/content.py)
Meaning: `content/` exists but has no `site.json`, so `build` would refuse the whole tree. `content-check` lists this and every other problem and exits 3.
Next: Restore `content/site.json` from your own copy or version history. Do not delete `content/`. Then run `python3 -m campaign_tool content-check --directory ../my-campaign`.

## meetings

### Neither online nor a recorded response
Message opening: `meetings needs --online to query the agenda platform`
Source: [campaign_tool/cli.py](../campaign_tool/cli.py)
Meaning: `meetings` makes no network call unless you pass `--online`. Offline, it replays a saved response you provide. The rest of the message names the offline options for your platform.
Next: Replay a saved response, for example `python3 -m campaign_tool meetings --directory ../my-campaign --client examplecity --from-json ../responses/legistar-events.json`. Staging a meeting later is not verifying its agenda.

### The Legistar client name is not a slug
Message opening: `Legistar client must be a short lowercase slug (e.g. the city name)`
Source: [campaign_tool/meetings.py](../campaign_tool/meetings.py)
Meaning: `--client` is the short part of the agency's Legistar address, in lowercase.
Next: Copy it from the verified agenda address: `python3 -m campaign_tool meetings --directory ../my-campaign --client examplecity --from-json ../responses/legistar-events.json`.

## meetings-stage

### No meetings have been imported
Message opening: ``kit/meetings.json not found; run `meetings` first``
Source: [campaign_tool/meetings_manual.py](../campaign_tool/meetings_manual.py)
Meaning: There is no meeting list to stage from.
Next: Import one first: `python3 -m campaign_tool meetings --directory ../my-campaign --client examplecity --from-json ../responses/legistar-events.json`. For one you enter by hand, see the [meetings guide](MEETINGS-GUIDE.md).

### The meeting list has no time zones
Message opening: `kit/meetings.json predates time zones; run meetings again`
Source: [campaign_tool/meeting_stage.py](../campaign_tool/meeting_stage.py)
Meaning: The saved list was written by an older version and has no time zone, so start times would be ambiguous.
Next: Import again, with the correct zone: `python3 -m campaign_tool meetings --directory ../my-campaign --client examplecity --from-json ../responses/legistar-events.json --tz America/Los_Angeles`.

### There is no content folder to stage into
Message opening: `content/ is required (the site reads content/meetings/); create content/site.json first`
Source: [campaign_tool/meeting_stage.py](../campaign_tool/meeting_stage.py)
Meaning: Staged meetings are written under `content/meetings/`.
Next: `python3 -m campaign_tool content init --directory ../my-campaign`, then `python3 -m campaign_tool meetings-stage --directory ../my-campaign --id example-meeting-1`. Staging is not verifying the agenda; read the agency's own agenda yourself.

## backup

### The folder is not a campaign
Message opening: `campaign.json not found; is this a campaign directory?`
Source: [campaign_tool/backup.py](../campaign_tool/backup.py)
Meaning: The `--directory` you gave has no `campaign.json`. Nothing was written.
Next: Check the path with `python3 -m campaign_tool doctor --directory ../my-campaign`, then back up the real campaign folder: `python3 -m campaign_tool backup --directory ../my-campaign --out ../backups/example.tar`.

### The destination folder is not private
Message opening: `backup: --into: must be an existing real folder that group and others cannot write`
Source: [campaign_tool/backup.py](../campaign_tool/backup.py)
Meaning: The folder is missing, is a symlink, or other users can write to it. A backup holds private records.
Next: Make a new real folder only you can write, such as `mkdir -m 700 ../private-backups`, and use it: `python3 -m campaign_tool backup --directory ../my-campaign --into ../private-backups`. Do not loosen any existing permissions to make this pass.

## restore

### The archive failed verification
Message opening: `verification failed:`
Source: [campaign_tool/backup.py](../campaign_tool/backup.py)
Meaning: A member of the archive does not match its manifest: corrupted, truncated, missing or unsafe. Nothing was restored.
Next: Keep the failed archive as evidence and do not edit it. Look at the details with `python3 -m campaign_tool verify --file ../backups/example.tar`, then restore from a known-good backup instead: `python3 -m campaign_tool restore --file ../backups/known-good.tar --directory ../restored-campaign`.

### The restore target is a symlink
Message opening: `restore target must not be a symlink`
Source: [campaign_tool/backup.py](../campaign_tool/backup.py)
Meaning: Restore refuses to write through a link.
Next: Leave the link as it is. Use a new, empty, real folder: `python3 -m campaign_tool restore --file ../backups/example.tar --directory ../restored-campaign`. Never force an overwrite of an existing campaign.

## Related guides

- [Records-request guide](REQUESTS-GUIDE.md)
- [Findings guide](FINDINGS-GUIDE.md)
- [Review guide](REVIEW-GUIDE.md)
- [Meetings guide](MEETINGS-GUIDE.md)
- [Backup, verify and restore](BACKUP.md)
- [Agency discovery](AGENCY-DISCOVERY.md)

If a message is not here, copy its first words into a search of the `campaign_tool/` folder to find where it comes from. This page lists a few dozen common refusals, not every message.
