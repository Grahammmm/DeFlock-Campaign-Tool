# Records policy applicability dates

`campaign_tool.records.understanding.policy.select_version` chooses a policy
version for an event day only when the supplied dates establish that it applied.
It is a documentary selector, not legal review: a hold means "the dates do not
say", never "the agency complied" or "the rule did not apply".

## Missing versus invalid dates

- A **malformed** date (not `YYYY-MM-DD`, impossible day, empty interval,
  both `effective_to` and `superseded_at`) is a validation issue; the whole
  collection is `invalid_versions` and nothing is selected.
- A **missing** date (`null`/absent) is a validation *gap*, not an error. The
  collection stays valid, but a version that depends on the missing date is
  unresolved for the event.
- Dates are never inferred from `issued_at`, document footers, input order, the
  current time or the presence of a requirement.

## Classification per version

For each version in scope (agency, jurisdiction, regulated role) the event day is
compared half-open: covered from `effective_from` (inclusive) up to
`effective_to`/`superseded_at` (exclusive).

1. **Outside**: a known boundary alone proves it, even if the other boundary is
   unknown: event before a known start, or on/after a known end. An undated
   version that has definitely ended does not block anything.
2. **Established**: known start and either a known end, or an open end attested
   by `supersession_status: not_superseded_as_of_retrieval` together with a
   valid `retrieved_at` on or after the event day. A closed interval needs no
   retrieval date.
3. **Unresolved**: everything else, including an unknown start, an open end with
   no or unrecognised supersession status, and an attested open end whose
   `retrieved_at` is missing or earlier than the event. An open interval is never
   extended past its retrieval date.

## Result

- Two or more established versions keep `overlapping_versions` (overlaps between
  dated versions are normally already rejected by validation).
- **Any** unresolved version blocks selection, even if exactly one other version
  is established. The result is `effective_date_unknown`, `version: null`, no
  `source_pointer`, `candidates` is the sorted union of established and
  unresolved ids, and the gap `applicability_date_unknown` lists the sorted
  unresolved ids. No latest or best version is chosen.
- One established version and nothing unresolved: `selected`.
- Only outside versions: `not_in_force`.

A held selection makes `requirements_for` return no requirements, exceptions or
incorporated terms; `reconcile.compare` reports `applicability_unresolved`; and
`correspondence.track` withholds explicit-policy deadline actions and reports its
`*_deadline_policy_version_unresolved` hold. A structurally valid collection can
therefore still be unusable on a given event date.

## Limits

Historical derivatives and saved runs are not rewritten; re-running with the
changed code (which `code_pins()` hashes) is what produces the new holds. Review
and publication authority is unchanged: findings remain proposals for human
review.
