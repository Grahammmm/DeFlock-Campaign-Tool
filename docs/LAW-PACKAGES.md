# Law package review process

Law packages (`jurisdictions/<country>-<state>/package.json`) are the only place
the engine learns about statutes. Because a wrong deadline or an overstated duty
propagates into every request draft and digest, packages move through an explicit
review process and carry their confidence on every rule.

## Roles

- **Author.** Anyone may draft a package. The author fetches primary text, records
  effective dates from the statute's own history note, and labels each rule's
  `review` honestly. Authors never set `status: reviewed` on their own work.
- **Reviewer.** A person who did not author the package and who has read each
  cited primary source. Legal training is expected for a `reviewed` package; a
  reviewer who is not a lawyer should say so in the pull request and leave rules
  they cannot vouch for at `likely` or `needs_attorney_review`.
- **Maintainer.** Merges the review pull request after checking that the
  reviewer is independent of the author and that CI passes.

## Review labels

Each rule's `review` field means exactly one of:

- `verified`: the author or reviewer fetched the primary text at the cited URL,
  and the `duty`, `exceptions`, `remedy`, and `effective_from` fields restate that
  text without extension. Paraphrase is allowed; inference is not.
- `likely`: the statement is believed correct but at least one of the following
  holds: the primary text was not fetched; the rule is an interpretive position
  (for example an Attorney General bulletin) rather than binding text; or the
  rule's application to ALPR is contested and unsettled. The `duty` text must say
  which.
- `needs_attorney_review`: the author could not confirm the statement, or the
  rule turns on a question a lay reader should not resolve. Digest conclusions
  citing such a rule are blocked from publication (see docs/CONTRACTS.md,
  confidence labels).

Guidance documents are encoded as their own rules, labelled at most `likely`, with
the binding statute cited alongside so a reader can see which is which.

## Effective-date discipline

- `effective_from` is the operative date from the statute's history note (for
  California, the parenthetical after the section text on leginfo). When a
  recodification renumbers a rule without changing substance, cite the new
  section and keep the original operative date in the source title.
- `effective_to` is set, never deleted, when a rule is repealed or superseded.
  Keep the old rule so `rules_in_force` answers correctly for historic event
  dates.
- The records-law deadline fields describe the determination period, not a
  production deadline. `campaign_tool.law.deadline` returns **provisional reminders
  only**, never a legal due-date or lateness determination. Its arithmetic and API
  are unchanged: calendar days count all days; business days skip weekends only.
  Public holidays are not modeled. A send date does not establish actual receipt.
- For California, [Gov. Code 6800](https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=GOV&sectionNum=6800.)
  excludes the first day and a holiday on the last day. Establish the applicable
  event-date/agency holiday rules, including section 6700 and relevant timing
  cross-references/local enactments; the current holiday list is not historical
  proof. No complete holiday calendar is supplied by this draft.
- Establish actual receipt of a **copy request**, and, for any extension, the
  version-correct unusual circumstance, reasonable necessity, written agency-head
  or designee notice, reasons and expected dispatch date under
  [Gov. Code 7922.535](https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=GOV&sectionNum=7922.535).
  Selecting `extension=True` proves none of those facts. Uncertain receipt or
  unresolved holidays/notices require provisional reminders, not allegations.
- The new 2026 CPRA rule is selected using the request-receipt event date; AB 370
  is effective January 1, 2026. Do not apply its cyberattack ground or new direct
  emergency-effect condition to earlier requests. This draft does not encode a
  complete pre-2026 CPRA version: an empty selection is a coverage gap, not proof
  that there was no duty or extension ground. Earlier law needs primary history.
- Select section 1798.90.55(a) using the **implementation event**, not the date of
  a later records request. Its January 1, 2016 start does not create a retrospective
  statutory hearing duty for a program implemented before that date. The
  [DOJ bulletin](https://oag.ca.gov/system/files/media/2023-dle-06.pdf) encourages
  retrospective public comment; encouragement is not a mandate. An expansion
  request item alone does not establish a new hearing trigger.
- CHP reporting under Vehicle Code 2413(e)/10901(b) is a distinct actor/event duty:
  report to the Legislature no later than 90 days following fiscal-year completion.
  Do not feed that deadline into CPRA determination arithmetic.
- Record `accessed` dates on every source. Re-verify a package when a cited
  section shows a newer amendment date than the one in the source title.

## Moving a package from draft to reviewed

1. The reviewer opens a pull request that changes only `status`, `reviewed_by`,
   `reviewed_at`, and any `review` labels or text they corrected.
   The maintainer checks that structure with
   `python3 tools/check_law_review.py OLD.json NEW.json` (OLD from `git show
   main:jurisdictions/us-ca/package.json`). It exits 0 only when NEW changes just
   those fields (per rule, matched by `rule_id`: `review`, `duty`, `exceptions`,
   `remedy`), is schema-valid, and, if `reviewed`, has distinct non-blank
   reviewers and a date not after today; 3 lists every other changed JSON path or
   error. It is a structural check only and never replaces the human legal review
   or the maintainer's reviewer-independence check. Run it on both package copies.
2. For each rule the reviewer lists in the PR description: the URL opened, the
   amendment date shown, and whether the rule text was accepted, edited, or
   downgraded.
3. `reviewed_by` holds stable reviewer identifiers (a name or handle the campaign
   can later contact), never email addresses or account credentials.
4. `tests/test_law.py` must still pass; add assertions for anything the reviewer
   relied on (for example an expected effective date).
5. After merge, `doctor` reports `reviewed_law_package: true` for campaigns in
   that jurisdiction. A later amendment to any cited section returns the package
   to `draft` until re-reviewed.

## Review worksheet (not a review)

`python3 -m campaign_tool.law review-sheet us-ca --out FILE.csv --as-of YYYY-MM-DD
[--stale-days N] [--base DIR]` writes a private CSV worksheet (mode 0600, refused
inside the repository or over an existing file): one `records_law` row, then one row
per rule in package order, with the rule text, its sources, the oldest access date
and its age, a `package_sha256` column (just before the reviewer columns), and four
blank reviewer columns. `package_sha256` is the same lowercase SHA-256 on every row:
the digest of the whole package as canonical JSON (sorted keys, compact separators,
UTF-8; every rule, source, label and review field counts, array order included).
It identifies the exact package revision the worksheet was made from; it is not a
signature and does not authenticate the reviewer. Stdout JSON names every source
accessed more than N days (default 180, 1..3650) before `--as-of`. The worksheet is
not legal advice and not a review: it never changes `status`, `reviewed_by`,
`reviewed_at` or any `review` label. Only a human reviewer moves a package to
reviewed, through the process above.

## Reading a filled worksheet back (not a review)

`python3 -m campaign_tool.law apply-sheet us-ca --sheet FILLED.csv --out PROPOSED.json
[--base DIR]` copies each `reviewer_conclusion` (`verified`, `likely` or
`needs_attorney_review`) into that rule's `review` label and writes the whole package
as a new private file (mode 0600, refused inside the repository or over an existing
file). A row with a conclusion needs `reviewer_name` and a `reviewed_on` date not after
today; any refused row stops the run and nothing is written. Edited `duty`,
`exceptions` or `remedy` text is listed in `text_edited`, never applied. It never sets
`status`, `reviewed_by` or `reviewed_at`: check the result with
`tools/check_law_review.py`, then record the reviewers through the process above.

A worksheet is accepted only against the package revision it was made from. Every row
must carry a `package_sha256` equal to the current package's digest; a missing,
malformed or different value, or a worksheet from before this column existed,
stops the run with `regenerate review-sheet or review-packet, nothing written`,
before any label is transcribed and before any output file is created. Any change
to the package (another rule, a request scope, a source, an intervening review label,
`status` or `reviewed_by`) makes every older worksheet stale, even a blank one. Recover
by regenerating the worksheet from the current package and having the human reviewer
reassess and re-enter the conclusions; old conclusions are never copied across. The
three text cells a reviewer may edit (`duty`, `exceptions`, `remedy`) do not change
the binding. There is no override and no automatic migration of old worksheets.
The binding detects a stale or mismatched worksheet; it cannot prevent someone from
editing the digest and is not a signature.

## Reviewer packet (not a review)

`python3 -m campaign_tool law review-packet --jurisdiction us-ca --out NEWDIR
[--as-of YYYY-MM-DD]` writes the one folder to hand a reviewer. NEWDIR must be an
absolute, normalised path outside the repository that does not exist yet, under an
existing parent with no symlink in its path; it is created mode 0700 with four 0600
files, and a refusal prints one line and creates nothing:

- `PACKET.md`: what the package is used for, what independence means here, what
  signing commits the reviewer to, and that a rule they cannot confirm stays
  `needs_attorney_review`.
- `RULES.md`: every rule, ordered by its own `review` field
  (`needs_attorney_review`, then `likely`, then `verified`), with the reading, the
  cited sources, the dates and one question; then the `records_law` row.
- `WORKSHEET.csv`: byte-identical to `review-sheet` for the same `--as-of` (default
  today) and package, including its `package_sha256` column, so `apply-sheet` reads
  the returned file unchanged while the package is unchanged.
- `RETURN.md`: the filled worksheet goes back to the maintainer, who opens the PR;
  the packet names no address. If the package changes before the worksheet comes
  back, `apply-sheet` refuses it: build a new packet and have the reviewer
  reassess; do not copy a digest across.

The packet states no conclusion and changes nothing in the package. While a
package is `draft`, `doctor` lists the reviewer requirement and this command in
`next_actions`.

## After an amendment: law impact

When a package is amended (a later amendment returns it to `draft`), list what the change touches before anyone
decides whether a correction is needed:

    python3 -m campaign_tool.law impact OLD.json NEW.json --directory D [--json]

OLD and NEW are two versions of one jurisdiction's package. Each is read with the same loader and validator as
`show`; a file that fails, or two different jurisdictions, stops with exit 2 and a message naming the file.
Rules are matched by `rule_id`; a rule is **changed** when its `citation`, `duty`, `exceptions` (compared in
order, so a reordered list counts), `effective_from` or `effective_to` differs. Every other rule field, including
`remedy`, `sources` and `review`, is ignored. Added and removed rules are named.

The *changed window* is the set of days whose answer may differ: the union of the old and new effective intervals
when the citation, duty or exceptions changed, the symmetric difference of the two intervals when only the dates
changed, the new interval for an added rule and the old one for a removed rule (`effective_to: null` is no end).

A published finding (including a corrected one) or an open draft is **affected** when it cites a changed, added or
removed rule, through `rule_ids` or `sources[].rule_id`, and its `event_date` falls in that rule's window.
Withdrawn findings are skipped. A draft whose `event_date` is still an `[EDIT ME` marker (or missing) is listed
under "drafts with no event date" because it may be affected; it does not set the exit code. A file that cannot be
read is listed as `unreadable <path>` and counted, never skipped. The next command depends on the finding's state:

- For an unpublished draft: `campaign_tool finding review-status <draft path> --directory D`.
  Edit the draft to reassess the cited rules, then obtain fresh reviews before publication.
  `finding diff` requires a published counterpart, so it is not offered for an unpublished draft.
- For a pending correction with a published counterpart:
  `campaign_tool finding diff ID --directory D --from <draft path>`.
  If a correction is needed, revise that draft and obtain fresh reviews before correcting the published finding.
- For a published finding: `campaign_tool finding diff ID --directory D --from DRAFT`,
  then `finding correct` if the diff shows the published text needs it.

Exit 0: no finding affected. Exit 3: at least one affected. Exit 2: an input error. The command is read-only: it
writes no file, never marks a package reviewed and never records a correction. It states no legal conclusion;
whether a correction is needed, and whether the amended package may be reviewed, stay with the human reviewers.

## Local ordinances: the campaign overlay

A city or county ordinance is not a state rule and never enters a state package. An organizer may transcribe one into
the campaign's own optional `law/local-rules.json` (runtime input; it is not shipped data and no public file is made
from it). Exact shape, one object per dated version of a rule:

    {"schema_version": 1, "jurisdiction": "us-ca", "rules": [
      {"rule_id": "local-us-ca-cedar-retention",
       "citation": "Cedar Municipal Code 5.1",
       "duty": "Delete plate reads after 30 days.",
       "source_url": "https://library.example.org/cedar/municode/5-1",
       "checked_on": "2026-01-02",
       "effective_from": "2020-01-01", "effective_to": null,
       "agency_ids": ["ca-cedar-police"]}]}

- `jurisdiction` must equal the campaign's `country-state`; there is no state fallback. `rule_id` is a lowercase slug
  starting `local-<jurisdiction>-` (for example `local-us-ca-`) with a name after it, and may not equal a state rule id.
- `citation` and `duty` are plain text as the organizer read them (no angle brackets). `source_url` is an `https://`
  address the organizer attests is the official primary text; it is never fetched and its host is not judged official.
  `checked_on` is a real date not after today. `effective_to` may be `null`; a future `effective_from` is allowed.
- `agency_ids` lists the campaign agencies the rule applies to; each must be a loaded agency. A rule applies to a
  finding only when the finding's `agency_id` is listed and its `event_date` is inside the inclusive window.
- A repeated `rule_id` is a dated version. Windows of one id may not overlap (a shared end/start day overlaps), whatever
  their agencies. No review or status field is accepted: organizers cannot mark a local rule reviewed.

Check it, read-only, before building (exit 0 valid or absent, exit 1 `Stopped:` plus the field, exit 2 bad usage):

    python3 -m campaign_tool.law check --directory DIR

`build`, `content-check`, `finding preview` and `finding packet` validate the same file, even when no finding cites it,
and refuse a finding that cites an unknown, out-of-scope or out-of-force `local-` id (in `rule_ids` or `sources[].rule_id`).
`law.rules_in_force(package, day, local_rules=..., agency_id=...)` appends the matching versions after the state rules;
without `agency_id` it is state-only. A finding page lists each cited local rule by citation with the label
"local rule, organizer-transcribed, not legally reviewed"; the state package, its review status and its citations
are unchanged. Validation proves shape only, never that the text is official, current, applicable or reviewed.

## Support matrix

[`docs/LAW-SUPPORT.md`](LAW-SUPPORT.md) lists, for every shipped jurisdiction package, each rule's id, citation,
effective window, review label and the request scopes that use it, plus the package status, recorded review
metadata and the `records_law` determination data. It is generated from the packages, never typed by hand. After
changing a package, regenerate it at the repository root:

    python3 -m campaign_tool.law support-matrix --md > docs/LAW-SUPPORT.md

`tests.test_law_support_matrix` fails when the committed file differs from a fresh render. The command writes
nothing itself; without `--md` it prints the same data as one JSON document, and `--base DIR` reads another
jurisdictions directory. The matrix restates package data: a rule row's label and the package status stay
separate, author review labels are not independent legal acceptance, and a listed jurisdiction is not all-state
support.


## Current state

`us-ca` is a draft authored from leginfo.legislature.ca.gov and the California
DOJ bulletin 2023-DLE-06. The seven approved correction issues were drafted on
2026-10-02; touched sources carry their actual access date. The package remains
`status: draft`, with no reviewers or review date. Definition cross-reference
eligibility, Values Act application, and the new current-version CPRA entry are
`needs_attorney_review`; the DOJ interpretation remains `likely`. Other unchanged
`verified` labels describe author-level restatements, not independent acceptance.
No qualified human legal reviewer has signed off. Tests are software evidence only.

Historical CPRA/holiday/personal-information amendment chains, federal and
transportation eligibility, electronic-format/fee and timing cross-references,
remedy discretion and case-law application remain unresolved. This bounded batch
neither supplies missing history nor declares agency misconduct, full California
compliance, production readiness or permission to publish.
