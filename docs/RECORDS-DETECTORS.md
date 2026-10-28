# WP6 pure supplied-evidence detectors

Seven pure Python detectors: search-before-training, purpose-quality, external-sharing,
retention-over-policy, audit-gap, cpra-deadline and volume-anomaly.
No filesystem access, models, network, wall clock, database writes or publication.
Hits are private triage signals, never legal conclusions or accepted stage receipts.
This package includes no verified statute text or current legal rule defaults.

## APIs

Each underscore-named module exposes detect(units, joins, rules, config) -> hits.
The runner should use campaign_tool.records.detectors.evaluate(detector_id, units,
joins, rules, config) -> {hits, manifest}, since an empty hit list alone cannot
distinguish a clean evaluation from missing context. run_all returns these results
keyed by the seven detector IDs. Database persistence belongs to a later adapter.

## Explicit input schema

A unit supplies original_sha256, a nonempty object locator, record_type, stable
event_id, timezone-bearing event_at and a fields object. No original hash, event
identity or agency is inferred recursively from hash-shaped strings or filenames.
Record types are search-log, training-roster, sharing-list, network-search,
retention-setting, audit and request. Supply separate event IDs for distinct
events, even when one original contains many rows.

Fields may be ordinary typed observed values or explicit {state: observed, value: ...},
{state: missing}, {state: blank}, {state: redacted}. Null is missing, whitespace is
blank, and exact [redacted]/[withheld] markers are redacted. Missing/blank/redacted
critical values are individually blocked and never create severity by themselves.

Joins are explicit rows {original_sha256, status: typed, agency_id}. Missing,
ambiguous or merely hinted agency identity blocks evaluation. Deduplication keys
use typed agency, record type and event ID. Semantically identical duplicate rows
are counted once while exact original/locator references are retained. Conflicting
declarations for the same event are blocked rather than selected arbitrarily.

Rules are {version, entries: [...], holiday_tables: {...} as needed}. Each entry:
detector, agency_id, rule_id, version, source_sha256, effective_from (ISO date),
effective_to (exclusive date or null), parameters. Exactly one event-effective
entry must apply. Before/after-policy events skip; absent/overlapping context blocks.
Every hit carries its rule/version/source/interval and global rules version.

## Detector parameters and interpretation

- Training: search user_id joins explicit training-roster user_id and
  training_completed_at. A search before the earliest supplied completion is a
  severity-3 timestamp observation, not proof of unauthorized access. Unknown or
  redacted training context blocks; absence alone produces no high-severity hit.
- Purpose: generic_reasons supplies exact generic strings. A conservative numeric
  case-only form is recognized. Only observed nonblank reasons enter the rate:
  strictly >25% gives severity 3; strictly >5% gives severity 2; 25% is severity 2
  and 5% produces no hit. Per-user and overall groups retain qualified denominators.
  Blank/missing/redacted rows remain explicit blocked outcomes, not bad purposes.
- Sharing: home_jurisdiction is explicit policy context. access_enabled,
  recipient_level and recipient_jurisdiction are explicit observations. Federal or
  outside-home access is a severity-3 permission signal, never proof of disclosure.
- Retention: unit=calendar_days and maximum_days compare observed retention_mode=days,
  retention_days. Indefinite/unknown modes block, never receive an invented conversion.
- Audit: frequency_days and first_due are supplied policy scheduling parameters.
  period_start/period_end (exclusive), production_complete and audit_dates establish
  the declared produced-document window. Missing produced documents yield a
  documentation-gap signal, not a claim that an audit did not occur. A window crossing
  a policy version blocks. Empty audit_dates is an explicit observation, not missing.
- Volume: daily_count_threshold, off_hours_threshold, business_hours=[start,end)
  and timezone_offset_minutes define a supplied operational baseline. Daily/user
  unique events and off-hours observations are triage-only severity 1. No timezone
  or daylight-saving convention is invented.

## CPRA timing is supplied-rule arithmetic, not legal advice

Input fields: request_received, determination_state=made/pending,
determination_at when made, extension_claimed, and if extended, extension_days,
extension_notice_at and extension_basis. event_at must identify the request's
received date. config.as_of is mandatory. Production dates NEVER substitute for
determination dates and are not treated as a production deadline.

Parameters must explicitly supply counting=calendar, exclude_received_day,
determination_days, extension_days_max, reminder_days, weekend_days, roll_due
(none/next_business_day), and holiday_table_sha256. Extensions additionally require
extension_allowed_bases, extension_notice_deadline=base_due and
extension_calculation=from_base_due. Unsupported conventions block rather than
silently applying a jurisdiction's assumed rule.

holiday_tables maps an exact SHA-256 to {version, coverage_start, coverage_end, dates}.
Hash the canonical JSON with core.digest; dates and required calculation/as-of range
must be covered. Table corruption or missing coverage blocks. Rule parameters and
the holiday table must be independently verified before real use; synthetic tests
do not seed law. Overdue supplied determinations yield severity 2; supplied-rule
reminders yield severity 1. All wording is an observation with legal_conclusion=null.

## Manifest, bounds and limits

Manifests bind exact supplied inputs, rule/config hashes, detector version,
classified per-event outcomes, dedupe keys and deterministic manifest_sha256.
No timestamp is generated. Identical input yields identical manifest and hit keys.
Input row order is included in the input hash; duplicate printed rows do not create
extra events/hits. Outcomes partition unique records into evaluated/skipped/blocked.
eligible records have a detector input type; some eligible records may be skipped
for policy dates or blocked for unsupported context. Support/irrelevant records skip.

Caps: 5,000 unit rows, 10,000 joins, 256 rule entries, 8 MiB serialized input.
Audits cap windows at 3,660 days; deadline roll-forward is bounded. This first slice
is for small representative inputs, not a corpus throughput certification.
Raw observation values and exact evidence locators are private outputs. The public
engine/tests contain only synthetic fixtures. No claim of actual detector deployment,
real records evaluation, independent findings or production readiness is made.


## v2 boundary and identity repairs

Detector version `wp6-pure-detectors-v2` intentionally changes hit identities.
Full canonical rule-entry fingerprints (including parameters, source hash and
exclusive effective interval) separate aggregate contexts even when labels match.
Hit identities also bind semantic unit observations, support observations used by
training, severity and reported values. Aggregate identities bind all qualified
member observations, not merely their count. Repeated print locations for an
identical event retain all exact references without creating new identities;
location/duplicate multiplicity is not itself a semantic observation. Manifests
continue to bind the complete input, including supplied reference changes.

Audit windows are disjoint `(previous_due, due]`, clipped to the inclusive
production start. Only the first window includes a production start that equals
its previous boundary; a scheduled due at that start is never counted again in
the next window. Production end remains exclusive.

Locator objects allow at most 16 named components. Keys are nonblank strings of
at most 64 characters. A value is a nonnegative integer below 2**63, a nonblank
string of at most 1024 characters, or a flat path list of 1..32 such values.
Null, blank, boolean, float, negative, nested and oversize values block explicitly.
The detector preserves caller-supplied positions exactly: it does not convert
zero-based traversal indexes into hierarchical MIME paths, infer missing positions,
or establish that a caller-supplied locator resolves against original bytes.

Event-date timezone applicability remains a supplied-context limitation, not a
newly verified jurisdiction-specific calendar rule. These repairs add no corpus
run, persistence adapter, review acceptance or publication authorization.
