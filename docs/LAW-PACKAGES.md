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
- The records-law deadline fields describe the determination period, not the
  production date. `campaign_tool.law.deadline` counts calendar days or business
  days (Saturday and Sunday only; holidays are not modeled) from the date given.
  Statutes usually count from the agency's receipt, which is later than the send
  date, so treat the result as the earliest plausible date.
- Record `accessed` dates on every source. Re-verify a package when a cited
  section shows a newer amendment date than the one in the source title.

## Moving a package from draft to reviewed

1. The reviewer opens a pull request that changes only `status`, `reviewed_by`,
   `reviewed_at`, and any `review` labels or text they corrected.
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

## Current state

`us-ca` is a draft authored from leginfo.legislature.ca.gov and the California
DOJ bulletin 2023-DLE-06, accessed 2026-09-30. Nine SB 34 and Vehicle Code rules
are `verified`; the California Values Act rule and the DOJ bulletin rule are
`likely` because their application to ALPR sharing is interpretive. No reviewer
has signed off.
