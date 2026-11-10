# Meetings guide: from finding a meeting to posting the outcome draft

This guide follows one public meeting through the organizer's work, in the order you do it: find the meeting (or add it by hand), stage it, verify how the public may take part, read the comment kit, print a pack, draft the announcement, record the comment you submitted, record the outcome and draft the outcome post. Every task names its command as it exists on `main` and links the section of [MEETINGS.md](MEETINGS.md) that stays the reference; nothing was moved out of it. Run `python3 -m campaign_tool COMMAND --help` for the exact flags on your checkout.

**No command in this guide sends, submits or posts anything.** You send the comment yourself, outside the tool, and you read any draft before it goes anywhere. Recording what you did is a private note, not an action. Putting the site on the internet is a separate, approved step that this guide does not cover ([PUBLICATION.md](PUBLICATION.md)).

## 1. Before you start

- **A prepared campaign folder `$D`** made by `init` and `content init`, outside this repository, with at least one **published finding** (the author's side is the [findings guide](FINDINGS-GUIDE.md)). Only published findings are ever cited; with none, the kit says so and cites the records request instead.
- **Facts come from the agency's own pages.** A keyword hit on an agenda is a lead, not a decision. The tool never guesses a deadline, a time limit, a vote or an outcome: a fact you do not supply is simply absent from the output.
- **Privacy.** Use team labels and public names of officials only. Do not put a home address, phone number or personal email in any flag, file or reason.

| What | Where it lands | Visible on the site? |
|---|---|---|
| Scan result (`meetings`) | `$D/kit/meetings.json` (private) | no |
| Hand-entered meeting (`meetings-add`) | `$D/kit/meetings-manual.json` (private) | no |
| Staged meeting and its comment kit (`meetings-stage`) | `$D/content/meetings/ID.json` | only after a separate `build` and publication |
| Meeting pack (`meeting-pack`) | `$D/kit/meeting-packs/ID/` (private) | no |
| Social drafts (`social`) | `$D/kit/social/meetings/` and `.../outcomes/` (private) | no, and never posted |
| Comment receipt (`meetings-comment-sent`) | `$D/kit/comments-sent.json` (private) | no |
| Outcome, votes, recording (`meetings-outcome`) | `$D/content/meetings/ID.json` | only after a separate `build` and publication |

## 2. Find a meeting, or add one by hand

**Supported agenda platforms on `main`** (`meetings --help` lists them): Legistar, CivicClerk, Granicus, CivicPlus AgendaCenter and a plain iCalendar feed. Choose one with `--platform` (default `legistar`); there is no other scan flag. Everything below writes only `kit/meetings.json` and prints the matches, which are keyword hits to check against the posted agenda.

| Platform | Command (online use is yours to run; it is the only network use) | Reference |
|---|---|---|
| Legistar | `meetings --directory $D --client SLUG --online` | [Legistar import](MEETINGS.md#legistar-import-cli) |
| CivicClerk | `meetings --directory $D --platform civicclerk --client SLUG --online` | [CivicClerk import](MEETINGS.md#civicclerk-import-cli) |
| Granicus | `meetings --directory $D --platform granicus --client NAME.granicus.com --view-id N --online` | [Granicus import](MEETINGS.md#granicus-import-cli) |
| CivicPlus AgendaCenter | `meetings --directory $D --platform agendacenter --client HOST --online` | [AgendaCenter import](MEETINGS.md#civicplus-agendacenter-import-cli) |
| iCalendar feed | `meetings --directory $D --platform ics --feed-url https://... --body "City Council" --online` | [iCalendar import](MEETINGS.md#icalendar-agenda-feed-import-cli) |

Offline, replay a saved response instead of `--online`: `--from-json` (Legistar, CivicClerk), `--from-xml` (Granicus, AgendaCenter) or `--from-ics`. The repository ships a synthetic Legistar replay:

```
python3 -m campaign_tool meetings --directory $D --client examplecity --from-json tests/fixtures/legistar-events.json
```

The fixture's dates are fixed in 2026-10, so those meetings may already be in the past; a meeting you will actually attend needs a fresh scan or a hand entry. Several bodies in one run: [Several governing bodies](MEETINGS.md#several-governing-bodies).

**Body on none of those platforms?** Enter it by hand after reading its official agenda page:

```
python3 -m campaign_tool meetings-add --directory $D --id manual-SLUG --body TEXT --agency-id ID \
  --starts-at 2026-11-12T18:00:00-08:00 --agenda-url https://... --item-title TEXT \
  --source-url https://... --checked-on YYYY-MM-DD
```

The id must start `manual-`; the start needs an explicit UTC offset and must not be in the past; `--checked-on` is the day you read the agenda. Nothing is fetched. `meetings-add --replace --on DATE --reason TEXT` (with all the fields) corrects an entry and `meetings-add --id manual-SLUG --cancel --on DATE --reason TEXT` cancels it. **A correction or cancel changes only the private file.** It does not change a page you already staged: re-run `meetings-stage --replace` for a new date, and review the staged meeting's `status` yourself after a cancel. Reference: [Organizer-entered meetings](MEETINGS.md#organizer-entered-meetings-meetings-add).

## 3. Check the agenda, then stage

Open the agency's posted agenda. Confirm the date, time, time zone, agency and agenda item. Then stage:

```
python3 -m campaign_tool meetings-stage --directory $D --id ID --agency-id AGENCY \
  --finding FINDING_ID [--finding ...] [--participation FILE] [--replace]
```

`--finding` is repeatable (1 to 3 published finding ids, in the order you give them); without it the tool chooses up to three. `--replace` is needed to overwrite an already staged meeting, and a cancelled meeting is refused. A staged hand-entered meeting uses its saved agency. This command writes `content/meetings/ID.json` including the **comment kit**: there is no separate "comment kit" or "spoken version" command. A meeting with no finding says so in the kit; a meeting with no participation file says only that participation was not verified. Reference: [Comment kit](MEETINGS.md#comment-kit).

## 4. Verify participation

Only what you checked on the agency's own page goes in. Write it to a JSON file and pass it as `--participation FILE` when you stage (or re-stage with `--replace`):

```json
{"in_person": true,
 "remote": {"available": true, "how": "Video link on the posted agenda"},
 "written_comment": {"how": "Email the clerk's office using the address on the posted agenda",
                     "deadline": "2026-11-12T12:00:00-08:00"},
 "speaker_minutes": 3,
 "source_url": "https://example.invalid/synthetic-council/agenda",
 "verified_by": "Synthetic Organizer",
 "verified_at": "2026-11-05"}
```

`source_url`, `verified_by` and `verified_at` are required. The deadline needs a UTC offset and must be before the start. `speaker_minutes` is a whole number from 1 to 10 and is optional; `null` or an absent key means "not stated" and prints nothing. The tool assumes no limit and no deadline. Reference: [Participation (organizer-verified)](MEETINGS.md#participation-organizer-verified).

## 5. Read the comment kit and make a pack

Open `content/meetings/ID.json` and read `comment_kit_md`. It holds a two-minute comment, a **spoken** section sized to your recorded `speaker_minutes` (whole finding bullets are dropped from the end to fit; no limit recorded means no spoken section), three asks, a **written** letter when a written-comment method is verified (placeholders `[name]` and `[city]` are yours to fill in; no recipient or address is added) and the sources. Edit nothing in the kit by hand that you want to survive a re-stage; keep your edited copy elsewhere. A volunteer plan for who covers which point is optional: [Private speaker plan](MEETINGS.md#private-speaker-plan).

```
python3 -m campaign_tool meeting-pack --directory $D --id ID [--replace] [--allow-past] [--max-findings N]
python3 -m campaign_tool meeting-pack --directory $D --upcoming [--within-days N]
```

The pack is a private `handout.html`, `comment.md` and `sources.csv` under `kit/meeting-packs/ID/`. A cancelled meeting or a stale kit (a cited finding was corrected after staging) stops: re-stage first with `meetings-stage --replace`. `--replace` regenerates an existing pack, `--allow-past` archives a past meeting, and `--upcoming` builds every staged meeting in the next N days (1 to 90, default 14, a different window from the social drafts). The pack lists published findings of the meeting's own agency, so a finding filed under another agency can be cited in the kit and still be absent from the handout. Review the print preview before you hand it out. Reference: [Printing a meeting pack](MEETINGS.md#printing-a-meeting-pack).

## 6. Draft the announcement (never posted)

```
python3 -m campaign_tool social --directory $D --meetings --platform x [--platform bluesky] [--within-days N]
```

Writes one private draft per **scheduled** meeting that starts within N days (1 to 60, default 14) and has verified participation, into `kit/social/meetings/`. `--platform` is repeated once per platform (`x`, `bluesky`, `mastodon`), each with its own length rule. A draft you edited is kept and the new text goes to a `.suggested` folder. `--meetings` cannot be combined with `--check` (which only reads finding drafts) or with `--outcomes`. Read any `review-required` line the command prints. Reference: [Social drafts](MEETINGS.md#social-drafts-never-posted).

## 7. Submit by hand, then record it

Send the written comment yourself through the channel the agency names (or speak at the meeting). Afterwards:

```
python3 -m campaign_tool meetings-comment-sent --directory $D --id ID --on YYYY-MM-DD \
  --channel email|portal|mail|in_person [--file LETTER] [--replace]
```

This is a private receipt in `kit/comments-sent.json` and prints `Recorded only. Nothing was sent.` It is refused for an unknown or cancelled meeting, a date in the future or after the meeting day, and a second record unless `--replace`. `--file` keeps only the letter's SHA-256 and size, never its text or path. After it, `today --directory $D --json` no longer lists that meeting's `comment_deadline`; other meeting work stays listed. The receipt is not an outcome and is not public. Reference: [Recording a comment you submitted](MEETINGS.md#recording-a-comment-you-submitted).

## 8. Record the outcome, votes and recording

After the meeting, read the result on the agency's own record (minutes, vote page or video) and record it:

```
python3 -m campaign_tool meetings-outcome --directory $D --id ID \
  --result approved|denied|continued|amended|no_action|informational \
  --on YYYY-MM-DD --source https://... --verified-by NAME \
  [--vote TEXT] [--votes FILE] [--recording-url URL [--recording-start H:MM:SS]] [--replace]
```

`--on` runs from the meeting day to today. `--vote` is the vote line exactly as the record states it; it is never computed. `--votes FILE` is a CSV with the header `name,vote` (`yes`, `no`, `abstain`, `absent`, `recused`), and each name must already be listed for the meeting's agency in `content/officials.json` (see [SITE-CONTENT.md](SITE-CONTENT.md)); this guide adds no officials command. A recording is only a link and goes in with the outcome (never alone), at most `12:00:00` for the start. `--replace` drops votes and recording unless you pass them again. The command prints `Recorded only. Nothing was published.` Continued items, staff directions, counts and approved minutes have their own flags: see [Recording an outcome](MEETINGS.md#recording-an-outcome).

## 9. Draft the outcome post (never posted)

```
python3 -m campaign_tool social --directory $D --outcomes [--within-days N] --platform x
```

One private draft per meeting whose recorded outcome date falls in the last N calendar days (1 to 60, default 14, today included), in `kit/social/outcomes/`. It quotes the result token, the vote line you recorded and the official record link exactly, and adds the meeting-page link only when `base_url` is set. A meeting with no outcome gets no draft. If the fixed text cannot fit a platform the run is refused rather than shortening what you recorded. Optional read-only check of staged data: `content-check --directory $D`. Reference: [Social drafts for outcomes](PUBLICATION.md#social-drafts-for-outcomes-never-posted).

## Meeting inventory (meetings-status)

```
python3 -m campaign_tool meetings-status --directory $D [--as-of YYYY-MM-DD] [--json]
```

A private, read-only list of every staged meeting in `content/meetings/`, one line each: upcoming first (soonest first, today included), then past (newest first), each with its body, its start exactly as stored (with its own UTC offset, never converted), and its status: `scheduled`, `cancelled` or `continued` (a recorded `continued` outcome; nothing is stored as a third status). Then the fields, as tokens only:

- `participation` `verified` when a participation object from task 4 is staged, else `not_verified`.
- `kit` `current`, `stale` (a cited finding has a newer correction; re-stage it), `missing` (no comment kit) or `unknown` (a finding file could not be read, so it cannot say current). `current` compares only with your local findings; it says nothing about the published site.
- `sent` `recorded` only when `kit/comments-sent.json` holds your receipt from task 7; a published comment or an outcome's written count is not a receipt.
- `speakers` `present` when a speaker plan file exists (even an empty one), else `absent`. It is not coverage, consent or attendance.
- `outcome` the recorded result token or `not_recorded`; `minutes` and `votes` `recorded` or `not_recorded`.
- `next` the command for the **first** missing step only, as a template: upcoming meetings verify participation, re-stage a missing or stale kit (pass your verified participation file and the same `--finding`, `--ask` and `--cosign` choices again), record a written comment you already submitted by hand (`CHANNEL` and the date are yours), then optionally look at speakers; past meetings record the outcome, then the approved minutes, then optionally review votes (`meetings-outcome --help`; never a `--replace` that would drop recorded fields). Cancelled and complete meetings show `-`. Capital words are facts you supply from the official source. Nothing is run for you.

After the staged meetings come `unstaged` lines: entries of `kit/meetings.json` and `kit/meetings-manual.json` not yet staged, with a `meetings-stage` command for scheduled ones and none for cancelled ones.

`--as-of` only moves the upcoming/past line; everything recorded stays visible and is still checked against the real date. Comment text, speaker labels, contact details, verifier names, vote lines and member names are never printed. A file that cannot be read safely (bad JSON, wrong shape, too large, a symlink, a folder or pipe where a file belongs, an id that differs from its file name or appears twice) is its own `unreadable | PATH | review input locally` line, never hides other meetings, turns the states it would have decided into `unknown` (`next=repair_input`), and makes the command exit 1; otherwise it exits 0, even with work left. In text mode `\` and `|` and control characters are escaped so each meeting stays one line; `--json` gives the exact values. The command writes, creates, sends and fetches nothing, and its output is private: do not paste it anywhere public. It is an inventory, not proof that anything was submitted or published.

## 10. Worked example (all synthetic)

Invented body "Synthetic Town Council" (agency `ca-cedar-city-council`), you as "Synthetic Organizer", officials Avery, Blake and Casey Example, and only `example.invalid` links. Copy `examples/fictional-campaign` to a disposable `$D` (it has the published finding `fnd_0000000000000001`). Meeting id `ID` is `manual-synthetic-council-2026-11`. The dates below are one consistent set assuming you start on **2026-11-05**; if you start later, move every date forward in the same order (a start in the past is refused). The fixture replay in step 2 is an alternative way to find a meeting and uses different ids.

| Phase | Date | Fact |
|---|---|---|
| Before the meeting | 2026-11-05 | `--checked-on`, `verified_at` |
| Before the meeting | 2026-11-12 18:00 (-08:00) | meeting start; written deadline 2026-11-12 12:00 (-08:00) |
| After you submit by hand | 2026-11-10 | `meetings-comment-sent --on` (not later than that day, not before you really sent it) |
| After the meeting | 2026-11-12 | `meetings-outcome --on`; run it on or after 2026-11-12, here 2026-11-13 |

**Before the meeting.** Save the participation JSON from task 4 as `participation.json`, then:

```
python3 -m campaign_tool meetings-add --directory $D --id manual-synthetic-council-2026-11 \
  --body "Synthetic Town Council" --agency-id ca-cedar-city-council \
  --starts-at 2026-11-12T18:00:00-08:00 --agenda-url https://example.invalid/synthetic-council/agenda \
  --item-title "Item 4: Automated license plate reader agreement" \
  --source-url https://example.invalid/synthetic-council/agenda --checked-on 2026-11-05
python3 -m campaign_tool meetings-stage --directory $D --id manual-synthetic-council-2026-11 \
  --agency-id ca-cedar-city-council --finding fnd_0000000000000001 --participation participation.json
python3 -m campaign_tool meeting-pack --directory $D --id manual-synthetic-council-2026-11
python3 -m campaign_tool social --directory $D --meetings --platform x
```

You get the staged kit with a 3-minute spoken section, a private pack (with 0 findings in the handout, because the finding belongs to the police agency; the kit cites it), and one draft in `kit/social/meetings/x/`.

**After you submit by hand** (you emailed the clerk yourself):

```
python3 -m campaign_tool meetings-comment-sent --directory $D --id manual-synthetic-council-2026-11 --on 2026-11-10 --channel email
```

**After the meeting**, with the minutes in hand. First add the voting members for the body to `$D/content/officials.json` in the shape described in [SITE-CONTENT.md](SITE-CONTENT.md) (`agency_id` `ca-cedar-city-council`, officials Avery, Blake and Casey Example), and save `votes.csv`:

```
name,vote
Avery Example,yes
Blake Example,yes
Casey Example,no
```

```
python3 -m campaign_tool meetings-outcome --directory $D --id manual-synthetic-council-2026-11 \
  --result approved --on 2026-11-12 --source https://example.invalid/synthetic-council/minutes \
  --verified-by "Synthetic Organizer" --vote 2-1 --votes votes.csv \
  --recording-url https://example.invalid/synthetic-council/video --recording-start 1:12:30
python3 -m campaign_tool social --directory $D --outcomes --platform x
```

The outcome draft reads `Synthetic Town Council — 2026-11-12: Item 4: Automated license plate reader agreement. Result: approved. Vote: 2-1. Official record: https://example.invalid/synthetic-council/minutes` followed by the meeting-page link when `base_url` is set. The `2-1` is the text you typed; the tool did not count the CSV. Nothing was sent, posted or published at any step.
