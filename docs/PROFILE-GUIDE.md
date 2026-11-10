# Agency profile research guide

For the volunteer who researches one agency at a time and records what the agency's own documents say. The guide goes in
the order you will do the work: find the source, record it, read it, and come back to it later. Each task names the
command or the `content/agencies.json` field as it exists today and links the reference section that holds every rule
and every refusal message. This guide links those sections; it does not repeat them.

Nothing here is legal advice. Everything in this guide runs offline: the tool never fetches a source, never contacts an
agency, a vendor portal or an archive, and sends and publishes nothing.

## Before you start

**What the tool never does.** It fetches no source, opens no portal and submits nothing to an archive. It does not confirm
that a link is genuine, current or complete, and it draws no legal conclusion: a posted policy is not a finding about
compliance, "not found" is not a finding that anything is missing or required, and a vendor figure is not a verified
transfer or an ownership fact. You read the source; the tool checks that what you typed has the right shape and shows it
with its source and date.

**What counts as an official source.** The agency's or governing body's own published record: its policy page or PDF, the
signed agreement, its budget, the minutes or resolution of its governing body, or its own official portal. Check by hand
that the page is the agency's (not a lookalike or a third-party copy) and that the document covers the agency and period
you are recording. A vendor transparency portal is a *vendor report* about the agency, not an independent official
determination: record it as the portal showed it, under the transparency and sharing tasks. A news story or a
neighbour's blog is a *lead* to find the primary record, never a replacement for it. The tool uses no host-name test to
decide that a source is official; that judgment is yours.

**What `checked_on` means.** The day *you* last re-read the linked source and confirmed your transcription against it. It
is not the date the document was issued, the date an archive copy was captured (that is `archived_on`), the date the
tool ran, or the date anyone reviewed it legally. Do not move a `checked_on` forward unless you actually re-read the
source that day. A field you have not checked is *absent*, which is different from a recorded zero or a recorded "not
found": absent means "not looked at yet", never "none".

**How old dates are named today.** `doctor --stale-days N` names inputs dated more than N days ago, and it covers
meeting data, map data, the agency seed, the law package's source dates, each dated object in the public
`content/agencies.json` profile including each `reports[i]` check (named `content/agencies.json:<agency_id>.<field>`,
see LAUNCH.md), each official in `content/officials.json`, each schedule in `content/body-schedules.json` and each
`public_copy` check in `content/sources.json`. Historical records (agency actions, statements, coverage and elsewhere
decisions) are left out on purpose: they record a past event, not a present fact to re-check. It only reads and
names; it never fetches a source or changes a date. Re-reading a policy or contract stays your manual step; `content
agency-profile list` shows every recorded profile date oldest first and `content agency-profile recheck` records the
day you re-read one of them (task 12 below). `doctor` also reports from a profile `contracts_ending`: contracts whose `end` is within
90 days (see the contract term section of [AGENCY-CARDS.md](AGENCY-CARDS.md#agency-contract-term)).

## Practice setup

Work in a copy of the fictional example campaign so you can try every step and break nothing. `$CAMPAIGN` is the copy.

```sh
python3 -m campaign_tool content-check --directory $CAMPAIGN
```

Add this synthetic agency to the list in `$CAMPAIGN/content/agencies.json` (one more element of the top-level list). It is
verified, has a Flock transparency slug (needed for the transparency task) and an official-looking but fictional home
page. Every task below merges one block into its `profile` object; nothing replaces the whole file.

```json
{
  "agency_id": "ca-example-profile-pd",
  "slug": "example-profile",
  "name": "Example Profile Police Department (fictional)",
  "kind": "police",
  "jurisdiction_name": "Cedar",
  "records_url": "https://example.invalid/example-profile/records",
  "records_email": null,
  "portal": {"vendor": "none", "url": null},
  "verified": true,
  "verification": {"source_url": "https://example.invalid/example-profile/contact", "checked_on": "2026-10-20",
                   "fields": ["records_url"]},
  "flock_transparency_slug": "example-profile-pd",
  "profile": {"status": "Records requested", "summary": "Synthetic entry used to practise the profile steps."}
}
```

`content-check` exits 0 when everything it checks is well formed and 3 when it lists problems, each naming
`content/agencies.json[n].profile.<field>`. Run it after every task.

## 1. Identify the agency and its official source

Pick the agency's `agency_id` from `content/agencies.json` (or add the agency first, see
[AGENCY-DISCOVERY.md](AGENCY-DISCOVERY.md)). Open the agency's own website and find where it publishes policies,
agreements, budgets and meeting records. Note the page address of each; the next tasks ask for the address of the
document *and*, for a policy, of the page where you found it. Reference:
[SITE-CONTENT.md, agencies, cards and the picker](SITE-CONTENT.md#agencies-cards-and-the-picker) (contact verification
by `kit-verify-agency`, which is separate from everything below).

## 2. Find and record the ALPR policy

Field: `profile.alpr_policy` = `{"url", "source_url", "checked_on"}`, exactly those three keys. `url` is the policy
document, `source_url` the agency page where you found it (both https), `checked_on` the day you read it. Linking a policy
says only that it was posted. Merge into `profile`:

```json
{"alpr_policy": {"url": "https://example.invalid/example-profile/alpr-policy.pdf",
                 "source_url": "https://example.invalid/example-profile/policies", "checked_on": "2026-10-21"}}
```

Reference: [SITE-CONTENT.md, agencies, cards and the picker](SITE-CONTENT.md#agencies-cards-and-the-picker) (the
`alpr_policy` paragraph).

## 3. Check what the posted policy covers

Field: `profile.policy_check` = `{"checked_on", "policy_url", "elements"}`. Read the posted policy against the draft
checklist of seven elements and give each one a `status` of `addressed` (optionally with the `page`), `not_found` or
`unclear`. `policy_url` must equal `alpr_policy.url`; every one of the seven ids is present and no other; `page` only with
`addressed`; `checked_on` is not before `alpr_policy.checked_on`. `not_found` describes your reading of the document, not
a violation and not a claim that no policy exists. Merge into `profile`:

```json
{"policy_check": {"checked_on": "2026-10-22", "policy_url": "https://example.invalid/example-profile/alpr-policy.pdf",
  "elements": {
    "authorized_purposes": {"status": "addressed", "page": 1},
    "authorized_users_training": {"status": "addressed", "page": 2},
    "security_monitoring": {"status": "unclear"},
    "sharing_restrictions": {"status": "addressed", "page": 2},
    "custodian_title": {"status": "not_found"},
    "accuracy_correction": {"status": "not_found"},
    "retention_destruction": {"status": "addressed", "page": 3}}}}
```

Reference: [SITE-CONTENT.md, agencies, cards and the picker](SITE-CONTENT.md#agencies-cards-and-the-picker) (the
`policy_check` paragraph).

## 4. Record the contract term and the stated cost

Field: `profile.contract` = `{vendor, start, end, renewal_note?, source_url, checked_on}` and optionally
`cost` = `{amount_usd, period, as_stated_in}`. Copy the dates and the vendor from the signed agreement; `source_url` is the
agreement itself and `checked_on` your reading date. `amount_usd` is a *string* (`"12345.60"`, never a JSON number),
`period` is `annual`, `total` or `one_time` exactly as the document states it, and `as_stated_in` is the document's title.
Do not total several years, divide by cameras or write a renewal date the document does not give. Merge into `profile`:

```json
{"contract": {"vendor": "Synthetic vendor", "start": "2025-01-01", "end": "2027-12-31",
              "renewal_note": "Renews yearly unless either side gives notice.",
              "source_url": "https://example.invalid/example-profile/agreement.pdf", "checked_on": "2026-10-23",
              "cost": {"amount_usd": "12345.60", "period": "annual", "as_stated_in": "Example services agreement"}}}
```

Reference: [AGENCY-CARDS.md, agency contract term](AGENCY-CARDS.md#agency-contract-term) and
[SITE-CONTENT.md, contract cost as stated](SITE-CONTENT.md#contract-cost-as-stated-profilecontractcost).

## 5. Read the contract against the checklist

Field: `profile.contract_check` = `{"checked_on", "contract_url", "elements"}`. Give each of the eight checklist ids a
`status` (`addressed`, `not_found`, `unclear`), with an optional `page` (any status) and a short plain-text `note`.
`contract_url` must equal `contract.source_url` exactly, and `checked_on` may not be before `contract.checked_on`. This
page rule is looser than the policy check's. "Not found" describes your reading of the linked document only. Merge into
`profile`:

```json
{"contract_check": {"checked_on": "2026-10-24", "contract_url": "https://example.invalid/example-profile/agreement.pdf",
  "elements": {
    "term_renewal": {"status": "addressed", "page": 1},
    "price_changes": {"status": "addressed", "page": 4, "note": "Annual price schedule on this page."},
    "data_ownership": {"status": "unclear", "page": 6},
    "retention": {"status": "not_found"},
    "sharing_network": {"status": "addressed", "page": 7},
    "vendor_data_use": {"status": "unclear"},
    "audit_search_logs": {"status": "not_found"},
    "termination_data_exit": {"status": "addressed", "page": 9}}}}
```

Reference: [SITE-CONTENT.md, agencies, cards and the picker](SITE-CONTENT.md#agencies-cards-and-the-picker) (the
`contract_check` paragraph).

## 6. Record how the agency is paid for

Field: `profile.funding`, a list of 1 to 10 entries. Each entry names its own document: `kind` (`grant`, `general_fund`,
`asset_forfeiture`, `donation`, `other`), `name` (the program or donor exactly as the document writes it; check that it
is not a person), `as_stated_in`, `source_url` and `checked_on`, with optional `amount_usd` plus `period` (together) and
`fiscal_year`. Entries are never added up, ranked or compared. Merge into `profile`:

```json
{"funding": [{"kind": "grant", "name": "Synthetic Safety Program", "as_stated_in": "Synthetic grant award letter",
              "source_url": "https://example.invalid/example-profile/award", "checked_on": "2026-10-25",
              "amount_usd": "50000", "period": "total", "fiscal_year": "2026"}]}
```

Reference: [SITE-CONTENT.md, how an agency is paid for](SITE-CONTENT.md#how-an-agency-is-paid-for-as-stated-profilefunding).

## 7. Snapshot the vendor transparency portal

Field: `profile.transparency`, a list of at most 24 snapshots, newest first. The agency must be `verified: true` with a
`flock_transparency_slug`, and each `source_url` is exactly `https://transparency.flocksafety.com/<slug>`. Copy the numbers
as the portal showed them on `checked_on`; leave out one the portal did not show (absent is *not shown*, a typed `0` means
the portal showed zero). Add a new snapshot at the top and keep older ones. The portal is the vendor's report; do not call
its numbers verified transfers or confirmed ownership. Merge into `profile`:

```json
{"transparency": [{"checked_on": "2026-10-26", "source_url": "https://transparency.flocksafety.com/example-profile-pd",
                   "cameras": 12, "retention_days": 30, "searches_last_30_days": 480}]}
```

Reference: [SITE-CONTENT.md, agencies, cards and the picker](SITE-CONTENT.md#agencies-cards-and-the-picker) (the
`profile.transparency` display, and the card's glance table) and
[AGENCY-CARDS.md, transparency-portal snapshots](AGENCY-CARDS.md#transparency-portal-snapshots).

## 8. Record who the portal says receives data

Field: `profile.sharing`, written by a command, not by hand. Save the receiving agencies the portal lists as a UTF-8 CSV
with the header `name,state,kind` (`kind` is `local`, `state`, `federal`, `private` or `unknown`; `state` a two-letter
code, or `US` for a federal agency). The CSV for this practice run is:

```text
name,state,kind
Neighbour Police Department (fictional),CA,local
Example State Patrol (fictional),NV,state
```

```sh
python3 -m campaign_tool content agency-sharing --directory $CAMPAIGN --agency ca-example-profile-pd --csv $CSV --checked-on 2026-10-27 --source-url https://transparency.flocksafety.com/example-profile-pd
```

Every bad row is named by its line and nothing is written if any is bad; an agency that already has a list needs
`--replace`. The list records what the portal names; a listing does not establish that a transfer occurred. Reference:
[SITE-CONTENT.md, agencies, cards and the picker](SITE-CONTENT.md#agencies-cards-and-the-picker) (the `sharing`
paragraphs, which also describe `sharing.html`).

## 9. Record reports the agency has published

Field: `profile.reports`, a list of 1 to 20 entries. Read each report on the agency's own site and give its `title` as
published, `kind` (`annual_report`, `audit_summary`, `usage_statistics`, `surveillance_report`, `other`), `period_start`
before `period_end`, `source_url` and `checked_on`, plus `published_on` only if the report states it. "None on record"
means you have recorded none, not that none is required or missing. Merge into `profile`:

```json
{"reports": [{"title": "2025 Annual ALPR Report", "kind": "annual_report", "period_start": "2025-01-01",
              "period_end": "2025-12-31", "source_url": "https://example.invalid/example-profile/reports/2025",
              "checked_on": "2026-10-28", "published_on": "2026-02-01"}]}
```

Reference: [SITE-CONTENT.md, reports the agency has published](SITE-CONTENT.md#reports-the-agency-has-published-about-its-alpr-use-profilereports).

## 10. Record agency actions outside a meeting

File: `content/agency-actions.json`, written by command. One dated action at a time, in the agency's own words
(`--as-stated`, up to 200 characters), with the official source and the day you checked it; `--on` may not be after
`--checked-on`. `add` has no update form; `remove --index N` takes the 1-based position in the file, which `add` prints.
Meeting decisions stay in the meetings workflow, not here.

```sh
python3 -m campaign_tool content agency-action add --directory $CAMPAIGN --agency ca-example-profile-pd --on 2026-09-01 --kind policy_posted --as-stated "Use policy posted on the department website" --source-url https://example.invalid/example-profile/policies --checked-on 2026-10-29
```

Reference: [SITE-CONTENT.md, agency actions outside a meeting](SITE-CONTENT.md#agency-actions-outside-a-meeting-contentagency-actionsjson).

## 11. Keep an archived copy of a source

Fields: `archived_url` and `archived_on`, added together to `alpr_policy`, `contract`, a `transparency` snapshot, a
`funding` entry or a `reports` entry, or to none. You save or find the copy yourself in a browser; the tool submits
nothing. The host must be `web.archive.org`, `archive.ph` or `archive.today`; a Wayback address needs a real 14-digit
timestamp and `archived_on` must be that timestamp's date. `archived_on` is the day of the *copy*, never `checked_on`;
an archive link does not show the copy is authentic or current. Merge into `profile.alpr_policy` (add the two keys to the
object from task 2):

```json
{"alpr_policy": {"url": "https://example.invalid/example-profile/alpr-policy.pdf",
                 "source_url": "https://example.invalid/example-profile/policies", "checked_on": "2026-10-21",
                 "archived_url": "https://web.archive.org/web/20261020120000/https://example.invalid/example-profile/alpr-policy.pdf",
                 "archived_on": "2026-10-20"}}
```

Reference: [SITE-CONTENT.md, archived copies of agency sources](SITE-CONTENT.md#archived-copies-of-agency-sources-optional).

## 12. Check, build locally and recheck later

```sh
python3 -m campaign_tool content-check --directory $CAMPAIGN
python3 -m campaign_tool doctor --directory $CAMPAIGN --stale-days 90
python3 -m campaign_tool doctor --directory $CAMPAIGN --stale-days 90 --checklist --json
python3 -m campaign_tool build --directory $CAMPAIGN
python3 -m campaign_tool build --directory $CAMPAIGN --check
```

`content-check` lists every problem in the content. `doctor --stale-days 90` names old meeting, map, seed and law inputs
and every dated profile object, report, official, schedule and public copy (see above); `--json` only works together
with `--checklist`. `build` writes the local public files
under the campaign directory and `build --check` validates an existing tree; neither deploys or publishes anything
([SITE-CONTENT.md, generated pages and files](SITE-CONTENT.md#generated-pages-and-files)). Known limit on this version:
once this practice agency has its own page (here because of the agency action recorded in task 10), the posted-policy
reading from task 3 is shown under an `h4` heading directly after the agency name (`h2`), so `build --check` reports
`heading-skip h2→h4` for `agencies/example-profile.html`. That is the page layout, not your data, and `content-check`
still passes; leave the content as it is and tell the maintainers.

Recheck later. List every profile date you have recorded, oldest first, with its age in days (add `--json` for one
array, `--agency ID` for one agency):

```sh
python3 -m campaign_tool content agency-profile list --directory $CAMPAIGN
python3 -m campaign_tool content agency-profile list --directory $CAMPAIGN --agency ca-example-profile-pd --json
```

For each source older than you are comfortable with, open it again. If it changed, update the fields by hand first. If
it still says the same thing, record the day you re-read it; only that `checked_on` changes. A reading may not be older
than its document, so recheck `policy_check` before `alpr_policy` (and `contract_check` before `contract`). The
practice run repeats the dates already recorded, which changes nothing; use the day you actually re-read the source:

```sh
python3 -m campaign_tool content agency-profile recheck --directory $CAMPAIGN --agency ca-example-profile-pd --field policy_check --checked-on 2026-10-22
python3 -m campaign_tool content agency-profile recheck --directory $CAMPAIGN --agency ca-example-profile-pd --field alpr_policy --checked-on 2026-10-21
```

`funding`, `transparency` and `reports` take `--index N`, the 1-based position `list` prints (`doctor` names the same
entry zero-based). A future date, one before the stored date, or one the build would refuse is refused and nothing is
written. Do not advance a date to silence a reminder. Reference:
[SITE-CONTENT.md, rechecking agency-profile dates](SITE-CONTENT.md#rechecking-agency-profile-dates-content-agency-profile).

## Checklist for one agency

- [ ] Every link is the agency's own record (or a vendor portal recorded as a vendor report), each with a `checked_on` you
      actually re-read.
- [ ] Policy: `alpr_policy` (document and finding page), then `policy_check` with all seven elements.
- [ ] Contract: dates, vendor, `cost` as a string with the document's own period, then `contract_check` with all eight
      elements.
- [ ] Funding, transparency snapshot, sharing list, reports and agency actions each carry their own source and date.
- [ ] Archive copies carry `archived_on`, separate from `checked_on`.
- [ ] Absent means not checked; a zero or `not_found` is a recorded reading, never a conclusion.
- [ ] `content-check` exits 0; `doctor --stale-days` run; old dates re-read and recorded with `content agency-profile
      recheck`, never advanced without reading the source.
