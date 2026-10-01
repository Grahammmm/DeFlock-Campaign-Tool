# Jurisdiction law packages

A law package encodes one state's public-records deadlines and ALPR-related rules
as data, so the engine can compute determination dates, list the rules in force on
an event date, and draft scoped records requests without hard-coding any law in
Python. Packages live at `jurisdictions/<country>-<state>/package.json` and must
conform to `schemas/law-package.schema.json`. `campaign_tool.law.load_package`
validates every field (required keys, types, enums, patterns, unique ids, date
order, scope-to-rule references) with the standard library only.

County/state inputs in a campaign remain organizer-supplied labels; a package
does not verify that a campaign is in the jurisdiction it names, and local
policies still need their own provenance.

## Package format

| Field | Meaning |
| --- | --- |
| `schema_version` | Always `1`. |
| `jurisdiction` | `<country>-<state>`, lowercase, matching the directory name (`us-ca`). |
| `status` | `draft` or `reviewed` (see below). |
| `reviewed_by`, `reviewed_at` | Reviewer identifiers and date; empty/`null` while draft. |
| `records_law` | Name, citation, `determination_days`, `determination_extension_days`, `day_type` (`calendar` or `business`), `fee_basis`, `appeal`, and primary `sources`. |
| `rules[]` | One entry per duty: `rule_id` (`<state>-<code>-<section>-<slug>`), `citation`, `actor`, `activity`, `duty`, `exceptions`, `remedy`, `effective_from`, `effective_to` (`null` while in force), `sources`, `review`. |
| `request_scopes[]` | Named bundles of request items (`agreements`, `policies`, `sharing`, `audit-logs`, `compliance`, `hearing-records`) with the `rule_ids` they relate to. |

Every `sources[]` entry is an `https://` URL to primary text (statute, regulation,
or official guidance) plus the date it was accessed. Each rule's `review` label
is one of `verified` (primary text fetched and matched by the author),
`likely` (correct to the author's knowledge but not fetched, or an interpretive
position that is not settled), or `needs_attorney_review`. Definitions of these
labels and the review process are in [docs/LAW-PACKAGES.md](../docs/LAW-PACKAGES.md).

## Draft versus reviewed

`status: draft` means the package was authored from primary sources but has not
been independently reviewed. The engine will load it, `campaign_tool.law show`
will summarize it, and request drafts will carry a "Law package status: draft"
line, but `doctor` reports `reviewed_law_package: false` and
`law_package_status: draft`. Nothing derived from a draft package is a legal
conclusion.

`status: reviewed` requires at least one entry in `reviewed_by` and a
`reviewed_at` date, and is set only by the review process in
docs/LAW-PACKAGES.md, never by the author of the package in the same pull
request. `doctor` then reports `reviewed_law_package: true`.

Only the shipped California package exists today, and it is a draft. The engine
does not model public holidays for `business` day types, and the statutory
period usually runs from the agency's receipt rather than the send date; the
computed date is a planning aid, not a determination.

## Contributing a state

1. Create `jurisdictions/<country>-<state>/package.json` with `status: draft`,
   `reviewed_by: []`, `reviewed_at: null`.
2. Encode the records law first: determination period, extension, day type, fee
   basis, and enforcement path, each with a primary-source URL and access date.
3. Add one rule per distinct duty. Record the effective date from the statute's
   history note, not from a secondary summary; set `effective_to` when a rule is
   repealed or superseded rather than deleting it.
4. Build request scopes that mirror `templates/records-request.md` and cite the
   rules each scope relies on.
5. Run `python3 -B -m unittest tests.test_law tests.test_schema_files -v` and
   `python3 -m campaign_tool.law show <jurisdiction>`. Add package-specific
   assertions to `tests/test_law.py` where they help (expected rule ids,
   deadline arithmetic, effective-date boundaries).
6. Open a pull request titled `law: <jurisdiction> draft`. Do not mark the
   package `reviewed`; a separate reviewer does that per docs/LAW-PACKAGES.md.

Do not generalize one state's public-records deadlines to another. Distinguish
determination deadlines, production dates, extension claims and agency promises.
No real legal rules are encoded in the synthetic fixtures under `examples/`.
